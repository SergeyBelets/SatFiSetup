"""Proof mint and treasury requests through the enrolled public approval board.

All values published here are public financial terms. FROST secrets stay in the
existing participant signer, and submitted transactions use its durable journal.
"""
from eth_abi import encode
from eth_utils import keccak
import re
from web3 import Web3
from web3.logs import DISCARD

from .bootstrap import Bootstrap
from .bitcoin_onchain_plan import decimal, hex_value, script, MAX_MONEY
from .bitcoin_onchain_payment import (_graph, _bitcoin, _auth_board, _check_cached,
                                      _find_session, _contract)
from .bitcoin_proofs import inclusion
from .bitcoin_rpc import satoshis
from .bitcoin_trust import check_bitcoin_graph, checked_runtime, confirmed_block
from .contract_provenance import prove_creation, birth_block
from .operations import canonical
from .onchain_transport import can_publish_intent
from .safe_errors import DiagnosticArgumentParser


def deposit_commitment(chain_id, mining_pool, vout, recipients, amounts):
    """Pure public encoding, usable before sending BTC and without local key files."""
    if not Web3.is_checksum_address(mining_pool) or int(mining_pool, 16) == 0:
        raise ValueError("BTC_MINT_POOL_BINDING")
    chain = decimal(str(chain_id), positive=True)
    output = decimal(str(vout), 2**32 - 1)
    from types import SimpleNamespace
    addresses, values = _distribution(SimpleNamespace(recipients=recipients, amounts=amounts))
    if len(addresses) != 1:
        raise ValueError("BTC_MINT_SINGLE_RECIPIENT")
    digest = keccak(encode(["bytes32", "uint256", "address", "uint32", "address[]", "uint256[]"],
        [keccak(text="SATOHSI_DEPOSIT_ALLOCATION_V1"), chain, mining_pool, output, addresses, values])).hex()
    data = "534d5031" + addresses[0][2:].lower() + digest
    return dict(status="BTC_DEPOSIT_COMMITMENT", data=data, script="6a38" + data, allocation_hash=digest,
                chain_id=str(chain), mining_pool=mining_pool, vout=str(output),
                recipients=addresses, amounts=[str(item) for item in values])


def commitment_main(argv):
    import json
    parser = DiagnosticArgumentParser(prog="frost-signer bitcoin deposit-commitment")
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--mining-pool", required=True)
    parser.add_argument("--vout", required=True, help="wallet deposit output index, not the OP_RETURN index")
    parser.add_argument("--recipient", dest="recipients", required=True, action="append")
    parser.add_argument("--amount", dest="amounts", required=True, action="append", help="MP base units; 10^10 per satoshi")
    args = parser.parse_args(argv)
    print(json.dumps(deposit_commitment(args.chain_id, args.mining_pool, args.vout, args.recipients, args.amounts), sort_keys=True))
    return 0


def _distribution(a):
    recipients, amounts = getattr(a, "recipients", None), getattr(a, "amounts", None)
    if (not isinstance(recipients, list) or not isinstance(amounts, list)
            or not 1 <= len(recipients) <= 64 or len(recipients) != len(amounts)):
        raise ValueError("BTC_MINT_DISTRIBUTION")
    if any(not Web3.is_checksum_address(item) or int(item, 16) == 0 for item in recipients):
        raise ValueError("BTC_MINT_RECIPIENT")
    if any(not isinstance(item, str) or not re.fullmatch(r"[1-9][0-9]{0,25}", item)
           or int(item) > MAX_MONEY * 10**10 for item in amounts):
        raise ValueError("BTC_MINT_AMOUNT")
    values = [int(item) for item in amounts]
    return list(recipients), values


def _mining(registry, contracts, wallet_script, block):
    address = contracts["deposits"].functions.scriptPools(keccak(bytes.fromhex(wallet_script))).call(block_identifier=block.number)
    kind = "MiningPoolV1"
    try:
        checked_runtime(registry.w3, address, kind, block.number)
    except ValueError as error:
        if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
            raise
        kind = "ConfederationBitcoinReserve"
        checked_runtime(registry.w3, address, kind, block.number)
    mining = _contract(registry, address, kind)
    read = lambda name: getattr(mining.functions, name)().call(block_identifier=block.number)
    if (read("deposits") != contracts["deposits"].address or read("escrow") != contracts["escrow"].address
            or read("authorization") != contracts["authorization"].address
            or bytes(read("payoutScript")).hex() != wallet_script):
        raise ValueError("BTC_MINT_POOL_BINDING")
    if kind == "ConfederationBitcoinReserve":
        prove_creation(registry.w3, address, kind, [read("poolId"), contracts["deposits"].address,
            contracts["escrow"].address, read("confederationPool")], block.number)
        return mining
    token = _contract(registry, read("mpToken"), "PoolMpToken")
    prove_creation(registry.w3, address, "MiningPoolV1", [read("poolId"),
        contracts["deposits"].address, contracts["escrow"].address,
        token.functions.name().call(block_identifier=block.number),
        token.functions.symbol().call(block_identifier=block.number)], block.number)
    return mining


