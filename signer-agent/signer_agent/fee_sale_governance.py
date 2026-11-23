"""Authenticate fee sale terms and the enrolled seller before signing."""

import json

from web3 import Web3

from .bootstrap import encode_args
from .bitcoin_trust import checked_runtime, runtime_artifact
from .contract_provenance import prove_creation
from .dao_client import address
from .dkg_protocol import Manifest
from .ethereum_intents import validate_signing_binding
from .evm_network import read_block
from .onboarding import read_public
from .onchain_transport import _board_and_invitation, board_at
from .ulr_treasury import authority_context, contract, number, read

SALE = "FundedFeeSale"
DAO = "ConfederationDAOIncome"
POOL = "SimpleMultiPool"
ZERO = "0x" + "00" * 20
OPERATIONS = {
    "bind": (DAO, "getSetFeeSaleOperationHash", "setFeeSale"),
    "share": (SALE, "getSetSellBpsOperationHash", "setSellBps"),
    "claim": (POOL, "getClaimFeesOperationHash", "claimFees"),
    "collect": (SALE, "getCollectMPFeesOperationHash", "collectMPFees"),
    "fill": (SALE, "getFillBidOperationHash", "fillBid"),
}


def validate_pin(pin):
    addresses = ("board", "factory", "sale", "fee_token", "stablecoin", "treasury")
    if (not isinstance(pin, dict)
            or set(pin) != {"version", "chain_id", "confirmations", "role", *addresses}
            or pin["version"] != "1" or pin["chain_id"] not in ("1", "11155111", "31337")
            or pin["role"] not in ("evm_ceremony", "conf_ceremony")
            or not (12 if pin["chain_id"] == "1" else 1) <= number(pin["confirmations"]) <= 256
            or any(address(pin[key]) != pin[key] or pin[key] == ZERO for key in addresses)
            or pin["fee_token"] == pin["stablecoin"]):
        raise ValueError("FEE_SALE_PIN")
    return pin


def load_pin(path):
    return validate_pin(read_public(path))


def matches(intent):
    return (intent.get("artifact") == SALE or "fee_sale" in intent
            or intent.get("hash_method") == OPERATIONS["bind"][1])


