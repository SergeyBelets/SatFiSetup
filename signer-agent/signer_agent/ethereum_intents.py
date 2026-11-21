"""Authenticate bootstrap targets and authorization before mainnet FROST approvals.

Every approval is bound to the enrolled role, coordinator and contract origins.
The project router exists before the confederation/Bitcoin graph, so its payer
approval also checks the enrolled reward token, treasury and zero staker allocation.
"""
from web3 import Web3

from .bootstrap import artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation, birth_block
from .dkg_protocol import Manifest
from .onboarding import validate_invite

ZERO = "0x" + "00" * 20


def require_mainnet_signing_enrollment(bundle, chain_id):
    """Mainnet signing is only supported through a reviewed V5/V6 enrollment."""
    if chain_id != 1:
        return
    onboarding = bundle.get("onboarding")
    invitation = onboarding.get("invitation") if isinstance(onboarding, dict) else None
    if not isinstance(invitation, dict) or invitation.get("version") not in ("5", "6"):
        raise ValueError("ETHEREUM_V5_ENROLLMENT_REQUIRED")


def validate_signing_binding(intent, bundle, coordinator_address, *, allow_legacy_file=False):
    """Bind the reviewed public intent to the client that will publish shares.

    A coordinator supplied on the command line must not be able to differ from
    the coordinator authenticated by the intent provenance review. This check
    precedes private signing and applies to test networks as well as mainnet.
    Only an explicit legacy file package on a non-mainnet, non-enrolled manifest
    may omit the field; its CLI coordinator remains mandatory. A declared field
    always has to match, including in that compatibility mode.
    """
    declared = intent.get("coordinator")
    if (not isinstance(coordinator_address, str)
            or not Web3.is_address(coordinator_address)
            or Web3.to_checksum_address(coordinator_address) == ZERO):
        raise ValueError("POLICY_MISMATCH")
    chain = bundle.get("chain_id")
    legacy_chain = ((type(chain) is int and chain > 1)
        or (isinstance(chain, str) and chain.isascii() and chain.isdecimal() and int(chain) > 1))
    legacy_missing = (allow_legacy_file is True and legacy_chain
        and "onboarding" not in bundle and "coordinator" not in intent)
    if not legacy_missing and (not isinstance(declared, str) or not Web3.is_address(declared)
            or Web3.to_checksum_address(declared) != Web3.to_checksum_address(coordinator_address)):
        raise ValueError("POLICY_MISMATCH")
    if ("onboarding" in bundle or chain in (1, "1")) and (bundle.get("role") not in ("evm_ceremony", "conf_ceremony")
            or intent.get("role") != bundle["role"]):
        raise ValueError("POLICY_MISMATCH")


