"""Participant-local, explicit trust anchor for a completed Bitcoin wallet's graph."""

import hashlib
import json
from pathlib import Path

from eth_abi import encode
from web3 import Web3

from .bootstrap import artifact
from .operations import canonical
from .bitcoin_network import is_public_network, relay_artifact


def checked_runtime(w3, address, kind, block):
    """Compare compiled instructions and require consistent immutable substitutions."""
    return runtime_artifact(w3, address, kind, block)[0]


def runtime_artifact(w3, address, kind, block):
    """Identify the exact reviewed build, including supported immutable ULR deployments."""
    compiled = artifact(kind)
    actual = bytes(w3.eth.get_code(address, block_identifier=block))
    try:
        _check_runtime(actual, compiled["deployedBytecode"])
    except ValueError as error:
        if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
            raise
        references = {
            "ULRConfederationTreasury": ("ULRConfederationTreasuryV1", "ULRConfederationTreasuryLegacy"),
            "ULRCustodianSnapshots": ("ULRCustodianSnapshotsLegacy",),
        }
        for name in references.get(kind, ()):
            path = Path(__file__).resolve().parents[1] / "contracts" / (name + ".json")
            legacy = json.loads(path.read_text())
            try:
                _check_runtime(actual, legacy["deployedBytecode"])
            except ValueError as mismatch:
                if str(mismatch) != "BTC_CONTRACT_BUILD_MISMATCH":
                    raise
                continue
            compiled = dict(legacy, abi=compiled["abi"], legacy_reference=name)
            break
        else:
            raise
    return Web3.keccak(actual).hex(), compiled


def _check_runtime(actual, compiled):
    expected = bytearray.fromhex(compiled["object"].removeprefix("0x"))
    masked = bytearray(actual)
    if not actual or len(actual) != len(expected):
        raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
    for positions in compiled.get("immutableReferences", {}).values():
        values = set()
        for position in positions:
            start, size = position["start"], position["length"]
            if start < 0 or size != 32 or start + size > len(actual):
                raise ValueError("BTC_ARTIFACT_IMMUTABLE_LAYOUT")
            values.add(actual[start : start + size])
            masked[start : start + size] = bytes(size)
            expected[start : start + size] = bytes(size)
        if len(values) != 1:
            raise ValueError("BTC_CONTRACT_IMMUTABLE_MISMATCH")
    if masked != expected:
        raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")