def context(w3, bundle, pin, at, *, require_finalized=True, require_sale=True, refund_context=False):
    validate_pin(pin)
    block = w3.eth.get_block(at)
    at = block.number
    manifest = Manifest.from_dict(bundle)
    invite = bundle.get("onboarding", {}).get("invitation", {})
    role = pin["role"]
    if (str(w3.eth.chain_id) != pin["chain_id"] or manifest.chain_id != w3.eth.chain_id
            or bundle.get("onchain_board") != pin["board"] or bundle.get("role") != role
            or manifest.purpose != "EVM_AUTH" or manifest.session_id.hex() != invite.get("sessions", {}).get(role)
            or number(pin["confirmations"]) < int(invite.get("evm_confirmations", "256"))):
        raise ValueError("FEE_SALE_ENROLLMENT")
    if require_finalized and at > read_block(w3, number(pin["confirmations"])).number:
        raise ValueError("FEE_SALE_UNFINALIZED")
    options = {"legacy_dao_exit": True} if refund_context else {}
    board, published = _board_and_invitation(w3, pin["board"], block, **options)
    if published != invite or read(board, at, "registry") != manifest.registry:
        raise ValueError("FEE_SALE_ENROLLMENT")

    def checked(kind, target):
        if refund_context:
            runtime_artifact(w3, target, kind, at, legacy_dao_exit=True)
        else:
            checked_runtime(w3, target, kind, at)
        return contract(w3, kind, target)

    def direct(kind, target):
        proof = prove_creation(w3, target, kind, None, at, legacy_dao_exit=refund_context)
        if proof.get("method") != "direct" or proof.get("sender") != invite["operator"]:
            raise ValueError("FEE_SALE_ORIGIN")
        return contract(w3, kind, target)

    confederation = role == "conf_ceremony"
    factory = direct("ConfederationFactory" if confederation else "FederationFactory", pin["factory"])
    if confederation:
        if invite.get("ulr", {}).get("version") != "3":
            raise ValueError("FEE_SALE_CONFEDERATION_PROFILE")
        entity = read(factory, at, "getConfederation", invite["confederation_id"])
        authorization, validator, pool_address, shares = entity[7:11]
        pool = checked("ConfederationPoolV2", pool_address)
        source = direct(DAO, read(pool, at, "daoIncome"))
        if (shares != pin["fee_token"] or source.address != pin["treasury"]
                or read(source, at, "pool") != pool.address or read(source, at, "shareToken") != shares
                or read(source, at, "authorization") != authorization or read(source, at, "validator") != validator
                or not read(source, at, "approvedToken", pin["stablecoin"])):
            raise ValueError("FEE_SALE_SOURCE")
        bound = read(source, at, "feeSale")
    else:
        entity = read(factory, at, "getFederation", invite["federation_id"])
        authorization, validator = entity[4], ZERO
        source = checked(POOL, entity[5])
        pool_info = read(source, at, "getPoolInfo", pin["fee_token"])
        if (not pool_info[3] or pool_info[0] != pin["fee_token"]
                or pin["treasury"] != invite["treasury"] or read(source, at, "getFrostMultisig") != authorization):
            raise ValueError("FEE_SALE_SOURCE")
        bound = ZERO

    if not entity[-1] and not refund_context:
        raise ValueError("FEE_SALE_SOURCE")
    auth = authority_context(w3, authorization, at, **options)
    if (read(auth, at, "registry") != manifest.registry or bytes(read(auth, at, "dkgId")) != manifest.session_id
            or read(factory, at, "dkgCoordinator") != manifest.registry
            or read(factory, at, "frostCoordinator") != read(auth, at, "coordinator")):
        raise ValueError("FEE_SALE_AUTHORIZATION")
    reward = invite.get("reward_token")
    code = w3.eth.get_code(pin["stablecoin"], block_identifier=at)
    if not code or (reward and (pin["stablecoin"] != reward["address"]
            or Web3.to_hex(Web3.keccak(code)) != reward["runtime_hash"]
            or read(contract(w3, "IERC20Metadata", pin["stablecoin"]), at, "decimals") != int(reward["decimals"]))):
        raise ValueError("FEE_SALE_STABLECOIN")
    sale = None
    if require_sale:
        sale = direct(SALE, pin["sale"])
        expected = dict(source=source.address, feeToken=pin["fee_token"], stablecoin=pin["stablecoin"],
                        treasury=pin["treasury"], authorization=auth.address, validator=validator)
        if any(read(sale, at, key) != value for key, value in expected.items()):
            raise ValueError("FEE_SALE_BINDING")
        # A prior bid remains a liability of its exact sale even if the DAO
        # subsequently binds a different sale for routing new fee income.
        if bound not in (ZERO, sale.address) and not refund_context:
            raise ValueError("FEE_SALE_ALREADY_BOUND")
    if bytes(w3.eth.get_block(at).hash) != bytes(block.hash):
        raise ValueError("FEE_SALE_SNAPSHOT_REORG")
    return dict(sale=sale, source=source, authority=auth, board=board, block=block,
                validator=validator, bound=bound, source_active=entity[-1])