def _prove_pool(w3, target, kind, operator, number):
    """Authenticate a pool created internally by its canonical direct leaf."""
    from eth_utils.abi import collapse_if_tuple
    from web3._utils.events import get_event_data
    leaf_kind, event_name, children = {
        "SimpleMultiPool": ("SimpleMultiPoolDeployer", "SimpleMultiPoolDeployed",
                            [("frostDeployer", "FROSTAuthorizationDeployer"),
                             ("shareTokenDeployer", "MultiPoolShareTokenDeployer")]),
        "ConfederationMultiPool": ("MultiPoolDeployer", "MultiPoolDeployed",
                                  [("shareTokenDeployer", "ConfederationTokenDeployer")]),
        "ConfederationPoolV2": ("ConfederationPoolV2Deployer", "MultiPoolDeployed",
                                [("shareTokenDeployer", "ConfederationShareV2Deployer")]),
    }[kind]
    from . import verified_cache
    cache_key = [target.address, kind, operator, Web3.keccak(hexstr=artifact(kind)["bytecode"]["object"]).hex()]
    cached = verified_cache.get(w3, "pool-origin", cache_key)
    if cached:
        if (int(cached["number"]) > number
                or w3.eth.get_block(int(cached["number"]))["hash"].hex() != cached["hash"]):
            raise ValueError("ETHEREUM_PROJECT_SNAPSHOT_REORG")
        for child, child_kind in cached["children"]:
            proof = prove_creation(w3, child, child_kind, [], number)
            if proof.get("method") != "direct" or proof.get("sender") != operator:
                raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
        proof = prove_creation(w3, cached["leaf"], leaf_kind, cached["args"], number)
        leaf = w3.eth.contract(address=cached["leaf"], abi=artifact(leaf_kind)["abi"])
        if (proof.get("method") != "direct" or proof.get("sender") != operator
                or leaf.functions.deployerOwner().call(block_identifier=number) != operator):
            raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
        return
    born = birth_block(w3, target.address, number)
    block = w3.eth.get_block(born, full_transactions=True)
    event = next(x for x in artifact(leaf_kind)["abi"] if x.get("type") == "event" and x["name"] == event_name)
    topic = Web3.keccak(text=event_name + "(" + ",".join(collapse_if_tuple(x) for x in event["inputs"]) + ")")
    for tx in block["transactions"]:
        receipt = w3.eth.get_transaction_receipt(tx["hash"])
        if (receipt["blockNumber"] != born or bytes(receipt["blockHash"]) != bytes(block["hash"])
                or bytes(receipt["transactionHash"]) != bytes(tx["hash"])):
            raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
        if receipt["status"] != 1:
            continue
        for log in receipt["logs"]:
            if not log.get("topics") or bytes(log["topics"][0]) != topic:
                continue
            try:
                decoded = get_event_data(w3.codec, event, log)
                if decoded["args"]["multiPool"] != target.address:
                    continue
            except Exception:
                continue
            leaf_address = Web3.to_checksum_address(log["address"])
            checked_runtime(w3, leaf_address, leaf_kind, number)
            leaf = w3.eth.contract(address=leaf_address, abi=artifact(leaf_kind)["abi"])
            args = []
            for getter, child_kind in children:
                child = getattr(leaf.functions, getter)().call(block_identifier=number)
                proof = prove_creation(w3, child, child_kind, [], number)
                if proof.get("method") != "direct" or proof.get("sender") != operator:
                    raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
                args.append(child)
            proof = prove_creation(w3, leaf_address, leaf_kind, args, number)
            if (proof.get("method") != "direct" or proof.get("sender") != operator
                    or leaf.functions.deployerOwner().call(block_identifier=number) != operator):
                raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
            if bytes(w3.eth.get_block(born).hash) != bytes(block["hash"]):
                raise ValueError("ETHEREUM_PROJECT_SNAPSHOT_REORG")
            verified_cache.put(w3, "pool-origin", cache_key, dict(number=str(born), hash=block["hash"].hex(),
                leaf=leaf_address, args=args, children=[[child, spec[1]] for child, spec in zip(args, children)]))
            return
    raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")


