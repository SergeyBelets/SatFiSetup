"""Prepare, authorize and settle public Bitcoin plans using only on-chain transport.

This module never creates a signing nonce or changes the signing attempt. The
operator's plan and authorization receipts live in encrypted participant state;
the public plan and typed approval intent are the only values sent to a board.
"""

from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from web3.logs import DISCARD

from . import crypto
from .bootstrap import Bootstrap, artifact
from .bitcoin_onchain_plan import (
    MAX_MONEY, MAX_PLAN_BYTES, decimal, hex_value, onchain_plan_hash, script, serialize_plan,
    validate_plan,
)
from .bitcoin_proofs import inclusion, sha256d
from .bitcoin_trust import require_registry, graph_network, bitcoin_for_graph, check_bitcoin_graph, confirmed_block
from .bitcoin_rpc import satoshis
from .bitcoin_worker import outpoint_hash
from .operations import canonical
from .bitcoin_network import is_public_network, relay_artifact


def _contract(registry, address, kind):
    return registry.w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact(kind)["abi"])


def _graph(manifest, key, registry, state, board, *, mint_only=False):
    address = board.functions.batchRegistry().call()
    pinned = (require_registry(manifest, key, registry, state, address, mint_only=True) if mint_only
              else require_registry(manifest, key, registry, state, address))
    graph = pinned["graph"]
    contracts = {
        name: _contract(registry, row["address"], row["artifact"])
        for name, row in graph["contracts"].items()
    }
    return graph, contracts


def _bitcoin(a, graph):
    bitcoin = bitcoin_for_graph(graph, getattr(a, "bitcoin_rpc", None), getattr(a, "bitcoin_cookie", None))
    bitcoin.check_network()
    return bitcoin


def _network_binding(graph, plan):
    if plan["network"] != graph_network(graph):
        raise ValueError("BTC_PLAN_NETWORK_MISMATCH")


def _build(plan):
    from .bitcoin_input_keys import build
    return build(plan)


def _txid(plan):
    return sha256d(bytes.fromhex(_build(plan)["unsigned"]))


def _read(call, block=None):
    return call.call(block_identifier=block) if block is not None else call.call()


def _validate_binding(manifest, key, board, plan, block=None):
    validate_plan(plan, manifest)
    if (plan["signing_board"] != board.address
            or plan["batch_registry"] != _read(board.functions.batchRegistry(), block)
            or plan["wallet_result"] != key["result_hash"]
            or plan["wallet_script"] != "5120" + key["result"]["group"][2:]):
        raise ValueError("BTC_PLAN_BINDING_MISMATCH")
    raw = serialize_plan(plan, manifest)
    if bytes(_read(board.functions.getPlanHash(bytes.fromhex(plan["batch_id"]), raw), block)) != onchain_plan_hash(manifest, plan):
        raise ValueError("BTC_PLAN_HASH_MISMATCH")


def _unspent(bitcoin, outpoint, wallet_script,*,include_mempool=True):
    if not isinstance(outpoint, str) or outpoint.count(":") != 1:
        raise ValueError("BTC_PLAN_OUTPOINT")
    txid, vout = outpoint.split(":")
    hex_value(txid, 32)
    index = decimal(vout, 2**32 - 1)
    utxo = bitcoin.call("gettxout", txid, index, include_mempool)
    if not utxo or (wallet_script is not None and utxo["scriptPubKey"]["hex"] != wallet_script):
        raise ValueError("UTXO_MISMATCH")
    if utxo["confirmations"] < 1 or (utxo["coinbase"] and utxo["confirmations"] < 101):
        raise ValueError("UTXO_IMMATURE")
    amount = satoshis(utxo["value"])
    if not 0 < amount <= MAX_MONEY:
        raise ValueError("UTXO_AMOUNT_INVALID")
    return dict(outpoint=outpoint, amount=str(amount), script=utxo["scriptPubKey"]["hex"])


def _previous(plan):
    if plan["version"] == "5":
        return bytes.fromhex(plan["previous"])
    if plan["version"] == "4":
        return bytes.fromhex(plan["withdrawal_fee"]["previous"])
    return bytes(32)


def _escrow_v2(escrow):
    return any(item.get("name") == "defaultMinerFeeLimitSats" for item in getattr(escrow, "abi", ()))


