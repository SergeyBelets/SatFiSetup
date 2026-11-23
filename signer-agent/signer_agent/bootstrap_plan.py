"""Build a full integration bootstrap from public ceremony inputs.
Every dependency is an explicit journal step; approvals pause for the real participant processes.
"""

from web3 import Web3
from .dkg_protocol import Manifest, PROTOCOL_ID, raw
from .bootstrap_policy import POLICY_FIELDS, validate_policy, validate_mainnet_policy
from .bitcoin_network import is_public_network, relay_artifact

ZERO = "0x" + "00" * 20


def spv_payment_deployment(confederation_id, *, historical=True):
    name = "spv_payment_registry"
    checks = [dict(contract=name, method=method, args=[], equals=expected) for method, expected in (
        ("factory", "$confederations"), ("confederationId", confederation_id),
        ("stateView", "$spv_service_view"), ("state", "$relay"),
        ("adapter", "$spv_guard"), ("authorization", "$conf_auth"), ("enabled", False),
    )]
    if historical:
        checks[-1]["at_receipt"] = True
    return dict(id=name, kind="deploy", artifact="SPVServicePaymentRegistry",
                args=["$confederations", confederation_id, "$spv_service_view"], outputs=[], checks=checks)


def build(config):
    p = dict(config)
    from .bootstrap import Bootstrap
    from .bootstrap_formation import expected_formation
    from .bootstrap_tokens import require_literal, resolve
    names = resolve(p)
    required = (
        "evm_ceremony",
        "conf_ceremony",
        "btc_ceremony",
        "operator",
        "treasury",
        *POLICY_FIELDS,
        "approval_deadline",
        "federation_id",
        "confederation_id",
    )
    missing = [k for k in required if k not in p]
    if missing:
        raise ValueError("MISSING_PUBLIC_BOOTSTRAP_INPUTS: " + ",".join(missing))
    for key in ("deployment_id", "federation_id", "confederation_id"):
        require_literal(p.get(key))
    public_bitcoin = is_public_network(p.get("bitcoin_network"))
    ethereum = p.get("chain_id") == "1"
    mainnet = public_bitcoin or ethereum  # Backed economic components, independent of BTC network.
    if p.get("bitcoin_network") not in ("mainnet", "testnet4", "regtest") or p.get("mode") != ("backed" if mainnet else "integration"):
        raise ValueError("INTEGRATION_REGTEST_PROFILE_REQUIRED")
    if p.get("chain_id") not in ("1", "31337", "11155111") or (ethereum and (not mainnet or "deployment_fee" not in p)):
        raise ValueError("INTEGRATION_NETWORK_NOT_ALLOWED")

    validate_policy(p, "INVALID_EXPLICIT_PARAMETER: ")
    if "system_bootstrap" in p:
        from .bootstrap_system import validate
        validate(p)
    if "ulr" in p:
        from .bootstrap_ulr import validate
        validate(p)
    if "withdrawal_fee" in p:
        from .bootstrap_withdrawal_fee import validate
        validate(p["withdrawal_fee"])
    from .bitcoin_recovery_policy import (ARTIFACT as RECOVERY_ARTIFACT, FACTORY_ARTIFACT,
        SUCCESSOR_DKG, constructor_args as recovery_args, enrolled_policy)
    recovery = enrolled_policy(p)
    if mainnet:
        validate_mainnet_policy(p, "INVALID_EXPLICIT_PARAMETER: ")
        contracts = dict(p.get("contracts", {}))
        for name, field, artifact in (("usd", "reward_token", "IERC20Metadata"),
                                      ("feed", "btc_usd_feed", "IChainlinkBTCUSD")):
            if field == "btc_usd_feed" and "ulr" in p:
                continue
            spec = p[field]
            expected = dict(address=spec["address"], artifact=artifact, runtime_hash=spec["runtime_hash"])
            if name in contracts and contracts[name] != expected:
                raise ValueError("EXTERNAL_COMPONENT_BINDING")
            contracts[name] = expected
        p["contracts"] = contracts
    if not Web3.is_address(p["treasury"]) or int(p["treasury"], 16) == 0:
        raise ValueError("INVALID_TREASURY")

    # An enrolled local profile has the same signed-input boundary as a profile
    # fetched from a board. Routing alone remains participant-local.
    invitation = None
    if "onboarding" in p or p.get("onchain_board"):
        from .onboarding import verify_profile

        invitation = verify_profile(p)
        # V1/V2 remain readable, but their enrollments do not commit the new
        # withdrawal, custody and financial policy used by these constructors.
        if invitation["version"] not in ("6", "5" if ethereum else "4" if mainnet else "3"):
            raise ValueError("BOOTSTRAP_REQUIRES_V3_INVITATION")

    ceremonies = {}
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        c = p[role]
        ceremonies[role] = Manifest(
            int(p["chain_id"]),
            p["treasury"],
            raw(c["id"], 32),
            int(c["epoch"]),
            "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH",
            tuple(c["participants"]),
            tuple(c["transport_keys"]),
            int(c["threshold"]),
        )
    if len({c.session_id for c in ceremonies.values()}) != 3:
        raise ValueError("BOOTSTRAP_CEREMONY_SESSIONS")
    federation = ceremonies["evm_ceremony"]
    confederation = ceremonies["conf_ceremony"]
    bitcoin = ceremonies["btc_ceremony"]
    # This plan adopts one federation through its first-member representative;
    # all remaining confederation participants are the explicit custodians.
    custody_only = "bitcoin_custody" in p
    if custody_only:
        from .bootstrap_custody import validate as validate_custody
        validate_custody(p["bitcoin_custody"], federation.participants, p["min_custodians"])
        if (tuple(federation.participants[1:]) != confederation.participants
                or confederation.threshold != int(p["bitcoin_custody"]["threshold"])):
            raise ValueError("BOOTSTRAP_CUSTODIAN_ROSTER")
    elif federation.participants[0] != confederation.participants[0]:
        raise ValueError("BOOTSTRAP_FEDERATION_REPRESENTATIVE")
    operator = Web3.to_checksum_address(p["operator"])
    if (
        operator not in federation.participants
        or not custody_only and operator not in confederation.participants
    ):
        raise ValueError("BOOTSTRAP_OPERATOR_NOT_PARTICIPANT")
    f, c = 1, len(confederation.participants) - 1
    threshold = confederation.threshold
    if not custody_only and (
        c < 1
        or threshold < (7 * (f + c) + 9) // 10
        or threshold <= f
        or not threshold - f <= int(p["min_custodians"]) <= c
    ):
        raise ValueError("BOOTSTRAP_CONFEDERATION_THRESHOLD")
    if (
        bitcoin.participants != confederation.participants
        or bitcoin.threshold != threshold
    ):
        raise ValueError("BOOTSTRAP_BITCOIN_ROSTER_THRESHOLD")
    formation = expected_formation(invitation if invitation is not None else p)
    federation_terms = formation["federation"]
    confederation_terms = formation["confederation"]

    from .bootstrap_confederation_dao import enabled as confederation_dao_enabled
    in_kind = confederation_dao_enabled(p)
    pool_kind = "ConfederationPoolV2" if in_kind else "ConfederationMultiPool"
    share_kind = "ConfederationShareV2" if in_kind else "ConfederationToken"
    steps = []
    p["steps"] = steps

    bindings = {
        "FROSTCoordinator": [("defaultVerifier", 0)],
        "SimpleMultiPoolDeployer": [("frostDeployer", 0), ("shareTokenDeployer", 1)],
        "MultiPoolDeployer": [("shareTokenDeployer", 0)],
        "FederationDeployer": [("multiPoolDeployer", 0), ("feeSystemDeployer", 1)],
        "ConfederationDeployer": [
            ("frostCustodianDeployer", 0),
            ("multiPoolDeployer", 1),
            ("feeSystemDeployer", 2),
        ],
        "FederationFactory": [
            ("dkgCoordinator", 0),
            ("deployer", 1),
            ("frostCoordinator", 2),
        ],
        "ConfederationFactory": [
            ("federationFactory", 0),
            ("dkgCoordinator", 1),
            ("deployer", 2),
            ("frostCoordinator", 3),
        ],
        "BitcoinBatchRegistry": [("authorization", 0), ("dkg", 1), ("walletId", 2)],
        "MainnetSPVGuard": [("relay", 0), ("maxTipAgeSeconds", 1)],
        "ZkSPVServiceStateView": [("state", 0)],
        "ZkTestnet4SPVServiceStateView": [("state", 0)],
        "SPVMessageBoard": [("factory", 0), ("confederationId", 1), ("stateView", 2)],
        "BitcoinWithdrawalEscrow": [("batches", 0), ("spv", 1), ("depth", 2), ("minP2WPKHSats", 3), ("minP2TRSats", 4)],
        "CallbackPoolRegistry": [("escrow", 0), ("authorization", 1), ("simpleDeployer", 2), ("confederationDeployer", 3)],
        "BitcoinMintAdmission": [("pool", 0), ("operator", 1)],
        "BitcoinDepositRegistry": [
            ("spv", 0),
            ("authorization", 1),
            ("depth", 2),
            ("spendRecorder", 3),
        ],
        "MiningPoolV1": [("poolId", 0), ("deposits", 1), ("escrow", 2)],
        "FederationFeeManager": [("frostMultisig", 0), ("custodianValidator", 1)],
        "FederationGovernance": [
            ("federationId", 0),
            ("frostMultisig", 1),
            ("multiPool", 2),
        ],
        "GovernanceRegistry": [("factory", 0)],
        "CustodianDAOToken": [("name", 0), ("symbol", 1)],
        "ULRStaking": [("stakeToken", 0), ("rewardToken", 1), ("CUSTODIAN_THRESHOLD", 2), ("minimumStakeAmount", 3)],
        "ULRStartupRewards": [("staking",0),("apyBps",1),("unitPrice",2),("startDay",3),("refundRecipient",5)],
        "ULRCustodianSnapshots": [("staking", 0), ("authorization", 1)],
        "ULRConfederationTreasury": [("factory", 0), ("confederationId", 1), ("snapshots", 2), ("reserveFloor",3), ("payoutBps",4)],
        "ULRConfederationVoting": [("staking", 0), ("validator", 1)],
        "CustodianDAOStaking": [
            ("stakeToken", 0),
            ("rewardToken", 1),
            ("custodianValidator", 2),
        ],
        "CustodianDAORateController": [("validator", 0), ("staking", 1)],
        "ChainlinkBTCDailyTWAP": [("feed", 0)],
        "CustodianDAOIncomeVault": [("rewardToken", 0), ("authorization", 1)],
        "CustodianDAODecisionBoard": [("staking", 0), ("authorization", 1)],
        "CustodianDAODailySettlement": [
            ("rewardToken", 0),
            ("staking", 1),
            ("controller", 2),
            ("authorization", 3),
            ("twap", 4),
            ("incomeVault", 5),
            ("treasury", 6),
        ],
        "CustodianDAOFeeRouter": [("incomeVault", 0)],
        "FundedFeeConverter": [
            ("inputToken", 0),
            ("rewardToken", 1),
            ("numerator", 2),
            ("denominator", 3),
        ],
        "PoolFeePipeline": [("converter", 0), ("router", 1)],
    }

    if in_kind:
        bindings.update(ConfederationPoolV2Deployer=bindings["MultiPoolDeployer"],
            BitcoinWithdrawalEscrowV2=bindings["BitcoinWithdrawalEscrow"] + [("defaultMinerFeeLimitSats", 5)],
            ConfederationDAOStake=[("stakeToken", 0), ("custodianValidator", 1), ("CUSTODIAN_THRESHOLD", 2)],
            ConfederationDAOIncome=[("pool", 0), ("shareToken", 1), ("authorization", 2), ("staking", 3),
                                     ("decisions", 4), ("factory", 5)],
            ConfederationNFTPass=[("name", 0), ("symbol", 1), ("income", 2), ("initialPriceSigner", 4)],
            ConfederationBitcoinReserve=[("poolId", 0), ("deposits", 1), ("escrow", 2), ("confederationPool", 3)],
            BitcoinSigningBoard=[("batchRegistry", 0), ("maxPacketBytes", 1)])

    def deploy(name, art, *args, outputs=None):
        steps.append(
            dict(
                id=name,
                kind="deploy",
                artifact=art,
                args=list(args),
                outputs=outputs or [],
                checks=[
                    dict(contract=name, method=getter, args=[], equals=args[index])
                    for getter, index in bindings.get(art, [])
                ],
            )
        )

    def call(name, target, method, args, outputs=None, checks=None):
        steps.append(
            dict(
                id=name,
                kind="call",
                contract=target,
                method=method,
                args=args,
                outputs=outputs or [],
                checks=checks or [],
            )
        )

    def output(name, target, method, args=None, index=None, art=None):
        result = dict(
            name=name,
            contract=target,
            method=method,
            args=args or [],
            index=index or [],
        )
        if art:
            result["artifact"] = art
        return result

    def approve(
        name,
        target,
        helper,
        args,
        method,
        execute_args,
        auth="conf_auth",
        ceremony="conf_ceremony",
        custodian=False,
    ):
        op = name + "_hash"
        session = name + "_session"
        steps.append(
            dict(
                id=name + "_intent",
                kind="read",
                outputs=[output(op, target, helper, args)],
            )
        )
        call(
            name + "_open",
            "coordinator",
            "createSessionFromDKG",
            [p[ceremony]["id"], "$" + op, "$" + target, p["approval_deadline"]],
            outputs=[
                dict(
                    name=session,
                    contract="coordinator",
                    event="SessionCreated",
                    field="sessionId",
                )
            ],
        )
        if p.get("onchain_board"):
            call(
                name + "_publish_intent",
                "onchain_board",
                "publishIntent",
                [
                    "$" + session,
                    {
                        "$json_hex": dict(
                            chain_id=p["chain_id"],
                            target="$" + target,
                            coordinator="$coordinator",
                            session="$" + session,
                            role=ceremony,
                            operation_hash="$" + op,
                            artifact=next(
                                (
                                    s["artifact"]
                                    for s in steps
                                    if s["id"] == target and "artifact" in s
                                ),
                                next(
                                    (
                                        o["artifact"]
                                        for s in steps
                                        for o in s.get("outputs", [])
                                        if o["name"] == target and "artifact" in o
                                    ),
                                    p.get("contracts", {}).get(target, {}).get("artifact", target),
                                ),
                            ),
                            hash_method=helper,
                            hash_args=args,
                        )
                    },
                ],
            )
        steps.append(
            dict(
                id=name + "_participants",
                kind="wait",
                contract="coordinator",
                method="getAuthorizationResult",
                args=["$" + session],
                index=["2"],
                equals=True,
                pending_status="AWAITING_PARTICIPANTS",
            )
        )
        call(
            name + "_authorize",
            auth,
            "authorizeOperation",
            ["$" + session, "$" + op, "$" + target],
        )
        if custodian:
            steps.append(
                dict(
                    id=name + "_custodians",
                    kind="wait",
                    contract="validator",
                    method="meetsMinimumCustodians",
                    args=["$" + op],
                    equals=True,
                    pending_status="AWAITING_CUSTODIAN_APPROVALS",
                )
            )
        call(name, target, method, execute_args + ["$" + op])

    deploy("verifier", "FROSTVerifier")
    successor_dkg = recovery.get(SUCCESSOR_DKG) if recovery else None
    if successor_dkg is None:
        deploy("dkg", "DKGCoordinator")
        for step in steps[-2:]:
            step["parallel_group"] = "base-contracts"
    else:
        from .bootstrap import artifact, artifact_fingerprint
        compiled = artifact("DKGCoordinator")
        spec = dict(address=successor_dkg["registry"], artifact="DKGCoordinator",
                    artifact_hash=artifact_fingerprint(compiled),
                    runtime_hash=Web3.keccak(hexstr=compiled["deployedBytecode"]["object"]).hex())
        contracts = dict(p.get("contracts", {}))
        if contracts.get("dkg", spec) != spec:
            raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
        contracts["dkg"] = spec
        p["contracts"] = contracts
    deploy("coordinator", "FROSTCoordinator", "$verifier")
    call(
        "bind_dkg",
        "coordinator",
        "bindDKGRegistry",
        ["$dkg"],
        checks=[
            dict(contract="coordinator", method="dkgRegistry", args=[], equals="$dkg")
        ],
    )
    call(
        "renounce_coordinator_admin", "coordinator", "renounceRole",
        ["0x" + Web3.keccak(text="ADMIN_ROLE").hex(), p["operator"]],
        checks=[dict(contract="coordinator", method="hasRole",
            args=["0x" + Web3.keccak(text="ADMIN_ROLE").hex(), p["operator"]], equals=False)],
    )
    if p.get("onchain_board"):
        call(
            "bind_onchain_transport",
            "onchain_board",
            "bindRegistry",
            ["$dkg"],
            checks=[
                dict(
                    contract="onchain_board", method="registry", args=[], equals="$dkg"
                )
            ],
        )
        call("bind_intent_coordinator","onchain_board","bindIntentCoordinator",["$coordinator"],
             checks=[dict(contract="onchain_board",method="intentCoordinator",args=[],equals="$coordinator")])
    if successor_dkg is None and not ethereum and p.get("phase_duration", "3600") != "3600":
        from .onboarding import validate_phase_duration
        duration = p["phase_duration"]
        validate_phase_duration(str(p["chain_id"]), duration)
        call("configure_dkg_phase_duration", "dkg", "configureIntegrationPhaseDuration", [duration],
             checks=[dict(contract="dkg", method="PHASE_DURATION", args=[], equals=duration)])
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        c = p[role]
        purpose = "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH"
        call(
            role + "_create",
            "dkg",
            "createCeremony",
            [
                c["id"],
                c["participants"],
                c["threshold"],
                c["transport_keys"],
                c["epoch"],
                "0x" + Web3.keccak(text=purpose).hex(),
                "0x" + PROTOCOL_ID.hex(),
            ],
        )
        steps[-1]["parallel_group"] = "create-independent-ceremonies"
    # These independent ceremonies use separate context and secret state. The
    # executor finalizes all three creations before any participant proceeds.
    ceremony_waits = []
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        c = p[role]
        purpose = "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH"
        ceremony_waits.append(
            dict(
                id=role + "_complete",
                kind="wait",
                contract="dkg",
                method="getCompletedResult",
                args=[c["id"]],
                index=["3"],
                equals="0x" + Web3.keccak(text=purpose).hex(),
                pending_status="AWAITING_PARTICIPANTS",
            )
        )
    for n, a in [
        ("auth_leaf", "FROSTAuthorizationDeployer"),
        ("share_leaf", "MultiPoolShareTokenDeployer"),
        ("conf_share_leaf", "ConfederationShareV2Deployer" if in_kind else "ConfederationTokenDeployer"),
        ("custodian_leaf", "FROSTCustodianDeployer"),
        ("fee_leaf", "FederationFeeManagerDeployer"),
        ("distribution_leaf", "FeeDistributionCalculatorDeployer"),
        ("nft_leaf", "FederationNFTModifierDeployer"),
        ("wallet_leaf", "ProjectWalletRegistryDeployer"),
    ]:
        deploy(n, a)
        steps[-1]["parallel_group"] = "factory-leaves"
    deploy(
        "fee_system",
        "FeeSystemDeployer",
        "$fee_leaf",
        "$distribution_leaf",
        "$nft_leaf",
        "$wallet_leaf",
    )
    deploy("simple_leaf", "SimpleMultiPoolDeployer", "$auth_leaf", "$share_leaf")
    deploy("conf_pool_leaf", "ConfederationPoolV2Deployer" if in_kind else "MultiPoolDeployer", "$conf_share_leaf")
    for step in steps[-3:]:
        step["parallel_group"] = "factory-composites"
    deploy("federation_deployer", "FederationDeployer", "$simple_leaf", "$fee_system")
    deploy(
        "confederation_deployer",
        "ConfederationDeployer",
        "$custodian_leaf",
        "$conf_pool_leaf",
        "$fee_system",
    )
    for step in steps[-2:]:
        step["parallel_group"] = "factory-deployers"
    deploy(
        "federations",
        "FederationFactory",
        "$dkg",
        "$federation_deployer",
        "$coordinator",
    )
    def reserve(kind, target, step, terms):
        identifier = terms["id"]
        session = Bootstrap.wire(terms["session_id"])
        digests = Bootstrap.wire([terms["formation_hash"], terms["token_hash"]])
        call(step, target, f"reserve{kind}Id", [session, identifier, *digests], checks=[
            dict(contract=target, method=f"reserved{kind}Session", args=[identifier], equals=session),
            dict(contract=target, method=f"reserved{kind}Terms", args=[identifier], equals=digests),
        ])

    reserve("Federation", "federations", "reserve_federation_id", federation_terms)
    deploy(
        "confederations",
        "ConfederationFactory",
        "$federations",
        "$dkg",
        "$confederation_deployer",
        "$coordinator",
    )
    reserve("Confederation", "confederations", "reserve_confederation_id", confederation_terms)
    for parent, child in [
        ("simple_leaf", "auth_leaf"),
        ("simple_leaf", "share_leaf"),
        ("federation_deployer", "simple_leaf"),
        ("federations", "federation_deployer"),
        ("conf_pool_leaf", "conf_share_leaf"),
        ("confederation_deployer", "conf_pool_leaf"),
        ("confederation_deployer", "custodian_leaf"),
        ("confederations", "confederation_deployer"),
    ]:
        call(
            parent + "_" + child,
            child,
            "setAuthorizedDeployer",
            ["$" + parent, True],
            checks=[
                dict(
                    contract=child,
                    method="authorizedDeployers",
                    args=["$" + parent],
                    equals=True,
                )
            ],
        )
        steps[-1]["parallel_group"] = "factory-permissions"
    # The ZK state contract depends only on the enrolled Bitcoin policy (its
    # confederation ID comes from the invitation), not on any DKG key. Deploying
    # it before the DKG waits lets workers catch up to the Bitcoin tip while the
    # ceremonies run. Consumers, views and the Fresh barrier stay after DKG.
    from .zk_spv import is_zk, constructor_args as zk_constructor_args, checkpoint_descriptor
    zk = is_zk(p.get("bitcoin_policy"))
    if zk:
        if p["bitcoin_network"] not in ("mainnet","testnet4"):
            raise ValueError("ZK_PROFILE_UNSUPPORTED")
        from .bootstrap import Bootstrap
        from .zk_profiles import profile as zk_profile
        selected=zk_profile(p['bitcoin_policy'])
        args = zk_constructor_args(p["bitcoin_policy"])
        deploy("relay", selected['state'], *Bootstrap.wire(args), outputs=[
            output("spv_guard", "relay", "adapter", [], [], selected['adapter']),
            output("zk_verifier", "relay", "verifier", [], [], "RiscZeroGroth16Verifier"),
        ])
        steps[-1]["checks"] = [
            dict(contract="relay", method=method, args=[], equals=Bootstrap.wire(value))
            for method, value in (
                ("confederationId", args[0][0]), ("programImageId", args[0][1]),
                ("RELAY_PROFILE", args[0][2]), ("maxStateAge", args[0][3]),
                ("maxTipLag", args[0][4]), ("baselineMaxTipAge", args[0][5]),
                ("checkpointDescriptor", checkpoint_descriptor(p["bitcoin_policy"])),
            )
        ] + [dict(contract="spv_guard", method="state", args=[], equals="$relay"),
             dict(contract="spv_guard", method="testHelpersEnabled", args=[], equals=False)]
    # Factory construction can overlap participant work; no key-dependent
    # constructor or authorization runs until all three results are finalized.
    steps.extend(ceremony_waits)
    if custody_only and p.get("onchain_board"):
        call("enable_custodian_publishers", "onchain_board", "enableCustodianPublishers",
             [p["conf_ceremony"]["id"]], checks=[dict(contract="onchain_board", method="custodyPublisherEpoch",
                 args=[], equals=p["conf_ceremony"]["id"])])
    if ethereum:
        steps.append(dict(id="project_dkg_result", kind="read", outputs=[
            output("project_group", "dkg", "getCompletedResult", [p["evm_ceremony"]["id"]], ["4"])]))
        deploy("project_auth", "DKGAuthorization", "$coordinator", p["evm_ceremony"]["id"],
               "$project_group", p["evm_ceremony"]["participants"], p["evm_ceremony"]["threshold"])
        deploy("project_vault", "CustodianDAOIncomeVault", "$usd", "$project_auth")
        # Project deployment income is 100% treasury: no user DAO exists yet.
        deploy("project_router", "CustodianDAOFeeRouter", "$project_vault", p["deployment_fee"]["treasury"], "0", "0")
        for factory in ("federations", "confederations"):
            approve("project_payer_" + factory, "project_router", "getSetPayerOperationHash",
                    ["$" + factory, True], "setPayer", ["$" + factory, True],
                    auth="project_auth", ceremony="evm_ceremony")
        for factory in ("federations", "confederations"):
            call(factory + "_paid_deployment_fee", factory, "configureDeploymentFee",
                 ["$project_router", p["deployment_fee"]["amount"], False], checks=[
                    dict(contract=factory, method="deploymentFeeRouter", args=[], equals="$project_router"),
                    dict(contract=factory, method="deploymentFee", args=[], equals=p["deployment_fee"]["amount"]),
                    dict(contract=factory, method="integrationFeeExemption", args=[], equals=False)])
    else:
        for factory in ("federations", "confederations"):
            call(factory + "_explicit_integration_fee_exemption", factory,
                 "configureDeploymentFee", [ZERO, "0", p["integration_fee_exemption"]])
    fed = p["federation_id"]
    conf = p["confederation_id"]
    call(
        "adopt_federation",
        "federations",
        "adoptFederationDKG",
        [p["evm_ceremony"]["id"], fed],
        checks=[dict(contract="federations", method="getFederation", args=[fed], indices=[0, 1, 2],
                     equals=[fed, federation_terms["members"], str(federation_terms["threshold"])])],
    )
    if ethereum:
        call("reset_federation_fee_allowance", "usd", "approve", ["$federations", "0"])
        call("approve_federation_fee", "usd", "approve", ["$federations", p["deployment_fee"]["amount"]], checks=[
            dict(contract="usd", method="allowance", args=[p["operator"], "$federations"], equals=p["deployment_fee"]["amount"], at_receipt=True)])
    call(
        "deploy_federation",
        "federations",
        "deployFederation",
        [p["evm_ceremony"]["id"], names["federation_share"]["name"], names["federation_share"]["symbol"]],
        outputs=[
            dict(
                name=n,
                contract="federations",
                event="FederationDeployed",
                field=f,
                artifact=a,
            )
            for n, f, a in [
                ("fed_auth", "frostMultisig", "DKGAuthorization"),
                ("fed_pool", "multiPool", "SimpleMultiPool"),
                ("fed_shares", "shareToken", "MultiPoolShareToken"),
            ]
        ],
    )
    if ethereum:
        steps[-1]["checks"] = [dict(contract="usd", method="allowance",
            args=[p["operator"], "$federations"], equals="0", at_receipt=True)]
    steps[-1]["checks"].extend(dict(contract="fed_shares", method=key, args=[], equals=value)
        for key, value in names["federation_share"].items())
    members = Bootstrap.wire(confederation_terms["custodians"])
    call(
        "adopt_confederation",
        "confederations",
        "adoptCustodianConfederationDKG" if custody_only else "adoptConfederationDKG",
        [
            p["conf_ceremony"]["id"],
            confederation_terms["federation_ids"],
            members,
            p["conf_ceremony"]["threshold"],
            p["min_custodians"],
            conf,
        ],
        checks=[
            dict(contract="confederations", method="getConfederation", args=[conf], indices=[0, 1, 4, 5],
                 equals=[conf, confederation_terms["federation_ids"], str(confederation_terms["threshold"]),
                         str(confederation_terms["min_custodians"])]),
            dict(contract="confederations", method="getCustodianMembers", args=[conf], equals=members),
            dict(contract="confederations", method="getFederationMembers", args=[conf],
                 equals=[[fed, federation_terms["members"][0], "$fed_shares"]]),
        ],
    )
    if ethereum:
        call("reset_confederation_fee_allowance", "usd", "approve", ["$confederations", "0"])
        call("approve_confederation_fee", "usd", "approve", ["$confederations", p["deployment_fee"]["amount"]], checks=[
            dict(contract="usd", method="allowance", args=[p["operator"], "$confederations"], equals=p["deployment_fee"]["amount"], at_receipt=True)])
    call(
        "deploy_confederation",
        "confederations",
        "deployConfederation",
        [p["conf_ceremony"]["id"], names["confederation_share"]["name"], names["confederation_share"]["symbol"]],
        outputs=[
            dict(
                name=n,
                contract="confederations",
                event="ConfederationDeployed",
                field=f,
                artifact=a,
            )
            for n, f, a in [
                ("conf_auth", "frostMultisig", "DKGAuthorization"),
                ("conf_pool", "multiPool", pool_kind),
                ("validator", "custodianValidator", "CustodianValidator"),
                ("conf_shares", "shareToken", share_kind),
            ]
        ],
    )
    if ethereum:
        steps[-1]["checks"] = [dict(contract="usd", method="allowance",
            args=[p["operator"], "$confederations"], equals="0", at_receipt=True)]
    steps[-1]["checks"].extend(dict(contract="conf_shares", method=key, args=[], equals=value)
        for key, value in names["confederation_share"].items())
    if zk:
        from .zk_profiles import profile as zk_profile
        selected=zk_profile(p['bitcoin_policy'])
        # The state contract, its adapter and verifier were deployed before the
        # DKG waits; read-only binding and inactive payment entry exist before the Fresh
        # barrier, so funding the first update cannot depend on a future mint.
        # Activation/payees/tariff still require explicit enrolled FROST review.
        deploy("spv_service_view", selected["view"], "$relay")
        steps[-1]["checks"].append(dict(contract="spv_service_view", method="adapter", args=[], equals="$spv_guard"))
        steps.append(spv_payment_deployment(conf))
        deploy("spv_message_board", "SPVMessageBoard", "$confederations", conf, "$spv_service_view")
    elif public_bitcoin:
        from .bitcoin_network import relay_constructor_args
        from .bootstrap import Bootstrap
        deploy("relay", relay_artifact(p["bitcoin_network"]), *Bootstrap.wire(relay_constructor_args(p["bitcoin_policy"])))
        from hashlib import sha256
        checkpoint_hash = "0x" + sha256(sha256(bytes.fromhex(p["bitcoin_policy"]["checkpoint_header"].removeprefix("0x"))).digest()).hexdigest()
        steps[-1]["checks"] = [
            dict(contract="relay", method=method, args=[], equals=value)
            for method, value in (
                ("checkpointHash", checkpoint_hash),
                ("checkpointHeight", p["bitcoin_policy"]["checkpoint_height"]),
                ("checkpointChainWork", str(int(p["bitcoin_policy"]["checkpoint_chainwork"], 16))),
                ("maxReorgDepth", p["bitcoin_policy"]["max_reorg_depth"]),
                ("testHelpersEnabled", False),
            )
        ]
    else:
        deploy("relay", "RegtestHeaderRelay")
    if public_bitcoin:
        if not zk:
            deploy("spv_guard", "MainnetSPVGuard", "$relay", p["bitcoin_policy"]["max_tip_age_seconds"])
        steps.append(dict(id="bitcoin_relay_ready", kind="bitcoin_ready"))
    spv = "$spv_guard" if public_bitcoin else "$relay"
    deploy(
        "batches", "BitcoinBatchRegistry", "$conf_auth", "$dkg", p["btc_ceremony"]["id"]
    )
    escrow_args = ["$batches", spv, p["bitcoin_confirmations"], p["min_p2wpkh_sats"], p["min_p2tr_sats"]]
    if in_kind:
        escrow_args.append(p["ulr"]["default_miner_fee_sats"])
    deploy("escrow", "BitcoinWithdrawalEscrowV2" if in_kind else "BitcoinWithdrawalEscrow", *escrow_args,
           outputs=[output("withdrawal_recovery_factory", "escrow", "recoveryFactory", art=FACTORY_ARTIFACT)]
           if recovery else None)
    deploy("callback_registry", "CallbackPoolRegistry", "$escrow", "$conf_auth", "$simple_leaf", "$conf_pool_leaf")
    approve(
        "bind_callback_registry",
        "escrow",
        "getSetCallbackPoolRegistryOperationHash",
        ["$callback_registry"],
        "setCallbackPoolRegistry",
        ["$callback_registry"],
    )
    approve(
        "bind_batch_escrow",
        "batches",
        "getSetWithdrawalEscrowOperationHash",
        ["$escrow"],
        "setWithdrawalEscrow",
        ["$escrow"],
    )
    deploy("deposits", "BitcoinDepositRegistry", spv, "$conf_auth", p["bitcoin_confirmations"], "$escrow")
    approve(
        "bind_deposits",
        "escrow",
        "getSetDepositRegistryOperationHash",
        ["$deposits"],
        "setDepositRegistry",
        ["$deposits"],
    )
    approve(
        "bind_escrow",
        "conf_pool",
        "getSetWithdrawalEscrowOperationHash",
        ["$escrow"],
        "setWithdrawalEscrow",
        ["$escrow"],
        custodian=True,
    )
    if custody_only:
        approve("require_local_custody", "escrow", "getRequireLocalCustodyOperationHash", ["$conf_pool"],
                "requireLocalCustody", ["$conf_pool"])
    if in_kind:
        deploy("mining", "ConfederationBitcoinReserve", p.get("pool_id", "integration-pool"), "$deposits", "$escrow", "$conf_pool",
               outputs=[output("payout_script", "mining", "payoutScript")] + (
                   [output("recovery_source_hash", "mining", "payoutScriptHash")] if recovery else []))
    else:
        deploy(
        "mining",
        "MiningPoolV1",
        p.get("pool_id", "integration-pool"),
        "$deposits",
        "$escrow",
        names["mining"]["name"],
        names["mining"]["symbol"],
        outputs=[
            output("mp", "mining", "mpToken", art="PoolMpToken"),
            output("payout_script", "mining", "payoutScript"),
        ],
    )
    if "token_metadata" in p and not in_kind:
        steps[-1]["checks"].extend(dict(contract="mp", method=key, args=[], equals=value)
            for key, value in names["mining"].items())
    # Admission must be bound while the pool is still in the explicit legacy
    # mode. Address mode requires admission before any private mint signing;
    # enabling it first would fence its own one-time configuration session.
    deploy("mint_admission", "BitcoinMintAdmission", "$mining",
           p["conf_ceremony"]["participants"][0] if custody_only else p["operator"])
    next(check for check in steps[-1]["checks"] if check["method"] == "operator")["at_receipt"] = True
    approve("bind_mint_admission", "mining", "getSetMintAdmissionOperationHash",
        ["$mint_admission"], "setMintAdmission", ["$mint_admission"])
    if "withdrawal_fee" in p:
        from .bootstrap_withdrawal_fee import extend_address
        extend_address(p, deploy, approve)
    if public_bitcoin:
        steps.append(dict(id="bitcoin_consumers_ready", kind="bitcoin_ready", sync=False))
    approve(
        "register_mining",
        "deposits",
        "getRegisterOperationHash",
        ["$mining", "$payout_script"],
        "register",
        ["$mining", "$payout_script"],
    )
    from .bootstrap_custody import extend as extend_custody
    if recovery:
        args = recovery_args(recovery, "$escrow", "$mining", "$recovery_source_hash")[1:]
        call("deploy_withdrawal_recovery", "withdrawal_recovery_factory", "deployRecovery", args,
             outputs=[output("withdrawal_recovery", "withdrawal_recovery_factory", "policyFor", args,
                             art=RECOVERY_ARTIFACT)])
        approve("bind_withdrawal_recovery", "escrow", "getSetRecoveryOperationHash", ["$withdrawal_recovery"],
                "setRecovery", ["$withdrawal_recovery"])
        steps[-1]["checks"].append(
            dict(contract="escrow", method="recovery", args=[], equals="$withdrawal_recovery"))
    extend_custody(p, deploy, approve)
    if in_kind:
        approve("bind_bitcoin_reserve", "conf_pool", "getBindBitcoinReserveOperationHash", ["$mining"],
                "bindBitcoinReserve", ["$mining"], custodian=True)
    else:
        approve("add_mining", "conf_pool", "getAddPoolOperationHash", ["$mp", p.get("pool_id", "integration-pool")],
                "addPool", ["$mp", p.get("pool_id", "integration-pool")], custodian=True)
    if "withdrawal_fee" in p:
        from .bootstrap_withdrawal_fee import extend
        extend(p, deploy, approve, output)
    deploy(
        "fee_manager",
        "FederationFeeManager",
        "$conf_auth",
        "$validator",
        p["treasury"],
        ["0", p["wrap_fee_bps"], "0", "0"],
        ["0", p["unwrap_fee_bps"], "0", "0"],
    )
    approve(
        "collector",
        "fee_manager",
        "getSetFeeCollectorOperationHash",
        ["$conf_pool", True],
        "setFeeCollector",
        ["$conf_pool", True],
        custodian=True,
    )
    approve(
        "bind_fees",
        "conf_pool",
        "getSetFeeManagerOperationHash",
        ["$fee_manager"],
        "setFeeManager",
        ["$fee_manager"],
        custodian=True,
    )
    deploy("governance", "FederationGovernance", fed, "$fed_auth", "$fed_pool")
    deploy("governance_registry", "GovernanceRegistry", "$federations")
    approve(
        "register_governance",
        "governance_registry",
        "getRegisterOperationHash",
        [fed, "$governance"],
        "register",
        [fed, "$governance"],
        auth="fed_auth",
        ceremony="evm_ceremony",
    )
    if "ulr" in p:
        from .bootstrap_ulr import extend
        extend(p, deploy, call, approve)
        return p
    if not mainnet:
        deploy("usd", "DAOTestUSD")
        deploy("feed", "DAOMockFeed")
    deploy(
        "dao_token",
        "CustodianDAOToken",
        names["governance"]["name"],
        names["governance"]["symbol"],
        p["dao_token_recipient"],
    )
    deploy("staking", "CustodianDAOStaking", "$dao_token", "$usd", "$validator")
    deploy("controller", "CustodianDAORateController", "$validator", "$staking", p["dao_use_reserve"])
    deploy("twap", "ChainlinkBTCDailyTWAP", "$feed")
    deploy("vault", "CustodianDAOIncomeVault", "$usd", "$conf_auth")
    deploy("decisions", "CustodianDAODecisionBoard", "$staking", "$conf_auth")
    deploy(
        "settlement",
        "CustodianDAODailySettlement",
        "$usd",
        "$staking",
        "$controller",
        "$conf_auth",
        "$twap",
        "$vault",
        p["treasury"],
    )
    call("bind_distributor", "staking", "setDistributor", ["$settlement"])
    call("bind_settlement", "vault", "setSettlement", ["$settlement"])
    dao = [
        "$" + x
        for x in (
            "dao_token",
            "staking",
            "controller",
            "twap",
            "vault",
            "decisions",
            "settlement",
        )
    ]
    approve(
        "register_dao",
        "confederations",
        "getRegisterCustodianDAOOperationHash",
        [conf, dao],
        "registerCustodianDAO",
        [conf, dao],
        custodian=True,
    )
    steps[-1]["checks"] = [
        dict(
            contract="confederations", method="getCustodianDAO", args=[conf], equals=dao
        )
    ]
    deploy(
        "fee_router", "CustodianDAOFeeRouter", "$vault", p["treasury"], p["wrap_staker_bps"], p["deploy_staker_bps"]
    )
    deploy("converter", "FundedFeeConverter", "$mp", "$usd", p["converter_numerator"], p["converter_denominator"])
    deploy("fee_pipeline", "PoolFeePipeline", "$converter", "$fee_router")
    call(
        "fund_mainnet_converter" if mainnet else "fund_integration_converter",
        "usd",
        "transfer" if mainnet else "mint",
        ["$converter", p["converter_funding"]],
    )
    if mainnet:
        steps[-1]["verify_token_transfer"] = dict(token="usd", recipient="$converter", amount=p["converter_funding"])
    for label, stream in [("WRAP_FEE", "1"), ("UNWRAP_FEE", "3"), ("DEPLOY_FEE", "2")]:
        args = [
            "0x" + Web3.keccak(text=label).hex(),
            "$fee_router",
            stream,
            True,
            ZERO,
            "0",
        ]
        approve(
            label + "_source",
            "vault",
            "getSetSourceOperationHash",
            args,
            "setSource",
            args,
        )
    approve(
        "fee_payer",
        "fee_router",
        "getSetPayerOperationHash",
        ["$fee_pipeline", True],
        "setPayer",
        ["$fee_pipeline", True],
    )
    p["integration_trust"] = {
        "usd": "enrolled external reward token; issuer and upgrade trust" if mainnet else "explicit local mintable test USD",
        "feed": "enrolled external BTC/USD feed; oracle and upgrade trust" if mainnet else "controllable historical fixture",
        "converter": "prefunded fixed treasury quote; no minting",
        "deployment_fee": "paid factories; project treasury receives 100%; same first EVM DKG key" if ethereum else "explicit initial integration exemption",
    }
    return p
