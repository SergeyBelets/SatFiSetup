"""Inspect enrolled Bitcoin custody handovers and execute durable owner actions."""
import hashlib
import json
import re
from pathlib import Path
from web3 import Web3
from web3.logs import DISCARD
from .safe_errors import DiagnosticArgumentParser
from .bootstrap import Bootstrap, artifact
from .bootstrap_custody import review_intent, SOURCE_FIELDS
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .dao_client import account_at, units, wire
from .evm_network import read_block
from .onboarding import read_public, write_public
from .operations import canonical, Journal


def inspect(w3, board_address):
    from .onchain_transport import ceremony_bundle
    from .board_intents import intent_rows
    bundle = ceremony_bundle(w3, board_address, "conf_ceremony")
    invite = bundle["onboarding"]["invitation"]
    if "bitcoin_custody" not in invite:
        raise ValueError("BITCOIN_CUSTODY_POLICY_REQUIRED")
    block = read_block(w3, int(invite["evm_confirmations"]))
    def contract(address, kind):
        checked_runtime(w3, address, kind, block.number)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])
    def read(c, method, *args):
        return getattr(c.functions, method)(*args).call(block_identifier=block.number)
    board = contract(board_address, "DKGOnchainBoard")
    if bytes(read(board, "custodyPublisherEpoch")).hex() != invite["sessions"]["conf_ceremony"]:
        raise ValueError("BITCOIN_CUSTODY_PUBLISHER_EPOCH")
    candidates = {}
    native = invite.get("ulr", {}).get("version") == "3"
    issuer_kind = "ConfederationBitcoinReserve" if native else "MiningPoolV1"
    gateway_kind = "BitcoinCustodyTransferV2" if native else "BitcoinCustodyTransfer"
    pool_kind = "ConfederationPoolV2" if native else "ConfederationMultiPool"
    for row in intent_rows(w3, board, block):
        intent = row["intent"]
        if intent.get("artifact") != issuer_kind or intent.get("hash_method") != "getSetCustodyTransferOperationHash":
            continue
        review_intent(w3, intent, bundle, block.number)
        mining = contract(intent["target"], issuer_kind)
        gateway = contract(intent["hash_args"][0], gateway_kind)
        auth = contract(read(mining, "authorization"), "DKGAuthorization")
        op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
        if not read(auth, "executedOperations", op):
            continue
        if (read(mining, "custodyTransfer") != gateway.address
                or read(auth, "operationTargets", op) != mining.address
                or bytes(read(auth, "dkgId")).hex() != invite["sessions"]["conf_ceremony"]
                or list(read(auth, "getParticipants")) != invite["participants"][1:]
                or int(read(auth, "threshold")) != int(invite["bitcoin_custody"]["threshold"])):
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        pool = contract(read(gateway, "pool"), pool_kind)
        if read(pool, "confederationId") != invite["confederation_id"]:
            raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")
        candidates[gateway.address] = dict(gateway=gateway, mining=mining, pool=pool)
    if len(candidates) != 1:
        raise ValueError("BITCOIN_CUSTODY_BINDING_REQUIRED")
    nodes = next(iter(candidates.values()))
    public = dict(chain_id=str(w3.eth.chain_id), bitcoin_network=invite["bitcoin_network"],
        board=board.address, gateway=nodes["gateway"].address, mining=nodes["mining"].address,
        pool=nodes["pool"].address, destination_script=bytes(read(nodes["gateway"], "destinationScript")).hex(),
        bitcoin_participants=invite["participants"][1:], threshold=invite["bitcoin_custody"]["threshold"],
        sources=invite["bitcoin_custody"]["sources"])
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BITCOIN_CUSTODY_REORG")
    return dict(nodes=nodes, invite=invite, block=block, public=public,
        fingerprint=hashlib.sha256(canonical(public).encode()).hexdigest())