def _mint_args(a):
    wire = hex_value(a.txid.removeprefix("0x"), 32)[::-1]
    vout = decimal(str(a.vout), 2**32 - 1)
    recipients, amounts = _distribution(a)
    return [wire, vout, recipients, amounts]


def _mint_complete(mining, args, block):
    key = keccak(encode(["bytes32", "uint32"], args[:2]))
    allocation = bytes(mining.functions.mintedAllocations(key).call(block_identifier=block.number))
    if allocation == bytes(32):
        return False
    if allocation != keccak(encode(["address[]", "uint256[]"], args[2:])):
        raise ValueError("BTC_MINT_ALLOCATION_MISMATCH")
    return True


def _has_deposit_marker(script_hex):
    script = bytes.fromhex(script_hex)
    if len(script) < 2 or script[0] != 0x6a:
        return False
    opcode = script[1]
    offset = 2 if 1 <= opcode <= 75 else {0x4c: 3, 0x4d: 4, 0x4e: 6}.get(opcode)
    return offset is not None and script[offset:offset + 4] == b"SMP1"


def _deposit_allocation(target, args, transaction):
    coinbase = len(transaction["vin"]) == 1 and "coinbase" in transaction["vin"][0]
    if len(transaction["vout"]) > 32:
        raise ValueError("BTC_MINT_DEPOSIT_OUTPUTS")
    marked = any(_has_deposit_marker(item["scriptPubKey"]["hex"]) for item in transaction["vout"])
    if not coinbase or marked:
        commitment = deposit_commitment(target.w3.eth.chain_id, target.address, args[1], args[2], [str(value) for value in args[3]])
        if not any(item["scriptPubKey"]["hex"] == commitment["script"]
                and satoshis(item["value"]) == 0 for item in transaction["vout"]):
            raise ValueError("BTC_MINT_DEPOSIT_COMMITMENT")
    return coinbase


def _mint_proof(a, bitcoin, contracts, target, args, block):
    proof = inclusion(bitcoin, a.txid.removeprefix("0x"))
    transaction = bitcoin.call("getrawtransaction", a.txid.removeprefix("0x"), True)
    try:
        output = next(item for item in transaction["vout"] if int(item["n"]) == args[1])
    except StopIteration:
        raise ValueError("BTC_MINT_OUTPUT") from None
    wallet_script = bytes(target.functions.payoutScript().call(block_identifier=block.number)).hex()
    coinbase = _deposit_allocation(target, args, transaction)
    utxo = bitcoin.call("gettxout", a.txid.removeprefix("0x"), args[1], True)
    if not utxo or utxo["scriptPubKey"]["hex"] != wallet_script or satoshis(utxo["value"]) != satoshis(output["value"]):
        raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    # Match BitcoinDepositRegistry.consume exactly. Coinbase mint maturity is
    # fixed at 100 even when the ordinary-deposit depth is configured above it.
    # Spendability of coinbase inputs remains a separate 101-confirmation rule.
    depth = 100 if coinbase else contracts["deposits"].functions.depth().call(block_identifier=block.number)
    spv = contracts.get("spv_guard", contracts["relay"])
    canonical_block, confirmations = spv.functions.getBlockStatus(bytes.fromhex(proof["block_hash"])).call(block_identifier=block.number)
    if (not canonical_block or confirmations < depth or int(proof["confirmations"]) < depth
            or output["scriptPubKey"]["hex"] != wallet_script
            or sum(args[3]) != satoshis(output["value"]) * 10**10):
        raise ValueError("BTC_MINT_PROOF_OR_ALLOCATION")
    if not spv.functions.checkTxInclusion(bytes.fromhex(proof["block_hash"]), args[0],
            [bytes.fromhex(item) for item in proof["siblings"]], int(proof["index"])).call(block_identifier=block.number):
        raise ValueError("BTC_MINT_PROOF_OR_ALLOCATION")
    outpoint = keccak(encode(["bytes32", "uint32"], args[:2]))
    if (contracts["deposits"].functions.consumed(outpoint).call(block_identifier=block.number)
            or contracts["deposits"].functions.walletOutput(outpoint).call(block_identifier=block.number)):
        raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    display = keccak(encode(["bytes32", "uint32"], [args[0][::-1], args[1]]))
    if bytes(contracts["batches"].functions.reservations(display).call(block_identifier=block.number)) != bytes(32):
        raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    return [bytes.fromhex(proof["raw"]), args[1], bytes.fromhex(proof["block_hash"]),
            [bytes.fromhex(item) for item in proof["siblings"]], int(proof["index"]), *args[2:]]


