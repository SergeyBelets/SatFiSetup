"""Enrolled custody-only monetary epochs and immutable source handover graphs."""
import re
from web3 import Web3

SOURCE_FIELDS = ("escrow", "token", "escrow_hash", "token_hash", "mining_hash", "deposit_hash", "batch_hash")


def validate(value, participants=None, min_custodians=None):
    if (not isinstance(value, dict) or set(value) != {"version", "mode", "threshold", "sources"}
            or value["version"] != "1" or value["mode"] != "custodians"
            or not isinstance(value["threshold"], str)
            or re.fullmatch(r"[1-9][0-9]*", value["threshold"]) is None
            or not 2 <= int(value["threshold"]) <= 15
            or not isinstance(value["sources"], list) or len(value["sources"]) > 4):
        raise ValueError("BITCOIN_CUSTODY_POLICY")
    seen = set()
    for source in value["sources"]:
        if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS):
            raise ValueError("BITCOIN_CUSTODY_SOURCE")
        for field in SOURCE_FIELDS:
            item = source[field]
            if field in ("escrow", "token"):
                if not isinstance(item, str) or not Web3.is_checksum_address(item) or int(item, 16) == 0:
                    raise ValueError("BITCOIN_CUSTODY_SOURCE")
            elif not isinstance(item, str) or re.fullmatch(r"0x[0-9a-f]{64}", item) is None or int(item, 16) == 0:
                raise ValueError("BITCOIN_CUSTODY_SOURCE")
        if source["token"] in seen:
            raise ValueError("BITCOIN_CUSTODY_DUPLICATE_SOURCE")
        seen.add(source["token"])
    if participants is not None:
        count = len(participants) - 1
        threshold = int(value["threshold"])
        if (count < 2 or not max(2, (7 * count + 9) // 10) <= threshold <= count
                or min_custodians is not None and not threshold <= int(min_custodians) <= count):
            raise ValueError("BITCOIN_CUSTODY_THRESHOLD")
    return value


def role_inputs(invite, role):
    if "bitcoin_custody" in invite and role in ("conf_ceremony", "btc_ceremony"):
        return invite["participants"][1:], invite["bitcoin_custody"]["threshold"]
    return invite["participants"], invite["threshold"]


def sources_args(policy):
    return [[source[field] for field in SOURCE_FIELDS] for source in policy["sources"]]


def extend(profile, deploy, approve):
    policy = profile.get("bitcoin_custody")
    if policy is None:
        return
    from .bootstrap_confederation_dao import enabled
    deploy("custody_transfer", "BitcoinCustodyTransferV2" if enabled(profile) else "BitcoinCustodyTransfer",
           "$mining", "$conf_pool", sources_args(policy))
    approve("bind_custody_transfer", "mining", "getSetCustodyTransferOperationHash", ["$custody_transfer"],
            "setCustodyTransfer", ["$custody_transfer"])


def review_publishers(w3, bundle, at, coordinator=None):
    """The representative cannot remain the only public-intent publisher."""
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    from .dkg_protocol import Manifest
    invite = bundle["onboarding"]["invitation"]
    address = bundle.get("onchain_board")
    if not address:
        raise ValueError("BITCOIN_CUSTODY_PUBLISHER_EPOCH")
    checked_runtime(w3, address, "DKGOnchainBoard", at)
    board = w3.eth.contract(address=address, abi=artifact("DKGOnchainBoard")["abi"])
    if (bytes(board.functions.custodyPublisherEpoch().call(block_identifier=at)).hex()
            != invite["sessions"]["conf_ceremony"]
            or board.functions.registry().call(block_identifier=at) != Manifest.from_dict(bundle).registry
            or coordinator is not None and board.functions.intentCoordinator().call(block_identifier=at) != coordinator):
        raise ValueError("BITCOIN_CUSTODY_PUBLISHER_EPOCH")


def review_intent(w3, intent, bundle, at):
    """Additional immutable transfer/source checks on all networks before signing."""
    invite = bundle.get("onboarding", {}).get("invitation", {})
    if "bitcoin_custody" not in invite:
        return
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation
    policy = validate(invite["bitcoin_custody"], invite["participants"], invite["min_custodians"])
    def contract(address, kind):
        checked_runtime(w3, address, kind, at)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])
    def read(c, getter, *args):
        return getattr(c.functions, getter)(*args).call(block_identifier=at)
    from .bootstrap_confederation_dao import enabled
    in_kind = enabled(invite)
    if in_kind:
        from .bootstrap_native_s import review
        return review(w3, intent, bundle, at)
    pool_kind = "ConfederationPoolV2" if in_kind else "ConfederationMultiPool"
    escrow_kind = "BitcoinWithdrawalEscrowV2" if in_kind else "BitcoinWithdrawalEscrow"
    kind, method = intent.get("artifact"), intent.get("hash_method")
    initial_binding = in_kind and (kind, method) in {
        ("BitcoinBatchRegistry", "getSetWithdrawalEscrowOperationHash"),
        (escrow_kind, "getSetDepositRegistryOperationHash"),
        (pool_kind, "getSetWithdrawalEscrowOperationHash"),
    }
    critical = (initial_binding
        or (kind == "MiningPoolV1" and method in ("getSetCustodyTransferOperationHash", "getSetMintAdmissionOperationHash"))
        or (kind == pool_kind and method == "getAddPoolOperationHash")
        or (kind == escrow_kind and method == "getRequireLocalCustodyOperationHash"))
    if critical:
        review_publishers(w3, bundle, at, intent["coordinator"])
        target = contract(intent["target"], kind)
        if kind == pool_kind:
            authority = read(target, "frostMultisig")
        elif kind == escrow_kind:
            authority = read(contract(read(target, "batches"), "BitcoinBatchRegistry"), "authorization")
        else:
            authority = read(target, "authorization")
        auth = contract(authority, "DKGAuthorization")
        from .dkg_protocol import Manifest
        manifest = Manifest.from_dict(bundle)
        if (bundle.get("role") != "conf_ceremony"
                or manifest.session_id.hex() != invite["sessions"]["conf_ceremony"]
                or list(manifest.participants) != invite["participants"][1:]
                or manifest.threshold != int(policy["threshold"])
                or list(read(auth, "getParticipants")) != invite["participants"][1:]
                or int(read(auth, "threshold")) != int(policy["threshold"])
                or bytes(read(auth, "dkgId")).hex() != invite["sessions"]["conf_ceremony"]
                or read(auth, "registry") != manifest.registry
                or read(auth, "coordinator") != intent["coordinator"]):
            raise ValueError("BITCOIN_CUSTODY_AUTHORIZATION")
        completed = read(contract(manifest.registry, "DKGCoordinator"), "getCompletedResult", bytes.fromhex(invite["sessions"]["conf_ceremony"]))
        prove_creation(w3, auth.address, "DKGAuthorization", [intent["coordinator"],
            bytes.fromhex(invite["sessions"]["conf_ceremony"]), bytes(completed[4]), invite["participants"][1:], int(policy["threshold"])], at)
        # Pin the complete destination graph on test networks as well as mainnet.
        # Genuine runtime/getters alone do not establish constructor storage or
        # the enrolled SPV backend and Bitcoin wallet epoch.
        local_mining = target if kind == "MiningPoolV1" else None
        if initial_binding and (not isinstance(intent.get("hash_args"), list) or len(intent["hash_args"]) != 1):
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        if kind == pool_kind:
            from .ethereum_intents import _prove_pool
            _prove_pool(w3, target, kind, invite["operator"], at)
            if in_kind and read(target, "confederationId") != invite["confederation_id"]:
                raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")
            if not initial_binding:
                token = contract(intent["hash_args"][0], "PoolMpToken")
                local_mining = contract(read(token, "poolDAO"), "MiningPoolV1")
        if kind == escrow_kind:
            escrow = target
        elif initial_binding:
            escrow = contract(intent["hash_args"][0], escrow_kind)
        else:
            escrow = contract(read(local_mining, "escrow"), escrow_kind)
        batches = contract(read(escrow, "batches"), "BitcoinBatchRegistry")
        if initial_binding:
            if kind == "BitcoinBatchRegistry" and target.address != batches.address:
                raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
            if kind != "BitcoinBatchRegistry" and read(batches, "withdrawalEscrow") != escrow.address:
                raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        expected = [(batches, "BitcoinBatchRegistry", [auth.address, manifest.registry,
                bytes.fromhex(invite["sessions"]["btc_ceremony"])]),
            (escrow, escrow_kind, [batches.address, read(escrow, "spv"),
                int(invite["bitcoin_confirmations"]), int(invite["min_p2wpkh_sats"]), int(invite["min_p2tr_sats"])]
                + ([int(invite["ulr"]["default_miner_fee_sats"])] if in_kind else []))]
        if not initial_binding or kind != "BitcoinBatchRegistry":
            deposits_address = (intent["hash_args"][0] if initial_binding and kind == escrow_kind
                                else read(escrow, "depositRegistry"))
            deposits = contract(deposits_address, "BitcoinDepositRegistry")
            expected.append((deposits, "BitcoinDepositRegistry", [read(escrow, "spv"), auth.address,
                int(invite["bitcoin_confirmations"]), escrow.address]))
        if local_mining is not None:
            from .bootstrap_tokens import resolve
            names = resolve(invite)["mining"]
            expected.append((local_mining, "MiningPoolV1", ["integration-pool", deposits.address,
                escrow.address, names["name"], names["symbol"]]))
        for node, node_kind, arguments in expected:
            evidence = prove_creation(w3, node.address, node_kind, arguments, at)
            if evidence.get("method") != "direct" or evidence.get("sender") != invite["operator"]:
                raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        from .zk_spv import review_consumer
        review_consumer(w3, invite, escrow, escrow_kind, intent, at)
    if kind == "MiningPoolV1" and method == "getSetCustodyTransferOperationHash":
        mining = contract(intent["target"], kind)
        gateway = contract(intent["hash_args"][0], "BitcoinCustodyTransfer")
        pool = contract(read(gateway, "pool"), pool_kind)
        escrow = contract(read(mining, "escrow"), escrow_kind)
        from .bootstrap import encode_args
        constructor = next(x for x in artifact("BitcoinCustodyTransfer")["abi"] if x["type"] == "constructor")
        arguments = encode_args(constructor["inputs"], [mining.address, pool.address, sources_args(policy)])
        proof = prove_creation(w3, gateway.address, "BitcoinCustodyTransfer", arguments, at)
        if (proof.get("method") != "direct" or proof.get("sender") != invite["operator"]
                or read(gateway, "mining") != mining.address or not read(escrow, "localCustodyPools", pool.address)
                or read(pool, "withdrawalEscrow") != read(mining, "escrow")
                or read(pool, "frostMultisig") != read(mining, "authorization")):
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        from .ethereum_intents import _prove_pool
        _prove_pool(w3, pool, pool_kind, invite["operator"], at)
        if read(pool, "confederationId") != invite["confederation_id"]:
            raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")
        if read(gateway, "sourceCount") != len(policy["sources"]):
            raise ValueError("BITCOIN_CUSTODY_SOURCE")
        for index, source in enumerate(policy["sources"]):
            if list(read(gateway, "sourceAt", index)) != [source[field] if field in ("escrow", "token")
                    else bytes.fromhex(source[field][2:]) for field in SOURCE_FIELDS]:
                raise ValueError("BITCOIN_CUSTODY_SOURCE")
            # Hashes are committed before enrollment. Only this build's genuine
            # non-proxy source contracts are supported; arbitrary legacy ABIs fail.
            escrow = contract(source["escrow"], "BitcoinWithdrawalEscrow")
            token = contract(source["token"], "PoolMpToken")
            origin = contract(read(token, "poolDAO"), "MiningPoolV1")
            nodes = ((escrow, "escrow_hash"), (token, "token_hash"), (origin, "mining_hash"),
                     (contract(read(origin, "deposits"), "BitcoinDepositRegistry"), "deposit_hash"),
                     (contract(read(escrow, "batches"), "BitcoinBatchRegistry"), "batch_hash"))
            for node, field in nodes:
                code = w3.eth.get_code(node.address, block_identifier=at)
                if Web3.to_hex(Web3.keccak(code)) != source[field]:
                    raise ValueError("BITCOIN_CUSTODY_SOURCE_CODE")
            if (read(origin, "mpToken") != token.address or read(origin, "escrow") != escrow.address
                    or read(origin, "deposits") != read(escrow, "depositRegistry")):
                raise ValueError("BITCOIN_CUSTODY_SOURCE_GRAPH")
    elif kind == "MiningPoolV1" and method == "getSetMintAdmissionOperationHash":
        from .mint_admission import configuration
        configuration(w3, intent["hash_args"][0], at, expected_pool=intent["target"],
                      expected_operator=invite["participants"][1], require_bound=False)
    elif kind == pool_kind and method == "getAddPoolOperationHash":
        pool = contract(intent["target"], kind)
        escrow = contract(read(pool, "withdrawalEscrow"), escrow_kind)
        token = contract(intent["hash_args"][0], "PoolMpToken")
        mining = contract(read(token, "poolDAO"), "MiningPoolV1")
        if (not read(escrow, "localCustodyPools", pool.address) or read(pool, "withdrawalEscrow") != read(mining, "escrow")
                or read(mining, "mpToken") != token.address):
            raise ValueError("BITCOIN_CUSTODY_FOREIGN_MP")
    elif kind == escrow_kind and method == "getRequireLocalCustodyOperationHash":
        escrow = contract(intent["target"], kind)
        pool = contract(intent["hash_args"][0], pool_kind)
        from .ethereum_intents import _prove_pool
        _prove_pool(w3, pool, pool_kind, invite["operator"], at)
        batches = contract(read(escrow, "batches"), "BitcoinBatchRegistry")
        if (read(pool, "withdrawalEscrow") != escrow.address
                or read(pool, "frostMultisig") != read(batches, "authorization")
                or read(pool, "confederationId") != invite["confederation_id"]):
            raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")


def guard_signing_session(client, session):
    context = getattr(client, "custody_context", None)
    if not context:
        return
    intent, bundle, confirmations = context
    from .evm_network import read_block
    block = read_block(client.w3, int(confirmations))
    expected = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
    if session.message_hash != expected or session.origin_contract.lower() != intent["target"].lower():
        raise ValueError("BITCOIN_CUSTODY_SESSION")
    review_intent(client.w3, intent, bundle, block.number)
    if client.w3.eth.get_block(block.number)["hash"] != block.hash:
        raise ValueError("BITCOIN_CUSTODY_REORG")
