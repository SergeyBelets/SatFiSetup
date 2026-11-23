"""Completed-DKG confederation + funded DAO graph registration, including real custodian approvals."""

import json
import pytest
from web3 import Web3
from web3.exceptions import ContractCustomError
from signer_agent.bootstrap_formation import confederation_formation_hash, federation_formation_hash, share_token_hash
from test_dkg_processes import deploy, tx, ceremony, RPC
from test_bitcoin_processes import authorize
from lifecycle_helpers import federation, contract, tx_from
from script_helpers import run_script, SENDER


def test_confederation_dkg_registers_dao_and_atomic_deploy_fee(
    tmp_path, record_property
):
    w3 = Web3(Web3.HTTPProvider(RPC))
    registry = deploy(w3, "DKGCoordinator")
    verifier = deploy(w3, "FROSTVerifier")
    coordinator = deploy(w3, "FROSTCoordinator", verifier.address)
    tx(w3, coordinator.functions.bindDKGRegistry(registry.address))
    d = tmp_path / "fed"
    d.mkdir()
    fed, _, _ = ceremony(d, w3, registry, 3, 2, "EVM_AUTH")
    factory, _, _ = federation(w3, registry, coordinator, fed)
    d = tmp_path / "conf"
    d.mkdir()
    # One founder plus two custodians requires ceil(70% * 3) = 3 signers.
    m, participant, _ = ceremony(d, w3, registry, 3, 3, "EVM_AUTH")
    token_leaf = deploy(w3, "ConfederationTokenDeployer")
    pool_leaf = deploy(w3, "MultiPoolDeployer", token_leaf.address)
    custodian_leaf = deploy(w3, "FROSTCustodianDeployer")
    fees = (
        contract(w3, "FederationDeployer", factory.functions.deployer().call())
        .functions.feeSystemDeployer()
        .call()
    )
    deployer = deploy(
        w3, "ConfederationDeployer", custodian_leaf.address, pool_leaf.address, fees
    )
    cf = deploy(
        w3,
        "ConfederationFactory",
        factory.address,
        registry.address,
        deployer.address,
        coordinator.address,
    )
    for leaf, parent in [
        (token_leaf, pool_leaf),
        (pool_leaf, deployer),
        (custodian_leaf, deployer),
        (deployer, cf),
    ]:
        tx(w3, leaf.functions.setAuthorizedDeployer(parent.address, True))
    tx(w3, cf.functions.configureDeploymentFee("0x" + "00" * 20, 0, True))
    custodians = [
        ("custodian2", m.participants[1], b"\x02", True),
        ("custodian3", m.participants[2], b"\x02", True),
    ]
    conf_metadata = ("Confederation Share", "CS")
    tx(w3, cf.functions.reserveConfederationId(m.session_id, "integration-conf",
        confederation_formation_hash(0, ["integration-fed"], custodians, m.threshold, 2),
        share_token_hash(*conf_metadata)))
    receipt = w3.eth.wait_for_transaction_receipt(
        cf.functions.adoptConfederationDKG(
            m.session_id, ["integration-fed"], custodians, 3, 2, "integration-conf"
        ).transact({"from": m.participants[0]})
    )
    assert receipt.status == 1
    assert cf.functions.deploymentInitiator(m.session_id).call() == m.participants[0]
    with pytest.raises(ContractCustomError, match="ea8e4eb5"):
        cf.functions.deployConfederation(m.session_id, *conf_metadata).call(
            {"from": w3.eth.accounts[0]}
        )
    receipt = tx_from(
        w3,
        cf.functions.deployConfederation(m.session_id, *conf_metadata),
        m.participants[0],
    )
    event = cf.events.ConfederationDeployed().process_receipt(receipt)[0]["args"]
    auth = contract(w3, "DKGAuthorization", event["frostMultisig"])
    validator = contract(w3, "CustodianValidator", event["custodianValidator"])
    assert auth.functions.dkgId().call() == m.session_id
    assert auth.functions.threshold().call() == 3
    assert validator.functions.getMinCustodiansRequired().call() == 2

    def approve(c, name, helper, args):
        return authorize(
            w3, coordinator, auth, participant, tmp_path, c.address, name, helper, args
        )

    usd = deploy(w3, "DAOTestUSD")
    feed = deploy(w3, "DAOMockFeed")
    deployed = run_script(
        w3,
        tmp_path / "dao-script",
        "DeployCustodianDAO",
        {
            "CONFEDERATION_FACTORY": cf.address,
            "CONFEDERATION_ID": "integration-conf",
            "DAO_TOKEN_NAME": "Custodian Governance",
            "DAO_TOKEN_SYMBOL": "CGOV",
            "DAO_TOKEN_RECIPIENT": w3.eth.accounts[0],
            "DAO_REWARD_TOKEN": usd.address,
            "DAO_TREASURY": w3.eth.accounts[9],
            "BTC_USD_CHAINLINK_FEED": feed.address,
            "DAO_USE_RESERVE": "false",
        },
    )
    gov, staking, controller, twap, vault, board, settlement = [
        deployed[name]
        for name in (
            "CustodianDAOToken",
            "CustodianDAOStaking",
            "CustodianDAORateController",
            "ChainlinkBTCDailyTWAP",
            "CustodianDAOIncomeVault",
            "CustodianDAODecisionBoard",
            "CustodianDAODailySettlement",
        )
    ]
    assert staking.functions.distributor().call() == settlement.address
    assert vault.functions.settlement().call() == settlement.address
    assert settlement.functions.treasury().call() == w3.eth.accounts[9]
    record_property(
        "script_deployments", (tmp_path / "dao-script/public-sizes.json").read_text()
    )
    dao = [
        c.address for c in (gov, staking, controller, twap, vault, board, settlement)
    ]
    op = approve(
        cf,
        "ConfederationFactory",
        "getRegisterCustodianDAOOperationHash",
        ["integration-conf", dao],
    )
    with pytest.raises(Exception):
        cf.functions.registerCustodianDAO("integration-conf", dao, op).call()
    for member in m.participants[1:]:
        w3.provider.make_request("anvil_impersonateAccount", [member])
        receipt = w3.eth.wait_for_transaction_receipt(
            validator.functions.registerCustodianSignature(op).transact(
                {"from": member}
            )
        )
        assert receipt.status == 1
    register_sig = "registerExisting(address,string,(address,address,address,address,address,address,address),uint256)"
    # The authorization already exists; the script must consume it and accept repeat discovery.
    register_args = [cf.address, "integration-conf", "(" + ",".join(dao) + ")", "0"]
    run_script(
        w3,
        tmp_path / "register-dao",
        "DeployCustodianDAO",
        sig=register_sig,
        args=register_args,
    )
    nonce = w3.eth.get_transaction_count(SENDER)
    run_script(
        w3,
        tmp_path / "register-dao-repeat",
        "DeployCustodianDAO",
        sig=register_sig,
        args=register_args,
    )
    assert w3.eth.get_transaction_count(SENDER) == nonce
    assert list(cf.functions.getCustodianDAO("integration-conf").call()) == dao
    with pytest.raises(Exception):
        cf.functions.registerCustodianDAO("integration-conf", dao, op).call()
    converter = deploy(w3, "DAOMockConverter")
    adapter = run_script(
        w3,
        tmp_path / "unwrap-adapter",
        "DeployCustodianDAOIncomeAdapter",
        {
            "DAO_INCOME_VAULT": vault.address,
            "DAO_ADAPTER_KIND": "CONVERSION",
            "DAO_SOURCE_NAME": "UNWRAP_FEE",
            "DAO_SOURCE_STREAM": "3",
            "DAO_INPUT_TOKEN": gov.address,
            "DAO_CONVERTER": converter.address,
        },
    )["CustodianDAOConversionAdapter"]
    assert adapter.functions.sourceId().call() == vault.functions.UNWRAP_FEE().call()
    assert adapter.functions.incomeVault().call() == vault.address
    # Paid deployment uses the same funded DAO fee source. Failure rolls back product creation and fee.
    router = deploy(
        w3, "CustodianDAOFeeRouter", vault.address, w3.eth.accounts[9], 2500, 5000
    )
    source = Web3.keccak(text="DEPLOY_FEE")
    args = ["0x" + source.hex(), router.address, 2, True, "0x" + "00" * 20, 0]
    op = approve(vault, "CustodianDAOIncomeVault", "getSetSourceOperationHash", args)
    tx(
        w3,
        vault.functions.setSource(
            source, router.address, 2, True, "0x" + "00" * 20, 0, op
        ),
    )
    day = vault.functions.firstSettlementDay().call()
    w3.provider.make_request(
        "evm_setNextBlockTimestamp",
        [max(day * 86400 + 1, w3.eth.get_block("latest").timestamp + 1)],
    )
    w3.provider.make_request("evm_mine", [])
    paid = deploy(
        w3,
        "FederationFactory",
        registry.address,
        factory.functions.deployer().call(),
        coordinator.address,
    )
    fd = contract(w3, "FederationDeployer", factory.functions.deployer().call())
    tx(w3, fd.functions.setAuthorizedDeployer(paid.address, True))
    tx(w3, paid.functions.configureDeploymentFee(router.address, 100000, False))
    op = approve(
        router,
        "CustodianDAOFeeRouter",
        "getSetPayerOperationHash",
        [paid.address, True],
    )
    tx(w3, router.functions.setPayer(paid.address, True, op))
    paid_metadata = ("Paid Shares", "PS")
    tx(w3, paid.functions.reserveFederationId(fed.session_id, "paid-fed",
        federation_formation_hash(fed.participants, fed.threshold), share_token_hash(*paid_metadata)))
    receipt = w3.eth.wait_for_transaction_receipt(
        paid.functions.adoptFederationDKG(fed.session_id, "paid-fed").transact(
            {"from": fed.participants[0]}
        )
    )
    assert receipt.status == 1
    payer = fed.participants[0]
    treasury = router.functions.treasury().call()
    before = usd.functions.balanceOf(vault.address).call()
    payer_before = usd.functions.balanceOf(payer).call()
    treasury_before = usd.functions.balanceOf(treasury).call()
    assert paid.functions.deploymentInitiator(fed.session_id).call() == payer
    with pytest.raises(ContractCustomError, match="ea8e4eb5"):
        paid.functions.deployFederation(fed.session_id, *paid_metadata).call(
            {"from": w3.eth.accounts[0]}
        )
    # Use the authorized deployer here: an outsider rejection would not exercise fee rollback.
    insufficient_allowance = Web3.keccak(
        text="ERC20InsufficientAllowance(address,uint256,uint256)"
    )[:4].hex()
    with pytest.raises(ContractCustomError, match=insufficient_allowance):
        paid.functions.deployFederation(fed.session_id, *paid_metadata).call(
            {"from": payer}
        )
    failed = w3.eth.wait_for_transaction_receipt(
        paid.functions.deployFederation(fed.session_id, *paid_metadata).transact(
            {"from": payer, "gas": 16_000_000}
        )
    )
    assert failed.status == 0
    assert not paid.functions.isFederationActive("paid-fed").call()
    assert usd.functions.balanceOf(vault.address).call() == before
    assert usd.functions.balanceOf(treasury).call() == treasury_before
    assert usd.functions.balanceOf(payer).call() == payer_before
    tx(w3, usd.functions.mint(payer, 100000))
    tx_from(w3, usd.functions.approve(paid.address, 100000), payer)
    tx_from(
        w3, paid.functions.deployFederation(fed.session_id, *paid_metadata), payer
    )
    assert paid.functions.isFederationActive("paid-fed").call()
    assert usd.functions.balanceOf(vault.address).call() == before + 50000
    assert usd.functions.balanceOf(treasury).call() == treasury_before + 50000
    assert usd.functions.balanceOf(payer).call() == payer_before
    assert usd.functions.balanceOf(paid.address).call() == 0
    assert usd.functions.allowance(payer, paid.address).call() == 0
    with pytest.raises(
        ContractCustomError,
        match=Web3.keccak(text="FederationAlreadyDeployed()")[:4].hex(),
    ):
        paid.functions.deployFederation(fed.session_id, *paid_metadata).call(
            {"from": payer}
        )
    assert usd.functions.balanceOf(vault.address).call() == before + 50000
    assert usd.functions.balanceOf(treasury).call() == treasury_before + 50000
    from dao_cli_helpers import exercise_dao_cli

    exercise_dao_cli(
        tmp_path,
        w3,
        cf,
        gov,
        staking,
        controller,
        board,
        settlement,
        usd,
        feed,
        vault,
        approve,
    )
