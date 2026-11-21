"""Typed, on-chain gas treasury governance through the existing FROST workflow.

Configuration requires the enrolled mainnet V2 state view, or an explicit local fixture.
Revocation does not depend on state availability. Public intents are review data;
only an explicit participant approval may create a nonce/share.
"""
import json
from pathlib import Path

from eth_abi import encode
from eth_utils.abi import function_abi_to_4byte_selector
from web3 import Web3
from web3.logs import DISCARD

from .bootstrap import Bootstrap, artifact, encode_args
from .bitcoin_trust import checked_runtime, confirmed_block
from .contract_provenance import prove_creation
from .dkg_protocol import Manifest
from .operations import canonical

KIND = "SPVServicePaymentRegistry"
ZERO = "0x" + "00" * 20
CONFIGURE = "getConfigureOperationHash"
PAUSE = "getPauseOperationHash"
CONFIG_TYPE = "(address,address,uint64,bytes32,uint256,bytes32)"


def read(c, method, block, *args):
    return getattr(c.functions, method)(*args).call(block_identifier=block)


def contract(w3, address, kind, number):
    checked_runtime(w3, address, kind, number)
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def anchor(w3, block):
    if bytes(w3.eth.get_block(block.number).hash) != bytes(block.hash):
        raise ValueError("GAS_GOVERNANCE_REORG")


def context(w3, address, bundle, number):
    manifest = Manifest.from_dict(bundle)
    if (manifest.purpose != "EVM_AUTH" or manifest.chain_id != w3.eth.chain_id
            or bundle.get("role") != "conf_ceremony" or not bundle.get("onchain_board")):
        raise ValueError("GAS_CONFEDERATION_ENROLLMENT_REQUIRED")
    block = w3.eth.get_block(number)
    number = block.number
    target = contract(w3, address, KIND, number)
    factory_address = read(target, "factory", number)
    conf_id = read(target, "confederationId", number)
    view = read(target, "stateView", number)
    prove_creation(w3, target.address, KIND, [factory_address, conf_id, view], number)
    factory = contract(w3, factory_address, "ConfederationFactory", number)
    factory_proof = prove_creation(w3, factory.address, "ConfederationFactory", None, number)
    conf = read(factory, "getConfederation", number, conf_id)
    auth = contract(w3, read(target, "authorization", number), "DKGAuthorization", number)
    coordinator = contract(w3, read(auth, "coordinator", number), "FROSTCoordinator", number)
    verifier = read(coordinator, "defaultVerifier", number)
    checked_runtime(w3, verifier, "FROSTVerifier", number)
    prove_creation(w3, verifier, "FROSTVerifier", [], number)
    prove_creation(w3, coordinator.address, "FROSTCoordinator", [verifier], number)
    dkg = contract(w3, manifest.registry, "DKGCoordinator", number)
    prove_creation(w3, dkg.address, "DKGCoordinator", [], number)
    completed = read(dkg, "getCompletedResult", number, manifest.session_id)
    if (read(auth, "verifier", number) != verifier
            or bytes(completed[0]) != manifest.context or bytes(completed[3]) != Web3.keccak(text="EVM_AUTH")
            or list(completed[5]) != list(manifest.participants) or completed[6] != manifest.threshold
            or read(coordinator, "dkgRegistry", number) != dkg.address
            or read(factory, "dkgCoordinator", number) != dkg.address
            or read(factory, "frostCoordinator", number) != coordinator.address
            or conf[7] != auth.address):
        raise ValueError("GAS_GOVERNANCE_AUTHORITY")
    prove_creation(w3, auth.address, "DKGAuthorization", [coordinator.address, manifest.session_id,
        bytes(completed[4]), list(manifest.participants), manifest.threshold], number)
    if (read(auth, "registry", number) != dkg.address
            or bytes(read(auth, "dkgId", number)) != manifest.session_id
            or bytes(read(auth, "resultHash", number)) != bytes(completed[1])):
        raise ValueError("GAS_GOVERNANCE_AUTHORITY")
    invite = bundle.get("onboarding", {}).get("invitation", {})
    if conf_id != invite.get("confederation_id") or factory_proof.get("sender") != invite.get("operator"):
        raise ValueError("GAS_GOVERNANCE_CONFEDERATION")
    anchor(w3, block)
    return dict(target=target, factory=factory, auth=auth, coordinator=coordinator,
        manifest=manifest, block=block, conf=conf, view=view, bitcoin_policy=invite.get("bitcoin_policy"))


def _arguments(helper, args):
    if helper not in (CONFIGURE, PAUSE):
        raise ValueError("GAS_GOVERNANCE_METHOD")
    abi = next(row for row in artifact(KIND)["abi"] if row.get("name") == helper)
    return encode_args(abi["inputs"], args)


