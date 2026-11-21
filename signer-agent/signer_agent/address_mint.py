"""Address-mint views and proofs. No BTC signing, address issuance or Ready claim."""
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from .bootstrap import artifact
from .deposit_intent import DepositIntentV1, PROFILE
from .contract_provenance import prove_creation
from .bitcoin_trust import checked_runtime

ZERO = "0x" + "00" * 20
CLAIM_TYPE = "(bytes32,bytes32,bytes32,uint32,address,uint64,uint256,uint256,bytes32)"


def read(contract, block, name, *args):
    return getattr(contract.functions, name)(*args).call(block_identifier=block)


def registry_identity(w3, address, deposits, authorization, escrow, batches, dkg, number,*,verify_derivation=True):
    """Authenticate immutable graph/provenance; statuses are deliberately not a fingerprint."""
    checked_runtime(w3, address, "BitcoinAddressRegistry", number)
    registry = w3.eth.contract(address=address, abi=artifact("BitcoinAddressRegistry")["abi"])
    r = lambda name: read(registry, number, name)
    if (r("deposits") != deposits or r("authorization") != authorization or r("escrow") != escrow
            or r("batches") != batches or r("dkg") != dkg or int(r("chainId")) != w3.eth.chain_id
            or bytes(r("PROFILE")) != keccak(text=PROFILE)):
        raise ValueError("BTC_ADDRESS_GRAPH_MISMATCH")
    verifier = r("verifier")
    # No caller-selected verifier implementation or proxy is accepted. The
    # current candidate is unqualified; a future qualified build needs explicit
    # source/artifact admission, not merely a self-reported QUALIFIED flag.
    if verify_derivation:
        checked_runtime(w3, verifier, "BitcoinDepositDerivation", number)
        prove_creation(w3, verifier, "BitcoinDepositDerivation", [], number)
        if keccak(bytes(w3.eth.get_code(verifier, block_identifier=number))) != bytes(r("verifierRuntimeHash")):
            raise ValueError("BTC_ADDRESS_VERIFIER_MISMATCH")
    arguments = [deposits, bytes(r("bitcoinGenesis")), verifier, bytes(r("verifierRuntimeHash")),
                 bytes(r("verifierSourceHash")), bytes(r("qualificationCommitment")), bytes(r("servicePolicyHash"))]
    provenance = prove_creation(w3, address, "BitcoinAddressRegistry", arguments, number)
    return registry, dict(graph_id=bytes(r("graphId")).hex(), custody_scope=bytes(r("custodyScope")).hex(),
        derivation_scope=bytes(r("derivationScope")).hex(), verifier=verifier,
        verifier_runtime_hash=bytes(r("verifierRuntimeHash")).hex()), arguments, provenance


def configuration(w3, pool, number,*,verify_derivation=True):
    checked_runtime(w3, pool.address, "MiningPoolV1", number)
    deposits = read(pool, number, "deposits")
    common = w3.eth.contract(address=deposits, abi=artifact("BitcoinDepositRegistry")["abi"])
    checked_runtime(w3, deposits, "BitcoinDepositRegistry", number)
    address = read(common, number, "addressRegistry")
    if address == ZERO or int(read(pool, number, "mintProofVersion")) != 2:
        raise ValueError("BTC_ADDRESS_REGISTRY_REQUIRED")
    escrow = read(pool, number, "escrow")
    esc = w3.eth.contract(address=escrow, abi=artifact("BitcoinWithdrawalEscrow")["abi"])
    checked_runtime(w3, escrow, "BitcoinWithdrawalEscrow", number)
    batches = read(esc, number, "batches")
    batch = w3.eth.contract(address=batches, abi=artifact("BitcoinBatchRegistry")["abi"])
    checked_runtime(w3, batches, "BitcoinBatchRegistry", number)
    for target, kind in ((pool.address, "MiningPoolV1"), (deposits, "BitcoinDepositRegistry"),
                         (escrow, "BitcoinWithdrawalEscrow"), (batches, "BitcoinBatchRegistry")):
        prove_creation(w3, target, kind, None, number)
    auth = read(pool, number, "authorization")
    if (read(common, number, "spendRecorder") != escrow or read(common, number, "authorization") != auth
            or read(esc, number, "depositRegistry") != deposits or read(batch, number, "withdrawalEscrow") != escrow
            or read(batch, number, "authorization") != auth):
        raise ValueError("BTC_ADDRESS_GRAPH_MISMATCH")
    registry, identity, _, _ = registry_identity(w3, address, deposits, auth, escrow, batches,
        read(batch, number, "dkg"), number,**({} if verify_derivation else {'verify_derivation':False}))
    return dict(registry=registry, common=common, batch=batch, identity=identity, pool=pool, number=number)


