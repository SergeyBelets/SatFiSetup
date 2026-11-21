"""Signed shared ULR graph, attached to the ordinary resumable bootstrap."""
import re

from web3 import Web3

SUPPLY = 10_000_000 * 10**18
ROOTS = {"dao_token": "CustodianDAOToken", "staking": "ULRStaking",
         "snapshots": "ULRCustodianSnapshots", "usd": "IERC20Metadata"}


def shared_roots(policy):
    if policy["version"] == "3":
        return {name: ROOTS[name] for name in ("dao_token", "usd")}
    from .launch_economics import economics
    return dict(ROOTS, **({"startup_rewards":"ULRStartupRewards"} if economics(policy)["startup"]["enabled"] else {}))


def validate(profile):
    policy = profile.get("ulr")
    if "btc_usd_feed" in profile:
        raise ValueError("ULR_BTC_USD_FEED_UNUSED")
    if (not isinstance(policy, dict) or policy.get("version") not in ("1", "2", "3")
            or policy.get("mode") not in ("create", "reuse")
            or set(policy) != ({"version", "mode", "bitcoin_backend"}
                               | ({"roots"} if policy.get("mode") == "reuse" else set())
                               | ({"token"} if "token" in policy else set())
                               | ({"economics"} if policy.get("version") == "2" else set())
                               | ({"candidate_threshold_wei", "default_miner_fee_sats"}
                                  if policy.get("version") == "3" else set()))):
        raise ValueError("ULR_BOOTSTRAP_POLICY")
    if policy["version"] == "3":
        from .bootstrap_confederation_dao import validate as validate_dao
        validate_dao(profile, policy)
    else:
        from .launch_economics import economics
        economics(policy)
    from .bootstrap_tokens import resolve
    resolve(profile)
    backend = policy["bitcoin_backend"]
    from .zk_spv import is_zk
    expected = "zk-state-v2" if is_zk(profile.get("bitcoin_policy", {})) else "relay"
    if backend != expected or backend == "zk-state-v2" and profile.get("bitcoin_network") not in ("mainnet","testnet4"):
        raise ValueError("ULR_BITCOIN_BACKEND_MISMATCH")
    # No second, legacy allocation before gross income reaches the ULR treasury.
    if (profile.get("wrap_staker_bps") != "0" or profile.get("deploy_staker_bps") != "0"
            or profile.get("dao_use_reserve") is not False):
        raise ValueError("ULR_LEGACY_SPLIT_FORBIDDEN")
    if policy["mode"] == "reuse":
        roots = policy["roots"]
        if not isinstance(roots, dict) or set(roots) != set(shared_roots(policy)):
            raise ValueError("ULR_SHARED_ROOTS")
        addresses = []
        for spec in roots.values():
            if (not isinstance(spec, dict) or set(spec) != {"address", "runtime_hash"}
                    or not isinstance(spec["address"], str) or not Web3.is_checksum_address(spec["address"])
                    or int(spec["address"], 16) == 0
                    or not isinstance(spec["runtime_hash"], str)
                    or not re.fullmatch(r"0x[0-9a-f]{64}", spec["runtime_hash"])
                    or int(spec["runtime_hash"], 16) == 0):
                raise ValueError("ULR_SHARED_ROOTS")
            addresses.append(spec["address"])
        if len(set(addresses)) != len(addresses):
            raise ValueError("ULR_SHARED_ROOTS")
        if "reward_token" in profile and roots["usd"] != {k: profile["reward_token"][k] for k in ("address", "runtime_hash")}:
            raise ValueError("ULR_REWARD_TOKEN_BINDING")
    return policy