def inspect_registry(manifest, key, registry, batch_address, *, prove=True,
                     bitcoin_network="regtest", bitcoin_policy=None, evm_confirmations="1", mint_only=False,
                     address_signing=False):
    """Read one block, validate the graph, and return public review data only."""
    if manifest.purpose != "BTC_SPEND":
        raise ValueError("WRONG_KEY_PURPOSE")
    if mint_only and address_signing: raise ValueError("BTC_REGISTRY_PROFILE_CONFLICT")
    if bitcoin_network not in ("regtest", "mainnet", "testnet4"):
        raise ValueError("BITCOIN_NETWORK_UNSUPPORTED")
    from .zk_spv import is_zk, inspect_binding
    zk = is_zk(bitcoin_policy)
    zk_provenance = {}
    if is_public_network(bitcoin_network):
        from .bitcoin_network import validate_policy

        bitcoin_policy = validate_policy(bitcoin_policy, bitcoin_network)
        if (not isinstance(evm_confirmations, str) or not evm_confirmations.isdecimal()
                or str(int(evm_confirmations)) != evm_confirmations
                or not 1 <= int(evm_confirmations) <= 256):
            raise ValueError("EVM_CONFIRMATIONS_INVALID")
    elif bitcoin_policy is not None:
        raise ValueError("REGTEST_POLICY_FORBIDDEN")
    w3 = registry.w3
    if w3.eth.chain_id != manifest.chain_id:
        raise ValueError("CHAIN_MISMATCH")
    block = (confirmed_block(w3, dict(evm_confirmations=evm_confirmations))
             if is_public_network(bitcoin_network) or w3.eth.chain_id == 1 or evm_confirmations != "1"
             else w3.eth.get_block("latest"))
    contracts = {}

    def contract(name, address, kind):
        address = Web3.to_checksum_address(address)
        code_hash = checked_runtime(w3, address, kind, block.number)
        contracts[name] = dict(address=address, artifact=kind, runtime_hash=code_hash)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    def read(c, method, *args):
        return getattr(c.functions, method)(*args).call(block_identifier=block.number)

    dkg = contract("dkg", manifest.registry, "DKGCoordinator")
    wallet = read(dkg, "getCompletedResult", manifest.session_id)
    if (
        bytes(wallet[0]) != manifest.context
        or bytes(wallet[1]).hex() != key["result_hash"]
        or bytes(wallet[3]) != Web3.keccak(text="BTC_SPEND")
        or bytes(wallet[4]).hex() != key["result"]["group"][2:]
        or list(wallet[5]) != list(manifest.participants)
        or int(wallet[6]) != manifest.threshold
    ):
        raise ValueError("BTC_WALLET_RESULT_MISMATCH")
    batches = contract("batches", batch_address, "BitcoinBatchRegistry")
    if (
        read(batches, "dkg") != manifest.registry
        or bytes(read(batches, "walletId")) != manifest.session_id
        or bytes(read(batches, "walletResult")).hex() != key["result_hash"]
    ):
        raise ValueError("WALLET_REGISTRY_MISMATCH")
    auth = contract("authorization", read(batches, "authorization"), "DKGAuthorization")
    coordinator = contract("coordinator", read(auth, "coordinator"), "FROSTCoordinator")
    verifier = contract(
        "verifier", read(coordinator, "defaultVerifier"), "FROSTVerifier"
    )
    evm_id = bytes(read(auth, "dkgId"))
    evm = read(dkg, "getCompletedResult", evm_id)
    if (
        read(auth, "registry") != manifest.registry
        or read(coordinator, "dkgRegistry") != manifest.registry
        or read(auth, "verifier") != verifier.address
        or bytes(evm[3]) != Web3.keccak(text="EVM_AUTH")
        or bytes(read(auth, "resultHash")) != bytes(evm[1])
        or bytes(read(auth, "groupPubkey")) != bytes(evm[4])
        or bytes(read(auth, "groupPubkeyHash")) != Web3.keccak(bytes(evm[4]))
        or list(evm[5]) != list(manifest.participants)
        or list(read(auth, "getParticipants")) != list(manifest.participants)
        or bytes(read(auth, "participantsHash"))
        != Web3.keccak(encode(["address[]"], [manifest.participants]))
        or int(evm[6]) != manifest.threshold
        or int(read(auth, "threshold")) != manifest.threshold
    ):
        raise ValueError("BTC_AUTHORIZATION_GRAPH_MISMATCH")
    escrow_address = read(batches, "withdrawalEscrow")
    try:
        escrow = contract("escrow", escrow_address, "BitcoinWithdrawalEscrow")
    except ValueError as error:
        if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
            raise
        escrow = contract("escrow", escrow_address, "BitcoinWithdrawalEscrowV2")
    user_funded = contracts["escrow"]["artifact"] == "BitcoinWithdrawalEscrowV2"
    deposits = contract(
        "deposits", read(escrow, "depositRegistry"), "BitcoinDepositRegistry"
    )
    # Generation-aware plans/journal recovery are a separate profile. Do not
    # enroll the new graph in a legacy signer that cannot enforce its incidents.
    address_registry = read(deposits, "addressRegistry")
    if int(address_registry, 16) != 0 and not (mint_only or address_signing):
        raise ValueError("BTC_ADDRESS_SIGNING_PROFILE_REQUIRED")
    if (mint_only or address_signing) and int(address_registry, 16) == 0:
        raise ValueError("BTC_ADDRESS_REGISTRY_REQUIRED")
    if zk:
        adapter_address = read(escrow, "spv")
        from .zk_profiles import profile as zk_profile
        guard = contract("spv_guard", adapter_address, zk_profile(bitcoin_policy)["adapter"])
        relay, guard, records, zk_provenance = inspect_binding(w3, read(guard, "state"), bitcoin_policy,
            block.number, adapter_address=adapter_address, prove=prove)
        contracts.update(records)
        spv = guard.address
    elif is_public_network(bitcoin_network):
        guard = contract("spv_guard", read(escrow, "spv"), "MainnetSPVGuard")
        relay = contract("relay", read(guard, "relay"), relay_artifact(bitcoin_network))
        from .bitcoin_network import header_hash

        if (read(guard, "maxTipAgeSeconds") != int(bitcoin_policy["max_tip_age_seconds"])
                or read(guard, "testHelpersEnabled")
                or bytes(read(relay, "checkpointHash")) != header_hash(bytes.fromhex(bitcoin_policy["checkpoint_header"]))
                or read(relay, "checkpointHeight") != int(bitcoin_policy["checkpoint_height"])
                or read(relay, "checkpointChainWork") != int(bitcoin_policy["checkpoint_chainwork"], 16)
                or read(relay, "maxReorgDepth") != int(bitcoin_policy["max_reorg_depth"])):
            raise ValueError("MAINNET_RELAY_POLICY_MISMATCH")
        spv = guard.address
    else:
        relay = contract("relay", read(escrow, "spv"), "RegtestHeaderRelay")
        spv = relay.address
    withdrawal_policy = dict(
        min_p2wpkh_sats=str(read(escrow, "minP2WPKHSats")),
        min_p2tr_sats=str(read(escrow, "minP2TRSats")),
    )
    if user_funded:
        withdrawal_policy["default_miner_fee_sats"] = str(read(escrow, "defaultMinerFeeLimitSats"))
    if (
        read(escrow, "batches") != batches.address
        or read(deposits, "spendRecorder") != escrow.address
        or read(deposits, "authorization") != auth.address
        or read(deposits, "spv") != spv
        or int(read(escrow, "depth")) < 6
        or int(read(deposits, "depth")) < 6
        or any(int(value) <= 0 for value in withdrawal_policy.values())
        or (not zk and read(relay, "testHelpersEnabled"))
    ):
        raise ValueError("BTC_ESCROW_GRAPH_MISMATCH")
    graph = dict(
        version="1",
        chain_id=str(manifest.chain_id),
        wallet_context=manifest.context.hex(),
        wallet_result=key["result_hash"],
        evm_dkg_id=evm_id.hex(),
        evm_result=bytes(evm[1]).hex(),
        participants=list(manifest.participants),
        threshold=str(manifest.threshold),
        withdrawal_policy=withdrawal_policy,
        bitcoin_confirmations=dict(
            withdrawal=str(read(escrow, "depth")),
            deposit=str(read(deposits, "depth")),
        ),
        contracts=contracts,
    )
    if is_public_network(bitcoin_network) or w3.eth.chain_id == 1 or evm_confirmations != "1":
        graph.update(version="2", bitcoin_network=bitcoin_network, evm_confirmations=evm_confirmations)
        if is_public_network(bitcoin_network):
            graph["bitcoin_policy"] = bitcoin_policy
    address_arguments = None
    if mint_only or address_signing:
        from .address_mint import registry_identity
        node, identity, address_arguments, _ = registry_identity(w3, address_registry, deposits.address,
            auth.address, escrow.address, batches.address, dkg.address, block.number)
        contracts["address_registry"] = dict(address=node.address, artifact="BitcoinAddressRegistry",
            runtime_hash=checked_runtime(w3, node.address, "BitcoinAddressRegistry", block.number))
        graph.update(usage="BTC_INPUT_KEYS_V1" if address_signing else "MINT_ONLY_V1", address_mint=identity)
        if bitcoin_policy is not None and bytes(read(node, "bitcoinGenesis")).hex() != bitcoin_policy["genesis_hash"]:
            raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
    fee_arguments = {}
    fee_address = read(batches, "feeReserve")
    if int(fee_address,16) and not mint_only:
        if not address_signing: raise ValueError("BTC_FEE_ADDRESS_PROFILE_REQUIRED")
        fee=contract("fee_reserve",fee_address,"BitcoinFeeReserve")
        fb=contract("fee_board",read(fee,"signingBoard"),"BitcoinSigningBoard")
        donor=contract("fee_donor",read(fee,"donorVerifier"),"FROSTVerifier")
        funding=contract("fee_funding",read(fee,"fundingVerifier"),"BitcoinFeeFundingVerifier")
        fp=contract("fee_plan",read(fee,"planVerifier"),"BitcoinFeePlanVerifier")
        um=contract('fee_user_module',read(fee,'userModule'),'BitcoinFeeReserveUser')
        if read(um,'reserve')!=fee.address:raise ValueError('BTC_FEE_GRAPH_MISMATCH')
        if (read(fee,"addresses")!=address_registry or read(fee,"deposits")!=deposits.address
                or read(fee,"batches")!=batches.address or read(fee,"escrow")!=escrow.address
                or read(fee,"authorization")!=auth.address or read(fb,"batchRegistry")!=batches.address
                or read(fb,"dkg")!=dkg.address or bytes(read(fb,"walletId"))!=manifest.session_id):
            raise ValueError("BTC_FEE_GRAPH_MISMATCH")
        fee_arguments=dict(fee_reserve=[address_registry,fb.address,donor.address,funding.address,fp.address,
            int(read(fee,"protocolFeeCap")),int(read(fee,"maxVbytes"))],fee_board=[batches.address,int(read(fb,"maxPacketBytes"))],
            fee_donor=[],fee_funding=[],fee_plan=[],fee_user_module=[fee.address])
        graph['btc_fee_reserve']=dict(scope=bytes(read(fee,"scope")).hex(),protocol_fee_cap=str(read(fee,"protocolFeeCap")),max_vbytes=str(read(fee,"maxVbytes")))
        from .withdrawal_fee import configuration as personal_configuration
        personal=personal_configuration(w3,dict(registry=node,common=deposits,batch=batches,escrow=escrow),block.number,prove=False)
        if personal:
            contracts.update(personal['nodes']);fee_arguments.update(personal['arguments'])
            graph['withdrawal_fees']=dict(scope=personal['scope'],profile='WITHDRAWAL_FEE_V1',plan_version='4',allocation='EXACT_OWNER_PER_VERSION_V1')
    provenance = None
    if prove:
        from .contract_provenance import prove_creation

        arguments = dict(
            dkg=[],
            verifier=[],
            relay=[],
            coordinator=[verifier.address],
            batches=[auth.address, dkg.address, manifest.session_id],
            authorization=[
                coordinator.address,
                evm_id,
                bytes(evm[4]),
                list(evm[5]),
                int(evm[6]),
            ],
            escrow=[
                batches.address, spv, int(read(escrow, "depth")),
                int(withdrawal_policy["min_p2wpkh_sats"]),
                int(withdrawal_policy["min_p2tr_sats"]),
            ],
            deposits=[
                spv,
                auth.address,
                int(read(deposits, "depth")),
                escrow.address,
            ],
        )
        if user_funded:
            arguments["escrow"].append(int(withdrawal_policy["default_miner_fee_sats"]))
        if is_public_network(bitcoin_network) and not zk:
            from .bitcoin_network import relay_constructor_args

            arguments["relay"] = relay_constructor_args(bitcoin_policy)
            arguments["spv_guard"] = [relay.address, int(bitcoin_policy["max_tip_age_seconds"])]
        if mint_only or address_signing:
            arguments["address_registry"] = address_arguments
        arguments.update(fee_arguments)
        provenance = {
            name: prove_creation(
                w3, item["address"], item["artifact"], arguments[name], block.number
            )
            for name, item in contracts.items() if not zk or name not in zk_provenance
        }
        if zk:
            provenance.update(zk_provenance)
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return dict(
        graph=graph,
        fingerprint=hashlib.sha256(
            canonical(dict(graph=graph, provenance=provenance)).encode()
        ).hexdigest(),
        provenance=provenance,
        anchor=dict(number=str(block.number), hash=block.hash.hex()),
    )


