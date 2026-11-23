import json
import pytest
from web3 import Web3
from web3.exceptions import ContractCustomError
from signer_agent.bootstrap_formation import federation_formation_hash, share_token_hash
from test_dkg_processes import RPC, ceremony, tx, deploy
from test_bitcoin_processes import authorize
from lifecycle_helpers import contract, tx_from
from script_helpers import run_script, SENDER


def test_federation_script_broadcast_and_factory_creation(tmp_path, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    run_script(
        w3,
        tmp_path / "wrong-chain",
        "DeployFederation",
        {"EXPECTED_CHAIN_ID": "1"},
        expect_failure="chain mismatch",
    )
    run_script(
        w3,
        tmp_path / "missing-fees",
        "DeployFederation",
        {"INTEGRATION_FEE_EXEMPTION": "false"},
        expect_failure="exempt_all requires exemption",
    )
    deployed = run_script(w3, tmp_path / "infrastructure", "DeployFederation")
    dkg = deployed["DKGCoordinator"]
    coordinator = deployed["FROSTCoordinator"]
    factory = deployed["FederationFactory"]
    cf = deployed["ConfederationFactory"]
    assert coordinator.functions.dkgRegistry().call() == dkg.address
    assert not coordinator.functions.hasRole(
        coordinator.functions.ADMIN_ROLE().call(), SENDER
    ).call()
    for f in (factory, cf):
        assert f.functions.deploymentFeeConfigured().call()
        assert f.functions.integrationFeeExemption().call()
    for parent, child in [
        ("SimpleMultiPoolDeployer", "FROSTAuthorizationDeployer"),
        ("SimpleMultiPoolDeployer", "MultiPoolShareTokenDeployer"),
        ("FederationDeployer", "SimpleMultiPoolDeployer"),
        ("FederationFactory", "FederationDeployer"),
        ("MultiPoolDeployer", "ConfederationTokenDeployer"),
        ("ConfederationDeployer", "MultiPoolDeployer"),
        ("ConfederationDeployer", "FROSTCustodianDeployer"),
        ("ConfederationFactory", "ConfederationDeployer"),
    ]:
        assert (
            deployed[child]
            .functions.authorizedDeployers(deployed[parent].address)
            .call()
        )
    # A successful script exit alone would miss broken factory permissions/bindings.
    ceremony_dir = tmp_path / "ceremony"
    ceremony_dir.mkdir()
    manifest, participant, _ = ceremony(ceremony_dir, w3, dkg, 3, 2, "EVM_AUTH")
    token_metadata = ("Script Shares", "SS")
    tx(w3, factory.functions.reserveFederationId(manifest.session_id, "script-fed",
        federation_formation_hash(manifest.participants, manifest.threshold), share_token_hash(*token_metadata)),
        sender=SENDER)
    receipt = w3.eth.wait_for_transaction_receipt(
        factory.functions.adoptFederationDKG(
            manifest.session_id, "script-fed"
        ).transact({"from": SENDER})
    )
    assert receipt.status == 1
    assert factory.functions.deploymentInitiator(manifest.session_id).call() == SENDER
    with pytest.raises(ContractCustomError, match="ea8e4eb5"):
        factory.functions.deployFederation(
            manifest.session_id, "Attacker Shares", "BAD"
        ).call({"from": w3.eth.accounts[0]})
    receipt = tx_from(
        w3,
        factory.functions.deployFederation(manifest.session_id, *token_metadata),
        SENDER,
    )
    event = factory.events.FederationDeployed().process_receipt(receipt)[0]["args"]
    auth = contract(w3, "DKGAuthorization", event["frostMultisig"])
    governance = run_script(
        w3,
        tmp_path / "governance",
        "DeployFederationGovernance",
        sig="run(string,address,address,uint256)",
        args=["script-fed", auth.address, event["multiPool"], "31337"],
    )["FederationGovernance"]
    gov_registry = deploy(w3, "GovernanceRegistry", factory.address)
    authorize(
        w3,
        coordinator,
        auth,
        participant,
        tmp_path,
        gov_registry.address,
        "GovernanceRegistry",
        "getRegisterOperationHash",
        ["script-fed", governance.address],
    )
    register_args = [gov_registry.address, "script-fed", governance.address, "0"]
    for suffix in ("first", "repeat"):
        run_script(
            w3,
            tmp_path / ("register-gov-" + suffix),
            "DeployFederationGovernance",
            sig="registerExisting(address,string,address,uint256)",
            args=register_args,
        )
    assert gov_registry.functions.governance("script-fed").call() == governance.address
    assert factory.functions.isFederationActive("script-fed").call()
    record_property(
        "script_deployments",
        (tmp_path / "infrastructure/public-sizes.json").read_text(),
    )


def test_legacy_demo_scripts_broadcast(tmp_path, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    evidence = []
    for name, pool_name in (
        ("DeployDemo", "SimpleMiningPool"),
        ("DeployDemoWithCalculator", "SimpleMiningPoolWithCalculator"),
    ):
        directory = tmp_path / name
        deployed = run_script(w3, directory, name)
        pool, token = deployed[pool_name], deployed["PoolMpToken"]
        assert pool.functions.mpToken().call() == token.address
        assert pool.functions.owner().call() == SENDER
        assert token.functions.hasRole(
            token.functions.MINTER_ROLE().call(), pool.address
        ).call()
        assert token.functions.hasRole(
            token.functions.BURNER_ROLE().call(), pool.address
        ).call()
        if name == "DeployDemoWithCalculator":
            assert (
                pool.functions.calculator().call()
                == deployed["ManualPercentageCalculator"].address
            )
        evidence.extend(json.loads((directory / "public-sizes.json").read_text()))
    record_property("script_deployments", json.dumps(evidence))