def review_bootstrap_intent(w3, intent, bundle, block_identifier):
    """V5/V6 preflight before any private signing of a typed bootstrap operation.

    Proves reviewed constructors and the enrolled authorization epoch. Financial
    terms still require the explicit typed-intent review; project payer terms
    additionally receive the complete enrolled-policy check below.
    """
    invite = validate_invite(bundle["onboarding"]["invitation"])
    manifest = Manifest.from_dict(bundle)
    role = bundle.get("role")
    if (invite["version"] not in ("5", "6") or w3.eth.chain_id != 1 or manifest.chain_id != 1
            or manifest.purpose != "EVM_AUTH" or role not in ("evm_ceremony", "conf_ceremony")
            or intent.get("role") != role or intent.get("chain_id") != "1"
            or manifest.session_id.hex() != invite["sessions"][role]):
        raise ValueError("ETHEREUM_BOOTSTRAP_POLICY")
    from .evm_network import read_block
    confirmed = read_block(w3, int(invite["evm_confirmations"]))
    if type(block_identifier) is not int or not 0 <= block_identifier <= confirmed.number:
        raise ValueError("ETHEREUM_PROJECT_UNFINALIZED")
    block = w3.eth.get_block(block_identifier)

    def read(target, method, *args):
        return getattr(target.functions, method)(*args).call(block_identifier=block_identifier)

    def contract(a, kind):
        if not isinstance(a, str) or not Web3.is_checksum_address(a) or a == ZERO:
            raise ValueError("ETHEREUM_BOOTSTRAP_POLICY")
        checked_runtime(w3, a, kind, block_identifier)
        return w3.eth.contract(address=a, abi=artifact(kind)["abi"])

    def direct(target, kind, args=None):
        proof = prove_creation(w3, target.address, kind, args, block_identifier)
        if proof.get("method") != "direct" or proof.get("sender") != invite["operator"]:
            raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
        return target

    registry = direct(contract(manifest.registry, "DKGCoordinator"), "DKGCoordinator", [])
    coordinator = contract(intent.get("coordinator"), "FROSTCoordinator")
    verifier = direct(contract(read(coordinator, "defaultVerifier"), "FROSTVerifier"), "FROSTVerifier", [])
    direct(coordinator, "FROSTCoordinator", [verifier.address])
    if (read(coordinator, "dkgRegistry") != registry.address
            or read(coordinator, "hasRole", Web3.keccak(text="ADMIN_ROLE"), invite["operator"])):
        raise ValueError("ETHEREUM_BOOTSTRAP_AUTHORIZATION")
    completed = read(registry, "getCompletedResult", manifest.session_id)
    if (bytes(completed[0]) != manifest.context or bytes(completed[3]) != Web3.keccak(text="EVM_AUTH")
            or list(completed[5]) != list(manifest.participants) or int(completed[6]) != manifest.threshold):
        raise ValueError("ETHEREUM_BOOTSTRAP_AUTHORIZATION")
    kind = intent.get("artifact")
    if kind in ('WithdrawalFeePayments','WithdrawalFeeFunding'):
        from .withdrawal_fee_policy import review
        review(w3,intent,bundle,block_identifier,executable=False)
        if bytes(w3.eth.get_block(block_identifier).hash)!=bytes(block.hash):raise ValueError('ETHEREUM_PROJECT_SNAPSHOT_REORG')
        return True
    auth_getters = {"DKGAuthorization": None, "SimpleMultiPool": "frostMultisig",
        "ConfederationMultiPool": "frostMultisig", "ConfederationPoolV2": "frostMultisig",
        "ConfederationDAOIncome": "authorization", "CustodianValidator": "frostMultisig", "FederationFeeManager": "frostMultisig",
        "CustodianDAOIncomeVault": "authorization", "CustodianDAODailySettlement": "authorization",
        "BitcoinBatchRegistry": "authorization", "BitcoinDepositRegistry": "authorization",
        "CustodianDAOFeeRouter": "authorization", "PoolFeePipeline": "authorization",
        "MiningPoolV1": "authorization", "ConfederationBitcoinReserve": "authorization", "BitcoinMintAdmission": "authorization",
        "SPVServicePaymentRegistry": "authorization", "BitcoinAddressRegistry": "authorization", "BitcoinKeyFamilyRegistry":"governance", "BitcoinFeeReserve": "authorization",
        "CustodianDAODecisionBoard": "authorization"}
    if kind not in {*auth_getters, "BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2", "GovernanceRegistry", "FederationFactory", "ConfederationFactory"}:
        raise ValueError("ETHEREUM_BOOTSTRAP_POLICY")
    target = contract(intent.get("target"), kind)
    if kind in ("SimpleMultiPool", "ConfederationMultiPool", "ConfederationPoolV2"):
        _prove_pool(w3, target, kind, invite["operator"], block_identifier)
    elif kind not in ("DKGAuthorization", "CustodianValidator"):
        direct(target, kind)
    if kind == "DKGAuthorization":
        auth_address = target.address
    elif kind in ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2"):
        batches = direct(contract(read(target, "batches"), "BitcoinBatchRegistry"), "BitcoinBatchRegistry")
        auth_address = read(batches, "authorization")
    elif kind in ("GovernanceRegistry", "FederationFactory", "ConfederationFactory"):
        args = intent.get("hash_args")
        expected_id = invite["confederation_id" if kind == "ConfederationFactory" else "federation_id"]
        if not isinstance(args, list) or not args or args[0] != expected_id:
            raise ValueError("ETHEREUM_BOOTSTRAP_POLICY")
        factory_kind = "ConfederationFactory" if kind == "ConfederationFactory" else "FederationFactory"
        factory = direct(contract(read(target, "factory"), factory_kind), factory_kind) if kind == "GovernanceRegistry" else target
        if (read(factory, "dkgCoordinator") != registry.address or read(factory, "frostCoordinator") != coordinator.address):
            raise ValueError("ETHEREUM_BOOTSTRAP_AUTHORIZATION")
        auth_address = read(factory, "getConfederation" if kind == "ConfederationFactory" else "getFederation", expected_id)[7 if kind == "ConfederationFactory" else 4]
    else:
        auth_address = read(target, auth_getters[kind])
    auth = contract(auth_address, "DKGAuthorization")
    prove_creation(w3, auth.address, "DKGAuthorization", [coordinator.address, manifest.session_id,
        bytes(completed[4]), list(manifest.participants), manifest.threshold], block_identifier)
    if (read(auth, "registry") != registry.address or read(auth, "coordinator") != coordinator.address
            or bytes(read(auth, "dkgId")) != manifest.session_id or bytes(read(auth, "resultHash")) != bytes(completed[1])):
        raise ValueError("ETHEREUM_BOOTSTRAP_AUTHORIZATION")
    if kind == "CustodianDAOFeeRouter" and role == "evm_ceremony":
        review_project_payer(w3, intent, bundle, block_identifier)
    from .zk_spv import review_consumer
    review_consumer(w3, invite, target, kind, intent, block_identifier)
    if kind=="BitcoinBatchRegistry" and intent.get("hash_method")=="getSetFeeReserveOperationHash":
        from .btc_fee_policy import review_binding
        ctx = review_binding(w3,intent,block_identifier)
        from .bootstrap_withdrawal_fee import review_enrolled
        review_enrolled(ctx, invite, block_identifier)
    if kind=='BitcoinWithdrawalEscrow' and intent.get('hash_method')=='getSetWithdrawalFeesOperationHash':
        from .withdrawal_fee_policy import review_binding
        ctx = review_binding(w3,intent,block_identifier)
        from .bootstrap_withdrawal_fee import review_enrolled
        review_enrolled(ctx, invite, block_identifier)
    if kind == "BitcoinFeeReserve":
        from .btc_fee_policy import review
        review(w3,intent,bundle,block_identifier,executable=False)
    if kind=='BitcoinKeyFamilyRegistry' or kind=='BitcoinAddressRegistry' and intent.get('hash_method')=='getBindFamilyRegistryOperationHash':
        from .family_registry import review,policy_for_intent
        policy=invite.get('withdrawal_fee',{}).get('family_root')
        if policy is None:raise ValueError('FAMILY_ROOT_ENROLLMENT')
        review(w3,intent,bundle,block_identifier,policy_for_intent(w3,intent,policy,block_identifier))
    elif kind == "BitcoinAddressRegistry":
        from .address_registration import review
        review(w3, intent, bundle, block_identifier)
    if kind in ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2") and intent.get("hash_method") == "getSetCallbackPoolRegistryOperationHash":
        from .callback_admission import review_callback_configuration
        arguments = intent.get("hash_args")
        if not isinstance(arguments, list) or len(arguments) != 1:
            raise ValueError("BTC_CALLBACK_CONFIGURATION_INTENT")
        review_callback_configuration(target, arguments[0], block_identifier)
    if kind == "MiningPoolV1" and intent.get("hash_method") == "getSetMintAdmissionOperationHash":
        from .mint_admission import configuration
        arguments = intent.get("hash_args")
        if not isinstance(arguments, list) or len(arguments) != 1:
            raise ValueError("BTC_MINT_CONFIGURATION_INTENT")
        configuration(w3, arguments[0], block_identifier, expected_pool=target.address,
            expected_operator=invite["participants"][1] if "bitcoin_custody" in invite else invite["operator"],
            require_bound=False)
    if bytes(w3.eth.get_block(block_identifier).hash) != bytes(block.hash):
        raise ValueError("ETHEREUM_PROJECT_SNAPSHOT_REORG")
    return True