def discover_registry(manifest, key, registry, board_address, *, address_signing=False):
    """Find the uniquely approved binding from the board, without an external file."""
    from .onchain_transport import invitation

    w3 = registry.w3
    board, invite = invitation(w3, board_address)
    if (
        board.functions.registry().call() != manifest.registry
        or invite["sessions"]["btc_ceremony"] != manifest.session_id.hex()
    ):
        raise ValueError("BTC_BOARD_WALLET_MISMATCH")
    latest = confirmed_block(w3, dict(evm_confirmations=invite.get("evm_confirmations", "1")))
    from .board_intents import intent_rows
    candidates = set()
    for row in intent_rows(w3, board, latest):
        intent = row["intent"]
        if (
            intent.get("artifact") != "BitcoinBatchRegistry"
            or intent.get("hash_method") != "getSetWithdrawalEscrowOperationHash"
        ):
            continue
        if intent.get("chain_id") != str(manifest.chain_id):
            raise ValueError("BTC_BOARD_INTENT_MISMATCH")
        options = invitation_options(invite)
        if address_signing: options["address_signing"] = True
        reviewed = inspect_registry(manifest, key, registry, intent["target"], **options)
        graph = reviewed["graph"]
        expected_policy = {name: invite[name] for name in ("min_p2wpkh_sats", "min_p2tr_sats")}
        if invite.get("ulr", {}).get("version") == "3":
            expected_policy["default_miner_fee_sats"] = invite["ulr"]["default_miner_fee_sats"]
        if invite["version"] in ("3", "4", "5", "6") and graph["withdrawal_policy"] != expected_policy:
            raise ValueError("BTC_BOARD_WITHDRAWAL_POLICY_MISMATCH")
        if invite["version"] in ("3", "4", "5", "6") and any(
            value != invite["bitcoin_confirmations"]
            for value in graph["bitcoin_confirmations"].values()
        ):
            raise ValueError("BTC_BOARD_CONFIRMATIONS_MISMATCH")
        if graph["evm_dkg_id"] != invite["sessions"]["conf_ceremony"] or intent.get(
            "hash_args"
        ) != [graph["contracts"]["escrow"]["address"]]:
            raise ValueError("BTC_BOARD_INTENT_MISMATCH")
        auth = w3.eth.contract(
            address=graph["contracts"]["authorization"]["address"],
            abi=artifact("DKGAuthorization")["abi"],
        )
        op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
        if (
            auth.functions.operationTargets(op).call()
            != graph["contracts"]["batches"]["address"]
            or not auth.functions.executedOperations(op).call()
        ):
            raise ValueError("BTC_BOARD_BINDING_NOT_APPROVED")
        candidates.add(graph["contracts"]["batches"]["address"])
    if w3.eth.get_block(latest.number).hash != latest.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    if len(candidates) != 1:
        raise ValueError("BTC_DISCOVERY_REQUIRES_ONE_APPROVED_REGISTRY")
    return candidates.pop()


