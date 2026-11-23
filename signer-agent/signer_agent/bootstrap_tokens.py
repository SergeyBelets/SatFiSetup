"""Enrolled ERC20 display metadata; addresses and code still define asset identity."""
import re

ROLES = ("governance", "mining", "federation_share", "confederation_share")


def require_literal(value, code="BOOTSTRAP_LITERAL_REQUIRED"):
    if not isinstance(value, str) or value.startswith("$"):
        raise ValueError(code)


def token(value):
    if (not isinstance(value, dict) or set(value) != {"name", "symbol"}
            or not all(isinstance(v, str) for v in value.values())):
        raise ValueError("TOKEN_METADATA_SCHEMA")
    name, symbol = value["name"], value["symbol"]
    require_literal(name, "TOKEN_METADATA_VALUE")
    if (not name or name != name.strip() or not name.isprintable()
            or len(name.encode("utf-8")) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", symbol) is None):
        raise ValueError("TOKEN_METADATA_VALUE")
    return dict(value)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise ValueError("TOKEN_METADATA_SCHEMA")
    return {role: token(value[role]) for role in ROLES}


def resolve(profile):
    ulr = profile.get("ulr", {})
    if not isinstance(ulr, dict):
        raise ValueError("ULR_BOOTSTRAP_POLICY")
    legacy_overrides = {} if "onboarding" in profile or "token_metadata" in profile else profile
    defaults = dict(governance=dict(name="ULR", symbol="ULR") if "ulr" in profile else
        dict(name=legacy_overrides.get("governance_name", "Custodian Governance"),
             symbol=legacy_overrides.get("governance_symbol", "CGOV")),
        mining=dict(name="Integration MP", symbol="IMP"),
        federation_share=dict(name="Federation Shares", symbol="FS"),
        confederation_share=dict(name="Confederation Shares", symbol="CS"))
    shared = token(ulr["token"]) if "token" in ulr else None
    if shared is not None:
        defaults["governance"] = shared
    result = validate(profile["token_metadata"]) if "token_metadata" in profile else defaults
    if legacy_overrides:
        for value in result["governance"].values():
            require_literal(value, "TOKEN_METADATA_VALUE")
    if shared is not None and result["governance"] != shared:
        raise ValueError("TOKEN_METADATA_SHARED_CONFLICT")
    if "onboarding" in profile or "token_metadata" in profile:
        for field, key in (("governance_name", "name"), ("governance_symbol", "symbol")):
            if field in profile and profile[field] != result["governance"][key]:
                raise ValueError("TOKEN_METADATA_OVERRIDE")
    return result


def check(w3, address, role, profile, at):
    from .bootstrap import artifact
    expected = resolve(profile)[role]
    erc20 = w3.eth.contract(address=address, abi=artifact("IERC20Metadata")["abi"])
    actual = {key: getattr(erc20.functions, key)().call(block_identifier=at) for key in ("name", "symbol")}
    if actual != expected:
        raise ValueError("TOKEN_METADATA_MISMATCH: " + role)
    return actual


def review_intent(w3, intent, bundle, at):
    """Supplement normal provenance/authority review on every supported network."""
    from .bootstrap_formation_signing import review_intent as review_formation

    formation_reviewed = review_formation(w3, intent, bundle, at)
    invite = bundle.get("onboarding", {}).get("invitation", {})
    if "token_metadata" not in invite:
        return formation_reviewed
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    kind = intent["artifact"]
    def contract(address, kind):
        checked_runtime(w3, address, kind, at)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])
    def read(c, name, *args):
        return getattr(c.functions, name)(*args).call(block_identifier=at)
    def mining(address):
        if invite.get("ulr", {}).get("version") == "3":
            reserve = contract(address, "ConfederationBitcoinReserve")
            pool = contract(read(reserve, "confederationPool"), "ConfederationPoolV2")
            check(w3, read(pool, "shareToken"), "confederation_share", invite, at)
            return
        pool = contract(address, "MiningPoolV1")
        mp = contract(read(pool, "mpToken"), "PoolMpToken")
        if read(mp, "poolDAO") != pool.address:
            raise ValueError("TOKEN_METADATA_POOL_BINDING")
        check(w3, mp.address, "mining", invite, at)
    if kind in ("SimpleMultiPool", "ConfederationMultiPool", "ConfederationPoolV2"):
        if not formation_reviewed:
            target = contract(intent["target"], kind)
            role, child_kind = {
                "SimpleMultiPool": ("federation_share", "MultiPoolShareToken"),
                "ConfederationMultiPool": ("confederation_share", "ConfederationToken"),
                "ConfederationPoolV2": ("confederation_share", "ConfederationShareV2"),
            }[kind]
            shares = contract(read(target, "shareToken"), child_kind)
            check(w3, shares.address, role, invite, at)
        if intent.get("hash_method") == "getAddPoolOperationHash":
            mp = contract(intent["hash_args"][0], "PoolMpToken")
            mining(read(mp, "poolDAO"))
    elif kind == "ConfederationBitcoinReserve" and not formation_reviewed:
        mining(intent["target"])
    elif kind == "MiningPoolV1":
        mining(intent["target"])
    elif kind == "BitcoinMintAdmission":
        mining(read(contract(intent["target"], kind), "pool"))
    elif kind == "BitcoinDepositRegistry" and intent.get("hash_method") == "getRegisterOperationHash":
        mining(intent["hash_args"][0])
    elif kind == "GovernanceRegistry" and not formation_reviewed:
        registry = contract(intent["target"], kind)
        factory = contract(read(registry, "factory"), "FederationFactory")
        fed = read(factory, "getFederation", invite["federation_id"])
        shares = contract(fed[6], "MultiPoolShareToken")
        check(w3, shares.address, "federation_share", invite, at)
    elif kind == "PoolFeePipeline":
        pipeline = contract(intent["target"], kind)
        converter = contract(read(pipeline, "converter"), "FundedFeeConverter")
        mp = contract(read(converter, "inputToken"), "PoolMpToken")
        mining(read(mp, "poolDAO"))
    return formation_reviewed


def guard_signing_session(client, session):
    context = getattr(client, "token_metadata_context", None)
    if context is None:
        return
    intent, bundle = context
    if (session.message_hash.hex() != intent["operation_hash"].removeprefix("0x")
            or session.origin_contract.lower() != intent["target"].lower()):
        raise ValueError("TOKEN_METADATA_OPERATION_MISMATCH")
    from .evm_network import read_block
    invite = bundle["onboarding"]["invitation"]
    local_depth = getattr(getattr(client, "config", None), "confirmations", 1)
    anchor = read_block(client.w3, max(int(invite.get("evm_confirmations", "1")), int(local_depth)))
    review_intent(client.w3, intent, bundle, anchor.number)
    if client.w3.eth.get_block(anchor.number).hash != anchor.hash:
        raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