def binding(context, intent_id):
    r, common, pool, n = (context[k] for k in ("registry", "common", "pool", "number"))
    b = read(r, n, "getBinding", intent_id)
    raw = bytes(b[0])
    intent = DepositIntentV1.decode(raw)
    if (intent.intent_id != intent_id or intent.evm_chain_id != pool.w3.eth.chain_id
            or Web3.to_checksum_address(intent.deposit_registry) != r.address
            or Web3.to_checksum_address(intent.pool) != pool.address or b[1] != pool.address
            or Web3.to_checksum_address(intent.mp_token) != read(pool, n, "mpToken") or b[2] != read(pool, n, "mpToken")
            or Web3.to_checksum_address(intent.requester) != b[3] or Web3.to_checksum_address(intent.recipient) != b[4]
            or intent.request_nonce != bytes(b[6]) or intent.custody_graph_id != bytes(read(r, n, "graphId"))
            or intent.bitcoin_genesis != bytes(read(r, n, "bitcoinGenesis"))
            or intent.dkg_registry != bytes.fromhex(read(r, n, "dkg")[2:])
            or intent.wallet_id != bytes(read(r, n, "walletId")) or intent.wallet_result != bytes(read(r, n, "walletResult"))
            or intent.key_epoch != int(read(r, n, "keyEpoch")) or intent.service_policy_hash != bytes(read(r, n, "servicePolicyHash"))):
        raise ValueError("BTC_ADDRESS_BINDING_MISMATCH")
    derived = intent.derive(b"\x02" + bytes(read(r, n, "baseX")))
    expected = [bytes.fromhex(derived[k]) for k in ("intent_id", "merkle_root", "tweak", "output_key")]
    expected += [derived["output_parity_odd"], bytes.fromhex(derived["binding_commitment"]), bytes.fromhex(derived["wallet_context"])]
    script = bytes.fromhex(derived["script_pubkey"])
    if (list(b[7]) != expected or bytes(b[5]) != keccak(script)
            or read(common, n, "scriptPools", keccak(script)) != pool.address
            or bytes(read(common, n, "addressIntents", keccak(script))) != intent_id):
        raise ValueError("BTC_ADDRESS_BINDING_MISMATCH")
    height = int(b[9])
    if height <= 0 or height > n or bytes(pool.w3.eth.get_block(height - 1).hash) != bytes(b[10]):
        raise ValueError("BTC_ADDRESS_VALIDATION_ANCHOR")
    provenance = keccak(encode(["bytes32"] * 10 + ["uint256", "bytes32"], [
        keccak(text="SATOHSI_ADDRESS_VALIDATION_V1"), bytes(read(r, n, "derivationScope")), keccak(text=PROFILE),
        bytes(read(r, n, "verifierRuntimeHash")), bytes(read(r, n, "verifierSourceHash")),
        bytes(read(r, n, "qualificationCommitment")), intent.wallet_result, expected[6], intent_id,
        expected[5], height, bytes(b[10])]))
    if provenance != bytes(b[8]):
        raise ValueError("BTC_ADDRESS_PROVENANCE_MISMATCH")
    return dict(intent=intent, script=script, binding=expected[5], provenance=provenance, recipient=b[4])


