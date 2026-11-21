"""Retain old graphs while enrolling a separate wallet; never migrate their claims.

This is an operator/signer-local prerequisite, not a global on-chain family
registry. A successful review preserves every original accounting domain.
"""
import json
from pathlib import Path

from .bitcoin_continuity import Catalog, digest, inventory, stable
from .operations import canonical

PROFILE = 'RETAIN_ORIGINAL_GRAPHS_V1'


def table(db):
    db.execute('CREATE TABLE IF NOT EXISTS btc_handover_bindings(family TEXT PRIMARY KEY,body TEXT NOT NULL)')


def bind(catalog, marker):
    """Only add restrictions. Independent spent storage survives encrypted-state rollback."""
    with catalog.databases() as sources:
        for db in sources:
            table(db)
            row = db.execute('SELECT body FROM btc_handover_bindings WHERE family=?', (marker['target_family'],)).fetchone()
            if row and json.loads(row[0]) != marker: raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
            db.execute('INSERT OR IGNORE INTO btc_handover_bindings VALUES(?,?)',
                       (marker['target_family'], canonical(marker)))


def markers(catalog, family):
    found = []
    with catalog.databases() as sources:
        for db in sources:
            table(db)
            row = db.execute('SELECT body FROM btc_handover_bindings WHERE family=?', (family,)).fetchone()
            if row: found.append(json.loads(row[0]))
    if found and any(m != found[0] for m in found): raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
    if found: bind(catalog, found[0])
    return found[0] if found else None


