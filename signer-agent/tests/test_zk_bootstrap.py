"""Configuration compatibility and signed proof-only backend selection."""
from copy import deepcopy
import pytest
from web3 import Web3
from signer_agent.bitcoin_network import validate_policy, relay_constructor_args, sync_relay
from signer_agent.bootstrap_plan import build
from signer_agent.onboarding import validate_invite, assemble, verify_profile
from signer_agent.zk_spv import MAINNET_IMAGE, constructor_args, checkpoint_descriptor
from signer_agent.zk_spv_cli import materialization_step, transition_step
from test_onboarding_mainnet import mainnet_invitation, signed_enrollments
from test_onboarding_policy_v3 import direct_profile


def zk_invite():
    p = mainnet_invitation()
    p["version"] = "6"
    policy = p["bitcoin_policy"]
    policy.update(version="2", max_reorg_depth="100", zk_spv=dict(
        backend="zk-state-v2", relay_profile="PROOF_ONLY_V1", program_image_id=MAINNET_IMAGE,
        confederation_id=Web3.keccak(text=p["confederation_id"]).hex(),
        max_state_age_seconds="3600", max_tip_lag_seconds="7200"))
    return p


def profile(p):
    result = direct_profile(p)
    for k in ("bitcoin_policy", "reward_token", "btc_usd_feed", "evm_confirmations", "deployment_fee"):
        if k in p:
            result[k] = deepcopy(p[k])
    return result


@pytest.mark.parametrize("chain", ["1", "31337", "11155111"])
def test_zk_plan_preserves_independent_evm_policy(chain):
    p = zk_invite()
    p["chain_id"] = chain
    if chain == "1":
        p.update(evm_confirmations="12", phase_duration="86400", integration_fee_exemption=False,
                 deployment_fee=dict(amount="100", treasury=p["treasury"]))
    assert validate_invite(p) == p
    plan = build(profile(p))
    steps = {s["id"]: s for s in plan["steps"]}
    assert steps["relay"]["artifact"] == "ZkBitcoinStateV2"
    assert {o["artifact"] for o in steps["relay"]["outputs"]} == {"ZkSpvAdapter", "RiscZeroGroth16Verifier"}
    assert "spv_guard" not in steps  # Child, not a replaceable deployment.
    assert steps["escrow"]["args"][1] == steps["deposits"]["args"][0] == "$spv_guard"
    assert steps["bitcoin_relay_ready"]["kind"] == "bitcoin_ready"
    assert steps["escrow"]["args"][2] == p["bitcoin_confirmations"]
    assert constructor_args(p["bitcoin_policy"])[0][1].hex() == MAINNET_IMAGE
    assert len(checkpoint_descriptor(p["bitcoin_policy"])) == 32