def pin_registry(manifest, key, registry, state, address, fingerprint, *, bitcoin=None, **options):
    inspected = inspect_registry(manifest, key, registry, address, **options)
    if not fingerprint or inspected["fingerprint"] != fingerprint:
        raise ValueError("BTC_REGISTRY_FINGERPRINT_MISMATCH")
    check_bitcoin_graph(registry, inspected["graph"], bitcoin)
    if inspected['graph'].get('btc_fee_reserve'):
        from .btc_fee_reserve import configured
        fee=configured(registry,inspected['graph']['contracts']['fee_reserve']['address'],bitcoin=bitcoin)
        fee.context()
    name = ("btc-mint-registry:" if options.get("mint_only") else "btc-registry:") + manifest.context.hex()
    with state.exclusive():
        from .bitcoin_continuity import trust_guard
        trust_guard(manifest,registry,state,inspected['graph'])
        old = state.get(name)
        if old and old["graph"] != inspected["graph"]:
            raise ValueError("BTC_REGISTRY_ALREADY_PINNED")
        if old:
            check_anchor(registry.w3, old)
        if not old or not old.get("provenance"):
            state.put(name, inspected)
    return dict(
        status="BTC_REGISTRY_PINNED",
        fingerprint=inspected["fingerprint"],
        batch_registry=inspected["graph"]["contracts"]["batches"]["address"],
    )


