"""ZK state deployed before enrollment, committed in the invitation and adopted by bootstrap."""
from copy import deepcopy
import pytest
from web3 import Web3
from signer_agent.bootstrap_plan import build
from signer_agent.onboarding import validate_invite, assemble, verify_profile
from signer_agent.zk_relay_deploy import validate_adoption, relay_step
from signer_agent.zk_worker import load_config
from signer_agent.zk_worker_prover import write
from test_onboarding_mainnet import mainnet_invitation, signed_enrollments
from test_zk_bootstrap import zk_invite, profile
from test_zk_worker_service import worker

ADOPT = dict(address=Web3.to_checksum_address("0x" + "ab" * 20), tx_hash="0x" + "cd" * 32)


def mainnet_zk():
    p = zk_invite(); p["chain_id"] = "1"
    p.update(evm_confirmations="12", phase_duration="86400", integration_fee_exemption=False,
             deployment_fee=dict(amount="100", treasury=p["treasury"]))
    return p


def test_adoption_schema_positive_and_negative():
    policy = zk_invite()["bitcoin_policy"]
    assert validate_adoption(dict(ADOPT), policy) == ADOPT
    bad = [None, {}, dict(ADOPT, extra="1"), dict(ADOPT, address=ADOPT["address"].lower()),
           dict(ADOPT, address="0x" + "00" * 20), dict(ADOPT, tx_hash="0x" + "CD" * 32),
           dict(ADOPT, tx_hash="0x" + "cd" * 31), dict(ADOPT, tx_hash=1), dict(ADOPT, address=None)]
    for value in bad:
        with pytest.raises(ValueError, match="ZK_RELAY_ADOPTION_SCHEMA"):
            validate_adoption(value, policy)
    with pytest.raises(ValueError, match="ZK_RELAY_REQUIRES_ZK_POLICY"):
        validate_adoption(dict(ADOPT), mainnet_invitation()["bitcoin_policy"])


def test_standalone_relay_step_is_the_signed_plan_step():
    p = mainnet_zk()
    plan_step = {s["id"]: s for s in build(profile(p))["steps"]}["relay"]
    standalone = relay_step(p["bitcoin_policy"])
    assert standalone["artifact"] == plan_step["artifact"]
    assert standalone["args"] == plan_step["args"]
    assert standalone["checks"] == [c for c in plan_step["checks"] if c["contract"] == "relay"]
    assert "adopt" not in plan_step


def test_plan_adopts_committed_relay_and_keeps_order():
    p = mainnet_zk(); p["zk_relay"] = dict(ADOPT)
    assert validate_invite(p) == p
    prof = profile(p); prof["zk_relay"] = dict(ADOPT)
    steps = build(prof)["steps"]
    relay = next(s for s in steps if s["id"] == "relay")
    assert relay["adopt"] == ADOPT and relay["kind"] == "deploy"
    without = build(profile(mainnet_zk()))["steps"]
    assert [s["id"] for s in steps] == [s["id"] for s in without]
    assert {k: v for k, v in relay.items() if k != "adopt"} == next(s for s in without if s["id"] == "relay")


def test_invite_rejects_bad_or_non_zk_adoption():
    p = mainnet_zk(); p["zk_relay"] = dict(ADOPT, tx_hash="0x00")
    with pytest.raises(ValueError):
        validate_invite(p)
    q = mainnet_invitation(); q["zk_relay"] = dict(ADOPT)
    with pytest.raises(ValueError):
        validate_invite(q)


def test_enrollment_commits_adoption_and_profile_cannot_change_it():
    p = zk_invite(); p["zk_relay"] = dict(ADOPT)
    built = assemble(p, signed_enrollments(p), "http://local.invalid")
    assert built["zk_relay"] == ADOPT and verify_profile(built) == p
    for field, value in (("address", Web3.to_checksum_address("0x" + "ac" * 20)), ("tx_hash", "0x" + "ce" * 32)):
        changed = deepcopy(built); changed["zk_relay"][field] = value
        with pytest.raises(ValueError):
            verify_profile(changed)
    dropped = deepcopy(built); del dropped["zk_relay"]
    with pytest.raises(ValueError):
        verify_profile(dropped)


def test_heartbeat_worker_may_start_before_any_consumer(tmp_path):
    w = worker(tmp_path); w.inbox.close()
    path = tmp_path / "config.json"
    c = deepcopy(w.c); c["allowed_consumers"] = {}
    write(path, c); path.chmod(0o600)
    assert load_config(path) == c
    for change in (lambda x: x["heartbeat"].update(enabled=False),
                   lambda x: x.update(version="1") or [x.pop(k) for k in ("service", "heartbeat")],
                   lambda x: x["service"].update(mode="paid", binding=dict(bitcoin_policy=x["bitcoin_policy"],
                                                 chain_id=x["chain_id"], evm_confirmations=x["evm_confirmations"]))):
        bad = deepcopy(c); change(bad); write(path, bad)
        with pytest.raises(ValueError, match="ZK_WORKER_CONSUMER_POLICY"):
            load_config(path)