def source_descriptor(w3, mining_address, block):
    def contract(address, kind):
        checked_runtime(w3, address, kind, block.number)
        prove_creation(w3, address, kind, None, block.number) if kind != "PoolMpToken" else None
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])
    def read(c, getter):
        return getattr(c.functions, getter)().call(block_identifier=block.number)
    mining = contract(mining_address, "MiningPoolV1")
    token = contract(read(mining, "mpToken"), "PoolMpToken")
    escrow = contract(read(mining, "escrow"), "BitcoinWithdrawalEscrow")
    deposits = contract(read(mining, "deposits"), "BitcoinDepositRegistry")
    batches = contract(read(escrow, "batches"), "BitcoinBatchRegistry")
    if (read(token, "poolDAO") != mining.address or read(escrow, "depositRegistry") != deposits.address
            or read(deposits, "spendRecorder") != escrow.address or read(batches, "withdrawalEscrow") != escrow.address):
        raise ValueError("BITCOIN_CUSTODY_SOURCE_GRAPH")
    result = dict(escrow=escrow.address, token=token.address)
    for node, field in ((escrow, "escrow_hash"), (token, "token_hash"), (mining, "mining_hash"),
                        (deposits, "deposit_hash"), (batches, "batch_hash")):
        result[field] = Web3.to_hex(Web3.keccak(w3.eth.get_code(node.address, block_identifier=block.number)))
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BITCOIN_CUSTODY_REORG")
    return result


def transact(w3, context, args, kind, target, method, values):
    if not args.identity_file:
        raise ValueError("SEND_REQUIRES_IDENTITY_STATE_AND_ID")
    sender = account_at(args.identity_file).address
    contract = w3.eth.contract(address=target, abi=artifact(kind)["abi"])
    if not args.send:
        result = getattr(contract.functions, method)(*values).call({"from": sender})
        return dict(status="SIMULATED", result=wire(result), target=target, method=method)
    if not args.state or not args.id or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.id) is None:
        raise ValueError("SEND_REQUIRES_IDENTITY_STATE_AND_ID")
    invite = context["invite"]
    prefix = "custody-" + sender + "-" + args.id
    request = dict(fingerprint=context["fingerprint"], target=target, kind=kind, method=method, args=wire(values))
    Journal(args.state).put(prefix + ":request", request)
    profile = dict(deployment_id=prefix, chain_id=invite["chain_id"], bitcoin_network=invite["bitcoin_network"],
        mode=invite["mode"], evm_confirmations=invite["evm_confirmations"], evm_rpc=args.rpc,
        contracts=dict(target=dict(address=target, artifact=kind,
            runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(target, block_identifier=context["block"].number))))),
        steps=[dict(id="send", kind="call", contract="target", method=method, args=wire(values))])
    if "bitcoin_policy" in invite:
        profile["bitcoin_policy"] = invite["bitcoin_policy"]
    runner = Bootstrap(profile, args.state)
    result = runner.apply_confirmed(args.identity_file)
    item = runner.journal.get(prefix + ":send")["result"]
    output = dict(status=result["status"], tx_hash=item.get("tx_hash"), target=target, method=method)
    if result["status"] == "VERIFIED" and method == "request":
        receipt = runner.verify_receipt(item)
        events = context["nodes"]["gateway"].events.Requested().process_receipt(receipt, errors=DISCARD)
        if len(events) != 1:
            raise ValueError("BITCOIN_CUSTODY_REQUEST_RECEIPT")
        output.update(transfer_id=str(events[0]["args"]["id"]),
                      source_request=Web3.to_hex(events[0]["args"]["requestKey"]))
    return output