def review_project_payer(w3, intent, bundle, block_identifier):
    """Raise a fixed public error on any policy/origin mismatch; return True."""
    invite = validate_invite(bundle["onboarding"]["invitation"])
    manifest = Manifest.from_dict(bundle)
    if (invite["version"] not in ("5", "6") or w3.eth.chain_id != 1 or manifest.chain_id != 1
            or manifest.purpose != "EVM_AUTH"
            or manifest.session_id.hex() != invite["sessions"]["evm_ceremony"]
            or list(manifest.participants) != invite["participants"]
            or manifest.threshold != int(invite["threshold"])
            or intent.get("chain_id") != "1" or intent.get("role") != "evm_ceremony"
            or intent.get("artifact") != "CustodianDAOFeeRouter"
            or intent.get("hash_method") != "getSetPayerOperationHash"):
        raise ValueError("ETHEREUM_PROJECT_INTENT_POLICY")
    args = intent.get("hash_args")
    if (not isinstance(args, list) or len(args) != 2 or args[1] is not True
            or not isinstance(args[0], str) or not Web3.is_checksum_address(args[0])
            or args[0] == ZERO):
        raise ValueError("ETHEREUM_PROJECT_INTENT_POLICY")
    from .evm_network import read_block
    confirmed = read_block(w3, int(invite["evm_confirmations"]))
    if type(block_identifier) is not int or block_identifier > confirmed.number:
        raise ValueError("ETHEREUM_PROJECT_UNFINALIZED")
    block = w3.eth.get_block(block_identifier)
    seen = {}

    def contract(address, kind):
        if not isinstance(address, str) or not Web3.is_checksum_address(address) or address == ZERO:
            raise ValueError("ETHEREUM_PROJECT_BINDING")
        checked_runtime(w3, address, kind, block_identifier)
        return w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    def read(target, method, *values):
        return getattr(target.functions, method)(*values).call(block_identifier=block_identifier)

    def prove(target, kind, values):
        proof = prove_creation(w3, target.address, kind, values, block_identifier)
        # These are direct bootstrap deployments. Restricting the creator also
        # pins immutable factory/leaf setup authority to the enrolled operator.
        if proof.get("method") != "direct" or proof.get("sender") != invite["operator"]:
            raise ValueError("ETHEREUM_PROJECT_CREATOR")
        return target

    registry = prove(contract(manifest.registry, "DKGCoordinator"), "DKGCoordinator", [])
    coordinator = contract(intent.get("coordinator"), "FROSTCoordinator")
    verifier = prove(contract(read(coordinator, "defaultVerifier"), "FROSTVerifier"), "FROSTVerifier", [])
    prove(coordinator, "FROSTCoordinator", [verifier.address])
    if (read(coordinator, "dkgRegistry") != registry.address
            or read(coordinator, "hasRole", Web3.keccak(text="ADMIN_ROLE"), invite["operator"])):
        raise ValueError("ETHEREUM_PROJECT_BINDING")
    completed = read(registry, "getCompletedResult", manifest.session_id)
    if (bytes(completed[0]) != manifest.context
            or bytes(completed[3]) != Web3.keccak(text="EVM_AUTH")
            or list(completed[5]) != invite["participants"]
            or int(completed[6]) != int(invite["threshold"])):
        raise ValueError("ETHEREUM_PROJECT_DKG")

    router = contract(intent.get("target"), "CustodianDAOFeeRouter")
    vault = contract(read(router, "incomeVault"), "CustodianDAOIncomeVault")
    auth = contract(read(vault, "authorization"), "DKGAuthorization")
    prove(auth, "DKGAuthorization", [coordinator.address, manifest.session_id,
          bytes(completed[4]), invite["participants"], int(invite["threshold"])])
    if (read(auth, "coordinator") != coordinator.address or read(auth, "registry") != registry.address
            or bytes(read(auth, "dkgId")) != manifest.session_id
            or bytes(read(auth, "resultHash")) != bytes(completed[1])):
        raise ValueError("ETHEREUM_PROJECT_DKG")
    reward = invite["reward_token"]
    reward_code = bytes(w3.eth.get_code(reward["address"], block_identifier=block_identifier))
    if not reward_code or Web3.to_hex(Web3.keccak(reward_code)) != reward["runtime_hash"]:
        raise ValueError("EXTERNAL_COMPONENT_CODE_HASH")
    token = w3.eth.contract(address=reward["address"], abi=artifact("IERC20Metadata")["abi"])
    if read(token, "decimals") != int(reward["decimals"]):
        raise ValueError("EXTERNAL_COMPONENT_DECIMALS")
    prove(vault, "CustodianDAOIncomeVault", [reward["address"], auth.address])
    prove(router, "CustodianDAOFeeRouter", [vault.address, invite["deployment_fee"]["treasury"], 0, 0])
    if (read(vault, "rewardToken") != reward["address"] or read(vault, "settlement") != ZERO
            or read(vault, "sources", Web3.keccak(text="DEPLOY_FEE"))[2]
            or read(router, "rewardToken") != reward["address"]
            or read(router, "authorization") != auth.address
            or read(router, "treasury") != invite["deployment_fee"]["treasury"]
            or read(router, "wrapStakerBps") != 0 or read(router, "deployStakerBps") != 0):
        raise ValueError("ETHEREUM_PROJECT_FEE_POLICY")

    dependencies = {
        "FederationFactory": [("dkgCoordinator", "DKGCoordinator"), ("deployer", "FederationDeployer"), ("frostCoordinator", "FROSTCoordinator")],
        "ConfederationFactory": [("federationFactory", "FederationFactory"), ("dkgCoordinator", "DKGCoordinator"),
                                  ("deployer", "ConfederationDeployer"), ("frostCoordinator", "FROSTCoordinator")],
        "FederationDeployer": [("multiPoolDeployer", "SimpleMultiPoolDeployer"), ("feeSystemDeployer", "FeeSystemDeployer")],
        "ConfederationDeployer": [("frostCustodianDeployer", "FROSTCustodianDeployer"), ("multiPoolDeployer", "MultiPoolDeployer"),
                                 ("feeSystemDeployer", "FeeSystemDeployer")],
        "SimpleMultiPoolDeployer": [("frostDeployer", "FROSTAuthorizationDeployer"), ("shareTokenDeployer", "MultiPoolShareTokenDeployer")],
        "MultiPoolDeployer": [("shareTokenDeployer", "ConfederationTokenDeployer")],
        "FeeSystemDeployer": [("feeManagerDeployer", "FederationFeeManagerDeployer"), ("feeDistributorDeployer", "FeeDistributionCalculatorDeployer"),
                              ("nftModifierDeployer", "FederationNFTModifierDeployer"), ("walletRegistryDeployer", "ProjectWalletRegistryDeployer")],
    }

    def factory_graph(address, kind):
        if kind == "DKGCoordinator":
            if address != registry.address:
                raise ValueError("ETHEREUM_PROJECT_BINDING")
            return registry
        if kind == "FROSTCoordinator":
            if address != coordinator.address:
                raise ValueError("ETHEREUM_PROJECT_BINDING")
            return coordinator
        cache_key = (address, kind)
        if cache_key in seen:
            return seen[cache_key]
        node = contract(address, kind)
        values = []
        for getter, child_kind in dependencies.get(kind, []):
            child = factory_graph(read(node, getter), child_kind)
            values.append(child.address)
        prove(node, kind, values)
        if kind.endswith("Factory"):
            if read(node, "feeSetupAuthority") != invite["operator"]:
                raise ValueError("ETHEREUM_PROJECT_CREATOR")
            if read(node, "deploymentFeeConfigured") and (
                    read(node, "integrationFeeExemption")
                    or read(node, "deploymentFeeRouter") != router.address
                    or read(node, "deploymentFee") != int(invite["deployment_fee"]["amount"])):
                raise ValueError("ETHEREUM_PROJECT_FEE_POLICY")
        else:
            if read(node, "deployerOwner") != invite["operator"]:
                raise ValueError("ETHEREUM_PROJECT_CREATOR")
        seen[cache_key] = node
        return node

    payer = args[0]
    try:
        checked_runtime(w3, payer, "FederationFactory", block_identifier)
        kind = "FederationFactory"
    except ValueError:
        checked_runtime(w3, payer, "ConfederationFactory", block_identifier)
        kind = "ConfederationFactory"
    factory_graph(payer, kind)
    if bytes(w3.eth.get_block(block_identifier).hash) != bytes(block.hash):
        raise ValueError("ETHEREUM_PROJECT_SNAPSHOT_REORG")
    return True