def validate_claim(context, claim):
    b = binding(context, bytes(claim[0]))
    if (bytes(claim[1]) != b["binding"] or claim[4] != b["recipient"] or bytes(claim[8]) != b["provenance"]
            or not 0 < int(claim[5]) <= 2_100_000_000_000_000):
        raise ValueError("BTC_ADDRESS_CLAIM_MISMATCH")
    r, n = context["registry"], context["number"]
    if (int(claim[6]) != int(read(r, n, "custodyGeneration"))
            or int(claim[7]) != int(read(r, n, "derivationGeneration"))
            or int(read(r, n, "custodyState")) not in (0, 1) or int(read(r, n, "derivationState")) != 0):
        raise ValueError("BTC_MINT_GENERATION_CHANGED")
    verifier = context["pool"].w3.eth.contract(address=read(r, n, "verifier"), abi=artifact("BitcoinDepositDerivation")["abi"])
    if read(verifier, n, "QUALIFIED") is not True:
        raise ValueError("BTC_ADDRESS_VERIFIER_UNQUALIFIED")
    if bytes(read(r, n, "validateMint", context["pool"].address, claim)) != keccak(b["script"]):
        raise ValueError("BTC_ADDRESS_BINDING_MISMATCH")
    return b


def economics(claim):
    return list(claim[:6]) + [claim[8]]


def complete(context, claim):
    key = keccak(encode(["bytes32", "uint32"], [claim[2], claim[3]]))
    p, n = context["pool"], context["number"]
    allocation = bytes(read(p, n, "mintedAllocations", key))
    intent = bytes(read(p, n, "mintedAddressIntents", key))
    if allocation == bytes(32) and intent == bytes(32):
        return False
    b = binding(context, bytes(claim[0]))
    if (bytes(claim[1]) != b["binding"] or claim[4] != b["recipient"] or bytes(claim[8]) != b["provenance"]
            or int(read(p, n, "mintedAddressSats", key)) != int(claim[5])):
        raise ValueError("BTC_ADDRESS_COMPLETION_MISMATCH")
    expected = keccak(encode(["address[]", "uint256[]"], [[claim[4]], [int(claim[5]) * 10**10]]))
    if allocation != expected or intent != bytes(claim[0]) or not read(context["common"], n, "consumed", key):
        raise ValueError("BTC_ADDRESS_COMPLETION_MISMATCH")
    return True


def receipt_claim(context, intent_id, txid, vout):
    b = binding(context, intent_id)
    key = keccak(encode(["bytes32", "uint32"], [txid, vout]))
    sats = int(read(context["pool"], context["number"], "mintedAddressSats", key))
    return [intent_id, b["binding"], txid, vout, b["recipient"], sats,
            int(read(context["registry"], context["number"], "custodyGeneration")),
            int(read(context["registry"], context["number"], "derivationGeneration")), b["provenance"]]