def extend(p, deploy, call, approve):
    policy = validate(p)
    if policy["version"] == "3":
        from .bootstrap_confederation_dao import extend as extend_dao
        return extend_dao(p, deploy, call, approve)
    from .bootstrap_tokens import resolve
    names = resolve(p)["governance"]
    from .launch_economics import encoded
    settings = encoded(policy, int(p.get("reward_token", {}).get("decimals", "6")))
    steps = p["steps"]
    create = policy["mode"] == "create"
    if create:
        if p["mode"] != "backed":
            deploy("usd", "DAOTestUSD")
        deploy("dao_token", "CustodianDAOToken", names["name"], names["symbol"], p["dao_token_recipient"])
        steps[-1]["checks"].append(dict(contract="dao_token", method="totalSupply", args=[], equals=str(SUPPLY)))
        deploy("staking", "ULRStaking", "$dao_token", "$usd", str(settings["candidate_threshold"]), str(settings["minimum_stake"]))
        steps.append(dict(id="ulr_distribution", kind="ulr_distribution", token="dao_token", staking="staking",
            participants=p["conf_ceremony"]["participants"], issue_recipient=p["dao_token_recipient"]))
        deploy("snapshots", "ULRCustodianSnapshots", "$staking", "$fed_auth")
    else:
        contracts = p.setdefault("contracts", {})
        for name, kind in shared_roots(policy).items():
            spec = dict(policy["roots"][name], artifact=kind)
            if name in contracts and contracts[name] != spec:
                raise ValueError("ULR_SHARED_ROOTS_OVERRIDE")
            contracts[name] = spec
        steps.append(dict(id="shared_ulr_bindings", kind="read", outputs=[], checks=[
            dict(contract=name, method=method, args=[], equals=value) for name, method, value in (
                ("dao_token", "name", names["name"]), ("dao_token", "symbol", names["symbol"]),
                ("dao_token", "totalSupply", str(SUPPLY)), ("staking", "stakeToken", "$dao_token"),
                ("staking", "rewardToken", "$usd"), ("snapshots", "staking", "$staking"))]))
    if not create:
        steps.append(dict(id="ulr_distribution", kind="ulr_distribution", token="dao_token", staking="staking",
            participants=p["conf_ceremony"]["participants"]))
    deploy("ulr_treasury", "ULRConfederationTreasury", "$confederations", p["confederation_id"], "$snapshots",
           str(settings["treasury_reserve"]), str(settings["treasury_payout_bps"]))
    startup = settings['startup']
    if startup['enabled']:
        if create:
            deploy('startup_rewards','ULRStartupRewards','$staking',str(startup['apy_bps']),str(startup['unit_price']),
                str(startup['start_day']),str(startup['duration_days']),p['operator'])
            call('fund_startup_rewards','usd','mint' if p['mode']!='backed' else 'transfer',['$startup_rewards',str(startup['budget'])])
            if p['mode']=='backed':steps[-1]['verify_token_transfer']=dict(token='usd',recipient='$startup_rewards',amount=str(startup['budget']))
        else:
            p.setdefault('contracts',{})['startup_rewards']=dict(policy['roots']['startup_rewards'],artifact='ULRStartupRewards')
    approve("enroll_ulr_treasury", "snapshots", "getEnrollTreasuryOperationHash",
            ["$confederations", p["confederation_id"], "$ulr_treasury"], "enrollTreasury",
            ["$confederations", p["confederation_id"], "$ulr_treasury"], custodian=True)
    approve("bind_ulr_candidates", "validator", "getBindCandidateStakingOperationHash", ["$staking"],
            "bindCandidateStaking", ["$staking"], custodian=True)
    steps[-1]["checks"] = [dict(contract="validator", method="candidateStaking", args=[], equals="$staking")]
    deploy("ulr_voting", "ULRConfederationVoting", "$staking", "$validator")
    deploy("decisions", "CustodianDAODecisionBoard", "$ulr_voting", "$conf_auth")
    # This vault supplies the legacy router's token/auth interface only. No
    # settlement or sources: zero shares send every realized dollar to the kazna.
    deploy("vault", "CustodianDAOIncomeVault", "$usd", "$conf_auth")
    deploy("fee_router", "CustodianDAOFeeRouter", "$vault", "$ulr_treasury", "0", "0")
    deploy("converter", "FundedFeeConverter", "$mp", "$usd", p["converter_numerator"], p["converter_denominator"])
    deploy("fee_pipeline", "PoolFeePipeline", "$converter", "$fee_router")
    mint_fixture = create and p["mode"] != "backed"
    call("fund_ulr_converter", "usd", "mint" if mint_fixture else "transfer", ["$converter", p["converter_funding"]])
    if not mint_fixture:
        steps[-1]["verify_token_transfer"] = dict(token="usd", recipient="$converter", amount=p["converter_funding"])
    approve("fee_payer", "fee_router", "getSetPayerOperationHash", ["$fee_pipeline", True],
            "setPayer", ["$fee_pipeline", True])
    descriptor = {name: "$" + name for name in (*shared_roots(policy), "ulr_treasury")}
    for step in steps:
        if step["id"] in {name + "_publish_intent" for name in ("enroll_ulr_treasury", "bind_ulr_candidates", "fee_payer")}:
            intent = step["args"][1]["$json_hex"]
            intent.update(ulr_bootstrap=descriptor, ulr_treasury="$ulr_treasury")
    p["integration_trust"] = dict(
        usd="enrolled USD stablecoin" if p["mode"] == "backed" else "local mintable test USD; shared across reuse profiles",
        converter="prefunded fixed quote; converted fees only", snapshots="system EVM FROST authority; historical holder inventory attestation",
        deployment_fee="project fee policy unchanged", ulr="one fixed 10m issue in create; reuse never deploys or mints ULR")


