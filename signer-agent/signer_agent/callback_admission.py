"""Authenticate every mandatory S/CS callback before Bitcoin signing.

Self-reported getters and successful eth_call are insufficient: reviewed CREATE
provenance for the pinned registry and leaves establishes creation records, then
exact current code and mutual bindings authenticate the particular callback.
"""
from web3 import Web3
from .bootstrap import artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation


def review_callback_configuration(escrow, registry_address, block):
    """Reusable bootstrap review before the one-time threshold registry binding."""
    w3 = escrow.w3
    # Legacy/local coordinator profiles use the tag "latest". Constructor
    # provenance requires a fixed numeric height, never a moving read tag.
    anchor = w3.eth.get_block(block) if type(block) is not int else None
    if anchor is not None:
        block = anchor.number

    def contract(address, kind):
        checked_runtime(w3, address, kind, block)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    def read(candidate, method, *args):
        return getattr(candidate.functions, method)(*args).call(block_identifier=block)

    if int(registry_address, 16) == 0:
        raise ValueError("BTC_CALLBACK_REGISTRY_REQUIRED")
    admission = contract(registry_address, "CallbackPoolRegistry")
    batches = contract(read(escrow, "batches"), "BitcoinBatchRegistry")
    authorization = read(batches, "authorization")
    simple = read(admission, "simpleDeployer")
    confederation = read(admission, "confederationDeployer")
    if read(admission, "escrow") != escrow.address or read(admission, "authorization") != authorization:
        raise ValueError("BTC_CALLBACK_GRAPH_MISMATCH")
    prove_creation(w3, registry_address, "CallbackPoolRegistry",
                   [escrow.address, authorization, simple, confederation], block)

    escrow_v2 = any(item.get("name") == "defaultMinerFeeLimitSats" for item in escrow.abi)
    conf_kind = ("ConfederationPoolV2Deployer", "ConfederationPoolV2", "ConfederationShareV2") if escrow_v2 else (
        "MultiPoolDeployer", "ConfederationMultiPool", "ConfederationToken")
    origins = []
    for address, kind, pool_kind, share_kind, reciprocal in (
        (simple, "SimpleMultiPoolDeployer", "SimpleMultiPool", "MultiPoolShareToken", "multiPool"),
        (confederation, *conf_kind, "confederationPool"),
    ):
        if int(address, 16) == 0:
            continue
        origin = contract(address, kind)
        share_origin_address = read(origin, "shareTokenDeployer")
        arguments = ([read(origin, "frostDeployer"), share_origin_address]
                     if kind == "SimpleMultiPoolDeployer" else [share_origin_address])
        prove_creation(w3, address, kind, arguments, block)
        share_origin_kind = share_kind + "Deployer"
        share_origin = contract(share_origin_address, share_origin_kind)
        prove_creation(w3, share_origin_address, share_origin_kind, [], block)
        origins.append((origin, pool_kind, share_kind, reciprocal, share_origin))
    if not origins:
        raise ValueError("BTC_CALLBACK_ORIGIN_REQUIRED")
    if anchor is not None and w3.eth.get_block(block).hash != anchor.hash:
        raise ValueError("BTC_CALLBACK_CONFIGURATION_REORG")
    return admission, authorization, origins


def validate_callback(escrow, request, block=None):
    pool_address, request_id, mp_token, owner, amount, destination = request[:6]
    if int(pool_address, 16) == 0:
        return  # The typed treasury path has no external monetary callback.
    w3 = escrow.w3
    block = w3.eth.get_block("latest").number if block is None else block

    def contract(address, kind):
        checked_runtime(w3, address, kind, block)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    def read(candidate, method, *args):
        return getattr(candidate.functions, method)(*args).call(block_identifier=block)

    admission, authorization, origins = review_callback_configuration(
        escrow, read(escrow, "callbackPoolRegistry"), block)
    profiles = []
    for origin, pool_kind, share_kind, reciprocal, share_origin in origins:
        recorded = bytes(read(origin, "deployedPoolCodehash", pool_address))
        if recorded != bytes(32) and recorded == Web3.keccak(w3.eth.get_code(pool_address, block_identifier=block)):
            profiles.append((pool_kind, share_kind, reciprocal, share_origin))
    if len(profiles) != 1 or not read(escrow, "isCallbackPoolAdmitted", pool_address):
        raise ValueError("BTC_CALLBACK_NOT_ADMITTED")
    pool_kind, share_kind, reciprocal, share_origin = profiles[0]
    pool = contract(pool_address, pool_kind)
    if pool_kind in ("ConfederationMultiPool", "ConfederationPoolV2"):
        validator_address = read(pool, "custodianValidator")
        validator = contract(validator_address, "CustodianValidator")
        # Custodian membership is mutable through its own authorized protocol;
        # prove the original constructor without treating today's roster as
        # constructor arguments, and check its immutable authority separately.
        prove_creation(w3, validator_address, "CustodianValidator", None, block)
        if read(validator, "frostMultisig") != authorization:
            raise ValueError("BTC_CALLBACK_CUSTODIAN_MISMATCH")
    share_address = read(pool, "shareToken")
    share = contract(share_address, share_kind)
    if (read(pool, "getFrostMultisig") != authorization or read(pool, "withdrawalEscrow") != escrow.address
            or read(share, reciprocal) != pool_address
            or bytes(read(share_origin, "deployedTokenCodehash", share_address))
                != Web3.keccak(w3.eth.get_code(share_address, block_identifier=block))):
        raise ValueError("BTC_CALLBACK_GRAPH_MISMATCH")
    withdrawal = read(pool, "getWithdrawal", request_id)
    if (withdrawal[0] != owner or withdrawal[1] != mp_token or int(withdrawal[3]) != int(amount)
            or bytes(withdrawal[4]) != bytes(destination) or withdrawal[6]
            or read(pool, "withdrawalCancelled", request_id)
            or not read(pool, "withdrawalPrepared", request_id)
            or int(withdrawal[2]) < int(amount)
            or read(pool, "pendingUserPoolShares", owner, mp_token) < int(withdrawal[2])
            or read(pool, "escrowedMP", mp_token) < int(amount)):
        raise ValueError("BTC_CALLBACK_REQUEST_MISMATCH")
    # The registry independently checks the same canonical request at this block.
    read(admission, "validateWithdrawal", pool_address, request_id, mp_token, owner,
         amount, destination, True)