def funding(context, bitcoin, txid, vout, intent_id, *, prove=True):
    """Validate Core bytes/UTXO/all inputs first. No SMP1 fallback and no paid work."""
    from .funding_envelope import inspect_funding
    from .bitcoin_proofs import inclusion
    from .bitcoin_rpc import satoshis
    r, n = context["registry"], context["number"]
    b = binding(context, intent_id)
    bitcoin.check_network()
    if bitcoin.call("getblockhash", 0) != bytes(read(r, n, "bitcoinGenesis")).hex():
        raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
    tx = bitcoin.call("getrawtransaction", txid, True)
    parsed = inspect_funding(tx["hex"], txid)
    if not 0 <= vout < len(parsed["outputs"]): raise ValueError("BTC_MINT_OUTPUT")
    output = parsed["outputs"][vout]
    claim = [intent_id, b["binding"], bytes.fromhex(txid)[::-1], vout, b["recipient"], int(output["amount"]),
             int(read(r, n, "custodyGeneration")), int(read(r, n, "derivationGeneration")), b["provenance"]]
    validate_claim(context, claim)
    utxo = bitcoin.call("gettxout", txid, vout, True)
    if (not utxo or output["script"] != b["script"].hex() or utxo["scriptPubKey"]["hex"] != output["script"]
            or satoshis(utxo["value"]) != claim[5]): raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    depth = 100 if parsed["coinbase"] else int(read(context["common"], n, "depth"))
    if int(utxo["confirmations"]) < depth: raise ValueError("BTC_MINT_PROOF_OR_ALLOCATION")
    for item in parsed["inputs"] + [dict(outpoint=f"{txid}:{vout}")]:
        display, index = item["outpoint"].split(":"); index = int(index)
        key = keccak(encode(["bytes32", "uint32"], [bytes.fromhex(display)[::-1], index]))
        reservation = keccak(encode(["bytes32", "uint32"], [bytes.fromhex(display), index]))
        if (read(context["common"], n, "consumed", key) or read(context["common"], n, "walletOutput", key)
                or bytes(read(context["batch"], n, "reservations", reservation)) != bytes(32)):
            raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    if not prove: return claim
    proof = inclusion(bitcoin, txid)
    from .zk_profiles import profile as zk_profile
    spv = context["pool"].w3.eth.contract(address=read(context["common"], n, "spv"),
        abi=artifact(zk_profile(bitcoin.policy)["adapter"] if (getattr(bitcoin, "policy", None) or {}).get("version") == "2" else "MainnetSPVGuard")["abi"])
    block_hash = bytes.fromhex(proof["block_hash"]); siblings = [bytes.fromhex(x) for x in proof["siblings"]]
    canonical, confirmations = read(spv, n, "getBlockStatus", block_hash)
    if (not canonical or int(confirmations) < depth or int(proof["confirmations"]) < depth
            or not read(spv, n, "checkTxInclusion", block_hash, claim[2], siblings, int(proof["index"]))):
        raise ValueError("BTC_MINT_PROOF_OR_ALLOCATION")
    return claim, [bytes.fromhex(parsed["stripped"]), block_hash, siblings, int(proof["index"])]


def review_record(record, bitcoin, graph):
    """Independent participant funding review before an address-mint nonce/share."""
    from .bitcoin_trust import check_bitcoin_graph
    claim = record["arguments"][0]
    checked, _ = funding(record["context"]["address_context"], bitcoin,
        bytes(claim[2])[::-1].hex(), int(claim[3]), bytes(claim[0]))
    if list(checked) != list(claim):
        raise ValueError("BTC_ADDRESS_CLAIM_MISMATCH")
    check_bitcoin_graph(type("RegistryView", (), {"w3": record["context"]["pool"].w3})(), graph, bitcoin)


def signing_graph(bundle, context):
    """Build the mint-only public wallet view from the signer's enrolled invitation.

    No BTC key package, graph pin or Bitcoin signing authority is imported into
    the EVM signer. Public completed-DKG data is sufficient for graph checking.
    """
    from types import SimpleNamespace
    from .bitcoin_trust import inspect_registry, invitation_options
    invitation = bundle.get("onboarding", {}).get("invitation")
    if not isinstance(invitation, dict): raise ValueError("BTC_ADDRESS_ENROLLMENT_REQUIRED")
    pool, w3, n = context["pool"], context["pool"].w3, context["anchor"].number
    c = configuration(w3, pool, n); r = c["registry"]
    dkg_address = read(r, n, "dkg")
    dkg = w3.eth.contract(address=dkg_address, abi=artifact("DKGCoordinator")["abi"])
    identifier = bytes(read(r, n, "walletId"))
    result = read(dkg, n, "getCompletedResult", identifier)
    from .bootstrap_custody import role_inputs
    roster,threshold=role_inputs(invitation,"btc_ceremony")
    if (invitation["sessions"]["btc_ceremony"] != identifier.hex()
            or context["dkg_id"].hex() not in invitation["sessions"].values()
            or tuple(roster) != tuple(result[5]) or int(threshold) != int(result[6])):
        raise ValueError("BTC_MINT_ADMISSION_GRAPH_MISMATCH")
    manifest = SimpleNamespace(purpose="BTC_SPEND", chain_id=w3.eth.chain_id, registry=dkg_address,
        session_id=identifier, context=bytes(result[0]), participants=tuple(result[5]), threshold=int(result[6]))
    key = dict(result_hash=bytes(result[1]).hex(), result=dict(group="02" + bytes(result[4]).hex()))
    registry = SimpleNamespace(w3=w3)
    return inspect_registry(manifest, key, registry, c["batch"].address,
        mint_only=True, **invitation_options(invitation))["graph"]


