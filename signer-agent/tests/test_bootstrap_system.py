"""Complete signed deployment graph across independent EVM/Bitcoin networks."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent.bootstrap import artifact, encode_args
from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_system import bundle, manifest
from signer_agent.onboarding import assemble, validate_invite, verify_profile, main
from test_independent_networks import invitation as networks
from test_bootstrap_ulr import proposal
from test_onboarding_policy_v3 import invite_args, address
from test_onboarding_mainnet import signed_enrollments
from test_withdrawal_bootstrap import policy
from test_zk_bootstrap import zk_invite


def full_invite(chain="11155111", network="testnet4", backend="relay", mode="create"):
    p = networks(chain, network)
    p.pop("btc_usd_feed", None)
    if backend == "zk-state-v2":
        if network=='testnet4':
            from test_zk_testnet4 import zk_policy
            p['bitcoin_policy']=zk_policy(p['confederation_id'])
        else:p["bitcoin_policy"] = zk_invite()["bitcoin_policy"]
        p["bitcoin_policy"]["zk_spv"]["confederation_id"] = Web3.keccak(text=p["confederation_id"]).hex()
    p.update(system_bootstrap="1", wrap_staker_bps="0", deploy_staker_bps="0", dao_use_reserve=False,
        ulr=proposal(mode, backend, p["mode"] == "backed")["ulr"], withdrawal_fee=policy())
    p["withdrawal_fee"].update(version="2", family_enrollment="ORIGINAL_AUTHORITY", family_root=dict(
        version="create-1", creation_code_hash=Web3.to_hex(Web3.keccak(
            hexstr=artifact("BitcoinKeyFamilyRegistry")["bytecode"]["object"]))))
    return p


NETWORKS = [(chain, btc, "relay") for chain in ("1", "11155111", "31337") for btc in ("regtest", "testnet4", "mainnet")]
NETWORKS += [(chain, network, "zk-state-v2") for chain in ("1", "11155111", "31337") for network in ('mainnet','testnet4')]


def test_signed_testnet_dkg_duration_is_configured_before_any_ceremony():
    invite = dict(full_invite(), phase_duration="21600")
    p = assemble(invite, signed_enrollments(invite), "http://local.invalid")
    steps = build(p)["steps"]
    ids = [step["id"] for step in steps]
    configure = ids.index("configure_dkg_phase_duration")
    assert configure < min(ids.index(role + "_create") for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"))
    assert steps[configure]["args"] == ["21600"]
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        build(dict(p, phase_duration="7200"))



@pytest.mark.parametrize("chain,network,backend", NETWORKS)
@pytest.mark.parametrize("mode", ["create", "reuse"])
def test_full_graph_on_all_supported_network_pairs(chain, network, backend, mode):
    invite = full_invite(chain, network, backend, mode)
    validate_invite(invite)
    p = assemble(invite, signed_enrollments(invite), "http://local.invalid")
    p["onchain_board"] = address(90)
    steps = {s["id"]: s for s in build(p)["steps"]}
    order = list(steps)
    assert order.index("federations") < order.index("reserve_federation_id") < order.index("adopt_federation")
    reservation = steps["reserve_federation_id"]
    assert reservation["contract"] == "federations" and reservation["method"] == "reserveFederationId"
    assert reservation["args"] == [p["evm_ceremony"]["id"], invite["federation_id"]]
    assert reservation["args"] == steps["adopt_federation"]["args"]
    assert reservation["checks"] == [dict(contract="federations", method="reservedFederationSession",
        args=[invite["federation_id"]], equals=p["evm_ceremony"]["id"])]
    assert order.index("confederations") < order.index("reserve_confederation_id") < order.index("adopt_confederation")
    reservation = steps["reserve_confederation_id"]
    assert reservation["contract"] == "confederations" and reservation["method"] == "reserveConfederationId"
    assert reservation["args"] == [p["conf_ceremony"]["id"], invite["confederation_id"]]
    assert reservation["args"] == [steps["adopt_confederation"]["args"][0], steps["adopt_confederation"]["args"][-1]]
    assert reservation["checks"] == [dict(contract="confederations", method="reservedConfederationSession",
        args=[invite["confederation_id"]], equals=p["conf_ceremony"]["id"])]
    for name in ("family_root", "address_registry", "recipient_policy", "address_message_board", "incident_board",
                 "signing_board", "fee_reserve", "withdrawal_payments", "withdrawal_funding", "withdrawal_credits",
                 "ulr_treasury", "ulr_voting", "decisions", "fee_pipeline"):
        # recipient policy is created by the registry constructor.
        assert name in steps or any(o.get("name") == name for s in steps.values() for o in s.get("outputs", []))
    assert order.index("bind_key_family") < order.index("bind_address_registry") < order.index("address_message_board")
    assert order.index("bind_address_registry") < order.index("register_mining") < order.index("bind_fee_reserve")
    assert order.index("bind_mint_admission") < order.index("bind_address_registry")
    assert steps["address_message_board"]["args"] == ["$address_registry"]
    assert steps["signing_board"]["args"][0] == "$batches"
    assert steps["bind_withdrawal_fees"]["args"][0] == "$withdrawal_credits"
    assert steps["fee_router"]["args"] == ["$vault", "$ulr_treasury", "0", "0"]
    assert steps["escrow"]["args"][3:] == [invite["min_p2wpkh_sats"], invite["min_p2tr_sats"]]
    assert steps["relay"]["artifact"] == (("ZkBitcoinTestnet4StateV2" if network=='testnet4' else "ZkBitcoinStateV2") if backend == "zk-state-v2" else
        {"mainnet": "MainnetHeaderRelay", "testnet4": "Testnet4HeaderRelay", "regtest": "RegtestHeaderRelay"}[network])
    assert ("dao_token" in steps) is (mode == "create")
    assert ("project_router" in steps) is (chain == "1")
    assert "settlement" not in steps
    for step in steps.values():
        if step["id"] == "address_message_board":
            abi = next(a for a in artifact(step["artifact"])["abi"] if a["type"] == "constructor")
            encode_args(abi["inputs"], [address(1)])
            for check in step["checks"]:
                assert any(a.get("name") == check["method"] for a in artifact(step["artifact"])["abi"])


def test_full_profile_is_committed_and_cannot_be_downgraded():
    p = full_invite()
    entries = signed_enrollments(p)
    profile = assemble(p, entries)
    assert verify_profile(profile) == p
    for field in ("system_bootstrap", "withdrawal_fee", "ulr"):
        changed = deepcopy(profile)
        del changed[field]
        with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
            verify_profile(changed)
    changed = deepcopy(p)
    changed["withdrawal_fee"]["version"] = "1"
    with pytest.raises(ValueError):
        validate_invite(changed)
    changed = deepcopy(p)
    changed["withdrawal_fee"]["max_packet_bytes"] = "2048"
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(changed, entries)
    legacy = networks("11155111", "testnet4")
    profile = assemble(legacy, signed_enrollments(legacy))
    profile["system_bootstrap"] = "1"
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(profile)


def test_full_policy_rejects_old_familyless_configuration():
    p = full_invite()
    with pytest.raises(ValueError, match="REQUIRES_FAMILY_ROOT"):
        bundle(dict(version="1", ulr=p["ulr"], withdrawal_fee=policy()))


@pytest.mark.parametrize("network", ["regtest", "testnet4"])
def test_mainnet_guest_policy_cannot_be_selected_for_other_bitcoin_network(network):
    invite=full_invite(network=network,backend='zk-state-v2')
    invite['bitcoin_policy']=zk_invite()['bitcoin_policy']
    invite['bitcoin_policy']['zk_spv']['confederation_id']=Web3.keccak(text=invite['confederation_id']).hex()
    with pytest.raises(ValueError):
        validate_invite(invite)


def test_single_system_file_drives_sepolia_testnet4_invitation(tmp_path, capsys):
    p = full_invite()
    system = tmp_path / "system.json"
    system.write_text(json.dumps(dict(version="1", ulr=p["ulr"], withdrawal_fee=p["withdrawal_fee"])))
    bitcoin = tmp_path / "bitcoin.json"
    bitcoin.write_text(json.dumps(p["bitcoin_policy"]))
    output = tmp_path / "invite.json"
    token = p["reward_token"]
    args = invite_args(p, output) + ["--ethereum-network", "sepolia", "--bitcoin-network", "testnet4",
        "--bitcoin-policy", str(bitcoin), "--system-policy", str(system),
        "--reward-token", token["address"], "--reward-token-code-hash", token["runtime_hash"], "--reward-token-decimals", token["decimals"]]
    assert main(args) == 0
    got = json.loads(output.read_text())
    assert got["system_bootstrap"] == "1" and got["withdrawal_fee"]["version"] == "2"
    assert got["chain_id"] == "11155111" and got["bitcoin_network"] == "testnet4"
    assert "btc_usd_feed" not in got
    with pytest.raises(ValueError, match="POLICY_CONFLICT"):
        main(args + ["--ulr-policy", str(system)])


@pytest.mark.parametrize("field,value", [("min_p2wpkh_sats", "1"), ("min_p2wpkh_sats", "293"),
    ("min_p2tr_sats", "1"), ("min_p2tr_sats", "329")])
def test_subminimum_withdrawals_fail_before_any_deployment(field, value):
    p = full_invite()
    p[field] = value
    with pytest.raises(ValueError):
        validate_invite(p)


@pytest.mark.parametrize("pair", [("294", "330"), ("500", "600")])
def test_exact_or_higher_withdrawal_minima_are_allowed(pair):
    p = full_invite()
    p["min_p2wpkh_sats"], p["min_p2tr_sats"] = pair
    validate_invite(p)


@pytest.mark.parametrize("connected", [True, False])
def test_complete_manifest_keeps_operational_qualification_explicit(connected):
    names = ("address_registry", "address_message_board", "incident_board", "signing_board", "family_root",
        "fee_reserve", "withdrawal_credits", "withdrawal_payments", "withdrawal_funding", "deposits", "escrow",
        "batches", "mining", "ulr_treasury", "staking", "snapshots")
    contracts = {name: dict(address=address(i + 1)) for i, name in enumerate(names)}
    runner = SimpleNamespace(profile=full_invite(), contracts=contracts)
    result = manifest(runner, dict(ready=connected), dict(bindings_confirmed=connected))
    assert result["graph_configured"] is connected
    assert result["ready"] is False
    assert result["address_transport"]["board"] == contracts["address_message_board"]["address"]
    assert result["bitcoin_network"] == "testnet4"


def test_prepare_profile_from_actual_build_and_pending_review_does_not_activate(tmp_path, capsys):
    from signer_agent.cli import main as command
    ulr_file = tmp_path / "ulr.json"
    ulr_file.write_text(json.dumps(dict(version="1", mode="create", bitcoin_backend="relay")))
    report = tmp_path / "review.json"
    report.write_text(json.dumps(dict(status="QUALIFICATION_PENDING", qualified=False)))
    service = tmp_path / "service.json"
    service.write_text(json.dumps(dict(version="1")))
    custody = tmp_path / "custody.json"
    custody.write_text(json.dumps(dict(version="1", mode="custodians", threshold="2", sources=[])))
    output = tmp_path / "system.json"
    args = ["system-profile", "--ulr-policy", str(ulr_file), "--qualification-report", str(report),
        "--bitcoin-custody-policy", str(custody),
        "--service-policy", str(service), "--protocol-fee-cap-sats", "100000", "--max-vbytes", "10000",
        "--max-packet-bytes", "1024", "--output", str(output)]
    assert command(args) == 0
    got = json.loads(output.read_text())
    assert got["withdrawal_fee"]["version"] == "2"
    assert got["withdrawal_fee"]["family_root"]["creation_code_hash"] == Web3.to_hex(Web3.keccak(
        hexstr=artifact("BitcoinKeyFamilyRegistry")["bytecode"]["object"]))
    assert got["withdrawal_fee"]["family_enrollment"] == "ORIGINAL_AUTHORITY"
    assert int(got["withdrawal_fee"]["derivation_source_hash"], 16) != 0
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert command(args) == 2  # Never overwrites a prepared/enrolled policy.


@pytest.mark.parametrize("field,value", [("registry", address(100)), ("authorization", address(101)), (None, None)])
def test_full_bitcoin_manifest_checks_address_transport_binding(field, value):
    from signer_agent.bootstrap_withdrawal_fee import manifest as bitcoin_manifest
    p = full_invite()
    names = ("address_registry", "recipient_policy", "incident_board", "signing_board", "fee_reserve", "fee_user_module",
        "withdrawal_payments", "withdrawal_funding", "withdrawal_credits", "family_root", "address_message_board", "mining", "conf_auth", "dkg", "coordinator", "escrow", "batches", "deposits")
    specs = {name: dict(address=address(i + 1)) for i, name in enumerate(names)}
    def contract(name):
        def getter(method):
            values = dict(familyRegistry=specs["family_root"]["address"], registryFamily=b"f" * 32,
                withdrawalFees=specs["withdrawal_credits"]["address"], feeReserve=specs["fee_reserve"]["address"],
                addressRegistry=specs["address_registry"]["address"], recipientPolicy=specs["recipient_policy"]["address"],
                registry=specs["address_registry"]["address"], authorization=specs["conf_auth"]["address"])
            answer = value if name == "address_message_board" and method == field else values[method]
            return lambda *args: SimpleNamespace(call=lambda **kwargs: answer)
        class Functions:
            def __getattr__(self, method):
                return getter(method)
        return SimpleNamespace(address=specs[name]["address"], functions=Functions())
    runner = SimpleNamespace(profile=p, contracts=specs, read_number=lambda: 5, contract=contract,
        w3=SimpleNamespace(eth=SimpleNamespace(get_code=lambda *args, **kwargs: b"runtime")))
    result = bitcoin_manifest(runner)
    assert result["bindings_confirmed"] is (field is None)
    assert result["ready"] is False


def test_reusing_ulr_requires_explicit_existing_family_in_preparation(tmp_path):
    from signer_agent.bootstrap_system import main as prepare
    ulr_file = tmp_path / "ulr.json"
    ulr_file.write_text(json.dumps(proposal("reuse")["ulr"]))
    output = tmp_path / "system.json"
    with pytest.raises(ValueError, match="REUSE_REQUIRES_FAMILY_ROOT"):
        prepare(["--ulr-policy", str(ulr_file), "--qualification-report", "unused", "--service-policy", "unused",
            "--bitcoin-custody-policy", "unused",
            "--protocol-fee-cap-sats", "100000", "--max-vbytes", "10000", "--max-packet-bytes", "1024", "--output", str(output)])
    assert not output.exists()


@pytest.mark.parametrize("example,backend", [("system-bootstrap-testnet.example.json", "relay"),
    ("system-bootstrap-zk.example.json", "zk-state-v2")])
def test_shipped_examples_match_the_current_family_build(example, backend):
    import hashlib
    from pathlib import Path
    from signer_agent.operations import canonical
    root = Path(__file__).resolve().parents[2]
    policy = bundle(json.loads((root / "config" / example).read_text()))
    metadata = artifact("BitcoinDepositDerivation")["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    expected = "0x" + hashlib.sha256(canonical({k: metadata[k] for k in ("compiler", "settings", "sources")}).encode()).hexdigest()
    assert policy["withdrawal_fee"]["derivation_source_hash"] == expected
    p = full_invite(network="mainnet" if backend == "zk-state-v2" else "testnet4", backend=backend)
    p.update(ulr=policy["ulr"], withdrawal_fee=policy["withdrawal_fee"])
    validate_invite(p)
