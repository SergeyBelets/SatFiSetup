"""Enrolled in-kind DAO profile. Legacy bootstrap graphs keep their original contracts."""

from web3 import Web3

from .bootstrap_ulr import SUPPLY
from .launch_economics import integer

CUSTODIAN_METHODS = {"getAddCustodianOperationHash", "getRemoveCustodianOperationHash",
                     "getSetMinCustodiansOperationHash"}


def enabled(profile):
    return profile.get("ulr", {}).get("version") == "3"


def validate(profile, policy):
    if ("bitcoin_custody" not in profile or "withdrawal_fee" in profile or "system_bootstrap" in profile
            or "token_metadata" not in profile):
        raise ValueError("CONFEDERATION_DAO_PROFILE")
    integer(policy["candidate_threshold_wei"], 1, SUPPLY)
    integer(policy["default_miner_fee_sats"], 1, 21_000_000 * 10**8)
    members = profile.get("participants") or profile.get("evm_ceremony", {}).get("participants", [])
    if not members or int(profile["min_custodians"]) < (2 * (len(members) - 1) + 2) // 3:
        raise ValueError("CONFEDERATION_DAO_CUSTODIAN_THRESHOLD")


def inspect_roots(w3, profile, roots, at):
    from .bootstrap_tokens import resolve
    from .ulr_treasury import contract, proven, read

    names = resolve(profile)["governance"]
    token = proven(w3, "CustodianDAOToken", roots["dao_token"], at)
    if (read(token, at, "totalSupply") != SUPPLY or read(token, at, "name") != names["name"]
            or read(token, at, "symbol") != names["symbol"]):
        raise ValueError("ULR_SHARED_ROOT_BINDING")
    if "reward_token" in profile:
        reward = contract(w3, "IERC20Metadata", roots["usd"])
        if read(reward, at, "decimals") != int(profile["reward_token"]["decimals"]):
            raise ValueError("ULR_REWARD_TOKEN_BINDING")
    else:
        proven(w3, "DAOTestUSD", roots["usd"], at, [])
    return token


def extend(p, deploy, call, approve):
    from .bootstrap_tokens import resolve

    policy = p["ulr"]
    names = resolve(p)
    create = policy["mode"] == "create"
    if create:
        if p["mode"] != "backed":
            deploy("usd", "DAOTestUSD")
        deploy("dao_token", "CustodianDAOToken", names["governance"]["name"], names["governance"]["symbol"],
               p["dao_token_recipient"])
    else:
        contracts = p.setdefault("contracts", {})
        for key, artifact in (("dao_token", "CustodianDAOToken"), ("usd", "IERC20Metadata")):
            contracts[key] = dict(policy["roots"][key], artifact=artifact)
    p["steps"].append(dict(id="dao_token_verified", kind="read", outputs=[], checks=[
        dict(contract="dao_token", method=method, args=[], equals=expected) for method, expected in (
            ("name", names["governance"]["name"]), ("symbol", names["governance"]["symbol"]),
            ("totalSupply", str(SUPPLY)))]))
    deploy("staking", "ConfederationDAOStake", "$dao_token", "$validator", policy["candidate_threshold_wei"])
    p["steps"].append(dict(id="ulr_distribution", kind="ulr_distribution", token="dao_token", staking="staking",
        participants=p["conf_ceremony"]["participants"], issue_recipient=p["dao_token_recipient"] if create else None))
    deploy("decisions", "CustodianDAODecisionBoard", "$staking", "$conf_auth")
    deploy("dao_income", "ConfederationDAOIncome", "$conf_pool", "$conf_shares", "$conf_auth", "$staking", "$decisions")
    share_name = names["confederation_share"]
    deploy("nft_pass", "ConfederationNFTPass", share_name["name"] + " Pass", share_name["symbol"] + "-PASS",
           "$dao_income", "$fee_manager", p["operator"])
    approve("bind_dao_income", "conf_pool", "getBindDAOIncomeOperationHash", ["$dao_income"],
            "bindDAOIncome", ["$dao_income"], custodian=True)
    approve("add_dao_stablecoin", "dao_income", "getAddStablecoinOperationHash", ["$usd"],
            "addStablecoin", ["$usd"], custodian=True)
    approve("bind_dao_sale", "dao_income", "getBindPassSaleOperationHash", ["$nft_pass"],
            "bindPassSale", ["$nft_pass"], custodian=True)
    approve("bind_dao_nft", "fee_manager", "getSetNFTModifierOperationHash", ["$nft_pass"],
            "setNFTModifier", ["$nft_pass"], custodian=True)
    approve("bind_dao_candidates", "validator", "getBindCandidateStakingOperationHash", ["$staking"],
            "bindCandidateStaking", ["$staking"], custodian=True)
    deploy("signing_board", "BitcoinSigningBoard", "$batches", "32768")
    p["integration_trust"] = dict(ulr="shared token; isolated staking by confederation",
        income="S and FROST-approved stablecoins", reimbursement="FROST-reviewed receipts and payment-day rates")