def policy(ctx, helper, args):
    target, number = ctx["target"], ctx["block"].number
    result = {name: Bootstrap.wire(read(target, name, number)) for name in (
        "domain", "confederationScope", "stateView", "state", "adapter", "policyHash",
        "stateViewCodeHash", "stateCodeHash", "adapterCodeHash")}
    result.update(authorization=ctx["auth"].address, factory=ctx["factory"].address,
        confederation_id=read(target, "confederationId", number),
        dkg_id=Bootstrap.wire(ctx["manifest"].session_id),
        result_hash=Bootstrap.wire(read(ctx["auth"], "resultHash", number)))
    if helper == CONFIGURE:
        result["configuration"] = dict(zip(("treasury", "issuer", "tariff_version", "tariff_hash",
                                             "amount_wei", "handover_commitment"), Bootstrap.wire(args[0])))
        treasury = contract(target.w3, args[0][0], "ConfederationGasTreasury", number)
        if (not read(target, "isTreasury", number, treasury.address)
                or read(treasury, "paymentRegistry", number) != target.address
                or read(treasury, "authorization", number) != ctx["auth"].address
                or read(treasury, "confederationScope", number) != read(target, "confederationScope", number)):
            raise ValueError("GAS_TREASURY_PROVENANCE")
        payees = list(read(treasury, "getPayees", number))
        weights = [read(treasury, "weight", number, payee) for payee in payees]
        result["treasury"] = dict(address=treasury.address,
            epoch=str(read(treasury, "epoch", number)), payees=payees, weights=Bootstrap.wire(weights),
            roster_hash=Bootstrap.wire(read(treasury, "rosterHash", number)))
    return result


def _active(ctx, helper, args):
    if helper == PAUSE:
        return  # Revocation must work with halted/uninitialized state or inactive confederation.
    w3, target, number = ctx["target"].w3, ctx["target"], ctx["block"].number
    policy = ctx.get("bitcoin_policy")
    if policy is not None and policy.get("version") == "2":
        from .zk_service_binding import inspect_service_view
        from .zk_spv import validate_confederation
        validate_confederation(policy, read(target, "confederationId", number))
        inspect_service_view(w3, ctx["view"], policy, number, state_address=read(target, "state", number))
    elif w3.eth.chain_id != 31337:
        raise ValueError("GAS_SPV_PROFILE_NOT_QUALIFIED")
    if not ctx["conf"][-1]:
        raise ValueError("GAS_CONFEDERATION_INACTIVE")
    view = w3.eth.contract(address=ctx["view"], abi=artifact("ISPVServiceStateView")["abi"])
    for getter, hash_getter in (("stateView", "stateViewCodeHash"), ("state", "stateCodeHash"), ("adapter", "adapterCodeHash")):
        if Web3.keccak(w3.eth.get_code(read(target, getter, number), block_identifier=number)) != read(target, hash_getter, number):
            raise ValueError("GAS_STATE_BINDING")
    for name in ("state", "adapter", "policyHash"):
        if read(view, name, number) != read(target, name, number):
            raise ValueError("GAS_STATE_BINDING")
    digest, _, initialized, halted = read(view, "snapshot", number)
    if not initialized or halted or digest == bytes(32):
        raise ValueError("GAS_STATE_UNAVAILABLE")
    next_treasury, issuer, version, tariff, amount, handover = args[0]
    epoch = read(contract(w3, next_treasury, "ConfederationGasTreasury", number), "epoch", number)
    current_epoch = read(target, "treasuryEpoch", number)
    current = read(target, "configuration", number)
    if (issuer == ZERO or version == 0 or tariff == bytes(32) or amount == 0
            or not (epoch == current_epoch + 1 or epoch == current_epoch and next_treasury == current[0])
            or current_epoch > 0 and epoch != current_epoch and handover == bytes(32)):
        raise ValueError("GAS_CONFIGURATION_TERMS")
    known = read(target, "tariffCommitments", number, version)
    if known not in (bytes(32), Web3.keccak(encode(["bytes32", "uint256"], [tariff, amount]))):
        raise ValueError("GAS_IMMUTABLE_TARIFF")


def prepare(w3, address, bundle, helper, args, deadline, number):
    typed = _arguments(helper, args)
    ctx = context(w3, address, bundle, number)
    _active(ctx, helper, typed)
    deadline = int(deadline)
    if not ctx["block"].timestamp < deadline < 2**64:
        raise ValueError("GAS_GOVERNANCE_DEADLINE")
    intent = dict(chain_id=str(w3.eth.chain_id), role="conf_ceremony", coordinator=ctx["coordinator"].address,
        target=address, artifact=KIND, hash_method=helper, hash_args=Bootstrap.wire(typed),
        operation_hash=Web3.to_hex(read(ctx["target"], helper, ctx["block"].number, *typed)),
        configuration_nonce=str(read(ctx["target"], "configurationNonce", ctx["block"].number)),
        deadline=str(deadline), gas_policy=policy(ctx, helper, typed))
    anchor(w3, ctx["block"])
    return intent