def dispatch(a, manifest, key, registry, state):
    from . import mint_admission as admission, mint_admission_client as sdk
    from .bitcoin_trust import require_registry, confirmed_block, check_bitcoin_graph
    from .bitcoin_onchain_payment import _bitcoin, _auth_board
    from .bitcoin_onchain import send, snapshot_check
    from .bitcoin_onchain_plan import hex_value, decimal
    if not getattr(a, "mint_admission", None): raise ValueError("BTC_MINT_ADMISSION_REQUIRED")
    pinned = state.get("btc-mint-registry:" + manifest.context.hex())
    if not pinned: raise ValueError("BTC_REGISTRY_NOT_PINNED")
    graph = require_registry(manifest, key, registry, state,
        pinned["graph"]["contracts"]["batches"]["address"], mint_only=True)["graph"]
    registry.bitcoin_graph = graph
    block = confirmed_block(registry.w3, graph)
    context = admission.configuration(registry.w3, a.mint_admission, block.number)
    pool, board = context["pool"], context["board"]
    if (context["authorization"].address != graph["contracts"]["authorization"]["address"]
            or context["dkg_id"].hex() != graph["evm_dkg_id"]
            or read(pool, block.number, "deposits") != graph["contracts"]["deposits"]["address"]):
        raise ValueError("BTC_MINT_ADMISSION_GRAPH_MISMATCH")
    view = configuration(registry.w3, pool, block.number)
    if view["registry"].address != graph["contracts"]["address_registry"]["address"]:
        raise ValueError("BTC_ADDRESS_GRAPH_MISMATCH")
    txid = hex_value(a.txid.removeprefix("0x"), 32); vout = decimal(str(a.vout), 2**32 - 1)
    intent_id = hex_value(a.intent_id.removeprefix("0x"), 32)
    recovered = receipt_claim(view, intent_id, txid[::-1], vout)
    if complete(view, recovered):
        snapshot_check(registry.w3, block)
        return dict(status="BTC_FUNDING_EXECUTED", operation="address-mint", target=pool.address,
            intent_id=intent_id.hex(), txid=txid.hex(), vout=str(vout))
    from .bitcoin_continuity import trust_guard
    trust_guard(manifest,registry,state,graph,mode='ADDRESS_MINT')
    bitcoin = _bitcoin(a, graph)
    if getattr(a, "zk_worker_config", None) or getattr(a, "zk_transport_config", None):
        if getattr(a,"zk_worker_config",None) and getattr(a,"zk_transport_config",None):raise ValueError("ZK_REQUEST_ROUTE_CONFLICT")
        # Validate the actual registered output and present fences before any
        # automatic admission/quote/payment. No SMP1/base-script fallback.
        funding(view,bitcoin,txid.hex(),vout,intent_id,prove=False)
        need=dict(kind="address_deposit",consumer=pool.address,intent_id=intent_id.hex(),txid=txid.hex(),vout=str(vout))
        if getattr(a,"zk_transport_config",None):
            from .zk_worker_transport import request_from_consumer
            readiness=request_from_consumer(a.zk_transport_config,graph,need,bitcoin,getattr(a,'zk_request_federation',None),getattr(a,'zk_service_recipient',None))
        else:
            from .zk_worker import request_from_consumer
            readiness=request_from_consumer(a.zk_worker_config,graph,need)
        if readiness["status"] not in ("READY","COMPLETED"):
            return dict(status="AWAITING_ZK_STATE",spv=readiness)
        block=confirmed_block(registry.w3,graph)
        view=configuration(registry.w3,pool,block.number)
        if complete(view,receipt_claim(view,intent_id,txid[::-1],vout)):
            snapshot_check(registry.w3,block)
            return dict(status="BTC_FUNDING_EXECUTED",operation="address-mint",target=pool.address,
                intent_id=intent_id.hex(),txid=txid.hex(),vout=str(vout))
    trust_guard(manifest,registry,state,graph,mode='ADDRESS_MINT')
    proposed, _ = funding(view, bitcoin, txid.hex(), vout, intent_id)
    check_bitcoin_graph(registry, graph, bitcoin)
    _, role = _auth_board(a, manifest, registry, graph)
    deadline = decimal(str(a.deadline), positive=True)
    reference = admission.claim_id(pool.address, txid[::-1], vout)
    with state.exclusive():
        c = read(board, block.number, "getClaim", reference)
        if c[4]:
            original = list(read(board, block.number, "getAddressClaim", reference))
            if (int(read(board, block.number, "claimModes", reference)) != 2 or int(c[4]) != deadline
                    or economics(original) != economics(proposed)):
                raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
        retry = a.action == "btc-address-mint-retry"
        if retry:
            if not c[4]: raise ValueError("BTC_MINT_ADMISSION_CLAIM_REQUIRED")
            previous = admission.session_record(registry.w3, board.address, int(c[6]), block.number,
                role=role, require_active=False)
            sdk.remember(state, registry, previous)
            if sdk._approved_current(previous): raise ValueError("BTC_MINT_OPERATION_ALREADY_APPROVED_EXECUTE_REQUIRED")
            if deadline <= registry.w3.eth.get_block("latest").timestamp: raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            receipt = send(registry, board.address, "BitcoinMintAdmission", "retryGenerationMint",
                [reference, read(pool, block.number, "nonce"), int(c[6]), proposed[6], proposed[7]], graph=graph)
            block = registry.w3.eth.get_block(receipt["blockNumber"])
            c = read(board, block.number, "getClaim", reference)
        elif not c[4]:
            if a.action != "btc-address-mint-propose": raise ValueError("BTC_MINT_ADMISSION_CLAIM_REQUIRED")
            receipt = send(registry, board.address, "BitcoinMintAdmission", "openAddressMint",
                [proposed, deadline, read(pool, block.number, "nonce")], graph=graph)
            block = registry.w3.eth.get_block(receipt["blockNumber"])
            c = read(board, block.number, "getClaim", reference)
        record = admission.session_record(registry.w3, board.address, int(c[6]), block.number,
            role=role, require_active=a.action != "btc-address-mint-execute",
            signing=a.action != "btc-address-mint-execute")
        if record.get("generation_mode") != 2 or economics(record["arguments"][0]) != economics(proposed):
            raise ValueError("BTC_ADDRESS_CLAIM_MISMATCH")
        sdk.remember(state, registry, record); snapshot_check(registry.w3, block)
        if a.action != "btc-address-mint-execute": return sdk._result(record)
        record = sdk._execution_record(registry, board.address, int(c[6]), role)
        current = confirmed_block(registry.w3, graph)
        view = configuration(registry.w3, pool, current.number)
        if complete(view, record["arguments"][0]): return sdk._result(record, "BTC_FUNDING_EXECUTED")
        checked, _ = funding(view, bitcoin, txid.hex(), vout, intent_id)
        if checked != record["arguments"][0]: raise ValueError("BTC_MINT_GENERATION_CHANGED")
        snapshot_check(registry.w3, current)
        trust_guard(manifest,registry,state,graph,mode='ADDRESS_MINT')
        sdk._authorize(registry, graph, record)
        record = sdk._execution_record(registry, board.address, int(c[6]), role)
        current = confirmed_block(registry.w3, graph)
        view = configuration(registry.w3, pool, current.number)
        checked, proof = funding(view, bitcoin, txid.hex(), vout, intent_id)
        if checked != record["arguments"][0]: raise ValueError("BTC_MINT_GENERATION_CHANGED")
        snapshot_check(registry.w3, current)
        trust_guard(manifest,registry,state,graph,mode='ADDRESS_MINT')
        from .zk_submission import options as proof_submission
        send(registry, pool.address, "MiningPoolV1", "mintFromAddressProof",
            [*proof, checked, record["operation_hash"]], graph=graph,
            **proof_submission(registry, graph, proof[1]))
    current = confirmed_block(registry.w3, graph)
    if not complete(configuration(registry.w3, pool, current.number), checked):
        raise ValueError("BTC_OPERATION_POSTCONDITION")
    snapshot_check(registry.w3, current)
    return sdk._result(record, "BTC_FUNDING_EXECUTED")
