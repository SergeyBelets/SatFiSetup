"""Review an enrolled formation at the same confirmed block as its FROST intent."""

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from web3 import Web3

from . import verified_cache
from .bitcoin_trust import checked_runtime, runtime_artifact
from .bootstrap import artifact
from .contract_provenance import birth_block


ZERO = "0x" + "00" * 20
FACTORIES = {"federation": "FederationFactory", "confederation": "ConfederationFactory"}
POOLS = {
    "SimpleMultiPool": "federation",
    "ConfederationMultiPool": "confederation",
    "ConfederationPoolV2": "confederation",
}


def _read(node, block, method, *args):
    return getattr(node.functions, method)(*args).call(block_identifier=block)


def _contract(w3, address, kind, block):
    if not isinstance(address, str) or not Web3.is_checksum_address(address) or address == ZERO:
        raise ValueError("BOOTSTRAP_FORMATION_TARGET")
    checked_runtime(w3, address, kind, block)
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def _origin_factory(w3, target, group, expected_id, member, block):
    """Find the factory that recorded this component's creation, at its birth height."""
    parameters = [target, group, expected_id, member]
    cached = verified_cache.get(w3, "formation-factory-origin", parameters)
    if cached:
        number = int(cached["number"])
        if number > block or w3.eth.get_block(number).hash.hex() != cached["hash"]:
            raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
        return _contract(w3, cached["factory"], FACTORIES[group], block)

    number = birth_block(w3, target, block)
    anchor = w3.eth.get_block(number)
    confederation = group == "confederation"
    fields = ["address"] * (4 if confederation else 3)
    event = ("ConfederationDeployed" if confederation else "FederationDeployed") + "(string," + ",".join(fields) + ")"
    member_index = 1 if member == "validator" or not confederation else 2
    topics = [Web3.keccak(text=event), Web3.keccak(text=expected_id)]
    logs = w3.eth.get_logs({"fromBlock": number, "toBlock": number,
                           "topics": [Web3.to_hex(topic) for topic in topics]})
    matches = []
    for log in logs:
        if log["blockNumber"] != number or bytes(log["blockHash"]) != bytes(anchor.hash):
            raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
        if [bytes(topic) for topic in log["topics"]] != topics:
            continue
        try:
            member_address = Web3.to_checksum_address(decode(fields, bytes(log["data"]))[member_index])
        except (DecodingError, ValueError, TypeError, IndexError, OverflowError):
            continue
        if member_address != target:
            continue
        try:
            factory = _contract(w3, log["address"], FACTORIES[group], block)
        except ValueError as exc:
            if str(exc) != "BTC_CONTRACT_BUILD_MISMATCH":
                raise
            continue
        matches.append(factory)
    if len(matches) != 1:
        raise ValueError("BOOTSTRAP_FORMATION_ORIGIN")
    if w3.eth.get_block(number).hash != anchor.hash:
        raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
    factory = matches[0]
    verified_cache.put(w3, "formation-factory-origin", parameters, {
        "number": str(number), "hash": anchor.hash.hex(), "factory": factory.address,
    })
    return factory


def _review_row(w3, factory, group, expected, bundle, intent, block, *, allow_reserved=False):
    from .bootstrap_formation import confederation_formation_hash, federation_formation_hash
    from .bootstrap_tokens import check

    title = group.capitalize()
    spec = expected[group]
    group_id = spec["id"]
    invite = bundle["onboarding"]["invitation"]
    if (_read(factory, block, "dkgCoordinator") != bundle["registry"]
            or _read(factory, block, "frostCoordinator") != intent["coordinator"]
            or _read(factory, block, "feeSetupAuthority") != invite["operator"]):
        raise ValueError("BOOTSTRAP_FORMATION_TARGET")
    reservation = "reserved" + title
    if not any(item.get("type") == "function" and item.get("name") == reservation + "Terms"
               for item in factory.abi):
        raise ValueError("BOOTSTRAP_FORMATION_RESERVATION")
    if (bytes(_read(factory, block, reservation + "Session", group_id)) != spec["session_id"]
            or tuple(map(bytes, _read(factory, block, reservation + "Terms", group_id)))
                != (spec["formation_hash"], spec["token_hash"])):
        raise ValueError("BOOTSTRAP_FORMATION_RESERVATION")

    adopted = bytes(_read(factory, block, "getDKGSession", group_id))
    unadopted = adopted == bytes(32)
    if adopted != spec["session_id"] and not (allow_reserved and unadopted):
        raise ValueError("BOOTSTRAP_FORMATION_MISMATCH")
    if group == "confederation":
        parent = _contract(w3, _read(factory, block, "federationFactory"), FACTORIES["federation"], block)
        federation = _review_row(w3, parent, "federation", expected, bundle, intent, block,
                                 allow_reserved=allow_reserved)
    if unadopted:
        return
    config = _read(factory, block, "get" + title, group_id)
    if group == "federation":
        if config[0] != group_id or federation_formation_hash(list(config[1]), int(config[2])) != spec["formation_hash"]:
            raise ValueError("BOOTSTRAP_FORMATION_MISMATCH")
        authorization, pool, share = config[4:7]
        pool_kind, share_kind = "SimpleMultiPool", "MultiPoolShareToken"
    else:
        members = list(_read(factory, block, "getCustodianMembers", group_id))
        if federation is None:
            raise ValueError("BOOTSTRAP_FORMATION_MISMATCH")
        founders = expected["federation"]["members"]
        if spec["mode"] != 1:
            founders = founders[:1]
        federation_members = [tuple(item) for item in _read(factory, block, "getFederationMembers", group_id)]
        if (config[0] != group_id or int(config[2]) != len(spec["federation_ids"]) or int(config[3]) != len(members)
                or confederation_formation_hash(spec["mode"], list(config[1]), members, int(config[4]), int(config[5]))
                    != spec["formation_hash"]
                or list(_read(factory, block, "getFounders", group_id)) != founders
                or federation_members != [(spec["federation_ids"][0], founders[0], federation["share"])]):
            raise ValueError("BOOTSTRAP_FORMATION_MISMATCH")
        authorization, validator, pool, share = config[7:11]
        native = invite.get("ulr", {}).get("version") == "3"
        pool_kind, share_kind = ("ConfederationPoolV2", "ConfederationShareV2") if native else (
            "ConfederationMultiPool", "ConfederationToken")
    if allow_reserved and authorization == pool == share == ZERO:
        return
    pool_contract = _contract(w3, pool, pool_kind, block)
    auth_contract = _contract(w3, authorization, "DKGAuthorization", block)
    share_contract = _contract(w3, share, share_kind, block)
    reciprocal = "multiPool" if group == "federation" else "confederationPool"
    if (_read(pool_contract, block, "shareToken") != share
            or _read(pool_contract, block, "getFrostMultisig") != authorization
            or _read(share_contract, block, reciprocal) != pool
            or _read(auth_contract, block, "registry") != bundle["registry"]
            or _read(auth_contract, block, "coordinator") != intent["coordinator"]
            or bytes(_read(auth_contract, block, "dkgId")) != spec["session_id"]):
        raise ValueError("BOOTSTRAP_FORMATION_TARGET")
    row = {"pool": pool, "share": share}
    if group == "confederation":
        if _read(pool_contract, block, "custodianValidator") != validator:
            raise ValueError("BOOTSTRAP_FORMATION_TARGET")
        row["validator"] = validator
    check(w3, share, group + "_share", invite, block)
    return row