def review(w3, intent, bundle, at, pin, *, require_finalized=True):
    ctx = context(w3, bundle, pin, at, require_finalized=require_finalized)
    at = ctx["block"].number
    action = intent.get("fee_sale")
    if not isinstance(action, str) or action not in OPERATIONS:
        raise ValueError("FEE_SALE_OPERATION")
    kind, helper, method = OPERATIONS[action]
    confederation = ctx["validator"] != ZERO
    if ((action == "bind" and not confederation)
            or (confederation and action != "bind"
                and (action in ("claim", "collect") or ctx["bound"] != ctx["sale"].address))):
        raise ValueError("FEE_SALE_SOURCE")
    target = ctx["sale"] if kind == SALE else ctx["source"]
    if (intent.get("artifact") != kind or intent.get("hash_method") != helper
            or intent.get("target") != target.address or intent.get("chain_id") != pin["chain_id"]):
        raise ValueError("FEE_SALE_OPERATION")
    auth, sale = ctx["authority"], ctx["sale"]
    validate_signing_binding(intent, bundle, read(auth, at, "coordinator"))
    abi = next(row for row in target.abi if row.get("name") == helper)
    values = encode_args(abi["inputs"], intent["hash_args"])
    op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
    complete = read(auth, at, "executedOperations", op)
    if action == "bind" and values != [sale.address]:
        raise ValueError("FEE_SALE_BINDING")
    if action == "claim" and (values[0] != pin["fee_token"] or values[2] != sale.address):
        raise ValueError("FEE_SALE_CLAIM")
    if complete:
        if read(auth, at, "operationTargets", op) != target.address:
            raise ValueError("FEE_SALE_OPERATION")
    else:
        if bytes(read(target, at, helper, *values)) != op:
            raise ValueError("FEE_SALE_OPERATION_HASH")
        if action == "share" and values[0] > 10_000:
            raise ValueError("FEE_SALE_SHARE")
        if action == "bind" and ctx["bound"] != ZERO:
            raise ValueError("FEE_SALE_ALREADY_BOUND")
        if action == "fill":
            buyer, amount, payment, deadline = read(sale, at, "bids", values[0])
            inventory = read(sale, at, "inventory")
            escrow = read(sale, at, "stableEscrow")
            stable = contract(w3, "IERC20Metadata", pin["stablecoin"])
            fee = contract(w3, "IERC20Metadata", pin["fee_token"])
            if (buyer == ZERO or amount == 0 or amount > inventory or payment == 0 or payment > escrow
                    or deadline < ctx["block"].timestamp or read(stable, at, "balanceOf", sale.address) < escrow
                    or read(fee, at, "balanceOf", sale.address) < inventory):
                raise ValueError("FEE_SALE_BID_UNAVAILABLE")
        if action == "collect":
            is_wrap, gross, nonce = values
            claim = read(sale, at, "getMPClaimOperationHash", *values)
            if (gross == 0 or read(sale, at, "processedMPClaims", claim)
                    or (not read(auth, at, "executedOperations", claim)
                        and (nonce != read(ctx["source"], at, "operationNonce")
                             or gross > read(ctx["source"], at, "accruedFees", pin["fee_token"], is_wrap)))):
                raise ValueError("FEE_SALE_CLAIM")
    if bytes(w3.eth.get_block(at).hash) != bytes(ctx["block"].hash):
        raise ValueError("FEE_SALE_SNAPSHOT_REORG")
    return dict(ctx, target=target, method=method, arguments=values, operation_hash=op, complete=complete)


def guard_signing_session(client, session):
    payload = getattr(client, "fee_sale_context", None)
    if payload is None:
        return
    intent, bundle, pin = payload
    if (intent["target"].lower() != session.origin_contract.lower()
            or intent["operation_hash"].removeprefix("0x") != session.message_hash.hex()
            or intent["coordinator"].lower() != client.address.lower()):
        raise ValueError("FEE_SALE_SESSION_BINDING")
    block = client.w3.eth.get_block("latest")
    board = board_at(client.w3, pin["board"], block)
    if json.loads(bytes(read(board, block.number, "intents", session.session_id))) != intent:
        raise ValueError("FEE_SALE_PUBLIC_INTENT")
    if review(client.w3, intent, bundle, block.number, pin, require_finalized=False)["complete"]:
        raise ValueError("FEE_SALE_OPERATION_COMPLETE")
    client.check_read_snapshot()
