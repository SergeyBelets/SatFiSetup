"""Start a separately enrolled replacement ceremony in a factory's existing registry."""

import os
from pathlib import Path
import time
from types import SimpleNamespace

from web3 import Web3

from .bootstrap import Bootstrap, artifact, artifact_fingerprint
from .bootstrap_formation import expected_formation
from .dao_client import account_at
from .dkg_protocol import Manifest, PROTOCOL_ID
from .evm_network import read_block, verify_anchor
from .evm_rpc import evm_web3
from .onchain_transport import _board_and_invitation, assembled_profile, invitation
from .onboarding import ROLES, fingerprint, read_public, validate_invite, write_public
from .operations import Journal
from .reservation_transfer_cli import (_digest, _entry, _factory, _read, _session, require_live_replacement,
                                       EVM_AUTH, ZERO_ADDRESS)
from .transaction_wait import apply_confirmed


def add_parsers(sub):
    parser = sub.add_parser("reservation-invite", help="copy agreed formation into a fresh enrollment invitation")
    parser.add_argument("--invite", required=True)
    parser.add_argument("--accept-fingerprint", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--deadline", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)

    parser = sub.add_parser("reservation-ceremony", help="start one replacement EVM_AUTH in an existing registry")
    parser.add_argument("--kind", choices=("federation", "confederation"), required=True)
    parser.add_argument("--factory", required=True)
    parser.add_argument("--from-session", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--confirmations", type=int, required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--accept-fingerprint")
    parser.add_argument("--identity-file")
    parser.add_argument("--state")


def fresh_invite(args):
    original = validate_invite(read_public(args.invite))
    if original["version"] != "6" or fingerprint(original) != args.accept_fingerprint:
        raise ValueError("RESERVATION_CEREMONY_INVITATION")
    if (not args.deployment_id or original["deployment_id"] == args.deployment_id
            or args.deadline <= int(time.time()) or args.output.exists() or args.output.is_symlink()):
        raise ValueError("RESERVATION_CEREMONY_INVITATION")
    replacement = dict(original, deployment_id=args.deployment_id, approval_deadline=str(args.deadline),
                       epoch=str(int(original["epoch"]) + 1), sessions={role: os.urandom(32).hex() for role in ROLES})
    validate_invite(replacement)
    write_public(args.output, replacement)
    return dict(status="RESERVATION_INVITED", fingerprint=fingerprint(replacement), output=str(args.output))


def inspect(w3, profile, factory_address, previous, kind, block):
    invite = profile["onboarding"]["invitation"]
    if invite["version"] != "6" or int(invite["approval_deadline"]) <= block.timestamp:
        raise ValueError("RESERVATION_CEREMONY_INVITATION")
    role = "evm_ceremony" if kind == "federation" else "conf_ceremony"
    factory, registry, coordinator, operator, runtimes = _factory(
        w3, dict(factory=factory_address, kind=kind), block)
    board, observed_invite = _board_and_invitation(w3, profile["onchain_board"], block, pinned=True)
    if invite != observed_invite or invite["operator"] != operator:
        raise ValueError("RESERVATION_CEREMONY_INVITATION")
    terms = expected_formation(profile)[kind]
    entity, replacement = terms["id"], terms["session_id"]
    scope = dict(kind=kind, entity_id=entity, registry=registry.address, coordinator=coordinator.address,
                 factory=factory.address, previous_session=Web3.to_hex(previous),
                 replacement_session=Web3.to_hex(replacement), board=board.address, role=role)
    title = kind.capitalize()
    current = bytes(_read(factory, block, "reserved" + title + "Session", entity))
    adoption_lookup = "get" + title + "ForSession"
    adopted = _read(factory, block, adoption_lookup, replacement)
    if (previous == replacement or current not in (previous, replacement)
            or _read(factory, block, adoption_lookup, previous)
            or (adopted and (adopted != entity or current != replacement))):
        raise ValueError("RESERVATION_TRANSFER_CHANGED")
    formation, token = _read(factory, block, "reserved" + title + "Terms", entity)
    if formation != terms["formation_hash"] or token != terms["token_hash"]:
        raise ValueError("RESERVATION_CEREMONY_TERMS")
    original = _read(registry, block, "getSessionInfo", previous)
    original_context, _, purpose, *_ = _read(registry, block, "ceremonies", previous)
    if not any(original_context) or purpose != EVM_AUTH or original[4] not in (5, 6) or original[9]:
        raise ValueError("RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL")
    selected = profile[role]
    members = selected["participants"]
    if (list(_read(registry, block, "getSessionParticipants", previous)) != members
            or original[3] != int(selected["threshold"])):
        raise ValueError("RESERVATION_TRANSFER_DKG_MISMATCH")
    if (_read(board, block, "registry") not in (ZERO_ADDRESS, registry.address)
            or _read(board, block, "intentCoordinator") not in (ZERO_ADDRESS, coordinator.address)
            or _read(registry, block, "PHASE_DURATION") != int(invite["phase_duration"])):
        raise ValueError("RESERVATION_TRANSFER_BINDING")
    manifest = Manifest(int(profile["chain_id"]), registry.address, replacement, int(selected["epoch"]),
                        "EVM_AUTH", tuple(members), tuple(map(_session, selected["transport_keys"])),
                        int(selected["threshold"]))
    existing = _read(registry, block, "ceremonies", replacement)[0]
    if any(existing):
        if existing != manifest.context:
            raise ValueError("RESERVATION_TRANSFER_DKG_MISMATCH")
        require_live_replacement(_read(registry, block, "getSessionInfo", replacement), block.timestamp)
    else:
        registrar = _read(registry, block, "ceremonyRegistrar")
        creator = _read(registry, block, "ceremonyCreators", replacement)
        if operator != (registrar if creator == ZERO_ADDRESS else creator):
            raise ValueError("RESERVATION_CEREMONY_CREATOR")
    creation = [Web3.to_hex(replacement), members, selected["threshold"], selected["transport_keys"],
                selected["epoch"], Web3.to_hex(EVM_AUTH), Web3.to_hex(PROTOCOL_ID)]
    adopt = (dict(method="adoptFederationDKG", args=[scope["replacement_session"], entity]) if kind == "federation"
             else dict(method="adoptCustodianConfederationDKG" if terms["mode"] == 2 else "adoptConfederationDKG",
                       args=[scope["replacement_session"], terms["federation_ids"],
                             [[name, member, Web3.to_hex(point), institution] for name, member, point, institution in terms["custodians"]],
                             str(terms["threshold"]), str(terms["min_custodians"]), entity]))
    runtime_hashes = dict(runtimes, board=Web3.to_hex(Web3.keccak(w3.eth.get_code(board.address, block_identifier=block.number))))
    review = dict(scope, domain="SATOHSI_RESERVATION_CEREMONY_V1", operator=operator,
                  chain_id=profile["chain_id"], genesis_hash=Web3.to_hex(w3.eth.get_block(0).hash),
                  invitation_hash=fingerprint(invite), manifest=manifest.to_dict(), runtimes=runtime_hashes,
                  formation_hash=Web3.to_hex(formation), token_hash=Web3.to_hex(token),
                  creation=creation, adoption=adopt, token_metadata=terms["token_metadata"])
    if kind == "confederation" and terms["mode"] == 2:
        review["enable_custodian_publishers"] = dict(target=board.address, method="enableCustodianPublishers",
                                                    args=[scope["replacement_session"]])
    verify_anchor(w3, block.number, block.hash)
    return review, bool(any(existing))


def run(args):
    if args.send and not all((args.accept_fingerprint, args.identity_file, args.state)):
        raise ValueError("RESERVATION_TRANSFER_SEND_OPTIONS")
    if not 1 <= args.confirmations <= 256:
        raise ValueError("EVM_CONFIRMATIONS_POLICY")
    w3 = evm_web3(args.rpc, timeout=20)
    _, invite = invitation(w3, args.board)
    confirmations = max(args.confirmations, int(invite["evm_confirmations"]))
    block = read_block(w3, confirmations)
    profile = assembled_profile(w3, args.board, snapshot=block)
    previous, factory = _session(args.from_session), Web3.to_checksum_address(args.factory)
    review, existing = inspect(w3, profile, factory, previous, args.kind, block)
    digest = _digest(review)
    if not args.send:
        return dict(status="RESERVATION_CEREMONY_REVIEW", fingerprint=digest, review=review,
                    already_created=existing, block_number=str(block.number))
    if args.accept_fingerprint != digest or account_at(args.identity_file).address != review["operator"]:
        raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
    public = {key: review[key] for key in ("board", "role", "replacement_session")}
    board, _ = _board_and_invitation(w3, review["board"], block, pinned=True)
    if (existing and _read(board, block, "registry") == review["registry"]
            and _read(board, block, "intentCoordinator") == review["coordinator"]):
        return dict(status="RESERVATION_CEREMONY_READY", **public)
    runtime = review["runtimes"]
    contracts = {key: dict(address=review[key], artifact=kind, runtime_hash=runtime[key],
                          artifact_hash=artifact_fingerprint(artifact(kind)))
                 for key, kind in (("board", "DKGOnchainBoard"), ("registry", "DKGCoordinator"))}
    steps = [
        dict(id="bind-registry", kind="call", contract="board", method="bindRegistry", args=[review["registry"]]),
        dict(id="bind-coordinator", kind="call", contract="board", method="bindIntentCoordinator", args=[review["coordinator"]]),
        dict(id="create-ceremony", kind="call", contract="registry", method="createCeremony", args=review["creation"]),
    ]
    plan = dict(deployment_id="replacement-ceremony-" + digest, chain_id=profile["chain_id"],
                evm_rpc=args.rpc, evm_confirmations=str(confirmations), mode=profile["mode"],
                bitcoin_network=profile["bitcoin_network"], contracts=contracts, steps=steps)
    journal = Journal(args.state)
    review_key = plan["deployment_id"] + ":review"
    saved = _entry(journal, review_key)
    if saved is None:
        saved = journal.put(review_key, dict(review=review, number=str(block.number), hash=Web3.to_hex(block.hash),
                                             confirmations=str(confirmations)))
    memo = saved["request"]
    if memo["review"] != review:
        raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
    confirmations = max(confirmations, int(memo["confirmations"]))
    safe = read_block(w3, confirmations)
    verify_anchor(w3, int(memo["number"]), memo["hash"], safe=safe)
    journal.update(review_key, "CONFIRMED", dict(read_anchor=dict(number=memo["number"], hash=memo["hash"])))
    plan["evm_confirmations"] = memo["confirmations"]
    runner = Bootstrap(plan, args.state, evm_rpc_override=args.rpc, evm_confirmation_floor=confirmations)

    def gate(_steps):
        latest = w3.eth.get_block("latest")
        verify_anchor(w3, int(memo["number"]), memo["hash"], safe=latest)
        current, created = inspect(w3, profile, factory, previous, args.kind, latest)
        if current != review:
            raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
        return created and _steps[0]["id"] == "create-ceremony"

    def apply(identity):
        return runner.apply(identity, before_submit=gate)

    result = apply_confirmed(SimpleNamespace(apply=apply), args.identity_file)
    if result["status"] == "VERIFIED":
        safe = read_block(w3, confirmations)
        current, created = inspect(w3, profile, factory, previous, args.kind, safe)
        board, _ = _board_and_invitation(w3, review["board"], safe, pinned=True)
        if (not created or current != review or _read(board, safe, "registry") != review["registry"]
                or _read(board, safe, "intentCoordinator") != review["coordinator"]):
            raise ValueError("RESERVATION_TRANSFER_RECEIPT")
    return dict(status="RESERVATION_CEREMONY_READY" if result["status"] == "VERIFIED" else result["status"],
                **public)


def execute(args):
    return fresh_invite(args) if args.action == "reservation-invite" else run(args)