def inspect_roots(w3, profile, roots, at):
    """Authenticate shared roots before transactions and participant signatures."""
    from .ulr_treasury import contract, read, proven, configuration
    if type(at) is not int:
        at = w3.eth.get_block(at).number
    validate(profile)
    policy = profile["ulr"]
    if set(roots) != set(shared_roots(policy)):
        raise ValueError("ULR_SHARED_ROOTS")
    if policy["mode"] == "reuse":
        for name, spec in policy["roots"].items():
            if (roots[name] != spec["address"] or
                    Web3.to_hex(Web3.keccak(w3.eth.get_code(roots[name], at))) != spec["runtime_hash"]):
                raise ValueError("ULR_SHARED_ROOT_CODE_HASH")
    if policy["version"] == "3":
        from .bootstrap_confederation_dao import inspect_roots as inspect_dao_roots
        return inspect_dao_roots(w3, profile, roots, at)
    pin = dict(version="1", chain_id=profile["chain_id"], staking=roots["staking"], snapshots=roots["snapshots"],
               confirmations=profile.get("evm_confirmations", "1"), treasuries=[])
    registry, staking = configuration(w3, pin, at)
    from .launch_economics import encoded
    reward_decimals=read(contract(w3,"IERC20Metadata",roots["usd"]),at,"decimals")
    expected=encoded(policy,reward_decimals)
    if (read(staking,at,"CUSTODIAN_THRESHOLD")!=expected["candidate_threshold"]
            or read(staking,at,"minimumStakeAmount")!=expected["minimum_stake"]
            or read(registry,at,"MIN_CUSTODIAN_ULR")!=expected["candidate_threshold"]):
        raise ValueError("ULR_ENROLLED_ECONOMICS_MISMATCH")
    if expected["startup"]["enabled"]:
        startup=contract(w3,"ULRStartupRewards",roots["startup_rewards"])
        spec=expected["startup"]
        startup_args=[staking.address,spec["apy_bps"],spec["unit_price"],spec["start_day"],spec["duration_days"],read(startup,at,"refundRecipient")]
        proven(w3,"ULRStartupRewards",startup.address,at,startup_args)
    token = proven(w3, "CustodianDAOToken", roots["dao_token"], at)
    from .bootstrap_tokens import resolve
    names = resolve(profile)["governance"]
    if (read(token, at, "totalSupply") != SUPPLY or read(token, at, "name") != names["name"]
            or read(token, at, "symbol") != names["symbol"] or read(staking, at, "stakeToken") != token.address
            or read(staking, at, "rewardToken") != roots["usd"]):
        raise ValueError("ULR_SHARED_ROOT_BINDING")
    if "reward_token" in profile:
        spec = profile["reward_token"]
        reward = contract(w3, "IERC20Metadata", roots["usd"])
        if (reward.address != spec["address"] or read(reward, at, "decimals") != int(spec["decimals"])
                or Web3.to_hex(Web3.keccak(w3.eth.get_code(reward.address, at))) != spec["runtime_hash"]):
            raise ValueError("ULR_REWARD_TOKEN_BINDING")
    else:
        proven(w3, "DAOTestUSD", roots["usd"], at, [])
    return registry, staking, token