def check_anchor(w3, pinned):
    try:
        block = w3.eth.get_block(int(pinned["anchor"]["number"]))
    except Exception:
        raise ValueError("BTC_REGISTRY_ANCHOR_UNAVAILABLE") from None
    if block.hash.hex() != pinned["anchor"]["hash"]:
        raise ValueError("BTC_REGISTRY_ANCHOR_REORG")


def require_registry(manifest, key, registry, state, address, *, mint_only=False):
    pinned = state.get(("btc-mint-registry:" if mint_only else "btc-registry:") + manifest.context.hex())
    if not pinned:
        raise ValueError("BTC_REGISTRY_NOT_PINNED")
    if mint_only and pinned["graph"].get("usage") != "MINT_ONLY_V1":
        raise ValueError("BTC_REGISTRY_NOT_TRUSTED")
    if (
        Web3.to_checksum_address(address)
        != pinned["graph"]["contracts"]["batches"]["address"]
    ):
        raise ValueError("BTC_REGISTRY_NOT_TRUSTED")
    check_anchor(registry.w3, pinned)
    from .contract_provenance import check_provenance_anchors

    check_provenance_anchors(registry.w3, pinned["graph"], pinned.get("provenance"))
    options = graph_options(pinned["graph"])
    if mint_only: options["mint_only"] = True
    elif pinned["graph"].get("usage") == "BTC_INPUT_KEYS_V1": options["address_signing"] = True
    inspected = inspect_registry(manifest, key, registry, address, prove=False, **options)
    if inspected["graph"] != pinned["graph"]:
        raise ValueError("BTC_REGISTRY_GRAPH_CHANGED")
    from .bitcoin_continuity import trust_guard
    trust_guard(manifest,registry,state,pinned['graph'],mode='READ')
    return pinned