class Handover:
    def __init__(self, fleet):
        if not fleet.configuration: raise ValueError('CONTINUITY_HANDOVER_CONFIG')
        self.fleet = fleet

    def get(self, identifier):
        try: plan = self.fleet.lane.get('continuity-handover:' + identifier)['request']
        except KeyError: raise ValueError('CONTINUITY_HANDOVER_NOT_FOUND') from None
        if digest(plan) != identifier or plan['fleet_id'] != self.fleet.c['fleet_id']:
            raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
        return plan

    def marker(self, plan):
        return dict(profile=PROFILE, configuration=str(Path(self.fleet.configuration).resolve()),
                    fleet_id=self.fleet.c['fleet_id'], plan_id=digest(plan), target_family=plan['target']['family'])

    def collect(self, source, target, requests=(), batches=()):
        entries = [self.fleet.entry(context) for context in (source, target)]
        if source == target or [e['mode'] for e in entries] != ['SERVICING', 'ISSUING']:
            raise ValueError('CONTINUITY_HANDOVER_MODES')
        clients = [self.fleet.client(e) for e in entries]
        snapshots = [self.fleet.snapshot(e, c) for e, c in zip(entries, clients)]
        reports = [inventory(c, requests=requests if i == 0 else (), batches=batches if i == 0 else ())
                   for i, c in enumerate(clients)]
        for entry, report, snapshot in zip(entries, reports, snapshots):
            i = report['source']['identity']
            expected = dict(registry=i['registry'], chain_id=i['chain_id'], bitcoin_genesis=i['bitcoin_genesis'],
                base_group='02'+i['base_x'], wallet_result=i['wallet_result'], key_epoch=i['key_epoch'],
                graph_id=i['graph_id'], wallet_context=entry['wallet_context'])
            if any(snapshot['identity'].get(k) != v for k, v in expected.items()):
                raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
        old, new = [r['source'] for r in reports]
        if (old['identity']['bitcoin_genesis'] != new['identity']['bitcoin_genesis']
                or old['family'] == new['family'] or old['identity']['base_x'] == new['identity']['base_x']):
            raise ValueError('CONTINUITY_HANDOVER_DISTINCT_KEYS_REQUIRED')
        # Populate both catalogs with observed canonical aliases before testing either parent.
        # No assertion about undisclosed tweaks or globally unknown deployments is made.
        for client in clients:
            catalog = Catalog(identity_file=client.c['identity_file'])
            for c, report in zip(clients, reports):
                s = report['source']; catalog.authorize(c.w3, s, 'IDENTITY')
                for row in report['registered_bindings']:
                    catalog.alias(c.w3, s, bytes.fromhex(row['script'])[2:], row['intent_id'])
            # Canonical fee aliases live in each origin identity journal.
            for origin in clients:
                other = Catalog(identity_file=origin.c['identity_file'])
                with other.databases() as databases:
                    families = [r['source']['family'] for r in reports]
                    rows = [tuple(row) for db in databases for row in db.execute(
                        'SELECT key,family,binding FROM btc_continuity_child_keys WHERE family IN (?,?) LIMIT 4097', families)]
                if len(rows) > 4096: raise ValueError('CONTINUITY_INVENTORY_LIMIT')
                for key, family, binding in rows:
                    for c, report in zip(clients, reports):
                        if report['source']['family'] == family:
                            catalog.alias(c.w3, report['source'], bytes.fromhex(key), binding)
            for c, report in zip(clients, reports): catalog.authorize(c.w3, report['source'], 'IDENTITY')
        for c, report, snapshot in zip(clients, reports, snapshots):
            for block in (snapshot['block'], snapshot['latest']):
                if c.w3.eth.get_block(block.number).hash != block.hash: raise ValueError('CONTINUITY_REORG')
            stable(c.w3, report['source'])
        return entries, clients, reports

    def prepare(self, source, target, *, requests=(), batches=()):
        with self.fleet.exclusive():
            entries, clients, reports = self.collect(source, target, requests, batches)
            plan = dict(profile=PROFILE, fleet_id=self.fleet.c['fleet_id'],
                source_context=source, target_context=target,
                source=dict(family=reports[0]['source']['family'], identity=reports[0]['source']['identity']),
                target=dict(family=reports[1]['source']['family'], identity=reports[1]['source']['identity']),
                source_requests=sorted(requests), source_batches=sorted(batches),
                transfers_assets=False, changes_original_bindings=False)
            identifier = digest(plan); marker = self.marker(plan)
            # Write the original fleet first. A partial enrollment only adds a hold.
            self.fleet.lane.put('continuity-handover:' + identifier, plan)
            for client in clients: bind(Catalog(identity_file=client.c['identity_file']), marker)
            try: self.fleet.lane.get('continuity-handover-initial:' + identifier)
            except KeyError: self.fleet.lane.put('continuity-handover-initial:' + identifier, reports)
        return dict(status='HANDOVER_ENROLLED_REVIEW_REQUIRED', plan_id=identifier, profile=PROFILE,
                    transfers_assets=False, activation_allowed=False, ready=False, production_ready=False)

    def review(self, identifier):
        plan = self.get(identifier)
        with self.fleet.exclusive():
            _, clients, reports = self.collect(plan['source_context'], plan['target_context'],
                                              plan['source_requests'], plan['source_batches'])
            marker = self.marker(plan); reasons = []
            for key, client, report in zip(('source', 'target'), clients, reports):
                if dict(family=report['source']['family'], identity=report['source']['identity']) != plan[key]:
                    raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
                if markers(Catalog(identity_file=client.c['identity_file']), marker['target_family']) != marker:
                    raise ValueError('CONTINUITY_HANDOVER_RECOVERY_REQUIRED')
                if report['fees']['status'] != 'OBSERVED': reasons.append('CONTINUITY_HANDOVER_INVENTORY_UNAVAILABLE')
                for custody, derivation in report['source']['states'].values():
                    if key == 'source' and (custody['state'] != 1 or not custody['issuance_closed']):
                        reasons.append('CONTINUITY_HANDOVER_SOURCE_NOT_DRAINING')
                    if key == 'target' and (custody['state'] != 0 or custody['issuance_closed']):
                        reasons.append('CONTINUITY_ISSUANCE_CLOSED')
                    if derivation['state'] != 0: reasons.append('CONTINUITY_DERIVATION_RESTRICTED')
            with self.fleet.lane.db() as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='epoch_retention_current'").fetchone()
                row = db.execute('SELECT body FROM epoch_retention_current WHERE fleet=?', (self.fleet.c['fleet_id'],)).fetchone() if exists else None
            retention = dict(status='BLOCKED')
            if row:
                from .epoch_retention import review
                saved = json.loads(row[0]); retention = review(self.fleet, saved['request'], saved['packets'])
            if retention['status'] != 'RETENTION_CHECKED': reasons.append('EPOCH_RETENTION_REQUIRED')
            # Recheck earlier anchors AND current state after all remote epoch reviews.
            from .bitcoin_continuity import sample
            for client, report in zip(clients, reports):
                stable(client.w3, report['source'])
                b, _, ctx, _, _ = client.context(); now = sample(client.w3, ctx, b.number)
                if now['identity'] != report['source']['identity'] or now['states'] != report['source']['states']:
                    reasons.append('CONTINUITY_HANDOVER_CHANGED')
            result = dict(profile=PROFILE, plan_id=identifier,
                status='BLOCKED' if reasons else 'RETAINED_SERVICE_CHECKED', reasons=sorted(set(reasons)),
                source_inventory_sha256=digest(reports[0]), target_inventory_sha256=digest(reports[1]),
                retention=retention, transfers_assets=False, history_complete=False,
                global_key_independence_proved=False, activation_allowed=False, ready=False, production_ready=False)
            self.fleet.lane.put('continuity-handover-review:' + digest(result), result)
            return result

    def enroll_signer(self, identifier, state):
        plan = self.get(identifier)
        # A restrictive enrollment is allowed before qualification. It grants no private work.
        with state.exclusive(): bind(Catalog(state=state), self.marker(plan))
        return dict(status='HANDOVER_SIGNER_ENROLLED', plan_id=identifier, ready=False, production_ready=False)


def issuance_guard(catalog, source):
    marker = markers(catalog, source['family'])
    if marker is None: return
    from .epoch_service import Fleet, load_config
    fleet = Fleet(load_config(marker['configuration']), configuration=marker['configuration'])
    handover = Handover(fleet); plan = handover.get(marker['plan_id'])
    if handover.marker(plan) != marker or plan['target']['identity'] != source['identity']:
        raise ValueError('CONTINUITY_HANDOVER_CONFLICT')
    if handover.review(marker['plan_id'])['status'] != 'RETAINED_SERVICE_CHECKED':
        raise ValueError('CONTINUITY_HANDOVER_REVIEW_REQUIRED')