def review_intent(w3, intent, bundle, block):
    """Use factory reservations and authenticated deployed children when the intent is formation-scoped."""
    from .bootstrap_formation import expected_formation

    invite = bundle.get("onboarding", {}).get("invitation", {})
    if invite.get("version") not in ("3", "4", "5", "6"):
        return False
    kind, method = intent["artifact"], intent.get("hash_method")
    target, args = intent["target"], intent.get("hash_args", [])
    group, factory, component, member = None, None, None, None
    early = False
    if kind in FACTORIES.values() or kind == "GovernanceRegistry":
        group = "confederation" if kind == "ConfederationFactory" else "federation"
        factory_address = (target if kind != "GovernanceRegistry" else
                           _read(_contract(w3, target, kind, block), block, "factory"))
        if not isinstance(args, list) or not args or args[0] != invite[group + "_id"]:
            raise ValueError("BOOTSTRAP_FORMATION_TARGET")
        factory = _contract(w3, factory_address, FACTORIES[group], block)
    elif kind in ("SPVServicePaymentRegistry", "ULRConfederationTreasury"):
        scoped = _contract(w3, target, kind, block)
        group = "confederation"
        if _read(scoped, block, "confederationId") != invite["confederation_id"]:
            raise ValueError("BOOTSTRAP_FORMATION_TARGET")
        factory_address = _read(scoped, block, "factory")
        if kind == "ULRConfederationTreasury":
            # Independent treasury instructions already authenticate their
            # own explicitly archived factory build and registered treasury.
            _, compiled = runtime_artifact(w3, factory_address, FACTORIES[group], block, legacy_ulr_factory=True)
            if compiled.get("legacy_reference") == "ConfederationFactoryULRLegacy2630366":
                return False
        factory = _contract(w3, factory_address, FACTORIES[group], block)
    elif kind == "CustodianDAOFeeRouter" and method == "getSetPayerOperationHash" and intent.get("role") == "evm_ceremony":
        if (not isinstance(args, list) or len(args) != 2 or args[1] is not True
                or not isinstance(args[0], str) or not Web3.is_checksum_address(args[0]) or args[0] == ZERO):
            return False
        for group, factory_kind in FACTORIES.items():
            try:
                factory = _contract(w3, args[0], factory_kind, block)
                break
            except ValueError as exc:
                if str(exc) != "BTC_CONTRACT_BUILD_MISMATCH":
                    raise
        if factory is None:
            return False
        early = True
    elif kind in POOLS:
        group, component, member = POOLS[kind], target, "pool"
    elif kind == "CustodianValidator":
        group, component, member = "confederation", target, "validator"
    elif kind == "ConfederationBitcoinReserve":
        scoped = _contract(w3, target, kind, block)
        group, component, member = "confederation", _read(scoped, block, "confederationPool"), "pool"
    elif kind == "BitcoinWithdrawalEscrowV2" and method == "getRequireLocalCustodyOperationHash":
        if not isinstance(args, list) or len(args) != 1:
            raise ValueError("BOOTSTRAP_FORMATION_TARGET")
        group, component, member = "confederation", args[0], "pool"
    if group is None:
        return False

    expected = expected_formation(invite)
    if factory is None:
        factory = _origin_factory(w3, component, group, expected[group]["id"], member, block)
    row = _review_row(w3, factory, group, expected, bundle, intent, block, allow_reserved=early)
    if member is not None and row[member] != component:
        raise ValueError("BOOTSTRAP_FORMATION_TARGET")
    return True