def review(w3, intent, bundle, number, *, active=True, session=None):
    if (intent.get("artifact") != KIND or intent.get("role") != "conf_ceremony"
            or intent.get("chain_id") != str(w3.eth.chain_id)):
        raise ValueError("GAS_GOVERNANCE_INTENT")
    helper = intent.get("hash_method")
    args = _arguments(helper, intent.get("hash_args"))
    ctx = context(w3, intent["target"], bundle, number)
    if intent.get("coordinator") != ctx["coordinator"].address or intent.get("gas_policy") != policy(ctx, helper, args):
        raise ValueError("GAS_GOVERNANCE_POLICY_CHANGED")
    try:
        nonce, deadline = int(intent["configuration_nonce"]), int(intent["deadline"])
        if str(nonce) != intent["configuration_nonce"] or not 0 <= nonce < 2**256:
            raise ValueError()
        if str(deadline) != intent["deadline"] or not 0 < deadline < 2**64:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("GAS_GOVERNANCE_INTENT") from None
    method = "configure" if helper == CONFIGURE else "pauseAdmissions"
    selector = function_abi_to_4byte_selector(next(x for x in artifact(KIND)["abi"] if x.get("name") == method))
    domain = read(ctx["target"], "domain", ctx["block"].number)
    params = Web3.keccak(encode(["bytes32", CONFIG_TYPE], [domain, args[0]])) if helper == CONFIGURE else domain
    digest = read(ctx["auth"], "hashOperation", ctx["block"].number, ctx["target"].address, selector, params, nonce)
    if Web3.to_hex(digest) != intent.get("operation_hash"):
        raise ValueError("GAS_GOVERNANCE_HASH")
    if active:
        if nonce != read(ctx["target"], "configurationNonce", ctx["block"].number):
            raise ValueError("GAS_CONFIGURATION_CHANGED")
        _active(ctx, helper, args)
    if session is not None:
        info = read(ctx["coordinator"], "getSession", ctx["block"].number, session)
        if (int(info[0]) != session or not info[4] or not info[8] or info[9] != ZERO
                or bytes(info[3]) != bytes(digest) or info[15] != ctx["target"].address
                or info[7] != deadline or intent.get("session") != str(session)
                or bytes(read(ctx["coordinator"], "sessionDKGId", ctx["block"].number, session)) != ctx["manifest"].session_id
                or read(ctx["coordinator"], "sessionDKGResult", ctx["block"].number, session)
                    != read(ctx["auth"], "resultHash", ctx["block"].number)):
            raise ValueError("GAS_GOVERNANCE_SESSION")
        if active and int(info[10]) == 1 and ctx["block"].timestamp >= deadline:
            raise ValueError("GAS_GOVERNANCE_DEADLINE")
    anchor(w3, ctx["block"])
    return ctx, args


def guard_signing_session(client, session):
    approved = getattr(client, "gas_intent_context", None)
    if approved is not None:
        intent, _ = approved
        if (intent.get("target") != session.origin_contract
                or intent.get("coordinator") != client.address
                or intent.get("operation_hash", "").removeprefix("0x") != session.message_hash.hex()):
            raise ValueError("GAS_GOVERNANCE_SESSION")
    if session.origin_contract == ZERO:
        if approved is not None:raise ValueError("GAS_GOVERNANCE_SESSION")
        return
    block = getattr(client, "_read_snapshot", None) or client.w3.eth.get_block("latest")
    try:
        checked_runtime(client.w3, session.origin_contract, KIND, block.number)
    except ValueError as error:
        if str(error) == "BTC_CONTRACT_BUILD_MISMATCH" and approved is None:
            return
        raise
    if approved is None:
        raise ValueError("GAS_TYPED_INTENT_REQUIRED")
    intent, bundle = approved
    from .onchain_transport import board_at
    for at in (block, client.w3.eth.get_block("latest")):
        board = board_at(client.w3, bundle["onchain_board"], at)
        if json.loads(bytes(read(board, "intents", at.number, session.session_id))) != intent:
            raise ValueError("GAS_GOVERNANCE_PUBLIC_INTENT")
        ctx, _ = review(client.w3, intent, bundle, at.number, session=session.session_id)
        if ctx["coordinator"].address != client.address or bytes.fromhex(intent["operation_hash"][2:]) != session.message_hash:
            raise ValueError("GAS_GOVERNANCE_SESSION")
    anchor(client.w3, block)
    client.check_read_snapshot()