def _requests(contracts, refs, wallet_script, block=None, *, root=None, fee=None):
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32 or len(set(refs)) != len(refs):
        raise ValueError("BTC_PLAN_WITHDRAWALS")
    due = {}
    escrow = contracts["escrow"]
    if fee is not None and not _escrow_v2(escrow):
        raise ValueError("BTC_WITHDRAWAL_FEE_PROFILE")
    for index, ref in enumerate(refs):
        request = _read(escrow.functions.requests(hex_value(ref, 32)), block)
        pending=int(request[-1])==1 and bytes(request[-2])==bytes(32)
        bound=root is not None and int(request[-1])==2 and bytes(request[-2])==root
        if not (pending or bound) or request[4] <= 0 or request[4] % 10**10:
            raise ValueError("ESCROW_REQUEST_STATE")
        destination = script(bytes(request[5]).hex())
        # A payment to our own wallet would make principal/change accounting ambiguous.
        if destination == wallet_script:
            raise ValueError("BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED")
        if (fee is not None or "address_registry" in contracts) and int(_read(contracts["deposits"].functions.scriptPools(
                keccak(bytes.fromhex(destination))), block), 16):
            raise ValueError("BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED")
        minimum = (_read(escrow.functions.minP2WPKHSats(), block) if destination.startswith("0014")
                   else _read(escrow.functions.minP2TRSats(), block))
        amount = int(request[4]) // 10**10
        if fee is not None:
            if not int(request[0], 16) or not int(request[2], 16):
                raise ValueError("BTC_WITHDRAWAL_FEE_PROFILE")
            pool = escrow.w3.eth.contract(address=request[0], abi=artifact("ConfederationPoolV2")["abi"])
            limit = int(_read(pool.functions.minerFeeLimitSats(request[1]), block))
            if limit == 2**64 - 1:
                limit = int(_read(escrow.functions.defaultMinerFeeLimitSats(), block))
            part, remainder = divmod(fee, len(refs))
            part += index < remainder
            if part > limit:
                raise ValueError("BTC_WITHDRAWAL_FEE_CAP")
            amount -= part
        if amount < minimum:
            raise ValueError("BTC_WITHDRAWAL_BELOW_MINIMUM")
        if int(request[0], 16):
            from .callback_admission import validate_callback
            validate_callback(escrow, request, block)
        due[destination] = due.get(destination, 0) + amount
    return due


def _backing(contracts, inputs, block=None, *, fully_backed=False):
    deposits = contracts["deposits"]
    total = 0
    for item in inputs:
        txid, vout = item["outpoint"].split(":")
        outpoint = keccak(encode(["bytes32", "uint32"], [bytes.fromhex(txid)[::-1], int(vout)]))
        backing = _read(deposits.functions.backingSats(outpoint), block)
        if fully_backed:
            value, btc_script, known = _read(deposits.functions.getValue(outpoint), block)
            if (not known or value != int(item["amount"]) or backing != value or bytes(btc_script).hex() != item["script"]
                    or not int(_read(deposits.functions.scriptPools(keccak(bytes(btc_script))), block), 16)):
                raise ValueError("BTC_WITHDRAWAL_PRINCIPAL_MISMATCH")
        total += backing
    return total


def protect_unminted_deposit(deposits, item, bitcoin, block=None, *, address_mode=False):
    """A committed user deposit is not spendable surplus before MP issuance.

    Recorded wallet change and existing MP backing need no historical Core
    lookup. Only unknown, unissued inputs require inspecting their public tx.
    This is a signer policy; threshold Bitcoin custody remains a trust boundary.
    """
    txid, vout = item["outpoint"].split(":")
    outpoint = keccak(encode(["bytes32", "uint32"], [bytes.fromhex(txid)[::-1], int(vout)]))
    if (_read(deposits.functions.backingSats(outpoint), block)
            or _read(deposits.functions.walletOutput(outpoint), block)):
        return
    if address_mode and bytes(_read(deposits.functions.addressIntents(keccak(bytes.fromhex(item["script"]))), block)) != bytes(32):
        raise ValueError("BTC_UNMINTED_COMMITTED_DEPOSIT")
    transaction = bitcoin.call("getrawtransaction", txid, True)
    if transaction["txid"] != txid:
        raise ValueError("UTXO_MISMATCH")
    protect_custody_transfer(deposits, item, transaction, block)
    from .bitcoin_funding import _has_deposit_marker
    if any(_has_deposit_marker(output["scriptPubKey"]["hex"]) for output in transaction["vout"]):
        raise ValueError("BTC_UNMINTED_COMMITTED_DEPOSIT")


def protect_custody_transfer(deposits, item, transaction, block):
    """An unaccepted incoming handover is never spendable treasury surplus.

    Real Web3 deposit contracts carry their provider. Lightweight legacy unit
    boundaries without Web3 exercise the SMP1 check above independently.
    """
    w3 = getattr(deposits, "w3", None)
    if w3 is None:
        return
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    origin = _read(deposits.functions.scriptPools(keccak(bytes.fromhex(item["script"]))), block)
    if int(origin, 16) == 0:
        return
    checked_runtime(w3, origin, "MiningPoolV1", block)
    mining = w3.eth.contract(address=origin, abi=artifact("MiningPoolV1")["abi"])
    gateway_address = _read(mining.functions.custodyTransfer(), block)
    if int(gateway_address, 16) == 0:
        return
    checked_runtime(w3, gateway_address, "BitcoinCustodyTransfer", block)
    gateway = w3.eth.contract(address=gateway_address, abi=artifact("BitcoinCustodyTransfer")["abi"])
    if _read(gateway.functions.isSourceFunding(bytes.fromhex(transaction["hex"])), block):
        raise ValueError("BTC_UNACCEPTED_CUSTODY_TRANSFER")


def _principal(contracts, refs, block=None):
    """Treasury obligations spend surplus and never extinguish MP backing."""
    return sum(int(request[4]) // 10**10 for request in (
        _read(contracts["escrow"].functions.requests(hex_value(ref, 32)), block) for ref in refs
    ) if int(request[2], 16) != 0)


