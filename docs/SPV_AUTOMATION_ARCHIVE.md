# Automatic service funding and permanent archive

[Current architecture diagrams](ZK_SPV_ARCHITECTURE.md) show payment/self-funding
and archive promotion/restore as separate flows.

Worker V4 connects the encrypted request workflow to automatic issuer admission,
quotes and an opt-in payer agent. It also uses a separate permanent admitted-header
archive with native replay recovery. These are code changes with focused checks;
combined real-chain/payment/proving/consumer E2E remains pending.

## Explicit financial policy

The default templates disable automatic admission and payment. Confederation
funding remains available under its configured budgets. An empty gas treasury
never authorizes debiting another payer, changing a tariff or using BTC/MP backing.

For issuer automation, copy the reviewed values from
[`zkspv-issuer-automation.example.json`](../config/zkspv-issuer-automation.example.json)
into `worker.automation`, with `enabled=true`. The worker's service mode must be
`paid` or `paid_or_confederation` and its binding must identify the actual canonical
payment registry, factory, state view and enrolled Bitcoin policy. The configured
issuer EOA must equal the registry's current FROST-approved issuer. Automation
cannot activate a registry, choose payees or replace FROST governance.

For the payer, use transport config V2 and insert the complete reviewed
[`spv-payment-policy.example.json`](../config/spv-payment-policy.example.json)
object under `payment`. Specify the exact service identities, federations,
consumers, treasury/epoch/roster hash, tariff version/hash, budget scope and fee
ceiling, plus transaction/job/lifetime gas-and-value limits and attempt caps.
The payer identity must own the transport request; a membership/delegate claim
alone grants no financial authority. Core RPC/cookie are local policy inputs.
Private config/identity files remain `0600`, directories `0700`.
The templates contain placeholders and are not live payment authorizations.

When enabled, the client includes a private payment offer in a version-2 encrypted
request. It sends only the data needed for negotiation; total local budget and
private key-file paths are not in the envelope. The worker independently validates
the operation and checks whether existing Fresh state already suffices. Immature
needs wait. A new paid job is unnecessary for a cached/sufficient state or header
maintenance that the confederation is configured to fund.

The automatic flow is:

1. The issuer selects a stable canonical job ID from registry domain and semantic
   need identity, reusing `semanticJob` if already present. State changes, permission
   renewal, restart and proving retries do not create another job or service fee.
2. It journals an exact zero-value `admit` request and sends it through the existing
   nonce engine with an issuer budget. Terms preserve original payer, federation,
   budget scope and service deadline. Only the permission's state/version, nonce
   and accept-by window can renew; the original deadline never moves.
3. After canonical admission, an encrypted sequenced `QUOTE` status carries the
   exact registry funding request. ACK and quote stay linked to the original
   request/operation. The quote is a hint, not a bearer payment authorization.
4. The payer reads canonical board bytes and ACK, re-reads registry terms/config,
   validates every pinned policy field, and checks its own Core and state/adapter.
   If Fresh state now suffices, it does not pay. A stale or expired permission
   requires a renewed canonical quote, not altered saved transaction bytes.
5. It reserves worst-case gas plus value in its private durable budget before
   signing. `fundRequest` still requires the exact payer and value and checks
   permission/configuration/state atomically. The canonical paid marker prevents
   a second full contribution. Signed recovery precedes preparing a new attempt.
6. The worker rechecks paid and connects it to the existing service/attempt flow.
   The receipt does not mint or settle; ordinary consumer checks remain required.

Run the existing worker loop for issuer automation. The opt-in payer executes on
`spv_transport poll` and when the participant retries its remote consumer command;
a process supervisor can invoke polling. No startup service is installed here.

Compatible concurrent needs share one active funding coverage for the observed
Core target. Additional covered needs keep links to the original job's semantic
set; grouping never rewrites paid terms or bills every requester separately. Only
the original payer route receives a payable quote. Another member's request does
not authorize spending its budget. If the selected payer is unavailable, the
obligations remain visibly pending or run under configured confederation funding;
there is no silent reassignment of the payer. Paid work and earlier deadlines
survive rebase/restart. A need outside the current coverage waits for subsequent
useful work rather than creating a racing duplicate quote automatically.

The per-identity `<identity>.spv-service-budget.sqlite` survives transport-directory
restarts. Its policy is immutable; reservations are conservative and not refunded
on failures, and changed policy/limits require an explicit reviewed migration.
Communication publication budgets remain separate and explicit. Registry paid
state, service completion and native balance are distinct facts. Missing liquidity,
caps, revoked permissions or ambiguous signed recovery leave pending/review state;
none invents a subsidy, reimbursement right or unlimited free execution.