def _treasury_complete(target, args, block):
    ref = target.functions.requestKey(target.address, args[0]).call(block_identifier=block.number)
    request = target.functions.requests(ref).call(block_identifier=block.number)
    if not int(request[-1]):
        return False, bytes(ref).hex()
    if (int(request[0], 16) != 0 or int(request[2], 16) != 0 or request[1] != args[0]
            or request[3] != args[1] or request[4] != args[2] * 10**10 or bytes(request[5]) != args[3]):
        raise ValueError("BTC_TREASURY_REQUEST_MISMATCH")
    if int(request[-1]) == 4:
        raise ValueError("BTC_TREASURY_REQUEST_CANCELLED")
    return True, bytes(ref).hex()


def _mint_session_deadline(registry, contracts, graph, cached, expected, *, require_available=True):
    """Recover the immutable creation deadline, including for old public intents.

    getSession's deadline can be extended by the coordinator's creator/admin.
    Neither that mutable value nor a board discovery block is creation evidence.
    Locate the session's birth and authenticate its original event before local
    attempt state may adopt it. Historical RPC failures remain explicit failures.
    """
    session = _check_cached(registry, contracts, graph, cached, expected, require_available=require_available)
    coordinator = contracts["coordinator"]
    upper = int(cached["anchor"]["number"])
    lower = birth_block(registry.w3, coordinator.address, upper)
    if int(coordinator.functions.getSession(session).call(block_identifier=upper)[0]) != session:
        raise ValueError("BTC_OPERATION_SESSION_MISMATCH")
    while lower < upper:
        middle = (lower + upper) // 2
        exists = int(coordinator.functions.getSession(session).call(block_identifier=middle)[0]) == session
        if exists:
            upper = middle
        else:
            lower = middle + 1
    anchor = registry.w3.eth.get_block(lower)
    events = coordinator.events.SessionCreated().get_logs(from_block=lower, to_block=lower,
        argument_filters={"sessionId": session})
    if (len(events) != 1 or int(events[0]["args"]["sessionId"]) != session
            or bytes(events[0]["blockHash"]) != bytes(anchor.hash)):
        raise ValueError("BTC_MINT_SESSION_CREATION_REQUIRED")
    deadline = int(events[0]["args"]["deadline"])
    if ((require_available and int(coordinator.functions.getSession(session).call()[7]) != deadline)
            or registry.w3.eth.get_block(lower).hash != anchor.hash):
        raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
    return deadline


def consumer_readiness(a,graph,bitcoin,target,args):
    if not (getattr(a,'zk_worker_config',None) or getattr(a,'zk_transport_config',None)):return None
    if getattr(a,'zk_worker_config',None) and getattr(a,'zk_transport_config',None):raise ValueError('ZK_REQUEST_ROUTE_CONFLICT')
    _deposit_allocation(target,args,bitcoin.call('getrawtransaction',a.txid.removeprefix('0x'),True))
    need=dict(kind='deposit',consumer=target.address,txid=a.txid.removeprefix('0x'),vout=str(args[1]))
    if getattr(a,'zk_transport_config',None):
        from .zk_worker_transport import request_from_consumer
        return request_from_consumer(a.zk_transport_config,graph,need,bitcoin,a.zk_request_federation,a.zk_service_recipient)
    from .zk_worker import request_from_consumer
    return request_from_consumer(a.zk_worker_config,graph,need)