def manifest(bootstrap):
    names = ("dao_token", "usd", "conf_pool", "conf_shares", "staking", "dao_income", "decisions", "nft_pass",
             "fee_manager", "validator", "escrow", "signing_board", "mining", "custody_transfer")
    if not all(name in bootstrap.contracts for name in names):
        return dict(version="3", status="BOOTSTRAP_PENDING", ready=False)
    from .ulr_treasury import read

    at = bootstrap.w3.eth.get_block(bootstrap.read_number())
    nodes = {name: bootstrap.contract(name) for name in names}
    links = (
        ("conf_pool", "daoIncome", "dao_income"), ("staking", "stakeToken", "dao_token"),
        ("staking", "custodianValidator", "validator"), ("dao_income", "staking", "staking"),
        ("dao_income", "decisions", "decisions"),
        ("dao_income", "shareToken", "conf_shares"), ("dao_income", "passSale", "nft_pass"),
        ("nft_pass", "income", "dao_income"), ("fee_manager", "nftModifier", "nft_pass"),
        ("validator", "candidateStaking", "staking"), ("decisions", "staking", "staking"),
        ("conf_pool", "bitcoinReserve", "mining"), ("mining", "confederationPool", "conf_pool"),
        ("mining", "shareToken", "conf_shares"), ("mining", "custodyTransfer", "custody_transfer"),
    )
    if not all(read(nodes[source], at.number, method) == nodes[target].address for source, method, target in links):
        return dict(version="3", status="BOOTSTRAP_PENDING", ready=False)
    if not read(nodes["dao_income"], at.number, "approvedToken", nodes["usd"].address):
        return dict(version="3", status="BOOTSTRAP_PENDING", ready=False)
    roots = {name: dict(address=nodes[name].address, runtime_hash=Web3.to_hex(Web3.keccak(
        bootstrap.w3.eth.get_code(nodes[name].address, at.number)))) for name in ("dao_token", "usd")}
    if bootstrap.w3.eth.get_block(at.number).hash != at.hash:
        raise ValueError("ULR_SNAPSHOT_REORG")
    reuse = dict(bootstrap.profile["ulr"], mode="reuse", roots=roots)
    return dict(version="3", status="CONFEDERATION_DAO_GRAPH_CONFIGURED", ready=False, graph_configured=True,
                confederation_id=bootstrap.profile["confederation_id"],
                reuse_policy=reuse, contracts={name: node.address for name, node in nodes.items()})


