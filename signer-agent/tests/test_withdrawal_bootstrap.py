"""Opt-in enrollment, actual ABI bindings and atomic circular CREATE recovery."""
from copy import deepcopy
from pathlib import Path
import pytest
from eth_account import Account

from signer_agent.bootstrap import artifact, encode_args
from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_create import creation_groups, create_address
from signer_agent.operations import Journal
from signer_agent.onboarding import validate_invite, assemble, verify_profile
from test_bootstrap_parallel import fixture
from test_onboarding_policy_v3 import invitation, direct_profile
from test_onboarding_mainnet import signed_enrollments


def policy():
    return dict(version="1", derivation_source_hash="0x" + "11" * 32,
                qualification_commitment="0x" + "22" * 32, service_policy_hash="0x" + "33" * 32,
                protocol_fee_cap_sats="100000", max_vbytes="10000", max_packet_bytes="1024")


def invite():
    p = invitation()
    p.update(version="6", evm_confirmations="1", withdrawal_fee=policy())
    return p


def test_fee_policy_is_explicitly_enrolled_and_cannot_be_appended_after_join():
    p = invite()
    assert validate_invite(p) == p
    enrolled = signed_enrollments(p)
    profile = assemble(p, enrolled, "http://local.invalid")
    assert verify_profile(profile) == p
    assert profile["withdrawal_fee"] == policy()
    for field in policy():
        altered = deepcopy(profile)
        altered["withdrawal_fee"][field] = "different"
        with pytest.raises(ValueError):
            verify_profile(altered)
    changed = deepcopy(p)
    changed["withdrawal_fee"]["max_vbytes"] = "9999"
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(changed, enrolled)
    legacy = invite()
    del legacy["withdrawal_fee"]
    legacy_profile = assemble(legacy, signed_enrollments(legacy))
    legacy_profile["withdrawal_fee"] = policy()
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(legacy_profile)


@pytest.mark.parametrize("field,value", [("max_vbytes", "99"), ("max_packet_bytes", "4097"),
    ("protocol_fee_cap_sats", "0"), ("service_policy_hash", "0x" + "00" * 32), ("unexpected", True)])
def test_bad_public_fee_profile_rejected_before_bootstrap(field, value):
    p = invite()
    p["withdrawal_fee"][field] = value
    with pytest.raises(ValueError):
        validate_invite(p)


def test_bootstrap_preserves_legacy_and_builds_full_fee_graph_without_manual_addresses():
    legacy = build(direct_profile(invitation()))
    assert not any(s["artifact"].startswith("WithdrawalFee") for s in legacy["steps"] if "artifact" in s)
    p = direct_profile(invitation())
    p["withdrawal_fee"] = policy()
    result = build(p)
    steps = {s["id"]: s for s in result["steps"]}
    assert steps["bind_fee_reserve"]["args"] == ["$fee_reserve", "$bind_fee_reserve_hash"]
    assert steps["bind_withdrawal_fees"]["args"] == ["$withdrawal_credits", "$bind_withdrawal_fees_hash"]
    order = list(steps)
    assert order.index("bind_batch_escrow") < order.index("bind_address_registry") < order.index("fee_reserve")
    assert order.index("bind_deposits") < order.index("address_registry") < order.index("bind_address_registry") < order.index("register_mining")
    assert steps['incident_board']['artifact']=='BitcoinIncidentMessageBoard'
    assert steps['incident_board']['args']==['$address_registry']
    assert order.index('bind_address_registry')<order.index('incident_board')
    assert order.index("register_mining") < order.index("fee_reserve") < order.index("bind_fee_reserve") < order.index("bind_withdrawal_fees")
    group = creation_groups(result["steps"])["withdrawal-fee-constructors"]
    assert [s["artifact"] for s in group] == ["WithdrawalFeePayments", "WithdrawalFeeFunding", "WithdrawalFeeCredits"]
    # Every generated constructor and postcondition really exists in the build.
    def resolve(value):
        if isinstance(value, str) and value.startswith("$"):
            return "0x" + "12" * 20
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value
    for step in result["steps"]:
        if step["kind"] == "deploy" and step["id"] in ("address_derivation", "address_registry", "signing_board",
                "fee_donor", "fee_funding", "fee_plan", "fee_reserve", "withdrawal_plan_verifier", "withdrawal_derivation",
                "withdrawal_payments", "withdrawal_funding", "withdrawal_credits", "incident_board"):
            a = artifact(step["artifact"])
            ctor = next((x for x in a["abi"] if x["type"] == "constructor"), {"inputs": []})
            encode_args(ctor["inputs"], resolve(step["args"]))
            for check in step["checks"]:
                assert any(x.get("name") == check["method"] for x in a["abi"])


def test_opt_in_fee_graph_preserves_mainnet_zk_backend_and_consumer_binding():
    from test_zk_bootstrap import zk_invite, profile
    p=profile(zk_invite())
    p['withdrawal_fee']=policy()
    steps={s['id']:s for s in build(p)['steps']}
    assert steps['relay']['artifact']=='ZkBitcoinStateV2'
    assert steps['escrow']['args'][1]==steps['deposits']['args'][0]=='$spv_guard'
    assert steps['bind_address_registry']['contract']=='deposits'
    assert steps['fee_reserve']['args'][0]=='$address_registry'