def _terms(contracts, plan, bitcoin=None, block=None, *, bound_root=None):
    """Check obligations and treasury-funded fees before asking for authorization."""
    core_anchor = None
    if bitcoin is not None and plan.get("version") in ("3", "4"):
        bitcoin.check_network()
        if bitcoin.call("getblockhash",0) != bytes(_read(contracts["address_registry"].functions.bitcoinGenesis(),block)).hex():
            raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
        core_anchor = bitcoin.call("getbestblockhash")
    previous = _previous(plan)
    root = (bytes(_read(contracts["batches"].functions.reservationRoot(previous), block))
            if previous != bytes(32) else bound_root)
    fee = int(plan["fee"]) if plan["version"] == "5" else None
    if fee is not None and previous != bytes(32):
        escrow = contracts["escrow"]
        old = _read(escrow.functions.payment(previous), block)
        previous_inputs = _read(contracts["batches"].functions.getInputs(previous), block)
        if (bytes(old[0]) == bytes(32) or [bytes(key).hex() for key in old[3]] != plan["withdrawal_refs"]
                or old[2] or fee <= _read(escrow.functions.minerFee(previous), block)
                or [bytes(item) for item in previous_inputs] != [
                    outpoint_hash(item["outpoint"]) for item in plan["inputs"]]):
            raise ValueError("BTC_REPLACEMENT_PLAN")
    due = _requests(contracts, plan["withdrawal_refs"], plan["wallet_script"], block, root=root, fee=fee)
    from .withdrawal_fee import obligations
    due=obligations(plan,contracts,due,block)
    for item in plan["inputs"]:
        if bitcoin is not None and _unspent(bitcoin, item["outpoint"], item["script"],**({'include_mempool':False} if previous!=bytes(32) else {})) != item:
            raise ValueError("UTXO_MISMATCH")
        if bitcoin is not None and fee is None:
            protect_unminted_deposit(contracts["deposits"], item, bitcoin, block,
                **({"address_mode": True} if plan.get("version") in ("3", "4") else {}))
    paid = {}
    for output in plan["outputs"]:
        destination = output["script"]
        if destination not in due and destination != plan["wallet_script"]:
            raise ValueError("BTC_UNEXPECTED_OUTPUT")
        paid[destination] = paid.get(destination, 0) + int(output["amount"])
    if any(paid.get(destination) != amount for destination, amount in due.items()):
        raise ValueError("ESCROW_PAYMENT_MISMATCH")
    backing = _backing(contracts, plan["inputs"], block,
                       **({"fully_backed": True} if fee is not None else {}))
    principal = _principal(contracts, plan["withdrawal_refs"], block)
    from .withdrawal_fee import token_fee
    principal += token_fee(plan,contracts,block)
    if fee is not None and paid.get(plan["wallet_script"], 0) != backing - principal:
        raise ValueError("BTC_WITHDRAWAL_PRINCIPAL_MISMATCH")
    if backing < principal or paid.get(plan["wallet_script"], 0) < backing - principal:
        raise ValueError("MINER_FEE_CONSUMES_PRINCIPAL")
    _build(plan)
    if core_anchor is not None and bitcoin.call("getbestblockhash") != core_anchor:
        raise ValueError("BTC_INPUT_CORE_CHANGED")


def _review_wallet(manifest, registry, board, plan, block,*,validate=True):
    from .bitcoin_trust import checked_runtime
    from .dkg_protocol import Manifest

    checked_runtime(registry.w3, board.address, "BitcoinSigningBoard", block.number)
    if (board.functions.dkg().call(block_identifier=block.number) != manifest.registry
            or board.functions.batchRegistry().call(block_identifier=block.number) != plan["batch_registry"]
            or board.functions.MAX_PLAN_BYTES().call(block_identifier=block.number) != MAX_PLAN_BYTES
            or board.functions.maxPacketBytes().call(block_identifier=block.number) < 512):
        raise ValueError("BTC_PLAN_BINDING_MISMATCH")
    wallet_id = bytes(board.functions.walletId().call(block_identifier=block.number))
    dkg = _contract(registry, manifest.registry, "DKGCoordinator")
    wallet = dkg.functions.getCompletedResult(wallet_id).call(block_identifier=block.number)
    if (bytes(wallet[3]) != Web3.keccak(text="BTC_SPEND")
            or list(wallet[5]) != list(manifest.participants)
            or int(wallet[6]) != manifest.threshold):
        raise ValueError("BTC_WALLET_RESULT_MISMATCH")
    btc_manifest = Manifest(
        manifest.chain_id, manifest.registry, wallet_id, int(wallet[2]), "BTC_SPEND",
        tuple(wallet[5]), tuple(bytes(dkg.functions.transportKeys(wallet_id, member).call(block_identifier=block.number))
                               for member in wallet[5]), int(wallet[6]), manifest.protocol_id,
    )
    if btc_manifest.context != bytes(wallet[0]):
        raise ValueError("BTC_WALLET_CONTEXT_MISMATCH")
    public_key = dict(result_hash=bytes(wallet[1]).hex(), result=dict(group="02" + bytes(wallet[4]).hex()))
    if validate:_validate_binding(btc_manifest, public_key, board, plan, block.number)
    if bytes(board.functions.walletResult().call(block_identifier=block.number)) != bytes(wallet[1]):
        raise ValueError("BTC_WALLET_RESULT_MISMATCH")
    return btc_manifest, public_key


