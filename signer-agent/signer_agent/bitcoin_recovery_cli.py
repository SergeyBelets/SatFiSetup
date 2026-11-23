"""Prepare and settle a CLTV recovery for one enrolled S withdrawal family."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from eth_abi import encode
from web3 import Web3
from web3.logs import DISCARD

from . import crypto
from .bitcoin_onchain import send, snapshot_check
from .bitcoin_onchain_plan import decimal, hex_value
from .bitcoin_proofs import inclusion
from .bitcoin_recovery_policy import ARTIFACT, enrolled_policy, review_module, review_successor
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError, payment_block_height, satoshis
from .bitcoin_trust import checked_runtime
from .bitcoin_worker import outpoint_hash
from .bootstrap import artifact
from .dao_client import account_at
from .evm_rpc import evm_web3
from .funding_envelope import inspect_funding
from .onboarding import read_public, write_public
from .operations import canonical
from .safe_errors import DiagnosticArgumentParser


def _context(w3, board, *, at=None):
    from .custody_transfer_cli import inspect

    context = inspect(w3, board, at=at)
    invite = context["invite"]
    policy = enrolled_policy(invite)
    if policy is None:
        raise ValueError("BTC_RECOVERY_POLICY")
    block = context["block"]
    reserve = context["nodes"]["mining"]

    def read(node, method, *args):
        return getattr(node.functions, method)(*args).call(block_identifier=block.number)

    def load(address, kind):
        checked_runtime(w3, address, kind, block.number)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    escrow = load(read(reserve, "escrow"), "BitcoinWithdrawalEscrowV2")
    module = load(read(escrow, "recovery"), ARTIFACT)
    batches = load(read(escrow, "batches"), "BitcoinBatchRegistry")
    deposits = load(read(reserve, "deposits"), "BitcoinDepositRegistry")
    source_script = bytes.fromhex(context["public"]["destination_script"])
    source_hash = Web3.keccak(source_script)
    active_script = bytes(read(reserve, "payoutScript"))
    active_hash = Web3.keccak(active_script)
    review_module(w3, escrow, reserve, module, block.number, policy, source_hash=source_hash)
    fee_verifier = read(batches, "withdrawalFeeVerifier")
    checked_runtime(w3, fee_verifier, "BitcoinWithdrawalFeeVerifier", block.number)
    if (read(escrow, "depositRegistry") != deposits.address
            or read(batches, "withdrawalEscrow") != escrow.address
            or read(batches, "recoveryController") != module.address
            or read(escrow, "feeVerifier") != fee_verifier
            or active_hash != read(module, "activeScriptHash")
            or active_hash != read(deposits, "poolScripts", reserve.address)):
        raise ValueError("BTC_RECOVERY_BINDING")
    public = dict(chain_id=invite["chain_id"], bitcoin_network=invite["bitcoin_network"],
                  board=board, reserve=reserve.address, escrow=escrow.address, recovery=module.address,
                  source_script=source_script.hex(), policy=policy, custody=context["fingerprint"])
    paused = read(module, "paused")
    snapshot_check(w3, block)
    return SimpleNamespace(w3=w3, block=block, invite=invite, policy=policy, module=module,
                           batches=batches, deposits=deposits, escrow=escrow, read=read, paused=paused,
                           source_hash=source_hash, active_script=active_script, load=load,
                           public=public, fingerprint=hashlib.sha256(canonical(public).encode()).hexdigest())


def _split(outpoint):
    if not isinstance(outpoint, str) or outpoint.count(":") != 1:
        raise ValueError("BTC_RECOVERY_INPUT")
    txid, vout = outpoint.split(":")
    digest = hex_value(txid, 32)
    index = decimal(vout, 2**32 - 1)
    wire_key = Web3.keccak(encode(["bytes32", "uint32"], [digest[::-1], index]))
    return txid, index, wire_key


def _unspent(bitcoin, outpoint, script, amount):
    txid, vout, _ = _split(outpoint)
    result = bitcoin.call("gettxout", txid, vout, False)
    if (not isinstance(result, dict) or not isinstance(result.get("scriptPubKey"), dict)
            or result["scriptPubKey"].get("hex") != script or satoshis(result.get("value")) != amount
            or type(result.get("confirmations")) is not int or result["confirmations"] < 1):
        raise ValueError("BTC_RECOVERY_INPUT")
    if result.get("coinbase") is True and result["confirmations"] < 100:
        raise ValueError("BTC_RECOVERY_INPUT")


def _plan(ctx, bitcoin, args):
    idle = getattr(args, "idle", False)
    root = None if idle else hex_value(args.root, 32)
    read = ctx.read
    if idle and not ctx.paused:
        raise ValueError("BTC_RECOVERY_IDLE_REQUIRES_PAUSE")
    if not idle and (read(ctx.batches, "batchState", root) != 2
                    or bytes(read(ctx.batches, "reservationRoot", root)) != root
                    or not read(ctx.escrow, "isBound", root) or any(read(ctx.module, "recoveredTxid", root))):
        raise ValueError("BTC_RECOVERY_FAMILY")
    if not 1 <= len(args.input) <= 16:
        raise ValueError("BTC_RECOVERY_INPUT")
    parsed = [_split(outpoint) for outpoint in args.input]
    keys = [outpoint_hash(outpoint) for outpoint in args.input]
    sponsor_key = outpoint_hash(args.sponsor)
    if len(set(keys)) != len(keys) or sponsor_key in keys:
        raise ValueError("BTC_RECOVERY_INPUT")
    if not idle and keys != list(read(ctx.batches, "getInputs", root)):
        raise ValueError("BTC_RECOVERY_INPUT")
    source = ctx.public["source_script"]
    inputs = []
    for outpoint, (_, _, key), reservation in zip(args.input, parsed, keys):
        amount = read(ctx.deposits, "backingSats", key)
        expected = bytes(32) if idle else root
        if not amount or bytes(read(ctx.batches, "reservations", reservation)) != expected:
            raise ValueError("BTC_RECOVERY_INPUT")
        _unspent(bitcoin, outpoint, source, amount)
        inputs.append(dict(outpoint=outpoint, script=source, amount=str(amount)))
    _, _, key = _split(args.sponsor)
    sponsor_amount = read(ctx.module, "sponsorSats", key)
    if (not sponsor_amount or any(read(ctx.batches, "reservations", sponsor_key))
            or any(read(ctx.deposits, name, key) for name in ("backingSats", "consumed", "walletOutput"))):
        raise ValueError("BTC_RECOVERY_SPONSOR")
    sponsor = ctx.policy["sponsor_script"]
    _unspent(bitcoin, args.sponsor, sponsor, sponsor_amount)
    fee = decimal(args.fee_sats, int(ctx.policy["max_fee_sats"]), positive=True)
    snapshot_check(ctx.w3, ctx.block)
    return dict(policy={name: ctx.policy[name] for name in ("height", "threshold", "keys")},
                internal_key=args.internal_key, inputs=inputs,
                sponsor=dict(outpoint=args.sponsor, script=sponsor, amount=str(sponsor_amount)),
                successor=ctx.policy["successor_script"], fee=str(fee))


def _read_transaction_file(path):
    with Path(path).open("rb") as stream:
        data = stream.read(800_003)
    if len(data) > 800_002:
        raise ValueError("BTC_RECOVERY_RAW")
    return data.decode("ascii").strip()


def _proof(bitcoin, txid, depth):
    hex_value(txid, 32)
    proof = inclusion(bitcoin, txid)
    if int(proof["confirmations"]) < depth:
        raise ValueError("BTC_RECOVERY_DEPTH")
    block_hash = bytes.fromhex(proof["block_hash"])
    payment_block_height(bitcoin, block_hash[::-1].hex())
    raw = bytes.fromhex(inspect_funding(proof["raw"], txid)["stripped"])
    return raw, block_hash, [bytes.fromhex(item) for item in proof["siblings"]], int(proof["index"])


ROTATE_HELPER = "getRotateOperationHash"
ROTATE_ROLE = "conf_ceremony"


def _authority(ctx):
    return ctx.load(ctx.read(ctx.batches, "authorization"), "DKGAuthorization")


def review_rotation_intent(w3, intent, bundle, number, *, finalized=True):
    from .dkg_protocol import Manifest
    from .evm_network import read_block

    if (intent.get("artifact") != ARTIFACT or intent.get("hash_method") != ROTATE_HELPER
            or intent.get("bitcoin_rotation") != "1" or intent.get("role") != ROTATE_ROLE
            or bundle.get("role") != ROTATE_ROLE or intent.get("chain_id") != str(w3.eth.chain_id)
            or not isinstance(bundle.get("onchain_board"), str)):
        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    ctx = _context(w3, bundle["onchain_board"], at=number)
    if finalized and number > read_block(w3, int(ctx.invite["evm_confirmations"])).number:
        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    manifest = Manifest.from_dict(bundle)
    auth = _authority(ctx)
    dkg = ctx.load(ctx.read(ctx.batches, "dkg"), "DKGCoordinator")
    if (ctx.invite != bundle.get("onboarding", {}).get("invitation") or manifest.purpose != "EVM_AUTH"
            or manifest.chain_id != w3.eth.chain_id or dkg.address != manifest.registry
            or ctx.read(auth, "registry") != dkg.address or ctx.read(auth, "dkgId") != manifest.session_id
            or ctx.read(auth, "coordinator") != intent.get("coordinator")
            or intent.get("target") != ctx.module.address):
        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    args = intent.get("hash_args")
    if not isinstance(args, list) or len(args) != 2:
        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    if not all(isinstance(value, str) for value in args):
        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    wallet_id, root = (hex_value(value.removeprefix("0x"), 32) for value in args)
    op = hex_value(str(intent.get("operation_hash", "")).removeprefix("0x"), 32)
    roster, threshold = ctx.read(auth, "getParticipants"), ctx.read(auth, "threshold")
    result = review_successor(dkg, number, wallet_id, ctx.policy, roster, threshold)
    if not ctx.read(ctx.module, "canRefund", root):
        raise ValueError("BTC_RECOVERY_ROTATION_PROOF")
    successor = bytes(ctx.read(ctx.module, "successorWalletId"))
    complete = any(successor)
    if complete:
        if (successor != wallet_id or ctx.read(ctx.module, "rotationRoot") != root
                or ctx.read(ctx.module, "successorWalletResult") != result
                or not ctx.read(auth, "executedOperations", op)
                or ctx.read(auth, "operationTargets", op) != ctx.module.address):
            raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
    else:
        if (not ctx.paused or wallet_id == ctx.read(ctx.batches, "initialWalletId")
                or ctx.read(ctx.module, ROTATE_HELPER, wallet_id, root) != op):
            raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
        if (ctx.read(ctx.deposits, "backingByScript", ctx.source_hash)
                or ctx.read(ctx.deposits, "backingByScript", ctx.read(ctx.module, "successorScriptHash"))
                    != ctx.read(ctx.deposits, "totalBackingSats")):
            raise ValueError("BTC_RECOVERY_ROTATION_BACKING")
    snapshot_check(w3, ctx.block)
    return SimpleNamespace(ctx=ctx, auth=auth, op=op, wallet_id=wallet_id, root=root, complete=complete)


def _executor(ctx, args):
    account = account_at(args.identity_file)
    if args.send and not args.state:
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    return SimpleNamespace(w3=ctx.w3, account=account, identity_file=args.identity_file, transaction_journal=args.state)


def _submit(ctx, executor, target, kind, method, values, *, header=None):
    graph = dict(version="2", chain_id=ctx.invite["chain_id"], bitcoin_network=ctx.invite["bitcoin_network"],
                 evm_confirmations=ctx.invite["evm_confirmations"])
    if "bitcoin_policy" in ctx.invite:
        graph["bitcoin_policy"] = ctx.invite["bitcoin_policy"]
    snapshot_check(ctx.w3, ctx.block)
    opts = {}
    from .zk_spv import is_zk
    if header is not None and is_zk(graph.get("bitcoin_policy")):
        from .custody_transfer_cli import _destination_zk_adapter

        reserve = ctx.w3.eth.contract(address=ctx.public["reserve"], abi=artifact("ConfederationBitcoinReserve")["abi"])
        adapter = _destination_zk_adapter(ctx.w3, dict(invite=ctx.invite, block=ctx.block, nodes={"mining": reserve}))
        opts["zk_proof"] = (adapter, header)
    return send(executor, target.address, kind, method, values, graph=graph, **opts)


def _evm(ctx, args, method, values, *, header=None):
    executor = _executor(ctx, args)
    if not args.send:
        snapshot_check(ctx.w3, ctx.block)
        getattr(ctx.module.functions, method)(*values).call({"from": executor.account.address},
            block_identifier=ctx.block.number)
        return dict(status="SIMULATED", target=ctx.module.address, method=method)
    receipt = _submit(ctx, executor, ctx.module, ARTIFACT, method, values, header=header)
    return dict(status="CONFIRMED", tx_hash=Web3.to_hex(receipt.transactionHash), method=method)


def _rotation(ctx, args):
    from .onchain_transport import can_publish_intent, ceremony_bundle
    from .ulr_treasury import intent_for

    w3 = ctx.w3
    bundle = ceremony_bundle(w3, ctx.public["board"], ROTATE_ROLE)
    if args.action == "rotate-plan":
        auth = _authority(ctx)
        values = [hex_value(value.removeprefix("0x"), 32) for value in (args.wallet_id, args.root)]
        intent = intent_for(w3, ctx.module, ARTIFACT, ROTATE_HELPER, values, auth.address,
                            ROTATE_ROLE, ctx.block.number, bitcoin_rotation="1")
        review_rotation_intent(w3, intent, bundle, ctx.block.number)
        write_public(args.intent, intent)
        return dict(status="BTC_RECOVERY_ROTATION_PREPARED", intent=intent)
    intent = read_public(args.intent)
    review = review_rotation_intent(w3, intent, bundle, ctx.block.number)
    if review.complete:
        return dict(status="ALREADY_EXECUTED", operation_hash=Web3.to_hex(review.op))
    ctx, auth, op = review.ctx, review.auth, review.op
    executor = _executor(ctx, args)
    board = ctx.load(ctx.public["board"], "DKGOnchainBoard")
    if args.action == "rotate-propose":
        deadline = decimal(args.deadline, 2**64 - 1, positive=True)
        if deadline <= ctx.block.timestamp or not can_publish_intent(board, executor.account.address, ctx.block.number):
            raise ValueError("BTC_RECOVERY_ROTATION_PUBLISHER")
        coordinator = ctx.load(ctx.read(auth, "coordinator"), "FROSTCoordinator")
        values = [ctx.read(auth, "dkgId"), op, ctx.module.address, deadline]
        if not args.send:
            coordinator.functions.createSessionFromDKG(*values).call({"from": executor.account.address},
                block_identifier=ctx.block.number)
            return dict(status="SIMULATED", operation_hash=Web3.to_hex(op))
        receipt = _submit(ctx, executor, coordinator, "FROSTCoordinator", "createSessionFromDKG", values)
        events = [event for event in coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
                  if event["address"] == coordinator.address]
        if len(events) != 1:
            raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
        session = int(events[0]["args"]["sessionId"])
        _submit(ctx, executor, board, "DKGOnchainBoard", "publishIntent",
                [session, canonical(dict(intent, session=str(session))).encode()])
        return dict(status="AWAITING_PARTICIPANTS", session=str(session), operation_hash=Web3.to_hex(op))
    if not ctx.read(auth, "isOperationApproved", op, ctx.module.address):
        if not args.send:
            return dict(status="AUTHORIZATION_REQUIRED", operation_hash=Web3.to_hex(op))
        session = decimal(args.session, 2**256 - 1, positive=True)
        if bytes(ctx.read(board, "intents", session)) != canonical(dict(intent, session=str(session))).encode():
            raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
        _submit(ctx, executor, auth, "DKGAuthorization", "authorizeOperation", [session, op, ctx.module.address])
    return _evm(ctx, args, "rotate", [review.wallet_id, review.root, op])


def _parser():
    parser = DiagnosticArgumentParser(prog="frost-signer bitcoin recovery", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    bitcoin_actions = ("register-sponsor", "prepare", "combine", "verify", "broadcast", "settle")
    for action in ("inspect", "pause", *bitcoin_actions, "rotate-plan", "rotate-propose", "rotate-execute"):
        command = actions.add_parser(action)
        command.add_argument("--rpc", required=True)
        command.add_argument("--board", type=Web3.to_checksum_address, required=True)
        if action != "inspect":
            command.add_argument("--accept-fingerprint", required=True)
        if action in ("pause", "register-sponsor", "settle", "rotate-propose", "rotate-execute"):
            command.add_argument("--identity-file", required=True)
            command.add_argument("--state")
            command.add_argument("--send", action="store_true")
        if action in bitcoin_actions:
            command.add_argument("--bitcoin-rpc", required=True)
            command.add_argument("--bitcoin-cookie", required=True)
        if action in ("prepare", "combine", "verify", "broadcast", "settle"):
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--root")
            mode.add_argument("--idle", action="store_true", help="Move unreserved backing from the original script")
        elif action == "inspect":
            command.add_argument("--root")
            command.add_argument("--backing-offset")
            command.add_argument("--migration-txid")
        if action in ("prepare", "combine", "verify", "broadcast"):
            command.add_argument("--input", action="append", required=True)
            command.add_argument("--sponsor", required=True)
            command.add_argument("--internal-key", required=True)
            command.add_argument("--fee-sats", required=True)
        if action in ("verify", "broadcast"):
            command.add_argument("--signed-raw", required=True)
        if action in ("prepare", "combine"):
            command.add_argument("--output", required=True)
        if action == "combine":
            command.add_argument("--partial-psbt", action="append", required=True, help="file containing one base64 PSBT")
        if action in ("register-sponsor", "settle"):
            command.add_argument("--txid", required=True)
        if action == "register-sponsor":
            command.add_argument("--vout", required=True)
        if action.startswith("rotate-"):
            command.add_argument("--intent", required=True)
        if action == "rotate-plan":
            command.add_argument("--root", required=True)
            command.add_argument("--wallet-id", required=True)
        if action == "rotate-propose":
            command.add_argument("--deadline", required=True)
        if action == "rotate-execute":
            command.add_argument("--session")
    return parser


def main(argv):
    args = _parser().parse_args(argv)
    ctx = _context(evm_web3(args.rpc), args.board)
    if args.action == "inspect":
        wallet = bytes(ctx.read(ctx.module, "successorWalletId"))
        rotation_root = bytes(ctx.read(ctx.module, "rotationRoot"))
        result = dict(status="BTC_RECOVERY_CONFIGURED", paused=ctx.paused, fingerprint=ctx.fingerprint, **ctx.public,
                      withdrawals_paused=ctx.read(ctx.module, "withdrawalsPaused"), active_script=ctx.active_script.hex(),
                      old_backing_sats=str(ctx.read(ctx.deposits, "backingByScript", ctx.source_hash)),
                      successor_wallet_id=wallet.hex() if any(wallet) else None)
        if any(rotation_root):
            result.update(rotation_root=rotation_root.hex(),
                          rotation_block=bytes(ctx.read(ctx.module, "recoveryBlock", rotation_root))[::-1].hex())
        if args.root is not None:
            root = hex_value(args.root, 32)
            txid = bytes(ctx.read(ctx.module, "recoveredTxid", root))
            result.update(root=args.root, recovered_txid=txid[::-1].hex() if any(txid) else None,
                          refund_available=ctx.read(ctx.module, "canRefund", root))
        if args.backing_offset is not None:
            offset = decimal(args.backing_offset, 2**256 - 1)
            result["backing_page"] = _backing_page(ctx, offset)
        if args.migration_txid is not None:
            txid = hex_value(args.migration_txid, 32)[::-1]
            header = bytes(ctx.read(ctx.module, "migrationBlock", txid))
            result["migration_block"] = header[::-1].hex() if any(header) else None
        snapshot_check(ctx.w3, ctx.block)
    else:
        if args.accept_fingerprint != ctx.fingerprint:
            raise ValueError("BTC_RECOVERY_FINGERPRINT")
        result = _action(ctx, args)
    print(json.dumps(result, sort_keys=True))
    return 0


def _backing_page(ctx, offset):
    total = ctx.read(ctx.deposits, "backingCount")
    end = min(total, offset + 16)
    entries = []
    for index in range(offset, end):
        txid, vout, sats = ctx.read(ctx.deposits, "backingAt", index)
        outpoint = f"{bytes(txid)[::-1].hex()}:{vout}"
        wire_key = _split(outpoint)[2]
        reservation = ctx.read(ctx.batches, "reservations", outpoint_hash(outpoint))
        entries.append(dict(outpoint=outpoint, sats=str(sats), original_script=
            ctx.read(ctx.deposits, "backingScripts", wire_key) == ctx.source_hash,
            reserved_root=bytes(reservation).hex() if any(reservation) else None))
    return dict(block=str(ctx.block.number), block_hash=Web3.to_hex(ctx.block.hash), entries=entries,
                next_offset=str(end) if end < total else None)


def _action(ctx, args):
    import base64

    if args.action == "pause":
        return _evm(ctx, args, "pause", [])
    if args.action.startswith("rotate-"):
        return _rotation(ctx, args)
    btc = BitcoinRPC(args.bitcoin_rpc, args.bitcoin_cookie, network=ctx.invite["bitcoin_network"],
                     policy=ctx.invite.get("bitcoin_policy"))
    tip = btc.check_network()
    if args.action in ("register-sponsor", "settle"):
        raw, block, siblings, index = _proof(btc, args.txid, int(ctx.policy["confirmations"]))
        if args.action == "register-sponsor":
            return _evm(ctx, args, "registerSponsor", [raw, decimal(args.vout, 2**32 - 1), block, siblings, index],
                        header=block)
        if args.idle:
            return _evm(ctx, args, "sweep", [raw, block, siblings, index], header=block)
        return _evm(ctx, args, "recover", [hex_value(args.root, 32), raw, block, siblings, index], header=block)
    plan = _plan(ctx, btc, args)
    if args.action == "prepare":
        package = crypto.call("bitcoin_recovery_plan_v1", **plan)
        psbt = base64.b64encode(bytes.fromhex(package["psbt"])).decode("ascii")
        write_public(args.output, dict(fingerprint=ctx.fingerprint, root=args.root, idle=args.idle, plan=plan,
            txid=package["txid"], psbt_base64=psbt, principal_sats=package["principal_sats"]))
        return dict(status="BTC_RECOVERY_PREPARED", txid=package["txid"])
    if args.action == "combine":
        from .dkg import atomic_private

        partials = args.partial_psbt
        if not int(ctx.policy["threshold"]) + 1 <= len(partials) <= len(ctx.policy["keys"]) + 1:
            raise ValueError("BTC_RECOVERY_KEYS")
        merged = crypto.call("bitcoin_recovery_attach_v1", **plan,
            partials=[base64.b64decode(_read_transaction_file(path), validate=True).hex() for path in partials])
        atomic_private(args.output, (merged["signed"] + "\n").encode("ascii"))
        return dict(status="BTC_RECOVERY_SIGNED", txid=merged["txid"])
    raw = _read_transaction_file(args.signed_raw)
    checked = crypto.call("bitcoin_recovery_verify_v1", **plan, signed=raw)
    if args.action == "verify":
        return dict(status="BTC_RECOVERY_VERIFIED", txid=checked["txid"])
    if type(tip.get("blocks")) is not int or tip["blocks"] < int(ctx.policy["height"]):
        raise ValueError("BTC_RECOVERY_HEIGHT")
    try:
        btc.call("getrawtransaction", checked["txid"])
    except BitcoinRPCError as error:
        if error.rpc_code != -5:
            raise
        observed = btc.call("testmempoolaccept", [raw])
        if (not isinstance(observed, list) or len(observed) != 1
                or not isinstance(observed[0], dict) or observed[0].get("txid") != checked["txid"]
                or observed[0].get("allowed") is not True):
            raise ValueError("MEMPOOL_REJECTED")
        snapshot_check(ctx.w3, ctx.block)
        if btc.call("sendrawtransaction", raw) != checked["txid"]:
            raise ValueError("TXID_MISMATCH")
    return dict(status="BTC_RECOVERY_BROADCAST", txid=checked["txid"])