def management(a, bundle, registry, state):
    from .bitcoin_onchain import send
    from .onchain_transport import board_at
    from .operation_status import executed_operation

    if not a.board or bundle.get("role") != "conf_ceremony":
        raise ValueError("GAS_CONFEDERATION_ENROLLMENT_REQUIRED")
    graph = getattr(registry, "bitcoin_graph", {})
    block = confirmed_block(registry.w3, graph)
    board = board_at(registry.w3, a.board, block)
    if a.action.startswith("gas-prepare-"):
        helper = CONFIGURE if a.action == "gas-prepare-configure" else PAUSE
        values = json.loads(Path(a.configuration).read_text()) if helper == CONFIGURE else None
        fields = ("treasury", "issuer", "tariffVersion", "tariffHash", "amount", "handoverCommitment")
        if values is not None and (not isinstance(values, dict) or set(values) != set(fields)):
            raise ValueError("GAS_CONFIGURATION_TERMS")
        args = [[values[name] for name in fields]] if values is not None else []
        return prepare(registry.w3, a.gas_registry, bundle, helper, args, a.deadline, block.number)
    if a.action == "gas-propose":
        from .onchain_transport import can_publish_intent
        if not can_publish_intent(board, registry.account.address, block.number):
            raise ValueError("GAS_BOARD_OPERATOR_REQUIRED")
        intent = json.loads(Path(a.package).read_text())
        if "session" in intent:
            raise ValueError("GAS_GOVERNANCE_INTENT")
    else:
        if not a.session:
            raise ValueError("GAS_GOVERNANCE_SESSION")
        intent = json.loads(bytes(read(board, "intents", block.number, a.session)))
    ctx, args = review(registry.w3, intent, bundle, block.number, active=False,
                       session=None if a.action == "gas-propose" else a.session)
    if executed_operation(registry.w3, ctx["target"], KIND, intent, block.number,
            session=intent.get("session") and int(intent["session"]), coordinator=ctx["coordinator"].address):
        return dict(status="ALREADY_EXECUTED", operation_hash=intent["operation_hash"])
    if a.action == "gas-status":
        return dict(status="GAS_GOVERNANCE_STATUS", intent=intent,
            configuration_nonce=str(read(ctx["target"], "configurationNonce", block.number)),
            enabled=read(ctx["target"], "enabled", block.number))
    review(registry.w3, intent, bundle, block.number,
           session=None if a.action == "gas-propose" else a.session)
    with state.exclusive():
        if a.action == "gas-propose":
            if block.timestamp >= int(intent["deadline"]):
                raise ValueError("GAS_GOVERNANCE_DEADLINE")
            receipt = send(registry, ctx["coordinator"].address, "FROSTCoordinator", "createSessionFromDKG",
                [ctx["manifest"].session_id, intent["operation_hash"], ctx["target"].address, int(intent["deadline"])], graph=graph)
            events = ctx["coordinator"].events.SessionCreated().process_receipt(receipt, errors=DISCARD)
            if len(events) != 1:
                raise ValueError("GAS_GOVERNANCE_RECEIPT")
            session = int(events[0]["args"]["sessionId"])
            intent = dict(intent, session=str(session))
            public = canonical(intent).encode()
            if len(public) > 32768:
                raise ValueError("GAS_GOVERNANCE_INTENT")
            send(registry, board.address, "DKGOnchainBoard", "publishIntent", [session, public], graph=graph)
            return dict(status="AWAITING_PARTICIPANTS", session=str(session), intent=intent)
        session, op = a.session, bytes.fromhex(intent["operation_hash"][2:])
        if not read(ctx["auth"], "approvedOperations", block.number, op):
            send(registry, ctx["auth"].address, "DKGAuthorization", "authorizeOperation",
                 [session, op, ctx["target"].address], graph=graph)
        latest = confirmed_block(registry.w3, graph)
        review(registry.w3, intent, bundle, latest.number, session=session)
        method = "configure" if intent["hash_method"] == CONFIGURE else "pauseAdmissions"
        receipt = send(registry, ctx["target"].address, KIND, method, [*args, op], graph=graph)
        if not read(ctx["auth"], "executedOperations", receipt.blockNumber, op):
            raise ValueError("GAS_GOVERNANCE_POSTCONDITION")
        return dict(status="GAS_GOVERNANCE_EXECUTED", session=str(session),
                    operation_hash=intent["operation_hash"], transaction_hash=Web3.to_hex(receipt.transactionHash))