def review_intent(intent, manifest, key, registry, state, *, board_address=None,
                  bitcoin_rpc=None, bitcoin_cookie=None):
    """Bind the public plan shown to an EVM signer to the exact operation being signed.

    No BTC private state is loaded. The completed BTC ceremony, original board's
    authorized registry binding and canonical deployment graph supply public
    trust data. Bitcoin UTXOs are independently rechecked by BTC signers later.
    """
    from .bitcoin_trust import (inspect_registry, discover_registry,
                                board_options, confirmed_block)
    from .contract_provenance import prove_creation

    if manifest.purpose != "EVM_AUTH" or registry.w3.eth.chain_id != manifest.chain_id:
        raise ValueError("WRONG_KEY_PURPOSE_OR_NETWORK")
    if not board_address:
        raise ValueError("ONCHAIN_BOARD_REQUIRED")
    if not isinstance(intent, dict) or "bitcoin_plan" not in intent:
        raise ValueError("BTC_PUBLIC_PLAN_REQUIRED")
    plan = validate_plan(intent["bitcoin_plan"])
    board = _contract(registry, plan["signing_board"], "BitcoinSigningBoard")
    block = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    btc_manifest, public_key = _review_wallet(manifest, registry, board, plan, block)
    profile = {"address_signing": True} if plan["version"] in ("3", "4") else {}
    if discover_registry(btc_manifest, public_key, registry, board_address, **profile) != plan["batch_registry"]:
        raise ValueError("BTC_REGISTRY_NOT_TRUSTED")
    options = board_options(btc_manifest, registry, board_address)
    reviewed = inspect_registry(btc_manifest, public_key, registry, plan["batch_registry"], **options, **profile)
    graph = reviewed["graph"]
    _network_binding(graph, plan)
    reviewed_block = confirmed_block(registry.w3, graph)
    if reviewed_block.hash != block.hash:
        reviewed_manifest, reviewed_key = _review_wallet(manifest, registry, board, plan, reviewed_block)
        if reviewed_manifest != btc_manifest or reviewed_key != public_key:
            raise ValueError("BTC_WALLET_RESULT_MISMATCH")
    block = reviewed_block
    prove_creation(registry.w3, board.address, "BitcoinSigningBoard", [
        plan["batch_registry"], board.functions.maxPacketBytes().call(block_identifier=block.number),
    ], block.number)
    bitcoin = None
    if is_public_network(graph_network(graph)) or plan["version"] in ("3", "4"):
        bitcoin = bitcoin_for_graph(graph, bitcoin_rpc, bitcoin_cookie)
        check_bitcoin_graph(registry, graph, bitcoin)
    if (graph["evm_dkg_id"] != manifest.session_id.hex() or graph["evm_result"] != key["result_hash"]
            or intent.get("coordinator") != graph["contracts"]["coordinator"]["address"]
            or intent.get("chain_id") != str(manifest.chain_id)):
        raise ValueError("BTC_AUTHORIZATION_GRAPH_MISMATCH")
    contracts = {name: _contract(registry, row["address"], row["artifact"])
                 for name, row in graph["contracts"].items()}
    pair = (intent.get("artifact"), intent.get("hash_method"))
    operations = {
        ("BitcoinBatchRegistry", "operationHash"): "reserve",
        ("BitcoinWithdrawalEscrow", "getBindOperationHash"): "bind",
        ("BitcoinBatchRegistry", "getReplacementOperationHash"): "reserve",
        ("BitcoinWithdrawalEscrow", "getBindReplacementOperationHash"): "bind",
        ("BitcoinWithdrawalEscrowV2", "getBindUserFundedOperationHash"): "bind",
        ("BitcoinWithdrawalEscrowV2", "getBindUserFundedReplacementOperationHash"): "bind",
    }
    if pair not in operations:
        raise ValueError("BTC_OPERATION_INVALID")
    target, helper, args, operation_hash = _operation(btc_manifest, contracts, plan, operations[pair], block.number)
    if (intent.get("target") != target.address or intent.get("hash_args") != Bootstrap.wire(args)
            or intent.get("operation_hash", "").removeprefix("0x") != operation_hash.hex()):
        raise ValueError("BTC_PUBLIC_PLAN_OPERATION_MISMATCH")
    _terms(contracts, plan, bitcoin, block.number)
    from .btc_fee_reserve import guard as fee_guard
    fee_guard(btc_manifest,plan,registry,graph,bitcoin=bitcoin,reserved=False)
    if plan["version"] in ("3", "4"):
        from .bitcoin_input_keys import guard
        guard(btc_manifest, public_key, plan, registry, graph)
    if int(plan["deadline"]) <= registry.w3.eth.get_block("latest").timestamp:
        raise ValueError("BATCH_EXPIRED")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_OPERATION_REORG")
    return dict(status="BTC_PUBLIC_PLAN_VERIFIED", plan_hash=onchain_plan_hash(btc_manifest, plan).hex())


