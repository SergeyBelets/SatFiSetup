"""Prepare a dedicated successor BTC_SPEND DKG on the future bootstrap registry."""

import hashlib

from web3 import Web3

from .bootstrap import Bootstrap
from .dao_client import account_at
from .dkg_protocol import Manifest, PURPOSES
from .evm_network import read_block, verify_anchor
from .evm_rpc import evm_web3
from .onchain_transport import ZERO, _board_and_invitation, assembled_profile, invitation, registry_at
from .onboarding import fingerprint
from .operations import canonical


def add_parser(sub):
    parser = sub.add_parser("successor-create", help="Prepare BTC_SPEND on a separate enrolled successor board")
    parser.add_argument("--board", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--accept-fingerprint")
    parser.add_argument("--identity-file")
    parser.add_argument("--state")
    parser.add_argument("--send", action="store_true")


def inspect(w3, board_address, registry_address, block):
    board, invite = _board_and_invitation(w3, board_address, block, pinned=True)
    if invite["version"] != "6" or "bitcoin_recovery" in invite:
        raise ValueError("BTC_SUCCESSOR_CEREMONY_INVITATION")
    profile = assembled_profile(w3, board.address, snapshot=block)
    dkg = registry_at(w3, registry_address, block)

    def read(contract, method, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=block.number)

    selected = profile["btc_ceremony"]
    manifest = Manifest(int(profile["chain_id"]), dkg.address, selected["id"], int(selected["epoch"]),
                        "BTC_SPEND", tuple(selected["participants"]), tuple(selected["transport_keys"]),
                        int(selected["threshold"]))
    bound = read(board, "registry")
    if bound not in (ZERO, dkg.address) or read(dkg, "PHASE_DURATION") != int(invite["phase_duration"]):
        raise ValueError("BTC_SUCCESSOR_CEREMONY_BINDING")
    context = bytes(read(dkg, "ceremonies", manifest.session_id)[0])
    created = any(context)
    if created:
        info = read(dkg, "getSessionInfo", manifest.session_id)
        expired = not info[9] and (info[4] in (5, 6) or block.timestamp > info[5])
        if context != manifest.context or expired:
            raise ValueError("BTC_SUCCESSOR_CEREMONY_BINDING")
    else:
        creator = read(dkg, "ceremonyCreators", manifest.session_id)
        if invite["operator"] != (read(dkg, "ceremonyRegistrar") if creator == ZERO else creator):
            raise ValueError("BTC_SUCCESSOR_CEREMONY_CREATOR")

    creation = [selected["id"], selected["participants"], selected["threshold"],
                selected["transport_keys"], selected["epoch"], Web3.to_hex(PURPOSES[manifest.purpose]),
                Web3.to_hex(manifest.protocol_id)]
    contracts = {
        name: dict(address=node.address, artifact=kind,
                   runtime_hash=Web3.keccak(w3.eth.get_code(node.address, block_identifier=block.number)).hex())
        for name, node, kind in (("board", board, "DKGOnchainBoard"), ("registry", dkg, "DKGCoordinator"))
    }
    review = dict(domain="SATOHSI_BITCOIN_SUCCESSOR_CEREMONY_V1", invitation=fingerprint(invite),
                  genesis_hash=Web3.to_hex(w3.eth.get_block(0).hash), operator=invite["operator"],
                  manifest=manifest.to_dict(), contracts=contracts, creation=creation)
    complete = None
    if created and info[9]:
        result = read(dkg, "getCompletedResult", manifest.session_id)
        complete = dict(result_hash=bytes(result[1]).hex(), successor_script="5120" + bytes(result[4]).hex(),
                        successor_dkg={"registry": dkg.address, "wallet_id": manifest.session_id.hex()})
    verify_anchor(w3, block.number, block.hash)
    return review, bound != ZERO, created, complete


def run(args):
    w3 = evm_web3(args.rpc, timeout=20)
    _, invite = invitation(w3, args.board)
    confirmations = int(invite["evm_confirmations"])
    block = read_block(w3, confirmations)
    review, bound, created, complete = inspect(w3, args.board, args.registry, block)
    digest = hashlib.sha256(canonical(review).encode()).hexdigest()
    result = dict(status="BTC_SUCCESSOR_CEREMONY_REVIEW", fingerprint=digest, review=review,
                  bound=bound, created=created, completed=complete)
    if not args.send:
        return result
    if (args.accept_fingerprint != digest or not args.identity_file or not args.state
            or account_at(args.identity_file).address != review["operator"]):
        raise ValueError("BTC_SUCCESSOR_CEREMONY_FINGERPRINT")
    latest = w3.eth.get_block("latest")
    verify_anchor(w3, block.number, block.hash, safe=latest)
    live, live_bound, live_created, _ = inspect(w3, args.board, args.registry, latest)
    if live != review:
        raise ValueError("BTC_SUCCESSOR_CEREMONY_BINDING")
    if (live_bound, live_created) != (bound, created):
        raise ValueError("EVM_CONFIRMATIONS_PENDING")
    steps = [
        dict(id="bind-registry", kind="call", contract="board", method="bindRegistry",
             args=[review["contracts"]["registry"]["address"]]),
        dict(id="create-ceremony", kind="call", contract="registry", method="createCeremony",
             args=review["creation"]),
    ]
    for step, submitted in zip(steps, (bound, created)):
        if submitted:
            continue
        # Separate fixed journal plans let a restart resume between operations.
        plan = dict(deployment_id="btc-successor-ceremony-" + digest + "-" + step["id"],
                    chain_id=invite["chain_id"], evm_rpc=args.rpc,
                    evm_confirmations=str(confirmations), mode=invite["mode"],
                    bitcoin_network=invite["bitcoin_network"], contracts=review["contracts"], steps=[step])
        if Bootstrap(plan, args.state, evm_rpc_override=args.rpc).apply_confirmed(args.identity_file)["status"] != "VERIFIED":
            raise ValueError("BTC_SUCCESSOR_CEREMONY_UNCONFIRMED")
    result.update(status="BTC_SUCCESSOR_CEREMONY_READY", bound=True, created=True)
    return result