def test_new_canonical_root_is_created_once_and_bound_by_two_original_approvals():
    from web3 import Web3
    p=direct_profile(invitation());fee=policy();fee.update(version='2',family_enrollment='ORIGINAL_AUTHORITY',
        family_root=dict(version='create-1',creation_code_hash='0x'+Web3.keccak(hexstr=artifact('BitcoinKeyFamilyRegistry')['bytecode']['object']).hex().removeprefix('0x')))
    p['withdrawal_fee']=fee;steps={s['id']:s for s in build(p)['steps']};order=list(steps)
    assert steps['family_root']['args']==['$conf_auth']
    assert steps['enroll_key_family']['method']=='enroll' and steps['enroll_key_family']['args']==['$address_registry','$enroll_key_family_hash']
    assert steps['bind_key_family']['args']==['$family_root','$bind_key_family_hash']
    assert order.index('family_root')<order.index('enroll_key_family')<order.index('bind_key_family')<order.index('bind_address_registry')
    assert steps['family_authority']['checks'][0]['equals']=='$conf_auth'


def test_pre_enrolled_root_does_not_use_new_graph_authority_for_old_governance():
    from test_address_mint import address
    p=direct_profile(invitation());fee=policy();fee.update(version='2',family_enrollment='PRE_ENROLLED',family_root=dict(
        version='1',chain_id='31337',address=address(31),initial_authority=address(32),dkg=address(33),coordinator=address(34),runtime_hash='0x'+'11'*32))
    p['withdrawal_fee']=fee;result=build(p);steps={s['id']:s for s in result['steps']}
    assert 'enroll_key_family' not in steps and 'family_root' not in steps
    assert steps['family_enrollment']['pending_status']=='FAMILY_ENROLLMENT_REQUIRED'
    assert steps['family_enrollment']['equals']=='$family_id'
    assert result['contracts']['family_root']['address']==address(31)


def circular(tmp_path):
    b, identity = fixture(tmp_path, 3)
    for step in b.profile["steps"]:
        del step["parallel_group"]
        step.update(create_group="circular", gas="8000000")
    return b, identity


def test_future_create_addresses_reserved_together_and_not_exposed_before_finality(tmp_path):
    b, identity = circular(tmp_path)
    original = b.build_transaction
    observed = []
    sender = Account.from_key(bytes.fromhex("00" * 31 + "01")).address
    def build_tx(step, account, nonce):
        observed.append(dict(b.values))
        return original(step, account, nonce)
    b.build_transaction = build_tx
    assert b.apply(identity)["status"] == "CONFIRMING"
    expected = {f"leaf{i}": create_address(sender, i) for i in range(3)}
    assert observed == [expected] * 3
    assert b.values == {} and not b.finished
    assert b.apply(identity)["status"] == "CONFIRMING"
    assert len(b.prepared) == len(b.w3.eth.sent) == 3
    b.w3.eth.block_number = 100
    assert b.apply(identity)["status"] == "VERIFIED"


def test_crash_during_first_local_write_restores_all_three_global_envelopes(tmp_path, monkeypatch):
    b, identity = circular(tmp_path)
    original = Journal.update
    def crash(journal, key, status, result):
        if journal.path == b.journal.path and status == "SIGNED":
            raise SystemExit("after global atomic reservation")
        return original(journal, key, status, result)
    monkeypatch.setattr(Journal, "update", crash)
    with pytest.raises(SystemExit):
        b.apply(identity)
    lane = Journal(str(Path(identity).with_suffix(".evm-nonce.lock")) + ".sqlite")
    assert len(lane.list()) == 3 and not b.w3.eth.sent
    saved = {row["id"] for row in lane.list()}
    monkeypatch.setattr(Journal, "update", original)
    assert b.apply(identity)["status"] == "CONFIRMING"
    assert len(b.prepared) == len(b.w3.eth.sent) == 3
    assert {b.journal.get(b.prefix + ":leaf" + str(i))["result"]["tx_hash"] for i in range(3)} == saved


def test_atomic_reservation_conflict_writes_no_prefix(tmp_path):
    j = Journal(tmp_path / "atomic.sqlite")
    j.put("b", {"v": "old"})
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        j.put_many([("a", {"v": "new"}), ("b", {"v": "other"})])
    with pytest.raises(KeyError):
        j.get("a")


@pytest.mark.parametrize("change", [dict(kind="call"), dict(gas="10000001"), dict(value="1"),
    dict(outputs=[{}]), dict(parallel_group="other")])
def test_create_groups_reject_unbounded_or_noncreation_steps(tmp_path, change):
    b, _ = circular(tmp_path)
    b.profile["steps"][0].update(change)
    with pytest.raises(ValueError):
        creation_groups(b.profile["steps"])