def enrolled_pin(w3, intent, bundle, at):
    """No manual pin is needed for the three exact operations in a signed bootstrap."""
    from .onboarding import verify_enrollments
    from .contract_provenance import prove_creation
    from .ulr_treasury import contract, read
    invite = bundle["onboarding"]["invitation"]
    verify_enrollments(invite, bundle["onboarding"]["enrollments"])
    validate(invite)
    descriptor = intent["ulr_bootstrap"]
    if not isinstance(descriptor, dict) or set(descriptor) != set(shared_roots(invite["ulr"])) | {"ulr_treasury"}:
        raise ValueError("ULR_BOOTSTRAP_DESCRIPTOR")
    roots = {name: descriptor[name] for name in shared_roots(invite["ulr"])}
    registry, staking, token = inspect_roots(w3, invite, roots, at)
    from .launch_economics import encoded
    settings=encoded(invite["ulr"],int(invite.get("reward_token",{}).get("decimals","6")))
    def origin(target, kind, args=None):
        proof = prove_creation(w3, target, kind, args, at)
        if proof["method"] != "direct" or proof["sender"] != invite["operator"]:
            raise ValueError("ULR_BOOTSTRAP_ORIGIN")
    if invite["ulr"]["mode"] == "create":
        from .bootstrap_tokens import resolve
        names = resolve(invite)["governance"]
        origin(token.address, "CustodianDAOToken", [names["name"], names["symbol"], invite["dao_token_recipient"]])
        origin(roots["staking"], "ULRStaking", [token.address, roots["usd"],settings["candidate_threshold"],settings["minimum_stake"]])
        if settings['startup']['enabled']:
            spec=settings['startup']
            origin(roots['startup_rewards'],'ULRStartupRewards',[roots['staking'],spec['apy_bps'],spec['unit_price'],
                spec['start_day'],spec['duration_days'],invite['operator']])
        origin(registry.address, "ULRCustodianSnapshots")
        auth = contract(w3, "DKGAuthorization", read(registry, at, "authorization"))
        if (bytes(read(auth, at, "dkgId")).hex() != invite["sessions"]["evm_ceremony"]
                or read(auth, at, "registry") != bundle["registry"]):
            raise ValueError("ULR_SYSTEM_AUTHORITY")
    treasury = contract(w3, "ULRConfederationTreasury", descriptor["ulr_treasury"])
    factory = contract(w3, "ConfederationFactory", read(treasury, at, "factory"))
    origin(factory.address, "ConfederationFactory")
    origin(treasury.address, "ULRConfederationTreasury", [factory.address, invite["confederation_id"], registry.address,settings["treasury_reserve"],settings["treasury_payout_bps"]])
    if (read(factory, at, "dkgCoordinator") != bundle["registry"]
            or read(treasury, at, "confederationId") != invite["confederation_id"]
            or intent.get("ulr_treasury") != treasury.address
            or (intent.get("artifact"), intent.get("hash_method")) not in {
                ("ULRCustodianSnapshots", "getEnrollTreasuryOperationHash"),
                ("CustodianValidator", "getBindCandidateStakingOperationHash"),
                ("CustodianDAOFeeRouter", "getSetPayerOperationHash")}):
        raise ValueError("ULR_BOOTSTRAP_INTENT")
    return dict(version="1", chain_id=invite["chain_id"], staking=roots["staking"], snapshots=roots["snapshots"],
                confirmations=invite["evm_confirmations"], treasuries=[treasury.address])


def review_pipeline(w3, target, router, factory, conf_id, bundle, at):
    from .ulr_treasury import contract, proven, read
    invite = bundle["onboarding"]["invitation"]
    pipeline = contract(w3, "PoolFeePipeline", target)
    converter_address = read(pipeline, at, "converter")
    proven(w3, "PoolFeePipeline", target, at, [converter_address, router])
    conf = read(factory, at, "getConfederation", conf_id)
    reward = read(contract(w3, "CustodianDAOFeeRouter", router), at, "rewardToken")
    converter = contract(w3, "FundedFeeConverter", converter_address)
    mp_address = read(converter, at, "inputToken")
    mp = contract(w3, "PoolMpToken", mp_address)
    from .bitcoin_trust import checked_runtime
    checked_runtime(w3, mp.address, "PoolMpToken", at)
    pool = proven(w3, "MiningPoolV1", read(mp, at, "poolDAO"), at)
    if read(pool, at, "mpToken") != mp.address or read(pool, at, "authorization") != conf[7]:
        raise ValueError("ULR_PIPELINE_MP_BINDING")
    if "token_metadata" in invite:
        from .bootstrap_tokens import check
        check(w3, mp.address, "mining", invite, at)
    # ULR intents have their own signing review and do not pass through the
    # generic Ethereum bootstrap review. Preserve the enrolled ZK verifier
    # checks here as well, before authorizing the fee pipeline.
    from .zk_spv import is_zk, review_consumer
    if is_zk(invite.get("bitcoin_policy")):
        review_consumer(w3, invite, pool, "MiningPoolV1", {}, at)
    proven(w3, "FundedFeeConverter", converter_address, at,
           [mp.address, reward, int(invite["converter_numerator"]), int(invite["converter_denominator"])])