def _prepare(a, manifest, key, registry, state, board):
    from .bitcoin_onchain import remember_plan

    graph, contracts = _graph(manifest, key, registry, state, board)
    bitcoin = _bitcoin(a, graph)
    check_bitcoin_graph(registry, graph, bitcoin)
    batch = getattr(a, "batch", "").removeprefix("0x")
    if hex_value(batch, 32) == bytes(32):
        raise ValueError("BTC_PLAN_ZERO_ID")
    deadline = decimal(str(a.deadline), positive=True)
    if deadline <= registry.w3.eth.get_block("latest").timestamp:
        raise ValueError("BATCH_EXPIRED")
    fee = decimal(str(a.fee), MAX_MONEY)
    max_fee = decimal(str(a.max_fee), MAX_MONEY)
    if fee > max_fee:
        raise ValueError("BTC_PLAN_FEE")
    wallet_script = "5120" + key["result"]["group"][2:]
    outpoints = getattr(a, "inputs", None)
    if not isinstance(outpoints, list) or not 1 <= len(outpoints) <= 16 or len(set(outpoints)) != len(outpoints):
        raise ValueError("BTC_PLAN_INPUTS")
    address_mode = graph.get("usage") == "BTC_INPUT_KEYS_V1"
    user_funded = _escrow_v2(contracts["escrow"])
    previous = hex_value((getattr(a, "previous", None) or "0" * 64).removeprefix("0x"), 32)
    if previous != bytes(32) and not user_funded:
        raise ValueError("BTC_WITHDRAWAL_FEE_PROFILE")
    root = bytes(contracts["batches"].functions.reservationRoot(previous).call()) if previous != bytes(32) else None
    inputs = [_unspent(bitcoin, item, None if address_mode else wallet_script,
                       **({"include_mempool": False} if root else {})) for item in outpoints]
    refs = [item.removeprefix("0x") for item in (getattr(a, "withdrawals", None) or [])]
    due = _requests(contracts, refs, wallet_script, root=root, fee=fee if user_funded else None)
    outputs = [dict(script=destination, amount=str(amount)) for destination, amount in due.items()]
    change = sum(int(item["amount"]) for item in inputs) - sum(due.values()) - fee
    if change < 0:
        raise ValueError("INSUFFICIENT_BITCOIN_INPUTS")
    if change:
        if change < int(contracts["escrow"].functions.minP2TRSats().call()):
            raise ValueError("BTC_CHANGE_BELOW_MINIMUM")
        outputs.append(dict(script=wallet_script, amount=str(change)))
    plan = dict(
        version="2", network=graph_network(graph), batch_registry=contracts["batches"].address,
        batch_id=batch, signing_board=board.address, wallet_result=key["result_hash"],
        wallet_script=wallet_script, deadline=str(deadline), inputs=inputs, outputs=outputs,
        fee=str(fee), max_fee=str(max_fee), withdrawal_refs=refs,
        signers=[str(item) for item in (getattr(a, "signers", None) or [])],
    )
    if user_funded:
        plan.update(version="5", previous=previous.hex())
    if address_mode:
        from .bitcoin_input_keys import prepare
        prepare(manifest, key, registry, graph, plan, confirmed_block(registry.w3, graph).number)
    _validate_binding(manifest, key, board, plan)
    _network_binding(graph, plan)
    _terms(contracts, plan, bitcoin)
    if address_mode:
        from .bitcoin_input_keys import guard
        guard(manifest, key, plan, registry, graph)
    existing = contracts["batches"].functions.batchState(bytes.fromhex(batch)).call()
    if existing:
        _batch_matches(manifest, contracts, plan)
    remember_plan(manifest, state, plan)
    return dict(status="BTC_PLAN_PREPARED", plan=plan, plan_hash=onchain_plan_hash(manifest, plan).hex())


def _batch_matches(manifest, contracts, plan, block=None):
    batches = contracts["batches"]
    batch = bytes.fromhex(plan["batch_id"])
    entry = _read(batches.functions.batches(batch), block)
    if not entry[3]:
        return False
    previous = _previous(plan)
    expected_root=bytes(_read(batches.functions.reservationRoot(previous),block)) if previous!=bytes(32) else batch
    if (int(entry[3]) not in (1, 2) or bytes(entry[0]) != onchain_plan_hash(manifest, plan)
            or bytes(entry[1]).hex() != plan["wallet_result"] or int(entry[2]) != int(plan["deadline"])
            or [bytes(item) for item in _read(batches.functions.getInputs(batch), block)]
            != [outpoint_hash(item["outpoint"]) for item in plan["inputs"]]
            or bytes(_read(batches.functions.reservationRoot(batch), block)) != expected_root
            or bytes(_read(batches.functions.replacementOf(batch), block)) != previous):
        raise ValueError("BATCH_APPROVAL_MISMATCH")
    return True


def _payment_matches(contracts, plan, block=None):
    payment = _read(contracts["escrow"].functions.payment(bytes.fromhex(plan["batch_id"])), block)
    if bytes(payment[0]) == bytes(32):
        return False
    if (bytes(payment[0]) != _txid(plan)
            or [bytes(item).hex() for item in payment[3]] != plan["withdrawal_refs"]):
        raise ValueError("ESCROW_PAYMENT_MISMATCH")
    return True


def _operation(manifest, contracts, plan, operation, block=None):
    batch = bytes.fromhex(plan["batch_id"])
    previous = _previous(plan)
    if operation == "reserve":
        name, method = "batches", "operationHash"
        args = [batch, onchain_plan_hash(manifest, plan),
                [outpoint_hash(item["outpoint"]) for item in plan["inputs"]], int(plan["deadline"])]
        if previous!=bytes(32):method='getReplacementOperationHash';args=[previous,batch,onchain_plan_hash(manifest,plan),int(plan['deadline'])]
    elif operation == "bind":
        if not _batch_matches(manifest, contracts, plan, block):
            raise ValueError("BATCH_NOT_RESERVED")
        name, method = "escrow", "getBindOperationHash"
        args = [batch, _txid(plan), [bytes.fromhex(item) for item in plan["withdrawal_refs"]]]
        if plan["version"] == "5":
            method = "getBindUserFundedOperationHash"
        if previous != bytes(32):
            method = "getBindUserFundedReplacementOperationHash" if plan["version"] == "5" else "getBindReplacementOperationHash"
            args = [previous, batch, _txid(plan)]
    else:
        raise ValueError("BTC_OPERATION_INVALID")
    target = contracts[name]
    return target, method, args, bytes(_read(getattr(target.functions, method)(*args), block))