## Permanent archive and native host

Worker V4 adds `archive.directory`, `max_headers` and `max_bytes` to the worker
config. Choose a durable local volume outside temporary directories, with backup
capacity and `0700` permissions. The archive stores public admitted headers,
content-addressed segments, ordered version links and exact V2 state tuples in
SQLite WAL/FULL. It contains no identity keys, FROST shares, payer budgets or signed
EOA transaction journals. Header indexes are hints; native authenticated state
remains authoritative.

Before signing a new transition the publisher checks that its exact native delta
fits the archive limits. New segments are promoted after the worker sees the
corresponding confirmed EVM state. Candidate proving files remain separate.
Versions retain
side-branch admission order and the known-root/log/count commitments. No tip-only
reconstruction guesses missing admitted forks. V3 retained arrays can migrate once
through native replay; the old array and monetary journal are retained.

The host now supports a resident, pipe-only `--serve` mode, with the same pinned
mainnet verifier and unchanged guest. Normal preparation reuses the validated
in-memory archive and appends only new headers; it does not replay the whole
history for every batch. The persistent header stream is a cache, not authority.
A cold process, rollback/rebase to another archive state, or restoration replays
headers and must reproduce the expected accepted commitment/roots/count exactly.
Bounded witness/input and journal bytes remain the same as the legacy native path.

Rebuild using the existing `ops.zkspv.worker_build` workflow and update the reviewed
`host_binary`/`host_sha256` pin. No new guest image or proof is generated by that
build. Place the installed binary in a durable operator-controlled location.
The proving R0VM/bridge/wrapper installation remains the separately pinned runner;
this archive change does not claim a portable Linux proving installation.

Limits are explicit: host protocol accepts at most two million archived headers,
with an operator-configured lower count and byte budget (template: 200,000 and
1 GiB). `max_bytes` bounds the raw segments. Optional `max_backup_bytes` bounds
the full SQLite file on both backup and restore. Its default is `max_bytes`
for compatibility with existing configurations. This setting can be adjusted
on the same archive. SQLite indexes/WAL, stream cache and temporary recovery
space require additional filesystem capacity. The native sparse trees
still occupy RAM and cold startup still requires replay. Large-archive time/RAM,
backup latency and crash/failover qualification must be measured on the target.
It is not claimed to be a fully disk-resident sparse-tree implementation.

## Backup, restore and upgrades

```sh
python -m signer_agent.zk_worker archive-backup --config worker-v4.json \
  --archive-file /durable/backup/spv-archive.sqlite
python -m signer_agent.zk_worker archive-restore --config worker-v4.json \
  --archive-file /durable/backup/spv-archive.sqlite
```

Backup uses SQLite's consistent backup API, writes a private new file, fsyncs it
and reports SHA-256. It does not overwrite an existing backup. Keep copies on
independent storage; no remote backup destination is configured automatically.

Restore holds the worker lock and copies the bounded backup into private staging.
It verifies scope, ordered segment chain, sizes/hashes and target state, starts a
cold native verifier, and replays against the **actual confirmed EVM tuple**.
It then rechecks that EVM state has not changed before importing only the target's
validated lineage. Missing/corrupt/incompatible histories fail before promotion.
Backup metadata or a matching filename cannot declare an arbitrary root accepted.
If another operator advanced state beyond the backup, restore requires the matching
admitted history instead of treating old state as current.

Restoring the archive never restores/deletes the worker's payment, service-cost,
transport, EOA nonce, signed-transaction or FROST journals. A financial/signer restore
is a separate procedure. Archive-only failover does not make cloned signer identities
safe, nor does a local file lock provide multi-host leader election.

V3→V4 can retain the same worker directory when its original economic/transport
scope is identical. V4 runtime options are recorded separately, so existing need
IDs, canonical semantic keys, paid links and spending counters do not change.
The first permanent archive use migrates a matching retained V3 header array by
native replay. Resolve pending proving/signed operations before changing tool/source
pins; source drift remains an explicit recovery stop. Archive parameters are bound
to the directory. First enabled issuer policy is pinned; disabling it pauses new
admission without erasing approvals or budgets. Re-enabling requires that same
policy; issuer rotation/limit migration needs a reviewed operational procedure.

The focused report separately records real native fixture replay/archive recovery
and mocked EVM/payment automation checks. Fresh STARK/Groth16, actual native-value
transfers and the combined transport/payment/prover/consumer E2E were deliberately
not run during this code phase.