def invitation_options(invite):
    network = invite["bitcoin_network"]
    if network == "regtest":
        if invite["version"] == "6":
            return dict(bitcoin_network="regtest", evm_confirmations=invite["evm_confirmations"])
        return {}
    if invite["version"] not in ("4", "5", "6") or not is_public_network(network):
        raise ValueError("BTC_NETWORK_INVITATION_REQUIRED")
    from .bitcoin_network import validate_policy
    policy = validate_policy(invite["bitcoin_policy"], network)
    return dict(bitcoin_network=network, bitcoin_policy=policy,
                evm_confirmations=invite["evm_confirmations"])


def board_options(manifest, registry, board_address):
    if not board_address:
        return {}
    from .onchain_transport import invitation

    board, invite = invitation(registry.w3, board_address)
    if (board.functions.registry().call() != manifest.registry
            or invite["sessions"]["btc_ceremony"] != manifest.session_id.hex()):
        raise ValueError("BTC_BOARD_WALLET_MISMATCH")
    return invitation_options(invite)


def graph_options(graph):
    network = graph.get("bitcoin_network", "regtest")
    if network == "regtest":
        if "bitcoin_policy" in graph:
            raise ValueError("REGTEST_POLICY_FORBIDDEN")
        if graph.get("version") == "2":
            return dict(bitcoin_network="regtest", evm_confirmations=graph["evm_confirmations"])
        return {}
    if graph.get("version") != "2" or not is_public_network(network):
        raise ValueError("BTC_NETWORK_INVITATION_REQUIRED")
    from .bitcoin_network import validate_policy
    policy = validate_policy(graph["bitcoin_policy"], network)
    return dict(bitcoin_network=network, bitcoin_policy=policy,
                evm_confirmations=graph["evm_confirmations"])


def graph_network(graph):
    graph_options(graph)
    return graph.get("bitcoin_network", "regtest")


def bitcoin_for_graph(graph, url, cookie):
    from .bitcoin_rpc import BitcoinRPC

    if not url or not cookie:
        raise ValueError("BITCOIN_RPC_REQUIRED")
    return BitcoinRPC(url, cookie, graph_network(graph), graph.get("bitcoin_policy"))


def check_bitcoin_graph(registry, graph, bitcoin):
    """Live mainnet readiness is separate from the immutable graph fingerprint."""
    if graph_network(graph) == "regtest":
        return None
    if bitcoin is None:
        raise ValueError("BITCOIN_RPC_REQUIRED")
    from .bitcoin_network import inspect_relay

    if getattr(bitcoin, "network", None) != graph_network(graph) or bitcoin.policy != graph["bitcoin_policy"]:
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    block = confirmed_block(registry.w3, graph)
    from .zk_spv import is_zk, inspect_live
    if is_zk(graph["bitcoin_policy"]):
        from .zk_profiles import profile as zk_profile
        relay = registry.w3.eth.contract(address=graph["contracts"]["relay"]["address"],
            abi=artifact(zk_profile(graph['bitcoin_policy'])['state'])["abi"])
        checked = inspect_live(bitcoin, relay, graph["bitcoin_policy"], block_identifier=block.number)
        if registry.w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("BTC_REGISTRY_READ_REORG")
        return checked
    relay = registry.w3.eth.contract(address=graph["contracts"]["relay"]["address"],
                                    abi=artifact(relay_artifact(graph_network(graph)))["abi"])
    guard = registry.w3.eth.contract(address=graph["contracts"]["spv_guard"]["address"],
                                    abi=artifact("MainnetSPVGuard")["abi"])
    checked = inspect_relay(bitcoin, relay, graph["bitcoin_policy"], block_identifier=block.number)
    if not guard.functions.isFresh().call(block_identifier=block.number):
        raise ValueError("MAINNET_RELAY_STALE")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return checked


def confirmed_block(w3, graph):
    from .evm_network import read_block
    return read_block(w3, int(graph.get("evm_confirmations", "1")))


def transaction_profile(graph):
    network = graph_network(graph)
    backed = graph.get("chain_id") == "1" or is_public_network(network)
    result = dict(mode="backed" if backed else "integration", bitcoin_network=network)
    if "evm_confirmations" in graph:
        result["evm_confirmations"] = graph["evm_confirmations"]
    return result