def _completed(manifest, contracts, plan, operation):
    if operation not in ("reserve", "bind"):
        raise ValueError("BTC_OPERATION_INVALID")
    reserved = _batch_matches(manifest, contracts, plan)
    return reserved if operation == "reserve" else reserved and _payment_matches(contracts, plan)


def _auth_board(a, manifest, registry, graph):
    from .onchain_transport import invitation
    from .bitcoin_trust import invitation_options, graph_options

    if not getattr(a, "board", None):
        raise ValueError("ONCHAIN_BOARD_REQUIRED")
    transport, invite = invitation(registry.w3, a.board)
    roles = [role for role, session in invite["sessions"].items() if session == graph["evm_dkg_id"]]
    roster = invite["participants"][1:] if "bitcoin_custody" in invite else invite["participants"]
    if (transport.functions.registry().call() != manifest.registry
            or invite["sessions"]["btc_ceremony"] != manifest.session_id.hex()
            or tuple(roster) != manifest.participants
            or len(roles) != 1 or roles[0] == "btc_ceremony"):
        raise ValueError("BTC_BOARD_WALLET_MISMATCH")
    if ("bitcoin_custody" in invite and transport.functions.intentCoordinator().call()
            != graph["contracts"]["coordinator"]["address"]):
        raise ValueError("BTC_BOARD_WALLET_MISMATCH")
    # Legacy graphs omit the regtest/one-confirmation defaults that V6
    # invitations spell out. Compare effective policies without changing pins.
    defaults = dict(bitcoin_network="regtest", evm_confirmations="1")
    if ({**defaults, **invitation_options(invite)}
            != {**defaults, **graph_options(graph)}):
        raise ValueError("BTC_BOARD_WALLET_MISMATCH")
    return transport, roles[0]


def _cache_key(manifest, plan, operation):
    return "btc-payment-operation:" + manifest.context.hex() + ":" + plan["batch_id"] + ":" + operation


def _check_cached(registry, contracts, graph, cached, expected_intent, *, require_available=True):
    if cached["intent"] != dict(expected_intent, session=cached["session"]):
        raise ValueError("BTC_OPERATION_CHANGED")
    anchor = cached["anchor"]
    if registry.w3.eth.get_block(int(anchor["number"])).hash.hex() != anchor["hash"]:
        raise ValueError("BTC_OPERATION_REORG")
    session = int(cached["session"])
    coordinator = contracts["coordinator"]
    info = coordinator.functions.getSession(session).call()
    if (int(info[0]) != session or bytes(info[3]).hex() != expected_intent["operation_hash"]
            or not info[4] or info[15] != expected_intent["target"]
            or bytes(coordinator.functions.sessionDKGId(session).call()).hex() != graph["evm_dkg_id"]
            or bytes(coordinator.functions.sessionDKGResult(session).call()).hex() != graph["evm_result"]):
        raise ValueError("BTC_OPERATION_SESSION_MISMATCH")
    if require_available and int(info[10]) not in (1, 2):
        raise ValueError("BTC_OPERATION_SESSION_UNAVAILABLE")
    return session


def _intent(manifest, graph, role, target, helper, args, operation_hash, plan):
    return dict(
        chain_id=str(manifest.chain_id), coordinator=graph["contracts"]["coordinator"]["address"],
        role=role, target=target.address, operation_hash=operation_hash.hex(),
        artifact="BitcoinBatchRegistry" if helper in ("operationHash","getReplacementOperationHash")
            else graph["contracts"]["escrow"]["artifact"],
        hash_method=helper, hash_args=Bootstrap.wire(args), bitcoin_plan=validate_plan(plan, manifest),
    )


def _find_session(registry, transport, expected):
    """A second participant can execute by discovering the already published intent."""
    import json

    from .board_intents import intent_rows
    latest = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    matches = [row for row in intent_rows(registry.w3, transport, latest)
               if row["intent"] == dict(expected, session=row["session"])]
    if len(matches) != 1:
        raise ValueError("BTC_OPERATION_PROPOSAL_REQUIRED")
    return matches[0]