def main(argv):
    parser = DiagnosticArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("inspect", "source", "approve", "request", "status", "cancel", "minimum", "finalize"):
        p = sub.add_parser(action)
        p.add_argument("--rpc", required=True)
        if action == "source":
            p.add_argument("--mining", required=True)
            p.add_argument("--confirmations", type=int, required=True)
            p.add_argument("--output", required=True)
            continue
        p.add_argument("--board", required=True)
        if action == "inspect":
            continue
        p.add_argument("--accept-fingerprint", required=True)
        if action in ("approve", "request"):
            p.add_argument("--source", type=int, required=True)
            p.add_argument("--satoshis", required=True)
        if action in ("request", "minimum"):
            p.add_argument("--minimum-shares", required=True, help="decimal cBTC amount, at most 18 decimal places")
        if action == "request":
            p.add_argument("--recipient", required=True)
        if action in ("status", "cancel", "minimum"):
            p.add_argument("--transfer-id", type=int, required=True)
        if action == "finalize":
            p.add_argument("--transfer-id", type=int, action="append", required=True)
            p.add_argument("--paid-batch", required=True)
            p.add_argument("--vout", type=int, required=True)
            p.add_argument("--bitcoin-rpc", required=True)
            p.add_argument("--bitcoin-cookie", required=True)
        if action != "status":
            p.add_argument("--identity-file", required=True)
            p.add_argument("--state")
            p.add_argument("--id")
            p.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if args.action == "source":
        value = source_descriptor(w3, Web3.to_checksum_address(args.mining), read_block(w3, args.confirmations))
        if Path(args.output).exists():
            raise ValueError("BITCOIN_CUSTODY_OUTPUT_EXISTS")
        write_public(args.output, value)
        print(json.dumps(dict(status="BITCOIN_CUSTODY_SOURCE_EXPORTED", source=value)))
        return 0
    context = inspect(w3, args.board)
    if args.action == "inspect":
        print(json.dumps(dict(status="BITCOIN_CUSTODY_CONFIGURED", fingerprint=context["fingerprint"], **context["public"])))
        return 0
    if args.accept_fingerprint != context["fingerprint"]:
        raise ValueError("BITCOIN_CUSTODY_FINGERPRINT")
    gateway = context["nodes"]["gateway"]
    target, kind, method = gateway.address, ("BitcoinCustodyTransferV2" if context["invite"].get("ulr", {}).get("version") == "3"
                                          else "BitcoinCustodyTransfer"), args.action
    at = context["block"].number
    if args.action == "status":
        t = gateway.functions.transfers(args.transfer_id).call(block_identifier=at)
        value = dict(status=("NONE", "WAITING", "COMPLETED", "CANCELLED")[int(t[5])], transfer_id=str(args.transfer_id),
                     owner=t[0], recipient=t[1], source=str(t[2]), amount_units=str(t[3]), minimum_share_units=str(t[4]))
        if int(t[5]):
            s = context["public"]["sources"][int(t[2])]
            escrow = w3.eth.contract(address=s["escrow"], abi=artifact("BitcoinWithdrawalEscrow")["abi"])
            key = escrow.functions.requestKey(gateway.address, args.transfer_id).call(block_identifier=at)
            request = escrow.functions.getRequest(key).call(block_identifier=at)
            value.update(source_request=Web3.to_hex(key), source_state=("NONE", "PENDING", "BOUND", "PAID", "REFUNDED")[int(request[7])],
                         original_batch=Web3.to_hex(request[6]))
        print(json.dumps(value)); return 0
    if args.action in ("approve", "request"):
        if (not re.fullmatch(r"[1-9][0-9]{0,15}", args.satoshis)
                or not 0 < int(args.satoshis) <= 2100000000000000
                or not 0 <= args.source < len(context["public"]["sources"])):
            raise ValueError("BITCOIN_CUSTODY_AMOUNT")
        amount = int(args.satoshis) * 10**10
        if args.action == "approve":
            kind, target = "PoolMpToken", context["public"]["sources"][args.source]["token"]
            values = [gateway.address, amount]
        else:
            values = [args.source, amount, units(args.minimum_shares, 18), Web3.to_checksum_address(args.recipient)]
    elif args.action in ("cancel", "minimum"):
        values = [args.transfer_id]
        if args.action == "minimum":
            method = "setMinimumShares"; values.append(units(args.minimum_shares, 18))
    else:
        from .bitcoin_rpc import BitcoinRPC
        from .bitcoin_proofs import inclusion
        row = gateway.functions.transfers(args.transfer_id[0]).call(block_identifier=at)
        source = context["public"]["sources"][int(row[2])]
        escrow = w3.eth.contract(address=source["escrow"], abi=artifact("BitcoinWithdrawalEscrow")["abi"])
        batch = bytes.fromhex(args.paid_batch.removeprefix("0x"))
        if len(batch) != 32:
            raise ValueError("BITCOIN_CUSTODY_BATCH")
        paid = escrow.functions.payment(batch).call(block_identifier=at)
        proof = inclusion(BitcoinRPC(args.bitcoin_rpc, args.bitcoin_cookie,
            context["invite"]["bitcoin_network"], context["invite"].get("bitcoin_policy")), bytes(paid[0])[::-1].hex())
        if int(proof["confirmations"]) < int(context["invite"]["bitcoin_confirmations"]):
            raise ValueError("BITCOIN_CUSTODY_IMMATURE")
        values = [args.transfer_id, batch, bytes.fromhex(proof["raw"]), args.vout, bytes.fromhex(proof["block_hash"]),
                  [bytes.fromhex(x) for x in proof["siblings"]], int(proof["index"])]
    print(json.dumps(transact(w3, context, args, kind, target, method, values)))
    return 0