@pytest.mark.parametrize("chain", ["1", "11155111"])
def test_zk_state_is_deployed_before_dkg_waits_and_consumers_after(chain):
    p = zk_invite()
    p["chain_id"] = chain
    if chain == "1":
        p.update(evm_confirmations="12", phase_duration="86400", integration_fee_exemption=False,
                 deployment_fee=dict(amount="100", treasury=p["treasury"]))
    ids = [s["id"] for s in build(profile(p))["steps"]]
    waits = [ids.index(role + "_complete") for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony")]
    relay = ids.index("relay")
    # The state contract needs no DKG key, so workers can start during the ceremonies.
    assert ids.index("confederations") < relay < min(waits)
    # Everything bound to the confederation authorization or Fresh state waits for DKG.
    for later in ("spv_service_view", "spv_payment_registry", "spv_message_board",
                  "bitcoin_relay_ready", "escrow"):
        assert max(waits) < ids.index(later), later
    assert ids.count("relay") == 1


def test_enrollment_commits_zk_selection_and_every_fresh_limit(tmp_path):
    p = zk_invite()
    entries = signed_enrollments(p)
    built = assemble(p, entries, "http://local.invalid")
    assert verify_profile(built) == p
    for field, value in (("max_state_age_seconds", "3601"), ("program_image_id", "00"*32),
                         ("max_tip_lag_seconds", "7199"), ("relay_profile", "FROST")):
        changed = deepcopy(built)
        changed["bitcoin_policy"]["zk_spv"][field] = value
        with pytest.raises(ValueError):
            verify_profile(changed)
    changed = deepcopy(p)
    changed["bitcoin_policy"]["zk_spv"]["max_state_age_seconds"] = "3601"
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(changed, entries)


@pytest.mark.parametrize("field,value", [("backend", "regtest"), ("relay_profile", "FROST"),
    ("program_image_id", "12c03eb130e4cdc5946ee7fcb09b6108ceac00d659f818a89439b52d3cbc4eca"),
    ("confederation_id", "00"*32), ("max_state_age_seconds", "0"),
    ("max_state_age_seconds", "01"), ("max_tip_lag_seconds", "7201")])
def test_reject_unsupported_zk_profiles(field, value):
    p = zk_invite()["bitcoin_policy"]
    p["zk_spv"][field] = value
    with pytest.raises(ValueError):
        validate_policy(p)


def test_mismatch_and_legacy_backend_fail_closed():
    p = zk_invite()
    p["confederation_id"] = "different-confederation"
    with pytest.raises(ValueError, match="ZK_CONFEDERATION_MISMATCH"):
        validate_invite(p)
    policy = zk_invite()["bitcoin_policy"]
    for depth in ("99", "101"):
        with pytest.raises(ValueError, match="ZK_REORG_BOUND"):
            validate_policy(dict(policy, max_reorg_depth=depth))
    with pytest.raises(ValueError, match="ZK_PROFILE_REQUIRES_PROOF_WORKFLOW"):
        relay_constructor_args(policy)
    with pytest.raises(ValueError, match="ZK_PROFILE_REQUIRES_PROOF_WORKFLOW"):
        sync_relay(None, None, policy, None)
    wrong = profile(zk_invite())
    wrong.update(bitcoin_network="regtest", mode="integration")
    with pytest.raises(ValueError, match="ZK_PROFILE_UNSUPPORTED"):
        build(wrong)


def test_bounded_materialization_and_deployment_bound_transition():
    p = zk_invite()["bitcoin_policy"]
    value = dict(height="32256", header=p["checkpoint_header"], siblings=["00"*32]*64)
    assert materialization_step(value)["method"] == "materializeHeader"
    with pytest.raises(ValueError):
        materialization_step(dict(value, siblings=value["siblings"][:-1]))
    with pytest.raises(ValueError, match="ZK_PROOF_DEPLOYMENT_MISMATCH"):
        transition_step(31337, "0x"+"11"*20, p, bytes(1216), bytes(260))
    # Admission bytes only; never a cryptographic proof.
    j = bytearray(1216)
    j[:32] = b"SATOSHIFI_TRANSITION_V2".ljust(32, b"\0")
    j[32:64] = b"bitcoin-mainnet".ljust(32, b"\0")
    j[64:96] = (31337).to_bytes(32, "big")
    j[108:128] = bytes.fromhex("11"*20)
    j[128:160] = checkpoint_descriptor(p)
    j[640:672] = (int(p["max_headers_per_sync"]) + 1).to_bytes(32, "big")
    with pytest.raises(ValueError, match="ZK_BATCH_POLICY_LIMIT"):
        from signer_agent.zk_spv import VERIFIER_SELECTOR
        transition_step(31337, "0x"+"11"*20, p, j, VERIFIER_SELECTOR+bytes(256))


@pytest.mark.parametrize("failure", [None, "latest_halt", "confirmed_halt", "work", "head", "stale", "core_reorg", "lag"])
def test_independent_core_and_confirmed_freshness(monkeypatch, failure):
    from types import SimpleNamespace
    from signer_agent import bitcoin_network
    from signer_agent.zk_spv import inspect_live
    p = zk_invite()["bitcoin_policy"]
    head = bytes.fromhex("11"*32)
    s = [bytes(32), 32256, head, 32357, 999, 1000, bytes(32), bytes(32), False]
    core = dict(blocks=32357, bestblockhash=head[::-1].hex())
    header = dict(hash=head[::-1].hex(), chainwork=f"{999:064x}", time=1000)
    monkeypatch.setattr(bitcoin_network, "check_core", lambda *a, **k: core)
    monkeypatch.setattr(bitcoin_network, "_core_header", lambda *a: (header, ""))
    if failure == "confirmed_halt": s[8] = True
    if failure == "work": s[4] += 1
    if failure == "head": s[2] = bytes(32)
    if failure == "lag": core["blocks"] += int(p["max_relay_lag_blocks"]) + 1
    def call(value):
        return SimpleNamespace(call=lambda **k: value)
    state = SimpleNamespace(functions=SimpleNamespace(
        halted=lambda: call(failure == "latest_halt"), getState=lambda: call(s),
        isFresh=lambda: call(failure != "stale")))
    bitcoin = SimpleNamespace(call=lambda *a: "wrong" if failure == "core_reorg" else core["bestblockhash"])
    if failure:
        with pytest.raises(ValueError):
            inspect_live(bitcoin, state, p, block_identifier=7, now=1001)
    else:
        assert inspect_live(bitcoin, state, p, block_identifier=7, now=1001)["status"] == "MAINNET_RELAY_READY"
