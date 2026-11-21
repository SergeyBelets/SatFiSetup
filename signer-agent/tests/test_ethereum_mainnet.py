"""Ethereum-mainnet admission, enrolled fees and finality regression tests.

These tests never contact a live RPC or sign/broadcast a transaction. Mainnet
chain data below is a deliberately local model for fail-closed boundaries.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from web3.datastructures import AttributeDict

from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_policy import MAINNET_FIELDS
from signer_agent.onboarding import assemble, fingerprint, join, validate_invite, verify_profile
from test_onboarding_mainnet import mainnet_invitation, signed_enrollments
from test_onboarding_policy_v3 import address, direct_profile


def ethereum_invitation():
    invitation = mainnet_invitation()
    invitation.update(version="5", chain_id="1", evm_confirmations="12", phase_duration="86400",
                      integration_fee_exemption=False,
                      deployment_fee=dict(amount="1234567", treasury=address(48)))
    return invitation


def ethereum_profile():
    invitation = ethereum_invitation()
    profile = direct_profile(invitation)
    profile.update({field: deepcopy(invitation[field]) for field in MAINNET_FIELDS})
    profile["deployment_fee"] = deepcopy(invitation["deployment_fee"])
    return profile


def test_v5_enrollment_commits_both_mainnets_and_exact_deployment_economics():
    invitation = ethereum_invitation()
    assert validate_invite(invitation) == invitation
    entries = signed_enrollments(invitation)
    profile = assemble(invitation, entries, "http://participant-local.invalid")
    assert verify_profile(profile) == invitation
    assert profile["chain_id"] == "1"
    assert profile["mode"] == "backed"
    assert profile["bitcoin_network"] == "mainnet"
    assert profile["deployment_fee"] == invitation["deployment_fee"]
    assert profile["evm_confirmations"] == "12"
    for key, value in (("amount", "1234568"), ("treasury", address(49))):
        changed = deepcopy(invitation)
        changed["deployment_fee"][key] = value
        assert fingerprint(changed) != fingerprint(invitation)
        with pytest.raises(ValueError):
            assemble(changed, entries)
        changed_profile = deepcopy(profile)
        changed_profile["deployment_fee"][key] = value
        with pytest.raises(ValueError):
            verify_profile(changed_profile)


@pytest.mark.parametrize("field,value", [
    ("chain_id", "31337"), ("chain_id", "11155111"), ("chain_id", "01"),
    ("chain_id", 1), ("chain_id", "10"), ("bitcoin_network", "regtest"),
    ("mode", "integration"), ("integration_fee_exemption", True),
    ("integration_fee_exemption", "false"), ("evm_confirmations", "1"),
    ("evm_confirmations", "11"), ("evm_confirmations", "257"),
    ("evm_confirmations", 12), ("evm_confirmations", "012"),
])
def test_v5_never_crosses_testnet_or_weakens_finality(field, value):
    invitation = ethereum_invitation()
    invitation[field] = value
    with pytest.raises(ValueError):
        validate_invite(invitation)


@pytest.mark.parametrize("field,value", [
    ("amount", "0"), ("amount", "-1"), ("amount", "01"),
    ("amount", "1.0"), ("amount", "1e6"), ("amount", 1),
    ("amount", True), ("amount", str(2**256)), ("amount", "١"),
    ("treasury", "0x" + "00" * 20), ("treasury", "not-an-address"),
    ("treasury", address(48).lower()), ("rpc", "https://private.invalid/token"),
])
def test_v5_deployment_fee_schema_is_exact_and_unambiguous(field, value):
    invitation = ethereum_invitation()
    invitation["deployment_fee"][field] = value
    with pytest.raises(ValueError):
        validate_invite(invitation)


@pytest.mark.parametrize("missing", ["amount", "treasury"])
def test_v5_never_fills_missing_financial_fields(missing):
    invitation = ethereum_invitation()
    del invitation["deployment_fee"][missing]
    before = deepcopy(invitation)
    with pytest.raises(ValueError):
        validate_invite(invitation)
    assert invitation == before


def test_legacy_v4_remains_test_evm_only_and_has_no_paid_field():
    invitation = mainnet_invitation()
    assert validate_invite(invitation) == invitation
    invitation["chain_id"] = "1"
    with pytest.raises(ValueError):
        validate_invite(invitation)
    invitation = mainnet_invitation()
    invitation["deployment_fee"] = ethereum_invitation()["deployment_fee"]
    with pytest.raises(ValueError):
        validate_invite(invitation)


def test_v5_join_requires_own_checked_core_before_secret_files(tmp_path, monkeypatch):
    invitation = ethereum_invitation()
    absent = tmp_path / "identity-must-not-be-opened"
    destination = tmp_path / "participant-must-not-be-created"
    with pytest.raises(ValueError, match="MAINNET_CORE_REQUIRED"):
        join(invitation, fingerprint(invitation), absent, destination)

    def reject(core, network, policy):
        assert network == "mainnet"
        assert policy == invitation["bitcoin_policy"]
        raise ValueError("MAINNET_CORE_NOT_SYNCED")

    monkeypatch.setattr("signer_agent.bitcoin_network.check_core", reject)
    with pytest.raises(ValueError, match="MAINNET_CORE_NOT_SYNCED"):
        join(invitation, fingerprint(invitation), absent, destination, bitcoin=object())
    assert not destination.exists()


def test_chain1_paid_plan_orders_payer_authorization_before_one_shot_fees():
    profile = ethereum_profile()
    before = deepcopy(profile)
    plan = build(profile)
    assert profile == before
    steps = plan["steps"]
    index = {step["id"]: i for i, step in enumerate(steps)}
    # Neither local test-token minting nor a free initial factory may enter a
    # mainnet plan. The factory contract itself also forbids that exemption.
    assert not any(step.get("artifact") in {"DAOTestUSD", "DAOMockFeed", "RegtestHeaderRelay"}
                   or step.get("method") == "mint" for step in steps)
    fee_steps = [step for step in steps if step.get("method") == "configureDeploymentFee"]
    assert {step["contract"] for step in fee_steps} == {"federations", "confederations"}
    router_name = fee_steps[0]["args"][0].removeprefix("$")
    assert all(step["args"] == ["$" + router_name, profile["deployment_fee"]["amount"], False]
               for step in fee_steps)
    router = next(step for step in steps if step["id"] == router_name)
    assert router["artifact"] == "CustodianDAOFeeRouter"
    assert router["args"][1:] == [profile["deployment_fee"]["treasury"], "0", "0"]
    vault_name = router["args"][0].removeprefix("$")
    vault = next(step for step in steps if step["id"] == vault_name)
    assert vault["artifact"] == "CustodianDAOIncomeVault"
    assert vault["args"][0] == "$usd"
    auth_name = vault["args"][1].removeprefix("$")
    auth = next(step for step in steps if step["id"] == auth_name)
    assert profile["evm_ceremony"]["id"] in auth["args"]
    assert index[auth_name] < index[vault_name] < index[router_name]
    payers = [step for step in steps if step.get("contract") == router_name
              and step.get("method") == "setPayer"]
    assert {step["args"][0] for step in payers} == {"$federations", "$confederations"}
    assert all(step["args"][1] is True for step in payers)
    for fee in fee_steps:
        assert all(index[payer["id"]] < index[fee["id"]] for payer in payers)
    for contract, method in (("federations", "deployFederation"), ("confederations", "deployConfederation")):
        creation = next(step for step in steps if step.get("contract") == contract and step.get("method") == method)
        approval = [step for step in steps if step.get("contract") == "usd"
                    and step.get("method") == "approve" and step.get("args") ==
                    ["$" + contract, profile["deployment_fee"]["amount"]]]
        assert len(approval) == 1  # No unlimited approval, no shared allowance.
        fee = next(step for step in fee_steps if step["contract"] == contract)
        assert index[fee["id"]] < index[approval[0]["id"]] < index[creation["id"]]


def _web3(*, chain=1, head=100, finalized=80):
    blocks = {number: AttributeDict(dict(number=number, hash=number.to_bytes(32, "big")))
              for number in range(head + 1)}
    calls = []

    def get_block(tag):
        calls.append(tag)
        if tag == "latest":
            return blocks[head]
        if tag == "finalized":
            if finalized is None:
                raise RuntimeError("finalized not available")
            return blocks[finalized]
        return blocks[tag]

    return SimpleNamespace(eth=SimpleNamespace(chain_id=chain, block_number=head, get_block=get_block)), blocks, calls


@pytest.mark.parametrize("finalized,expected", [(80, 80), (95, 89)])
def test_chain1_read_snapshot_obeys_both_depth_and_finalized(finalized, expected):
    from signer_agent.evm_network import read_block
    w3, blocks, calls = _web3(finalized=finalized)
    assert read_block(w3, 12) == blocks[expected]
    assert "finalized" in calls


@pytest.mark.parametrize("confirmations", [0, 1, 11, 257])
def test_chain1_rejects_insufficient_or_out_of_range_depth(confirmations):
    from signer_agent.evm_network import read_block
    w3, _, _ = _web3()
    with pytest.raises(ValueError):
        read_block(w3, confirmations)


def test_chain1_never_falls_back_to_latest_if_finalized_is_unavailable():
    from signer_agent.evm_network import read_block
    w3, _, _ = _web3(finalized=None)
    with pytest.raises(ValueError):
        read_block(w3, 12)


@pytest.mark.parametrize("number,expected", [(80, True), (81, False), (89, False), (99, False)])
def test_chain1_receipt_requires_finalized_block_even_after_twelve_confirmations(number, expected):
    from signer_agent.evm_network import receipt_finalized
    w3, blocks, _ = _web3()
    assert receipt_finalized(w3, number, blocks[number]["hash"], 12) is expected


def test_chain1_receipt_reorg_cannot_be_accepted_as_finalized():
    from signer_agent.evm_network import receipt_finalized
    w3, _, _ = _web3()
    try:
        result = receipt_finalized(w3, 70, b"x" * 32, 12)
    except ValueError:
        return
    assert result is False


@pytest.mark.parametrize("chain,depth", [(31337, 1), (11155111, 3)])
def test_test_evm_depth_semantics_do_not_require_finalized_rpc(chain, depth):
    from signer_agent.evm_network import read_block
    w3, blocks, calls = _web3(chain=chain, finalized=None)
    assert read_block(w3, depth) == blocks[100 - depth + 1]
    assert "finalized" not in calls


def _project_graph(monkeypatch, factory="federations"):
    """Model public getters; verify the exact expected constructor arguments."""
    from signer_agent import ethereum_intents as review
    from signer_agent.bootstrap import artifact, encode_args
    from signer_agent.dkg_protocol import Manifest
    from web3 import Web3

    invite = ethereum_invitation()
    reward_code = b"reviewed non-test external reward token"
    invite["reward_token"]["runtime_hash"] = Web3.to_hex(Web3.keccak(reward_code))
    profile = assemble(invite, signed_enrollments(invite), "http://local.invalid")
    plan = build(profile)
    deployments = [step for step in plan["steps"] if step["kind"] == "deploy"]
    addresses = {step["id"]: address(100 + index) for index, step in enumerate(deployments)}
    addresses["usd"] = invite["reward_token"]["address"]
    addresses["feed"] = invite["btc_usd_feed"]["address"]
    group = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
    values = dict(addresses, project_group="0x" + group.hex())
    constructors, kinds, getters, proofs = {}, {}, {}, []

    def resolve(value):
        if isinstance(value, str) and value.startswith("$"):
            return values[value[1:]]
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    for step in deployments:
        a = addresses[step["id"]]
        kinds[a] = step["artifact"]
        getters[a] = {}
        # Only the fee graph is requested by the verifier. Other deployments
        # may refer to event-derived addresses unavailable before factories run.
        try:
            resolved = resolve(step["args"])
            constructor = next((x for x in artifact(step["artifact"])["abi"] if x["type"] == "constructor"), {"inputs": []})
            constructors[a] = encode_args(constructor["inputs"], resolved)
            for check in step["checks"]:
                getters[a][check["method"]] = resolve(check["equals"])
        except KeyError:
            continue
        if step["artifact"].endswith("Deployer"):
            getters[a]["deployerOwner"] = invite["operator"]
        if step["artifact"].endswith("Factory"):
            getters[a].update(feeSetupAuthority=invite["operator"], deploymentFeeConfigured=False)
    kinds[addresses["usd"]] = "IERC20Metadata"
    getters[addresses["usd"]] = {"decimals": 6}
    c = profile["evm_ceremony"]
    manifest = Manifest(1, addresses["dkg"], bytes.fromhex(c["id"][2:]), int(c["epoch"]), "EVM_AUTH",
                        tuple(c["participants"]), tuple(c["transport_keys"]), int(c["threshold"]))
    bundle = dict(manifest.to_dict(), onboarding=profile["onboarding"], role="evm_ceremony")
    result = [manifest.context, b"r" * 32, b"t" * 32, Web3.keccak(text="EVM_AUTH"), group,
              list(manifest.participants), manifest.threshold]
    getters[addresses["dkg"]]["getCompletedResult"] = result
    getters[addresses["coordinator"]].update(dkgRegistry=addresses["dkg"], hasRole=False)
    getters[addresses["fee_system"]].update(feeManagerDeployer=addresses["fee_leaf"],
        feeDistributorDeployer=addresses["distribution_leaf"], nftModifierDeployer=addresses["nft_leaf"],
        walletRegistryDeployer=addresses["wallet_leaf"])
    getters[addresses["project_auth"]].update(coordinator=addresses["coordinator"], registry=addresses["dkg"],
                                              dkgId=manifest.session_id, resultHash=result[1])
    getters[addresses["project_vault"]].update(settlement="0x" + "00" * 20, sources=[0, 0, False, 0, 0])
    getters[addresses["project_router"]].update(rewardToken=addresses["usd"], authorization=addresses["project_auth"],
                                                treasury=invite["deployment_fee"]["treasury"], wrapStakerBps=0, deployStakerBps=0)
    w3, blocks, calls = _web3()

    class Functions:
        def __init__(self, a):
            self.address = a
        def __getattr__(self, method):
            def method_call(*args):
                def call(*, block_identifier):
                    assert block_identifier == 80
                    return getters[self.address][method]
                return SimpleNamespace(call=call)
            return method_call

    def contract(*, address, abi):
        return SimpleNamespace(address=address, functions=Functions(address))

    def checked(w3, a, kind, number):
        assert number == 80
        if kinds[a] != kind:
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
        return "runtime"

    def prove(w3, a, kind, args, number):
        assert number == 80
        assert kinds[a] == kind
        if args is not None:
            assert args == constructors[a], (kind, args, constructors[a])
        proofs.append((a, kind, args))
        return dict(method="direct", sender=invite["operator"])

    w3.eth.contract = contract
    w3.eth.get_code = lambda a, block_identifier: reward_code
    monkeypatch.setattr(review, "checked_runtime", checked)
    monkeypatch.setattr(review, "prove_creation", prove)
    intent = dict(chain_id="1", role="evm_ceremony", artifact="CustodianDAOFeeRouter", target=addresses["project_router"],
                  coordinator=addresses["coordinator"], hash_method="getSetPayerOperationHash", hash_args=[addresses[factory], True])
    return review, w3, intent, bundle, addresses, getters, proofs


@pytest.mark.parametrize("factory", ["federations", "confederations"])
def test_project_payer_proves_entire_pre_dao_graph_with_exact_constructors(monkeypatch, factory):
    review, w3, intent, bundle, addresses, _, proofs = _project_graph(monkeypatch, factory)
    assert review.review_project_payer(w3, intent, bundle, 80) is True
    names = {kind for _, kind, _ in proofs}
    assert {"DKGCoordinator", "FROSTCoordinator", "FROSTVerifier", "DKGAuthorization",
            "CustodianDAOIncomeVault", "CustodianDAOFeeRouter", "FederationFactory",
            "FederationDeployer", "FeeSystemDeployer", "SimpleMultiPoolDeployer",
            "FROSTAuthorizationDeployer", "MultiPoolShareTokenDeployer"} <= names
    if factory == "confederations":
        assert {"ConfederationFactory", "ConfederationDeployer", "MultiPoolDeployer",
                "ConfederationTokenDeployer", "FROSTCustodianDeployer"} <= names
    assert not {"BitcoinBatchRegistry", "CustodianValidator", "MiningPoolV1"} & names


@pytest.mark.parametrize("target,method,value", [
    ("project_router", "treasury", address(50)), ("project_router", "wrapStakerBps", 1),
    ("project_router", "deployStakerBps", 1), ("project_router", "rewardToken", address(50)),
    ("project_router", "authorization", address(50)), ("project_vault", "settlement", address(50)),
    ("project_vault", "sources", [0, 0, True, 0, 0]), ("project_auth", "resultHash", b"x" * 32),
    ("project_auth", "dkgId", b"x" * 32), ("coordinator", "hasRole", True),
    ("coordinator", "dkgRegistry", address(50)), ("usd", "decimals", 18),
    ("federations", "feeSetupAuthority", address(50)), ("simple_leaf", "deployerOwner", address(50)),
])
def test_project_payer_refuses_changed_public_economics_authority_or_dkg(monkeypatch, target, method, value):
    review, w3, intent, bundle, addresses, getters, _ = _project_graph(monkeypatch)
    getters[addresses[target]][method] = value
    with pytest.raises(ValueError):
        review.review_project_payer(w3, intent, bundle, 80)


def test_project_payer_refuses_foreign_canonical_creator(monkeypatch):
    review, w3, intent, bundle, _, _, _ = _project_graph(monkeypatch)
    original = review.prove_creation
    def foreign(*args):
        proof = original(*args)
        proof["sender"] = address(51)
        return proof
    monkeypatch.setattr(review, "prove_creation", foreign)
    with pytest.raises(ValueError, match="ETHEREUM_PROJECT_CREATOR"):
        review.review_project_payer(w3, intent, bundle, 80)


def test_project_payer_never_reviews_unfinalized_intent(monkeypatch):
    review, w3, intent, bundle, _, _, proofs = _project_graph(monkeypatch)
    with pytest.raises(ValueError, match="ETHEREUM_PROJECT_UNFINALIZED"):
        review.review_project_payer(w3, intent, bundle, 81)
    assert proofs == []


@pytest.mark.parametrize("field,value", [("role", "conf_ceremony"), ("chain_id", "31337"),
    ("hash_method", "getSetTreasuryOperationHash"), ("hash_args", [address(1), False]),
    ("hash_args", [address(1), 1])])
def test_project_payer_cannot_be_repurposed_for_unenrolled_operation(monkeypatch, field, value):
    review, w3, intent, bundle, _, _, proofs = _project_graph(monkeypatch)
    intent[field] = value
    with pytest.raises(ValueError, match="ETHEREUM_PROJECT_INTENT_POLICY"):
        review.review_project_payer(w3, intent, bundle, 80)
    assert proofs == []


BUDGET = {
    "SATOSHIFI_MAX_FEE_PER_GAS_WEI": "100000000000",
    "SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI": "2000000000",
    "SATOSHIFI_MAX_TX_GAS": "15000000",
}


@pytest.mark.parametrize("missing", BUDGET)
def test_chain1_transaction_budget_requires_each_operator_local_cap(monkeypatch, missing):
    from signer_agent.evm_network import transaction_policy
    for field, value in BUDGET.items():
        monkeypatch.setenv(field, value)
    monkeypatch.delenv(missing)
    w3, _, _ = _web3()
    with pytest.raises(ValueError, match="ETHEREUM_TRANSACTION_BUDGET_REQUIRED"):
        transaction_policy(w3)


@pytest.mark.parametrize("field", BUDGET)
@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "01", "1.5", "1e6", "١", str(2**256)])
def test_chain1_transaction_budget_rejects_ambiguous_values(monkeypatch, field, value):
    from signer_agent.evm_network import transaction_policy
    for name, default in BUDGET.items():
        monkeypatch.setenv(name, default)
    monkeypatch.setenv(field, value)
    w3, _, _ = _web3()
    with pytest.raises(ValueError, match="ETHEREUM_TRANSACTION_BUDGET"):
        transaction_policy(w3)


@pytest.mark.parametrize("field,value", [
    ("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "100000000001"),
    ("SATOSHIFI_MAX_TX_GAS", str(2**24 + 1)),
])
def test_chain1_transaction_budget_obeys_fee_relationship_and_protocol_gas_cap(monkeypatch, field, value):
    from signer_agent.evm_network import transaction_policy
    for name, default in BUDGET.items():
        monkeypatch.setenv(name, default)
    monkeypatch.setenv(field, value)
    w3, _, _ = _web3()
    with pytest.raises(ValueError, match="ETHEREUM_TRANSACTION_BUDGET_INVALID"):
        transaction_policy(w3)


@pytest.mark.parametrize("gas,accepted", [(1, True), (15000000, True), (15000001, False),
    (2**24 + 1, False), (0, False), (-1, False), (True, False), ("1", False), (None, False)])
def test_chain1_estimated_transaction_cannot_exceed_local_budget(monkeypatch, gas, accepted):
    from signer_agent.evm_network import transaction_policy, check_transaction_gas
    for field, value in BUDGET.items():
        monkeypatch.setenv(field, value)
    w3, _, _ = _web3()
    assert transaction_policy(w3) == dict(maxFeePerGas=100000000000, maxPriorityFeePerGas=2000000000)
    if accepted:
        assert check_transaction_gas(w3, dict(gas=gas)) is None
    else:
        with pytest.raises(ValueError, match="ETHEREUM_TRANSACTION_GAS_LIMIT"):
            check_transaction_gas(w3, dict(gas=gas))


def test_test_evm_legacy_transactions_do_not_need_mainnet_budget(monkeypatch):
    from signer_agent.evm_network import transaction_policy, check_transaction_gas
    for field in BUDGET:
        monkeypatch.delenv(field, raising=False)
    w3, _, _ = _web3(chain=31337)
    assert transaction_policy(w3) == {}
    assert check_transaction_gas(w3, {}) is None


def test_mainnet_fresh_gas_margin_preserves_fees_and_original_estimate(monkeypatch):
    from signer_agent.evm_network import transaction_gas_with_margin
    for field, value in BUDGET.items():
        monkeypatch.setenv(field, value)
    w3, _, _ = _web3()
    original = dict(gas=77196, nonce=71, maxFeePerGas=3_000_000_000,
        maxPriorityFeePerGas=1_000_000_000, value=0)
    padded = transaction_gas_with_margin(w3, original)
    assert padded == dict(original, gas=202196)
    assert original["gas"] == 77196


@pytest.mark.parametrize("estimate,ceiling,accepted", [(21000, 146000, True),
    (21000, 145999, False), (77196, 77196, False), (2**24, 2**24, False),
    (1_000_001, 1_300_002, True), (1_000_001, 1_300_001, False)])
def test_mainnet_gas_margin_must_fit_the_operator_cap_without_clipping(monkeypatch, estimate, ceiling, accepted):
    from signer_agent.evm_network import transaction_gas_with_margin
    for field, value in BUDGET.items():
        monkeypatch.setenv(field, value)
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", str(ceiling))
    w3, _, _ = _web3()
    if accepted:
        assert transaction_gas_with_margin(w3, dict(gas=estimate))["gas"] <= ceiling
    else:
        with pytest.raises(ValueError, match="^ETHEREUM_TRANSACTION_GAS_LIMIT$"):
            transaction_gas_with_margin(w3, dict(gas=estimate))


@pytest.mark.parametrize("chain", [11155111, 31337])
def test_test_evm_gas_margin_covers_future_checkpoints_without_mainnet_budget(monkeypatch, chain):
    from signer_agent.evm_network import transaction_gas_with_margin
    for field in BUDGET:
        monkeypatch.delenv(field, raising=False)
    w3, _, _ = _web3(chain=chain)
    original = dict(gas=77196, nonce=71, gasPrice=1_000_000_000, value=0)
    assert transaction_gas_with_margin(w3, original) == dict(original, gas=202196)
    assert original["gas"] == 77196


@pytest.mark.parametrize("chain", [1, 11155111, 31337])
@pytest.mark.parametrize("gas", [None, 0, -1, True, "77196", 77196.0])
def test_gas_margin_rejects_invalid_estimates_on_every_supported_evm(monkeypatch, chain, gas):
    from signer_agent.evm_network import transaction_gas_with_margin
    for field, value in BUDGET.items():
        monkeypatch.setenv(field, value)
    w3, _, _ = _web3(chain=chain)
    with pytest.raises(ValueError, match="^ETHEREUM_TRANSACTION_GAS_LIMIT$"):
        transaction_gas_with_margin(w3, dict(gas=gas))


@pytest.mark.parametrize("kind", ["registry", "coordinator"])
def test_mainnet_legacy_sender_cannot_bypass_durable_journal_or_fee_budget(kind):
    from signer_agent.dkg import Registry
    from signer_agent.coordinator import CoordinatorClient
    cls = Registry if kind == "registry" else CoordinatorClient
    client = cls.__new__(cls)
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    client.account = SimpleNamespace(address=address(1))
    # No contract/account/config is supplied: reaching construction/signing
    # would fail the test rather than silently send through a legacy path.
    with pytest.raises(ValueError, match="ETHEREUM_DURABLE_IDENTITY_REQUIRED"):
        if kind == "registry":
            client.send("submitRound1")
        else:
            client._send(object())


def test_v5_mailbox_reads_enrolled_depth_even_when_discovery_default_is_newer(monkeypatch):
    from signer_agent import onchain_transport as transport
    from signer_agent.dkg_protocol import Manifest
    invitation = ethereum_invitation()
    invitation["evm_confirmations"] = "256"
    m = Manifest(1, address(60), bytes.fromhex(invitation["sessions"]["evm_ceremony"]), 1,
                 "EVM_AUTH", tuple(invitation["participants"]),
                 tuple(i.to_bytes(32, "big") for i in range(1, 5)), 3)
    w3, _, _ = _web3(head=400, finalized=380)
    calls = []
    def registry():
        def call(*, block_identifier):
            calls.append(("binding", block_identifier))
            return m.registry
        return SimpleNamespace(call=call)
    def packet(*args):
        def call(*, block_identifier):
            calls.append(("packet", block_identifier))
            assert block_identifier == 145
            return b'{"package":"finalized-enrolled-depth"}'
        return SimpleNamespace(call=call)
    board = SimpleNamespace(address=address(61), functions=SimpleNamespace(registry=registry, packet=packet))
    monkeypatch.setattr(transport, "_board_and_invitation", lambda *args: (board, invitation))
    monkeypatch.setattr(transport, "registry_at", lambda w3, a, b: calls.append(("origin", b.number)))
    box = transport.OnchainMailbox.__new__(transport.OnchainMailbox)
    box.registry = SimpleNamespace(w3=w3, contract=SimpleNamespace(address=m.registry))
    box.board = board
    assert box.get(m, "round1", m.participants[0]) == {"package": "finalized-enrolled-depth"}
    assert calls == [("origin", 145), ("binding", 145), ("packet", 145)]


def test_v5_manifest_anchors_registry_and_reads_to_enrolled_finalized_depth(monkeypatch):
    from signer_agent import onboarding, bootstrap, onchain_transport
    from web3 import Web3
    invitation = ethereum_invitation()
    invitation["evm_confirmations"] = "256"
    profile = assemble(invitation, signed_enrollments(invitation), "http://local.invalid")
    w3, blocks, _ = _web3(head=400, finalized=380)
    code_reads = []
    def get_code(a, *, block_identifier):
        code_reads.append(block_identifier)
        return b"\x01"
    w3.eth.get_code = get_code
    registry = SimpleNamespace(w3=w3)
    checks = []
    def check(manifest):
        assert registry.read_block_identifier == 145
        assert registry.bitcoin_graph["evm_confirmations"] == "256"
        checks.append(manifest.registry)
    registry.check = check
    registry.read = lambda name: int(invitation["phase_duration"])
    monkeypatch.setattr(onboarding, "Registry", lambda *args: registry)
    monkeypatch.setattr(onchain_transport, "registry_at", lambda w3, a, block: checks.append(block.number))
    monkeypatch.setattr(bootstrap, "artifact", lambda kind: dict(deployedBytecode=dict(object="01")))
    deployment = dict(contracts=dict(dkg=dict(address=address(60), runtime_hash=Web3.keccak(b"\x01").hex())))
    bundle = onboarding.make_manifest(profile, deployment, "evm_ceremony", "http://local.invalid")
    assert bundle["anchor"] == dict(number="145", hash=blocks[145].hash.hex())
    assert checks == [145, address(60)]
    assert code_reads == [145, 145]


def test_v5_ceremony_bundle_uses_finalized_registry_binding_and_anchor(monkeypatch):
    from signer_agent import onchain_transport as transport, onboarding
    w3, blocks, _ = _web3(head=400, finalized=380)
    w3.provider = SimpleNamespace(endpoint_uri="http://local.invalid")
    invitation = ethereum_invitation()
    invitation["evm_confirmations"] = "256"
    calls = []
    def read(method, result):
        def call(*, block_identifier):
            calls.append((method, block_identifier))
            return result
        return lambda: SimpleNamespace(call=call)
    board = SimpleNamespace(address=address(61), functions=SimpleNamespace(
        registry=read("registry", address(60)), registryBlock=read("registryBlock", 140)))
    w3.eth.get_code = lambda a, block_identifier: calls.append(("code", block_identifier)) or b"\x01"
    monkeypatch.setattr(transport, "invitation", lambda *args: (board, invitation))
    monkeypatch.setattr(transport, "registry_at", lambda w3, a, block: calls.append(("origin", block.number)))
    monkeypatch.setattr(transport, "assembled_profile", lambda *args: {})
    monkeypatch.setattr(onboarding, "make_manifest", lambda *args: {})
    bundle = transport.ceremony_bundle(w3, board.address, "evm_ceremony")
    assert bundle == dict(anchor=dict(number="140", hash=blocks[140].hash.hex()), onchain_board=board.address)
    assert calls == [("registry", 145), ("origin", 145), ("code", 145), ("registryBlock", 145)]


def test_v5_generic_preflight_authenticates_coordinator_and_invokes_project_policy(monkeypatch):
    review, w3, intent, bundle, addresses, _, proofs = _project_graph(monkeypatch)
    assert review.review_bootstrap_intent(w3, intent, bundle, 80) is True
    assert (addresses["coordinator"], "FROSTCoordinator", [addresses["verifier"]]) in proofs
    assert (addresses["project_router"], "CustodianDAOFeeRouter", None) in proofs
    assert (addresses["project_router"], "CustodianDAOFeeRouter",
            [addresses["project_vault"], bundle["onboarding"]["invitation"]["deployment_fee"]["treasury"], 0, 0]) in proofs


@pytest.mark.parametrize("role", [None, "conf_ceremony", "btc_ceremony"])
def test_v5_generic_preflight_cannot_skip_project_policy_by_relabeling_role(monkeypatch, role):
    review, w3, intent, bundle, _, _, proofs = _project_graph(monkeypatch)
    intent["role"] = role
    with pytest.raises(ValueError, match="ETHEREUM_BOOTSTRAP_POLICY"):
        review.review_bootstrap_intent(w3, intent, bundle, 80)
    assert proofs == []


@pytest.mark.parametrize("component", ["coordinator", "verifier", "dkg", "project_router"])
def test_v5_generic_preflight_rejects_forged_runtime_before_private_crypto(monkeypatch, component):
    review, w3, intent, bundle, addresses, _, _ = _project_graph(monkeypatch)
    original = review.checked_runtime
    def reject(w3, a, kind, block):
        if a == addresses[component]:
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
        return original(w3, a, kind, block)
    monkeypatch.setattr(review, "checked_runtime", reject)
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        review.review_bootstrap_intent(w3, intent, bundle, 80)


@pytest.mark.parametrize("component,method,value", [
    ("coordinator", "hasRole", True), ("coordinator", "dkgRegistry", address(99)),
    ("project_auth", "resultHash", b"x" * 32), ("project_auth", "dkgId", b"x" * 32),
])
def test_v5_generic_preflight_refuses_wrong_authority_and_epoch(monkeypatch, component, method, value):
    review, w3, intent, bundle, addresses, getters, _ = _project_graph(monkeypatch)
    getters[addresses[component]][method] = value
    with pytest.raises(ValueError, match="ETHEREUM_BOOTSTRAP_AUTHORIZATION"):
        review.review_bootstrap_intent(w3, intent, bundle, 80)


@pytest.mark.parametrize("kind,leaf,event_name", [
    ("SimpleMultiPool", "simple_leaf", "SimpleMultiPoolDeployed"),
    ("ConfederationMultiPool", "conf_pool_leaf", "MultiPoolDeployed"),
])
@pytest.mark.parametrize("attack", [None, "fake_creator", "missing_event", "reorg"])
def test_internal_pool_provenance_authenticates_birth_event_and_leaf_constructor(monkeypatch, kind, leaf, event_name, attack):
    from eth_abi import encode
    from web3 import Web3
    from signer_agent.bootstrap import artifact
    from eth_utils.abi import collapse_if_tuple
    review, w3, _, bundle, addresses, _, proofs = _project_graph(monkeypatch)
    target = SimpleNamespace(address=address(88))
    leaf_kind = "SimpleMultiPoolDeployer" if kind == "SimpleMultiPool" else "MultiPoolDeployer"
    event = next(x for x in artifact(leaf_kind)["abi"] if x.get("type") == "event" and x["name"] == event_name)
    topic = Web3.keccak(text=event_name + "(" + ",".join(collapse_if_tuple(x) for x in event["inputs"]) + ")")
    birth_hash, tx_hash = (70).to_bytes(32, "big"), b"t" * 32
    args = [target.address, addresses["project_auth"], address(89)] if kind == "SimpleMultiPool" else [target.address, address(89)]
    log = dict(address=addresses[leaf], topics=[topic, b"i" * 32, bytes.fromhex(address(90)[2:]).rjust(32, b"\0")],
               data=encode(["address"] * len(args), args), blockNumber=70, blockHash=birth_hash,
               transactionHash=tx_hash, transactionIndex=0, logIndex=0)
    receipt = dict(status=1, blockNumber=70, blockHash=birth_hash, transactionHash=tx_hash,
                   logs=[] if attack == "missing_event" else [log])
    original_block = w3.eth.get_block
    def get_block(number, full_transactions=False):
        if number == 70 and full_transactions:
            return AttributeDict(dict(number=70, hash=birth_hash, transactions=[dict(hash=tx_hash)]))
        if number == 70 and attack == "reorg":
            return AttributeDict(dict(number=70, hash=b"x" * 32))
        return original_block(number)
    w3.eth.get_block = get_block
    w3.eth.get_transaction_receipt = lambda tx: receipt
    w3.codec = Web3().codec
    monkeypatch.setattr(review, "birth_block", lambda *args: 70)
    if attack == "fake_creator":
        original_prove = review.prove_creation
        def foreign(*args):
            proof = original_prove(*args)
            proof["sender"] = address(99)
            return proof
        monkeypatch.setattr(review, "prove_creation", foreign)
    if attack:
        with pytest.raises(ValueError, match="ETHEREUM_BOOTSTRAP_ORIGIN|ETHEREUM_PROJECT_SNAPSHOT_REORG"):
            review._prove_pool(w3, target, kind, bundle["onboarding"]["invitation"]["operator"], 80)
    else:
        review._prove_pool(w3, target, kind, bundle["onboarding"]["invitation"]["operator"], 80)
        assert (addresses[leaf], leaf_kind) in {(a, k) for a, k, _ in proofs}