def _authorize(action, a, manifest, key, registry, state, board, plan):
    from .bitcoin_onchain import send

    graph, contracts = _graph(manifest, key, registry, state, board)
    _validate_binding(manifest, key, board, plan)
    _network_binding(graph, plan)
    operation = a.operation
    if _completed(manifest, contracts, plan, operation):
        return dict(status="BTC_OPERATION_EXECUTED", operation=operation, batch=plan["batch_id"])
    if int(plan["deadline"]) <= registry.w3.eth.get_block("latest").timestamp:
        raise ValueError("BATCH_EXPIRED")
    bitcoin = _bitcoin(a, graph)
    check_bitcoin_graph(registry, graph, bitcoin)
    _terms(contracts, plan, bitcoin)
    from .bitcoin_input_keys import guard
    guard(manifest, key, plan, registry, graph)
    from .btc_fee_reserve import guard as fee_guard
    fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state,reserved=False)
    transport, role = _auth_board(a, manifest, registry, graph)
    if action == "btc-propose" and (not registry.account or
            transport.functions.operator().call() != registry.account.address
            and not transport.functions.canPublishIntent(registry.account.address).call()):
        raise ValueError("BTC_PROPOSAL_REQUIRES_BOARD_OPERATOR")
    target, helper, args, operation_hash = _operation(manifest, contracts, plan, operation)
    expected = _intent(manifest, graph, role, target, helper, args, operation_hash, plan)
    cache_key = _cache_key(manifest, plan, operation)
    with state.exclusive():
        cached = state.get(cache_key)
        if cached is None and action == "btc-propose":
            coordinator = contracts["coordinator"]
            receipt = send(registry, coordinator.address, "FROSTCoordinator", "createSessionFromDKG", [
                bytes.fromhex(graph["evm_dkg_id"]), operation_hash, target.address, int(plan["deadline"]),
            ], graph=graph)
            events = coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
            if len(events) != 1:
                raise ValueError("BTC_OPERATION_RECEIPT_INVALID")
            session = str(events[0]["args"]["sessionId"])
            cached = dict(session=session, intent=dict(expected, session=session), anchor=dict(
                number=str(receipt["blockNumber"]), hash=receipt["blockHash"].hex()))
            # Persist before publishing so interruption cannot manufacture another signing session.
            state.put(cache_key, cached)
        elif cached is None:
            cached = _find_session(registry, transport, expected)
            state.put(cache_key, cached)
        session = _check_cached(registry, contracts, graph, cached, expected)
        public = canonical(cached["intent"]).encode()
        if len(public) > (131072 if plan.get('version')=='4' else 32768):
            raise ValueError("BTC_INTENT_TOO_LARGE")
        existing = bytes(transport.functions.intents(session).call())
        if existing and existing != public:
            raise ValueError("BTC_OPERATION_INTENT_CONFLICT")
        if action == "btc-propose":
            if not existing:
                if plan.get('version')=='4':
                    from .withdrawal_fee_transport import publish_intent
                    def publish(method,args,part):
                        guard(manifest,key,plan,registry,graph)
                        fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state,reserved=False)
                        return send(registry,transport.address,'DKGOnchainBoard',method,args,graph=graph)
                    publish_intent(transport,session,public,publish)
                else:send(registry, transport.address, "DKGOnchainBoard", "publishIntent", [session, public], graph=graph)
            return dict(status="AWAITING_PARTICIPANTS", operation=operation, session=str(session),
                        operation_hash=operation_hash.hex(), role=role, intent=cached["intent"])
        if existing != public:
            raise ValueError("BTC_OPERATION_INTENT_REQUIRED")
        authorization = contracts["authorization"]
        if not authorization.functions.isOperationApproved(operation_hash, target.address).call():
            finalized = contracts["coordinator"].functions.getAuthorizationResult(session).call()
            if bytes(finalized[1]) != operation_hash or finalized[4] != target.address:
                raise ValueError("BTC_OPERATION_SESSION_MISMATCH")
            send(registry, authorization.address, "DKGAuthorization", "authorizeOperation", [
                session, operation_hash, target.address,
            ], graph=graph)
        kind = "BitcoinBatchRegistry" if operation == "reserve" else graph["contracts"]["escrow"]["artifact"]
        guard(manifest, key, plan, registry, graph)
        fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state,reserved=False)
        apply_method = {"getReplacementOperationHash": "reserveReplacement",
            "getBindReplacementOperationHash": "bindReplacement",
            "getBindUserFundedOperationHash": "bindUserFunded",
            "getBindUserFundedReplacementOperationHash": "bindUserFundedReplacement"}.get(helper, operation)
        if operation == "bind" and plan["version"] == "5":
            index = 1 if _previous(plan) == bytes(32) else 2
            args[index] = bytes.fromhex(_build(plan)["unsigned"])
        send(registry, target.address, kind, apply_method, [*args, operation_hash], graph=graph)
        if not _completed(manifest, contracts, plan, operation):
            raise ValueError("BTC_OPERATION_POSTCONDITION")
        return dict(status="BTC_OPERATION_EXECUTED", operation=operation, batch=plan["batch_id"], session=str(session))


