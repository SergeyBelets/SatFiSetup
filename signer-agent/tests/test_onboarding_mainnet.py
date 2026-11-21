"""V4 Bitcoin mainnet enrollment and deployment graph boundaries.

Uses authentic historical Bitcoin headers for schema validation, never a live
mainnet wallet, RPC transaction or invented proof of work.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict

from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_policy import MAINNET_FIELDS
from signer_agent.bitcoin_network import MAINNET_GENESIS, relay_constructor_args
from signer_agent.onboarding import (
    ROLES, assemble, enrollment_message, fingerprint, join, main, validate_invite, verify_profile,
)
from signer_agent.operations import Journal
from test_onboarding_policy_v3 import invitation, direct_profile, invite_args, address

ROOT = Path(__file__).resolve().parents[2]


def bitcoin_policy():
    folder = ROOT / "web3/test/fixtures/mainnet"
    headers = (folder / "headers-32246-36288.bin").read_bytes()
    manifest = json.loads((folder / "manifest.json").read_text())
    return dict(
        version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
        checkpoint_header=headers[800:880].hex(), checkpoint_height="32256",
        checkpoint_chainwork=manifest["anchors"]["32256"]["chainwork"],
        checkpoint_predecessors=[headers[i * 80:(i + 1) * 80].hex() for i in range(10)],
        max_reorg_depth="12", max_tip_age_seconds="7200", max_relay_lag_blocks="2",
        max_headers_per_sync="16",
    )


def mainnet_invitation():
    proposal = invitation()
    proposal.update(version="4", mode="backed", bitcoin_network="mainnet",
                    bitcoin_policy=bitcoin_policy(), evm_confirmations="1",
                    reward_token=dict(address=address(44), runtime_hash="0x" + "ab" * 32, decimals="6"),
                    btc_usd_feed=dict(address=address(45), runtime_hash="0x" + "cd" * 32))
    return proposal


def mainnet_profile():
    proposal = mainnet_invitation()
    profile = direct_profile(proposal)
    profile.update({field: proposal[field] for field in MAINNET_FIELDS})
    return profile


def signed_enrollments(proposal):
    result = []
    for index, member in enumerate(proposal["participants"], 1):
        body = dict(invitation_hash=fingerprint(proposal), participant=member,
                    transport_keys={role: (index * 32 + offset).to_bytes(32, "big").hex()
                                    for offset, role in enumerate(ROLES)})
        result.append(dict(body, signature=Account.from_key(index.to_bytes(32, "big")).sign_message(
            enrollment_message(body)).signature.hex()))
    return result


def test_v4_commits_every_mainnet_policy_and_economic_input():
    proposal = mainnet_invitation()
    assert validate_invite(proposal) == proposal
    entries = signed_enrollments(proposal)
    profile = assemble(proposal, entries, "http://own-node.invalid")
    for field in MAINNET_FIELDS:
        assert profile[field] == proposal[field]
    assert verify_profile(profile) == proposal
    for field in MAINNET_FIELDS:
        changed = deepcopy(profile)
        changed[field] = None
        with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
            verify_profile(changed)
    changed = deepcopy(proposal)
    changed["bitcoin_policy"]["max_tip_age_seconds"] = "3600"
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(changed, entries)


@pytest.mark.parametrize("field,value", [
    ("version", "3"), ("bitcoin_network", "regtest"), ("mode", "integration"),
    ("chain_id", "1"), ("chain_id", "01"), ("evm_confirmations", "0"),
    ("evm_confirmations", 1), ("evm_confirmations", "01"),
])
def test_v4_rejects_network_and_policy_mixing(field, value):
    proposal = mainnet_invitation()
    proposal[field] = value
    with pytest.raises(ValueError):
        validate_invite(proposal)


@pytest.mark.parametrize("field", MAINNET_FIELDS)
def test_v4_requires_every_policy(field):
    proposal = mainnet_invitation()
    del proposal[field]
    with pytest.raises(ValueError, match="INVITATION_SCHEMA"):
        validate_invite(proposal)


@pytest.mark.parametrize("field,key,value", [
    ("reward_token", "decimals", "19"), ("reward_token", "decimals", 6),
    ("reward_token", "runtime_hash", "0x" + "AB" * 32),
    ("reward_token", "runtime_hash", "0x" + "00" * 32),
    ("reward_token", "address", "0x" + "00" * 20),
    ("btc_usd_feed", "rpc", "http://private-credential.invalid"),
    ("btc_usd_feed", "runtime_hash", "cd" * 32),
])
def test_v4_rejects_ambiguous_or_uncommitted_external_components(field, key, value):
    proposal = mainnet_invitation()
    proposal[field][key] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER"):
        validate_invite(proposal)


def test_v4_mainnet_plan_has_no_test_tokens_no_mint_and_guarded_spv():
    profile = mainnet_profile()
    result = build(profile)
    steps = {step["id"]: step for step in result["steps"]}
    artifacts = {step.get("artifact") for step in result["steps"]}
    assert "RegtestHeaderRelay" not in artifacts
    assert "DAOTestUSD" not in artifacts
    assert "DAOMockFeed" not in artifacts
    assert not any(step.get("method") == "mint" for step in result["steps"])
    assert steps["relay"]["args"] == Bootstrap.wire(relay_constructor_args(profile["bitcoin_policy"]))
    assert steps["spv_guard"]["args"] == ["$relay", "7200"]
    assert steps["escrow"]["args"][1] == "$spv_guard"
    assert steps["deposits"]["args"][0] == "$spv_guard"
    assert steps["bitcoin_relay_ready"]["kind"] == "bitcoin_ready"
    assert list(steps).index("bitcoin_relay_ready") < list(steps).index("escrow")
    assert steps["fund_mainnet_converter"]["method"] == "transfer"
    assert steps["fund_mainnet_converter"]["verify_token_transfer"]["amount"] == profile["converter_funding"]
    assert result["contracts"]["usd"]["artifact"] == "IERC20Metadata"
    assert result["contracts"]["feed"]["artifact"] == "IChainlinkBTCUSD"


@pytest.mark.parametrize("chain_id", ["1", "10", "42161"])
def test_v4_retains_actual_factory_evm_gates(chain_id, tmp_path):
    profile = mainnet_profile()
    profile["chain_id"] = chain_id
    with pytest.raises(ValueError, match="INTEGRATION_NETWORK_NOT_ALLOWED"):
        build(profile)
    with pytest.raises(ValueError, match="INTEGRATION_NETWORK_NOT_ALLOWED"):
        Bootstrap(profile, tmp_path / "must-not-exist.sqlite")
    assert not (tmp_path / "must-not-exist.sqlite").exists()


def test_v4_cli_publishes_policy_in_invite_without_private_routing(tmp_path, capsys):
    proposal = mainnet_invitation()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(proposal["bitcoin_policy"]))
    output = tmp_path / "invitation.json"
    args = invite_args(proposal, output) + [
        "--bitcoin-network", "mainnet", "--bitcoin-policy", str(policy_path),
        "--reward-token", proposal["reward_token"]["address"],
        "--reward-token-code-hash", proposal["reward_token"]["runtime_hash"],
        "--reward-token-decimals", "6", "--btc-usd-feed", proposal["btc_usd_feed"]["address"],
        "--btc-usd-feed-code-hash", proposal["btc_usd_feed"]["runtime_hash"],
        "--evm-confirmations", "1",
    ]
    assert main(args) == 0
    saved = json.loads(output.read_text())
    assert saved["version"] == "6"
    assert saved["bitcoin_policy"] == proposal["bitcoin_policy"]
    assert str(policy_path) not in output.read_text()
    assert saved["mode"] == "backed"
    assert json.loads(capsys.readouterr().out)["fingerprint"] == fingerprint(saved)


@pytest.mark.parametrize("mainnet,args,error", [
    (True, ["--bitcoin-network", "mainnet"], "MAINNET_INVITATION_INPUTS_REQUIRED"),
    (False, ["--reward-token", "0x" + "11" * 20], "REGTEST_MAINNET_POLICY_MIXED"),
])
def test_v4_cli_never_silently_ignores_mainnet_fields(mainnet, args, error, tmp_path):
    proposal = mainnet_invitation() if mainnet else invitation()
    output = tmp_path / "invitation.json"
    with pytest.raises(ValueError, match=error):
        main(invite_args(proposal, output) + args)
    assert not output.exists()


def test_v4_join_requires_own_checked_core_before_opening_identity(tmp_path, monkeypatch):
    proposal = mainnet_invitation()
    path = tmp_path / "not-created"
    with pytest.raises(ValueError, match="MAINNET_CORE_REQUIRED"):
        join(proposal, fingerprint(proposal), tmp_path / "absent-identity", path)
    def reject(*args):
        assert args[1:] == ("mainnet", proposal["bitcoin_policy"])
        raise ValueError("MAINNET_CORE_NOT_SYNCED")
    monkeypatch.setattr("signer_agent.bitcoin_network.check_core", reject)
    with pytest.raises(ValueError, match="MAINNET_CORE_NOT_SYNCED"):
        join(proposal, fingerprint(proposal), tmp_path / "absent-identity", path, bitcoin=object())
    assert not path.exists()


def test_mainnet_ready_gate_rechecked_even_after_previous_confirmation(tmp_path):
    bootstrap = Bootstrap.__new__(Bootstrap)
    bootstrap.profile = dict(mode="backed", steps=[dict(id="ready", kind="bitcoin_ready")])
    bootstrap.prefix = "ready-check"
    bootstrap.journal = Journal(tmp_path / "state.sqlite")
    bootstrap.journal.put(bootstrap.prefix, {})
    bootstrap.contracts = {}
    bootstrap.manifest = lambda: {}
    checks = []
    def ready(identity, readonly):
        checks.append(readonly)
        return dict(status="MAINNET_RELAY_READY" if len(checks) == 1 else "IN_PROGRESS")
    bootstrap.bitcoin_ready = ready
    assert bootstrap.apply()["status"] == "VERIFIED"
    assert bootstrap.apply()["status"] == "AWAITING_BITCOIN_RELAY"
    assert checks == [False, False]


@pytest.mark.parametrize("received,success", [(100, True), (0, False), (99, False)])
def test_mainnet_converter_requires_exact_receipt_time_transfer(received, success):
    bootstrap = Bootstrap.__new__(Bootstrap)
    calls = []
    def balance(recipient):
        assert recipient == address(46)
        def call(**kwargs):
            calls.append(kwargs)
            return 7 + received
        return SimpleNamespace(call=call)
    bootstrap.contract = lambda token: SimpleNamespace(functions=SimpleNamespace(balanceOf=balance))
    bootstrap.resolve = lambda value: value
    step = dict(verify_token_transfer=dict(token="usd", recipient=address(46), amount="100"))
    if success:
        bootstrap.verify_token_funding(step, dict(transfer_balance_before="7"), SimpleNamespace(blockNumber=12))
    else:
        with pytest.raises(ValueError, match="INEXACT_BOOTSTRAP_FUNDING"):
            bootstrap.verify_token_funding(step, dict(transfer_balance_before="7"), SimpleNamespace(blockNumber=12))
    assert calls == [{"block_identifier": 12}]


def _external_bootstrap(code, decimals=6, *, expected_hash=None, policy_field="reward_token", chain=31337, network="mainnet"):
    bootstrap = Bootstrap.__new__(Bootstrap)
    component = dict(address=address(44), runtime_hash=expected_hash or "0x" + Web3.keccak(code).hex().removeprefix("0x"))
    if policy_field == "reward_token":
        component["decimals"] = "6"
    name = "usd" if policy_field == "reward_token" else "feed"
    bootstrap.profile = dict(mode="backed", chain_id=str(chain), bitcoin_network=network, **{policy_field: component})
    bootstrap.contracts = {name: dict(address=component["address"])}
    bootstrap.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=chain, get_code=lambda address, **kw: code))
    bootstrap.read_number = lambda: 7
    bootstrap.contract = lambda name: SimpleNamespace(functions=SimpleNamespace(
        decimals=lambda: SimpleNamespace(call=lambda **kwargs: decimals)))
    return bootstrap


def test_external_components_recheck_expected_code_and_decimal_scale():
    bootstrap = _external_bootstrap(b"external-reviewed-token")
    bootstrap.check_external_components()
    bootstrap.w3.eth.get_code = lambda _, **kw: b"changed"
    with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_CODE_HASH"):
        bootstrap.check_external_components()
    with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_DECIMALS"):
        _external_bootstrap(b"external-reviewed-token", decimals=18).check_external_components()
    with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_DECIMALS"):
        _external_bootstrap(b"external-reviewed-feed", decimals=6, policy_field="btc_usd_feed").check_external_components()


@pytest.mark.parametrize("artifact_name,policy_field,decimals", [("DAOTestUSD", "reward_token", 6), ("DAOMockFeed", "btc_usd_feed", 8)])
@pytest.mark.parametrize("network", ["regtest", "testnet4", "mainnet"])
def test_known_test_dependencies_cannot_be_relabelled_for_mainnet(artifact_name, policy_field, decimals, network):
    from signer_agent.bootstrap import artifact
    code = bytes.fromhex(artifact(artifact_name)["deployedBytecode"]["object"].removeprefix("0x"))
    with pytest.raises(ValueError, match="MAINNET_TEST_COMPONENT_FORBIDDEN"):
        _external_bootstrap(code, decimals=decimals, policy_field=policy_field, chain=1, network=network).check_external_components()


@pytest.mark.parametrize("artifact_name,policy_field,decimals", [("DAOTestUSD", "reward_token", 6), ("DAOMockFeed", "btc_usd_feed", 8)])
@pytest.mark.parametrize("chain", [31337, 11155111])
@pytest.mark.parametrize("network", ["testnet4", "mainnet"])
def test_explicit_pinned_test_components_are_allowed_on_test_evm_with_public_bitcoin(artifact_name, policy_field, decimals, chain, network):
    from signer_agent.bootstrap import artifact
    code = bytes.fromhex(artifact(artifact_name)["deployedBytecode"]["object"].removeprefix("0x"))
    bootstrap = _external_bootstrap(code, decimals=decimals, policy_field=policy_field, chain=chain, network=network)
    bootstrap.check_external_components()
    bootstrap.w3.eth.get_code = lambda address, **kwargs: b"another-test-component"
    with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_CODE_HASH"):
        bootstrap.check_external_components()


@pytest.mark.parametrize("confirmed", [False, True])
def test_publish_reports_board_block_only_after_confirmation(tmp_path, monkeypatch, capsys, confirmed):
    from signer_agent.onboarding import chain_main
    proposal = mainnet_invitation()
    proposal["evm_confirmations"] = "12"
    invite_file = tmp_path / "invite.json"
    invite_file.write_text(json.dumps(proposal))
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(dict(private_key="%064x" % 1)))
    identity.chmod(0o600)
    observed = []
    class Publication:
        def __init__(self, profile, state):
            observed.append(profile)
        def apply(self, identity):
            if confirmed:
                return dict(status="VERIFIED", manifest=dict(
                    contracts={"board": {"address": proposal["operator"]}},
                    steps=[dict(id=observed[0]["deployment_id"] + ":board", result=dict(block_number="400"))],
                ))
            return dict(status="CONFIRMING", step="board")
    monkeypatch.setattr("signer_agent.bootstrap.Bootstrap", Publication)
    assert chain_main(SimpleNamespace(action="publish", invite=invite_file, identity_file=identity,
                                     rpc="http://local.invalid", state=tmp_path / "state.sqlite")) == 0
    assert observed[0]["bitcoin_network"] == "mainnet"
    assert observed[0]["evm_confirmations"] == "12"
    result = json.loads(capsys.readouterr().out)
    if confirmed:
        assert result["status"] == "VERIFIED" and result["block"] == "400"
    else:
        assert result["status"] == "CONFIRMING" and "block" not in result


@pytest.mark.parametrize("readonly", [False, True])
def test_readiness_uses_enrolled_confirmed_snapshot_for_relay_and_guard(monkeypatch, readonly, tmp_path):
    bootstrap = Bootstrap.__new__(Bootstrap)
    bootstrap.bitcoin = object()
    bootstrap.profile = dict(chain_id="31337", bitcoin_policy=bitcoin_policy(), evm_confirmations="3")
    bootstrap.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, block_number=10,
        get_block=lambda number: AttributeDict(dict(number=10 if number == "latest" else number, hash=b"canonical"))))
    calls = []
    relay = SimpleNamespace(address="0x" + "11" * 20)
    def fresh():
        def call(**kwargs):
            calls.append(("guard", kwargs["block_identifier"]))
            return True
        return SimpleNamespace(call=call)
    bootstrap.contract = lambda name: relay if name == "relay" else SimpleNamespace(functions=SimpleNamespace(isFresh=fresh))
    def inspect(bitcoin, received, policy, **kwargs):
        assert received is relay
        calls.append(("relay", kwargs["block_identifier"]))
        return dict(status="MAINNET_RELAY_READY")
    def sync(bitcoin, received, policy, callback, **kwargs):
        assert kwargs["evm_confirmations"] == 3
        calls.append(("sync", 3))
        return dict(status="MAINNET_RELAY_READY", submitted="0")
    monkeypatch.setattr("signer_agent.bitcoin_network.inspect_relay", inspect)
    monkeypatch.setattr("signer_agent.bitcoin_network.sync_relay", sync)
    assert bootstrap.bitcoin_ready(str(tmp_path / "identity"), readonly=readonly)["status"] == "MAINNET_RELAY_READY"
    assert calls == ([] if readonly else [("sync", 3)]) + [("relay", 8), ("guard", 8)]


def test_mainnet_consumers_get_fresh_readiness_check_before_registration():
    result = build(mainnet_profile())
    steps = {step["id"]: step for step in result["steps"]}
    assert steps["bitcoin_consumers_ready"] == dict(id="bitcoin_consumers_ready", kind="bitcoin_ready", sync=False)
    assert list(steps).index("mining") < list(steps).index("bitcoin_consumers_ready") < list(steps).index("register_mining_intent")


@pytest.mark.parametrize("client_kind", ["registry", "coordinator"])
@pytest.mark.parametrize("network", ["regtest", "mainnet"])
def test_dkg_and_evm_authorization_senders_preserve_bitcoin_network_and_confirmations(monkeypatch, client_kind, network):
    from signer_agent.dkg import Registry
    from signer_agent.coordinator import CoordinatorClient
    sent = []
    class Sender:
        def __init__(self, profile, state, *, evm_confirmation_floor=None, evm_rpc_override):
            assert evm_rpc_override == profile["evm_rpc"]
            sent.append((profile, evm_confirmation_floor))
            self.journal = SimpleNamespace(get=lambda key: dict(result=dict(tx_hash="durable-tx")))
        def apply_confirmed(self, identity):
            return dict(status="VERIFIED")
    monkeypatch.setattr("signer_agent.bootstrap.Bootstrap", Sender)
    # _send uses the wire helper on the same public class before constructing it.
    Sender.wire = Bootstrap.wire
    client = (Registry if client_kind == "registry" else CoordinatorClient).__new__(
        Registry if client_kind == "registry" else CoordinatorClient)
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_code=lambda addr: b"compiled"),
                               provider=SimpleNamespace(endpoint_uri="http://local.invalid"))
    client.identity_file = "own-private-file"
    client.transaction_journal = "own-private-journal"
    client.contract = SimpleNamespace(address=address(44), abi=[])
    if network == "mainnet":
        client.bitcoin_graph = dict(version="2", bitcoin_network="mainnet", bitcoin_policy=bitcoin_policy(), evm_confirmations="12")
    if client_kind == "registry":
        assert client.send("submitKey", "0x" + "ab" * 32) == "durable-tx"
    else:
        client.config = SimpleNamespace(rpc_url="http://local.invalid")
        assert client._send(SimpleNamespace(address=address(44), fn_name="submitNonceCommitments", args=[])) == "durable-tx"
    [(profile, floor)] = sent
    assert profile["bitcoin_network"] == network
    assert profile["mode"] == ("backed" if network == "mainnet" else "integration")
    enrolled_depth = "12" if network == "mainnet" else "1"
    assert profile.get("evm_confirmations", "1") == enrolled_depth
    assert floor == (int(enrolled_depth) if client_kind == "coordinator" else None)


def test_evm_signer_mainnet_reads_one_confirmed_snapshot_and_refuses_reorg_before_send():
    from signer_agent.coordinator import CoordinatorClient
    client = CoordinatorClient.__new__(CoordinatorClient)
    client.bitcoin_graph = dict(version="2", bitcoin_network="mainnet", bitcoin_policy=bitcoin_policy(), evm_confirmations="3")
    blocks = {8: SimpleNamespace(number=8, hash=b"stable"), 10: SimpleNamespace(number=10, hash=b"latest")}
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda number: blocks[10] if number == "latest" else blocks[number]))
    client.pin_read_snapshot()
    assert client._read_snapshot.number == 8
    calls = []
    client.sender = address(1)
    def committed(session, sender):
        def call(**kwargs):
            calls.append(kwargs)
            return True
        return SimpleNamespace(call=call)
    client.contract = SimpleNamespace(functions=SimpleNamespace(hasCommitted=committed))
    assert client.has_committed(7)
    assert calls == [{"block_identifier": 8}]
    blocks[8] = SimpleNamespace(number=8, hash=b"replacement")
    # No identity or sender/gas configuration is supplied: if _send reaches
    # transaction construction, the test fails instead of making an EVM call.
    with pytest.raises(ValueError, match="EVM_SIGNING_SNAPSHOT_REORG"):
        client._send(object())


def test_evm_signer_regtest_read_context_retains_legacy_call_shape():
    from signer_agent.coordinator import CoordinatorClient
    client = CoordinatorClient.__new__(CoordinatorClient)
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337))
    client.pin_read_snapshot()
    assert client._read_snapshot is None
    assert client._read_call(SimpleNamespace(call=lambda: "legacy")) == "legacy"


def test_evm_signer_sepolia_regtest_respects_enrolled_depth():
    from signer_agent.coordinator import CoordinatorClient
    client = CoordinatorClient.__new__(CoordinatorClient)
    client.bitcoin_graph = dict(version="2", bitcoin_network="regtest", evm_confirmations="3")
    blocks = {8: SimpleNamespace(number=8, hash=b"stable"), 10: SimpleNamespace(number=10, hash=b"latest")}
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111,
        get_block=lambda number: blocks[10] if number == "latest" else blocks[number]))
    client.pin_read_snapshot()
    assert client._read_snapshot.number == 8
    assert client._read_call(SimpleNamespace(call=lambda **kwargs: kwargs)) == {"block_identifier": 8}