def manifest(bootstrap):
    if bootstrap.profile["ulr"]["version"] == "3":
        from .bootstrap_confederation_dao import manifest as dao_manifest
        return dao_manifest(bootstrap)
    names = (*shared_roots(bootstrap.profile["ulr"]), "ulr_treasury", "ulr_voting", "decisions", "fee_router", "fee_pipeline")
    if not all(name in bootstrap.contracts for name in names):
        return dict(version="1", status="BOOTSTRAP_PENDING", ready=False)
    anchor = bootstrap.w3.eth.get_block(bootstrap.read_number())
    at = anchor.number
    roots = {name: bootstrap.contracts[name]["address"] for name in shared_roots(bootstrap.profile["ulr"])}
    inspect_roots(bootstrap.w3, bootstrap.profile, roots, at)
    pin = dict(version="1", chain_id=bootstrap.profile["chain_id"], staking=roots["staking"], snapshots=roots["snapshots"],
               confirmations=bootstrap.profile.get("evm_confirmations", "1"),
               treasuries=[bootstrap.contracts["ulr_treasury"]["address"]])
    from .ulr_treasury import treasury_context, voting_context, read
    registry, _, treasury, _, _, _ = treasury_context(bootstrap.w3, pin, pin["treasuries"][0], at)
    voting_context(bootstrap.w3, pin, bootstrap.contracts["decisions"]["address"], treasury.address, at)
    router = bootstrap.contract("fee_router")
    if (read(registry, at, "treasuries", read(treasury, at, "scope")) != treasury.address
            or not read(router, at, "authorizedPayers", bootstrap.contracts["fee_pipeline"]["address"])):
        return dict(version="1", status="BOOTSTRAP_PENDING", ready=False)
    if (read(router, at, "treasury") != treasury.address or read(router, at, "wrapStakerBps") != 0
            or read(router, at, "deployStakerBps") != 0):
        raise ValueError("ULR_GROSS_FEES_REQUIRED")
    reuse = dict(version=bootstrap.profile["ulr"]["version"], mode="reuse", bitcoin_backend=bootstrap.profile["ulr"]["bitcoin_backend"],
                 roots={name: dict(address=roots[name], runtime_hash=Web3.to_hex(Web3.keccak(
                     bootstrap.w3.eth.get_code(roots[name], at)))) for name in shared_roots(bootstrap.profile["ulr"])})
    if bootstrap.profile["ulr"]["version"] == "2":
        from .launch_economics import economics
        reuse["economics"] = economics(bootstrap.profile["ulr"])
    from .bootstrap_tokens import resolve
    names = resolve(bootstrap.profile)["governance"]
    if "token_metadata" in bootstrap.profile or "token" in bootstrap.profile["ulr"]:
        reuse["token"] = names
    if bootstrap.w3.eth.get_block(at).hash != anchor.hash:
        raise ValueError("ULR_SNAPSHOT_REORG")
    startup = dict(address=roots["startup_rewards"], command="frost-signer startup-rewards pay") if "startup_rewards" in roots else None
    return dict(version="1", status="ULR_GRAPH_READY", ready=True, pin=pin, reuse_policy=reuse,
                bitcoin_backend=bootstrap.profile["ulr"]["bitcoin_backend"], supply=str(SUPPLY), startup_rewards=startup,
                automation="publish historical UTC snapshot with system FROST; call payDay daily; claim by owners")