def review(w3, intent, bundle, at):
    """Authenticate the new money graph before either FROST or custodian approval."""
    kind, helper = intent.get("artifact"), intent.get("hash_method")
    reports = {"getSettlementOperationHash", "getStageExpensesOperationHash"}
    dao_methods = {"getAddStablecoinOperationHash", "getBindPassSaleOperationHash"} | reports
    relevant = (kind in ("ConfederationDAOIncome", "ConfederationNFTPass")
        or kind == "CustodianValidator" and helper in CUSTODIAN_METHODS
        or (kind, helper) in {("ConfederationPoolV2", "getBindDAOIncomeOperationHash"),
            ("FederationFeeManager", "getSetNFTModifierOperationHash"),
            ("CustodianValidator", "getBindCandidateStakingOperationHash"),
            ("CustodianDAODecisionBoard", "getCreateOperationHash")})
    invite = bundle.get("onboarding", {}).get("invitation", {})
    if not enabled(invite):
        if kind in ("ConfederationDAOIncome", "ConfederationNFTPass", "ConfederationPoolV2", "BitcoinWithdrawalEscrowV2",
                    "ConfederationBitcoinReserve", "BitcoinCustodyTransferV2"):
            raise ValueError("CONFEDERATION_DAO_PROFILE")
        return None
    if not relevant:
        return None

    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation
    from .dkg_protocol import Manifest
    from .ethereum_intents import _prove_pool

    anchor = w3.eth.get_block(at)
    at = anchor.number
    manifest = Manifest.from_dict(bundle)
    validate(invite, invite["ulr"])
    if (manifest.chain_id != w3.eth.chain_id or bundle.get("role") != "conf_ceremony"
            or manifest.purpose != "EVM_AUTH" or manifest.session_id.hex() != invite["sessions"]["conf_ceremony"]):
        raise ValueError("CONFEDERATION_DAO_EPOCH")

    def node(address, artifact_name):
        from .bootstrap import artifact
        checked_runtime(w3, address, artifact_name, at)
        return w3.eth.contract(address=address, abi=artifact(artifact_name)["abi"])

    def get(contract, method, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=at)

    def direct(contract, artifact_name, args=None):
        proof = prove_creation(w3, contract.address, artifact_name, args, at)
        if proof.get("method") != "direct" or proof.get("sender") != invite["operator"]:
            raise ValueError("CONFEDERATION_DAO_ORIGIN")

    target = node(intent["target"], kind)
    args = intent["hash_args"]
    if (kind == "ConfederationDAOIncome" and helper not in dao_methods
            or kind == "ConfederationNFTPass" and helper != "getApproveTariffOperationHash"):
        raise ValueError("CONFEDERATION_DAO_METHOD")

    sale = None
    income = None
    if kind == "ConfederationDAOIncome":
        income = target
        if helper == "getBindPassSaleOperationHash":
            sale = node(args[0], "ConfederationNFTPass")
    elif kind == "ConfederationPoolV2":
        income = node(args[0], "ConfederationDAOIncome")
    elif kind == "ConfederationNFTPass" or kind == "FederationFeeManager":
        sale = target if kind == "ConfederationNFTPass" else node(args[0], "ConfederationNFTPass")
        income = node(get(sale, "income"), "ConfederationDAOIncome")
    if income is not None:
        staking_address = get(income, "staking")
    elif kind == "CustodianDAODecisionBoard":
        staking_address = get(target, "staking")
    elif kind == "CustodianValidator" and helper in CUSTODIAN_METHODS:
        staking_address = get(target, "candidateStaking")
    else:
        staking_address = args[0]
    staking = node(staking_address, "ConfederationDAOStake")
    validator = node(get(staking, "custodianValidator"), "CustodianValidator")
    auth = node(get(validator, "frostMultisig"), "DKGAuthorization")
    complete = get(node(manifest.registry, "DKGCoordinator"), "getCompletedResult", manifest.session_id)
    if (get(auth, "registry") != manifest.registry or get(auth, "coordinator") != intent["coordinator"]
            or bytes(get(auth, "dkgId")) != manifest.session_id
            or list(get(auth, "getParticipants")) != invite["participants"][1:]
            or bytes(complete[0]) != manifest.context or bytes(get(auth, "resultHash")) != bytes(complete[1])):
        raise ValueError("CONFEDERATION_DAO_EPOCH")
    prove_creation(w3, auth.address, "DKGAuthorization", [intent["coordinator"], manifest.session_id,
        bytes(complete[4]), list(manifest.participants), manifest.threshold], at)
    prove_creation(w3, validator.address, "CustodianValidator", None, at)
    if kind == "CustodianValidator" and validator.address != target.address:
        raise ValueError("CONFEDERATION_DAO_BINDING")

    token = node(get(staking, "stakeToken"), "CustodianDAOToken")
    threshold = int(invite["ulr"]["candidate_threshold_wei"])
    if get(token, "totalSupply") != SUPPLY or get(staking, "CUSTODIAN_THRESHOLD") != threshold:
        raise ValueError("CONFEDERATION_DAO_BINDING")
    from .bootstrap_tokens import resolve

    names = resolve(invite)
    if (get(token, "name"), get(token, "symbol")) != (names["governance"]["name"], names["governance"]["symbol"]):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    if invite["ulr"]["mode"] == "create":
        direct(token, "CustodianDAOToken", [names["governance"]["name"], names["governance"]["symbol"],
            invite["dao_token_recipient"]])
    else:
        pin = invite["ulr"]["roots"]["dao_token"]
        if token.address != pin["address"] or Web3.to_hex(Web3.keccak(
                w3.eth.get_code(token.address, at))) != pin["runtime_hash"]:
            raise ValueError("ULR_SHARED_ROOT_CODE_HASH")
    direct(staking, "ConfederationDAOStake", [token.address, validator.address, threshold])

    op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
    executed = get(auth, "executedOperations", op)
    if executed and get(auth, "operationTargets", op) != target.address:
        raise ValueError("POLICY_MISMATCH")

    if not executed and kind == "CustodianValidator" and helper in CUSTODIAN_METHODS:
        if helper == "getAddCustodianOperationHash" and (
                get(validator, "isCustodian", args[0]) or not get(staking, "isCustodianCandidate", args[0])):
            raise ValueError("CONFEDERATION_DAO_CANDIDATE")
        if helper == "getRemoveCustodianOperationHash" and not get(validator, "isCustodian", args[0]):
            raise ValueError("CONFEDERATION_DAO_CUSTODIAN")
        if helper == "getSetMinCustodiansOperationHash":
            count = get(validator, "getCustodianCount")
            integer(args[0], (2 * count + 2) // 3, count)

    if kind == "CustodianDAODecisionBoard":
        direct(target, kind, [staking.address, auth.address])
        if get(validator, "candidateStaking") != staking.address:
            raise ValueError("CONFEDERATION_DAO_BINDING")
        pool = node(intent.get("dao_pool"), "ConfederationPoolV2")
        income = node(get(pool, "daoIncome"), "ConfederationDAOIncome")
        if get(income, "decisions") != target.address or get(income, "staking") != staking.address:
            raise ValueError("CONFEDERATION_DAO_BINDING")

    if income is not None:
        pool = node(get(income, "pool"), "ConfederationPoolV2")
        _prove_pool(w3, pool, "ConfederationPoolV2", invite["operator"], at)
        if (get(pool, "confederationId") != invite["confederation_id"] or get(pool, "frostMultisig") != auth.address
                or get(pool, "custodianValidator") != validator.address
                or (kind == "ConfederationPoolV2" and target.address != pool.address)
                or (kind != "ConfederationPoolV2" and get(pool, "daoIncome") != income.address)):
            raise ValueError("CONFEDERATION_DAO_BINDING")
        shares = node(get(pool, "shareToken"), "ConfederationShareV2")
        if get(shares, "confederationPool") != pool.address:
            raise ValueError("CONFEDERATION_DAO_BINDING")
        decisions = node(get(income, "decisions"), "CustodianDAODecisionBoard")
        direct(decisions, "CustodianDAODecisionBoard", [staking.address, auth.address])
        direct(income, "ConfederationDAOIncome", [pool.address, shares.address, auth.address, staking.address, decisions.address])
        if helper == "getAddStablecoinOperationHash" and len(get(income, "rewardTokens")) == 1:
            if "reward_token" in invite:
                spec = invite["reward_token"]
                if args[0] != spec["address"] or Web3.to_hex(Web3.keccak(
                        w3.eth.get_code(args[0], at))) != spec["runtime_hash"]:
                    raise ValueError("ULR_REWARD_TOKEN_BINDING")
            elif invite["ulr"]["mode"] == "reuse":
                spec = invite["ulr"]["roots"]["usd"]
                if args[0] != spec["address"] or Web3.to_hex(Web3.keccak(
                        w3.eth.get_code(args[0], at))) != spec["runtime_hash"]:
                    raise ValueError("ULR_REWARD_TOKEN_BINDING")
            else:
                direct(node(args[0], "DAOTestUSD"), "DAOTestUSD", [])
        if sale is not None:
            manager = get(pool, "feeManager")
            if (get(sale, "income") != income.address or get(sale, "priceSigner") != invite["operator"]
                    or (kind == "FederationFeeManager" and manager != target.address)
                    or (helper != "getBindPassSaleOperationHash" and get(income, "passSale") != sale.address)):
                raise ValueError("CONFEDERATION_DAO_BINDING")
            direct(sale, "ConfederationNFTPass", [names["confederation_share"]["name"] + " Pass",
                names["confederation_share"]["symbol"] + "-PASS", income.address, manager, invite["operator"]])
        if not executed and helper in reports:
            from .confederation_dao_report import review as review_report
            review_report(w3, intent, invite["evm_confirmations"], at)
    if w3.eth.get_block(at).hash != anchor.hash:
        raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
    return auth.address, validator.address