def dispatch(a, manifest, key, registry, state, board):
    from .bitcoin_onchain import send, snapshot_check

    graph, contracts = _graph(manifest, key, registry, state, board)
    block = confirmed_block(registry.w3, graph)
    wallet_script = "5120" + key["result"]["group"][2:]
    mint = a.action.startswith("btc-mint-")
    propose = a.action.endswith("-propose")
    withdrawal_ref = None
    if mint:
        target = _mining(registry, contracts, wallet_script, block)
        args = _mint_args(a)
        from .bitcoin_issuer import kind as issuer_kind
        helper, method, kind = "getMintOperationHash", "mintFromProof", issuer_kind(target)
        completed = _mint_complete(target, args, block)
        operation = "mint"
    else:
        target = contracts["escrow"]
        if not registry.account:
            raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
        destination = script(a.destination_script.removeprefix("0x"))
        if destination == wallet_script:
            raise ValueError("BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED")
        owner = getattr(a, "treasury_owner", None) or registry.account.address
        if not Web3.is_checksum_address(owner) or int(owner, 16) == 0:
            raise ValueError("BTC_TREASURY_OWNER")
        args = [decimal(str(a.request_id)), owner, decimal(str(a.satoshis), MAX_MONEY, positive=True), bytes.fromhex(destination)]
        helper, method, kind = "getTreasuryOperationHash", "requestTreasury", "BitcoinWithdrawalEscrow"
        completed, withdrawal_ref = _treasury_complete(target, args, block)
        minimum = (target.functions.minP2WPKHSats() if destination.startswith("0014") else target.functions.minP2TRSats()).call(block_identifier=block.number)
        if args[2] < minimum:
            raise ValueError("BTC_WITHDRAWAL_BELOW_MINIMUM")
        operation = "treasury"
    result = dict(status="BTC_FUNDING_EXECUTED", operation=operation, target=target.address)
    if withdrawal_ref:
        result["withdrawal_ref"] = withdrawal_ref
    if mint and int(target.functions.mintProofVersion().call(block_identifier=block.number)) == 2:
        from .custody_mint import dispatch as custody_mint
        return custody_mint(a,manifest,key,registry,state,graph,contracts,target,args,block)
    if completed:
        snapshot_check(registry.w3, block)
        return result
    if mint and int(target.functions.mintProofVersion().call(block_identifier=block.number)) != 1:
        # Only the two explicit deployed mint profiles are supported.
        raise ValueError("BTC_MINT_GENERATION_PROFILE_REQUIRED")
    bitcoin = _bitcoin(a, graph)
    readiness=consumer_readiness(a,graph,bitcoin,target,args) if mint else None
    if readiness and readiness['status']!='READY':return dict(status='AWAITING_ZK_STATE',spv=readiness)
    check_bitcoin_graph(registry, graph, bitcoin)
    execute_args = _mint_proof(a, bitcoin, contracts, target, args, block) if mint else args
    if mint:
        admission = target.functions.mintAdmission().call(block_identifier=block.number)
        if int(admission, 16) != 0:
            from .mint_admission_client import dispatch_funding
            return dispatch_funding(a, manifest, key, registry, state, graph, contracts,
                target, args, bitcoin, block, admission)
        if a.action == "btc-mint-retry":
            raise ValueError("BTC_MINT_ADMISSION_REQUIRED")
    deadline = decimal(str(a.deadline), positive=True)
    if deadline <= registry.w3.eth.get_block("latest").timestamp:
        raise ValueError("BATCH_EXPIRED")
    transport, role = _auth_board(a, manifest, registry, graph)
    if propose and (not registry.account or not can_publish_intent(transport, registry.account.address, block.number)):
        raise ValueError("BTC_PROPOSAL_REQUIRES_BOARD_OPERATOR")
    operation_hash = bytes(getattr(target.functions, helper)(*args).call(block_identifier=block.number))
    expected = dict(chain_id=str(manifest.chain_id), coordinator=contracts["coordinator"].address,
                    role=role, target=target.address, artifact=kind, hash_method=helper,
                    hash_args=Bootstrap.wire(args), operation_hash=operation_hash.hex())
    snapshot_check(registry.w3, block)
    cache_key = "btc-funding:" + manifest.context.hex() + ":" + keccak(canonical([target.address, helper, Bootstrap.wire(args)]).encode()).hex()
    with state.exclusive():
        if mint:
            from .mint_attempts import select_attempt
            legacy = state.get(cache_key)
            legacy_nonce, legacy_deadline, legacy_verified = None, None, False
            operation_nonce = target.functions.nonce().call(block_identifier=block.number)
            discovered = None
            if legacy is not None and state.get(cache_key + ":attempts-v1") is None:
                old_expected = {name: value for name, value in legacy["intent"].items() if name != "session"}
                legacy_deadline = _mint_session_deadline(registry, contracts, graph, legacy, old_expected,
                    require_available=False)
                legacy_verified = True
                # The old cache saved the session receipt block, not the block
                # at which its hash was computed. Other mints can precede that
                # receipt, including within its block; never infer an old nonce
                # from the receipt. An identical current hash authenticates its
                # nonce; otherwise retain the legacy nonce explicitly unknown.
                if legacy["intent"]["operation_hash"] == expected["operation_hash"]:
                    legacy_nonce = operation_nonce
            if not propose and state.get(cache_key + ":attempts-v1") is None:
                discovered = _find_session(registry, transport, expected)
                if _mint_session_deadline(registry, contracts, graph, discovered, expected) != deadline:
                    raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            cache_key, cached = select_attempt(state, cache_key, expected,
                operation_nonce, deadline, legacy_nonce=legacy_nonce,
                legacy_deadline=legacy_deadline, legacy_verified=legacy_verified)
            # A canonical confirmed observation may already lag a competing
            # legitimate mint. Do not spend gas starting its stale session.
            if bytes(getattr(target.functions, helper)(*args).call()) != operation_hash:
                raise ValueError("BTC_MINT_STALE_CONTRACT_NONCE")
        else:
            cached = state.get(cache_key)
        if cached is None and propose:
            coordinator = contracts["coordinator"]
            receipt = send(registry, coordinator.address, "FROSTCoordinator", "createSessionFromDKG",
                           [bytes.fromhex(graph["evm_dkg_id"]), operation_hash, target.address, deadline], graph=graph)
            events = coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
            if len(events) != 1:
                raise ValueError("BTC_OPERATION_RECEIPT_INVALID")
            session = str(events[0]["args"]["sessionId"])
            cached = dict(session=session, intent=dict(expected, session=session), anchor=dict(
                number=str(receipt["blockNumber"]), hash=receipt["blockHash"].hex()))
            state.put(cache_key, cached)
        elif cached is None:
            cached = discovered if mint and discovered is not None else _find_session(registry, transport, expected)
            if mint and _mint_session_deadline(registry, contracts, graph, cached, expected) != deadline:
                raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            state.put(cache_key, cached)
        if mint and _mint_session_deadline(registry, contracts, graph, cached, expected) != deadline:
            raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
        session = _check_cached(registry, contracts, graph, cached, expected)
        payload = canonical(cached["intent"]).encode()
        existing = bytes(transport.functions.intents(session).call())
        if existing and existing != payload:
            raise ValueError("BTC_OPERATION_INTENT_CONFLICT")
        if propose:
            if not existing:
                send(registry, transport.address, "DKGOnchainBoard", "publishIntent", [session, payload], graph=graph)
            return dict(status="AWAITING_PARTICIPANTS", session=str(session), intent=cached["intent"],
                        role=role, operation=operation, operation_hash=operation_hash.hex())
        if existing != payload:
            raise ValueError("BTC_OPERATION_INTENT_REQUIRED")
        if mint and bytes(getattr(target.functions, helper)(*args).call()) != operation_hash:
            raise ValueError("BTC_MINT_STALE_CONTRACT_NONCE")
        authorization = contracts["authorization"]
        if not authorization.functions.isOperationApproved(operation_hash, target.address).call():
            finalized = contracts["coordinator"].functions.getAuthorizationResult(session).call()
            if bytes(finalized[1]) != operation_hash or finalized[4] != target.address:
                raise ValueError("BTC_OPERATION_SESSION_MISMATCH")
            send(registry, authorization.address, "DKGAuthorization", "authorizeOperation",
                 [session, operation_hash, target.address], graph=graph)
        from .zk_submission import options as proof_submission
        submission = proof_submission(registry, graph, execute_args[2]) if mint else {}
        send(registry, target.address, kind, method, [*execute_args, operation_hash], graph=graph, **submission)
    block = confirmed_block(registry.w3, graph)
    completed = _mint_complete(target, args, block) if mint else _treasury_complete(target, args, block)[0]
    if not completed:
        raise ValueError("BTC_OPERATION_POSTCONDITION")
    snapshot_check(registry.w3, block)
    return dict(result, session=str(session))