def _settle(a, manifest, key, registry, state, board, plan):
    from .bitcoin_onchain import send
    from .bitcoin_trust import confirmed_block

    graph, contracts = _graph(manifest, key, registry, state, board)
    block = confirmed_block(registry.w3, graph)
    _validate_binding(manifest, key, board, plan, block.number)
    _network_binding(graph, plan)
    if not _batch_matches(manifest, contracts, plan, block.number) or not _payment_matches(contracts, plan, block.number):
        raise ValueError("ESCROW_PAYMENT_MISMATCH")
    batch = bytes.fromhex(plan["batch_id"])
    escrow = contracts["escrow"]
    wire_txid = _txid(plan)
    txid = wire_txid[::-1].hex()
    paid = escrow.functions.payment(batch).call(block_identifier=block.number)[2]
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_OPERATION_REORG")
    if paid:
        return dict(status="BTC_PAYMENT_SETTLED", batch=plan["batch_id"], txid=txid)
    if contracts["batches"].functions.batchState(batch).call(block_identifier=block.number) != 2:
        raise ValueError("BATCH_NOT_SIGNING")
    bitcoin = _bitcoin(a, graph)
    proof = inclusion(bitcoin, txid)
    depth = int(escrow.functions.depth().call(block_identifier=block.number))
    if int(proof["confirmations"]) < depth:
        raise ValueError("PAYMENT_DEPTH")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_OPERATION_REORG")
    relay = contracts["relay"]
    from .zk_spv import is_zk
    if is_zk(graph.get("bitcoin_policy")):
        if getattr(a,"zk_worker_config",None) or getattr(a,"zk_transport_config",None):
            need=dict(kind="withdrawal",consumer=escrow.address,txid=txid,batch=plan["batch_id"])
            if getattr(a,"zk_transport_config",None):
                if getattr(a,"zk_worker_config",None):raise ValueError("ZK_REQUEST_ROUTE_CONFLICT")
                from .zk_worker_transport import request_from_consumer
                readiness=request_from_consumer(a.zk_transport_config,graph,need,bitcoin,a.zk_request_federation,a.zk_service_recipient)
            else:
                from .zk_worker import request_from_consumer
                readiness=request_from_consumer(a.zk_worker_config,graph,need)
            if readiness["status"] != "READY":
                return dict(status="AWAITING_ZK_STATE", batch=plan["batch_id"], spv=readiness)
        check_bitcoin_graph(registry, graph, bitcoin)
        spv = contracts["spv_guard"]
    elif is_public_network(graph_network(graph)):
        from .bitcoin_network import sync_relay

        def send_header(raw):
            try:
                return send(registry, relay.address, relay_artifact(graph_network(graph)), "addBlockHeader",
                            [bytes.fromhex(raw)], graph=graph)
            except ValueError as error:
                if str(error) != "BTC_TRANSACTION_UNCONFIRMED":
                    raise
                return dict(status="PENDING")

        from .relay_batch import DurableRelayBatch
        send_headers = DurableRelayBatch.for_rpc(registry.identity_file, registry.w3, relay.address,
                                                 graph["bitcoin_policy"], int(graph["evm_confirmations"]))
        synced = sync_relay(bitcoin, relay, graph["bitcoin_policy"], send_header,
                            send_headers=send_headers, evm_confirmations=int(graph["evm_confirmations"]))
        if synced["status"] != "MAINNET_RELAY_READY":
            return dict(status="AWAITING_BITCOIN_RELAY", batch=plan["batch_id"], bitcoin=synced)
        check_bitcoin_graph(registry, graph, bitcoin)
        spv = contracts["spv_guard"]
    else:
        head = bytes(relay.functions.getMainchainHead().call())
        height = int(bitcoin.call("getblockcount"))
        if head == bytes(32):
            start = 0
        else:
            relayed = int(relay.functions.getMainchainHeight().call())
            if relayed > height or bytes.fromhex(bitcoin.call("getblockhash", relayed))[::-1] != head:
                raise ValueError("BTC_RELAY_FORK_REQUIRES_REVIEW")
            start = relayed + 1
        for number in range(start, height + 1):
            block_hash = bitcoin.call("getblockhash", number)
            raw_header = bytes.fromhex(bitcoin.call("getblockheader", block_hash, False))
            send(registry, relay.address, "RegtestHeaderRelay", "addBlockHeader", [raw_header], graph=graph)
        spv = relay
    block_hash = bytes.fromhex(proof["block_hash"])
    block = confirmed_block(registry.w3, graph)
    status = spv.functions.getBlockStatus(block_hash).call(block_identifier=block.number)
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_OPERATION_REORG")
    if not status[0] or int(status[1]) < depth:
        if is_zk(graph.get("bitcoin_policy")) and not status[0]:
            return dict(status="AWAITING_ZK_HEADER_MATERIALIZATION", batch=plan["batch_id"],
                        block_hash=proof["block_hash"])
        raise ValueError("PAYMENT_DEPTH")
    # Refresh Core's canonical-chain proof after potentially lengthy relay synchronization.
    fresh = inclusion(bitcoin, txid)
    if fresh != proof:
        if (fresh["block_hash"] != proof["block_hash"] or fresh["raw"] != proof["raw"]
                or fresh["siblings"] != proof["siblings"] or fresh["index"] != proof["index"]
                or int(fresh["confirmations"]) < depth):
            raise ValueError("BITCOIN_PAYMENT_REORG")
    kind = "BitcoinWithdrawalEscrowV2" if plan.get("version") == "5" else "BitcoinWithdrawalEscrow"
    send(registry, escrow.address, kind, "settle", [
        batch, bytes.fromhex(proof["raw"]), block_hash,
        [bytes.fromhex(item) for item in proof["siblings"]], int(proof["index"]),
    ], graph=graph)
    block = confirmed_block(registry.w3, graph)
    payment = escrow.functions.payment(batch).call(block_identifier=block.number)
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_OPERATION_REORG")
    if not payment[2] or bytes(payment[0]) != wire_txid:
        raise ValueError("BTC_SETTLEMENT_POSTCONDITION")
    return dict(status="BTC_PAYMENT_SETTLED", batch=plan["batch_id"], txid=txid)


def dispatch(action, a, manifest, key, registry, state, board, plan=None):
    if manifest.purpose != "BTC_SPEND":
        raise ValueError("WRONG_KEY_PURPOSE")
    if action == "btc-prepare":
        return _prepare(a, manifest, key, registry, state, board)
    if plan is None:
        from .bitcoin_onchain import cached_plan

        plan = cached_plan(manifest, state, getattr(a, "batch", None))
    if action in ("btc-propose", "btc-execute"):
        return _authorize(action, a, manifest, key, registry, state, board, plan)
    if action == "btc-settle":
        return _settle(a, manifest, key, registry, state, board, plan)
    raise ValueError("BTC_PAYMENT_ACTION_INVALID")
