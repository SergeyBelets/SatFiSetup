"""Reject coordinator substitution before any nonce or share is generated."""
import pytest

from signer_agent.ethereum_intents import validate_signing_binding, require_mainnet_signing_enrollment
from test_ethereum_mainnet import _project_graph
from test_onboarding_policy_v3 import address


def test_reviewed_intent_matches_the_actual_signing_client():
    declared = address(48)
    # Address casing does not change the EVM account identity.
    validate_signing_binding(dict(coordinator=declared), {}, declared.lower())


@pytest.mark.parametrize("declared,actual", [
    (None, address(48)), (True, address(48)), (48, address(48)),
    ("not-an-address", address(48)), (address(48), None),
    (address(48), "not-an-address"), (address(48), address(49)),
    ("0x" + "00" * 20, "0x" + "00" * 20),
])
def test_mismatched_or_malformed_coordinator_cannot_receive_signatures(declared, actual):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding(dict(coordinator=declared), {}, actual)


@pytest.mark.parametrize("actual_role", ["evm_ceremony", "conf_ceremony"])
def test_matching_enrolled_signing_role_is_accepted(actual_role):
    validate_signing_binding(dict(coordinator=address(48), role=actual_role),
        dict(onboarding={"invitation": {}}, role=actual_role), address(48))


@pytest.mark.parametrize("bundle_role,intent_role", [
    ("evm_ceremony", "conf_ceremony"), ("conf_ceremony", "evm_ceremony"),
    ("btc_ceremony", "btc_ceremony"), (None, None), ("evm_ceremony", None),
])
def test_enrolled_signing_role_cannot_be_relabelled(bundle_role, intent_role):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding(dict(coordinator=address(48), role=intent_role),
            dict(onboarding={"invitation": {}}, role=bundle_role), address(48))


@pytest.mark.parametrize("chain", ["31337", "11155111", 31337])
def test_explicit_legacy_file_can_use_its_explicit_cli_coordinator(chain):
    validate_signing_binding({}, dict(chain_id=chain), address(48), allow_legacy_file=True)


@pytest.mark.parametrize("bundle", [dict(chain_id="31337"), dict(chain_id="11155111"),
    dict(chain_id="1", role="evm_ceremony"), {}])
def test_missing_intent_coordinator_is_rejected_without_explicit_legacy_mode(bundle):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding({}, bundle, address(48))


@pytest.mark.parametrize("bundle", [dict(chain_id="1"), dict(chain_id=1),
    dict(chain_id="31337", onboarding={}), dict(chain_id="31337", onboarding=None),
    {}, dict(chain_id=True), dict(chain_id="١"), dict(chain_id="0")])
def test_legacy_option_cannot_bypass_mainnet_enrollment_or_unknown_chain(bundle):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding({}, bundle, address(48), allow_legacy_file=True)


@pytest.mark.parametrize("declared", [None, True, "not-an-address", address(49), "0x" + "00" * 20])
def test_legacy_file_still_rejects_any_declared_mismatch(declared):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding(dict(coordinator=declared), dict(chain_id="31337"),
            address(48), allow_legacy_file=True)


@pytest.mark.parametrize("actual", [None, True, "not-an-address", "0x" + "00" * 20])
def test_legacy_file_always_requires_a_real_actual_coordinator(actual):
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding({}, dict(chain_id="31337"), actual, allow_legacy_file=True)


def test_mainnet_never_accepts_unlabelled_role_even_without_onboarding_field():
    with pytest.raises(ValueError, match="^POLICY_MISMATCH$"):
        validate_signing_binding(dict(coordinator=address(48)), dict(chain_id="1"), address(48))


@pytest.mark.parametrize("bundle", [{}, dict(onboarding=None), dict(onboarding={}),
    dict(onboarding={"invitation": None}), dict(onboarding={"invitation": {"version": "4"}}),
    dict(onboarding={"invitation": {"version": 5}})])
def test_chain1_requires_v5_enrollment_before_private_signing(bundle):
    from signer_agent.participant_errors import participant_exception_code
    with pytest.raises(ValueError, match="^ETHEREUM_V5_ENROLLMENT_REQUIRED$") as error:
        require_mainnet_signing_enrollment(bundle, 1)
    assert participant_exception_code(error.value) == "ETHEREUM_V5_ENROLLMENT_REQUIRED"


def test_v5_enrollment_is_admitted_for_full_provenance_review():
    require_mainnet_signing_enrollment(dict(onboarding={"invitation": {"version": "5"}}), 1)


@pytest.mark.parametrize("chain", [31337, 11155111])
def test_legacy_test_network_manifests_do_not_require_v5(chain):
    require_mainnet_signing_enrollment({}, chain)


@pytest.mark.parametrize("snapshot", [-1, True, "80", 81])
def test_generic_mainnet_review_rejects_invalid_or_unfinalized_snapshot(monkeypatch, snapshot):
    review, w3, intent, bundle, _, _, proofs = _project_graph(monkeypatch)
    with pytest.raises(ValueError, match="^ETHEREUM_PROJECT_UNFINALIZED$"):
        review.review_bootstrap_intent(w3, intent, bundle, snapshot)
    assert proofs == []


@pytest.mark.parametrize("component", ["dkg", "coordinator", "verifier", "project_router"])
def test_generic_mainnet_review_rejects_same_runtime_from_foreign_creator(monkeypatch, component):
    review, w3, intent, bundle, addresses, _, _ = _project_graph(monkeypatch)
    original = review.prove_creation
    def foreign(*args):
        proof = original(*args)
        if args[1] == addresses[component]:
            proof["sender"] = address(99)
        return proof
    monkeypatch.setattr(review, "prove_creation", foreign)
    with pytest.raises(ValueError, match="^ETHEREUM_BOOTSTRAP_ORIGIN$"):
        review.review_bootstrap_intent(w3, intent, bundle, 80)


def test_generic_mainnet_review_rechecks_anchor_after_component_proofs(monkeypatch):
    review, w3, intent, bundle, _, _, _ = _project_graph(monkeypatch)
    original = review.review_project_payer
    original_block = w3.eth.get_block
    def replace_anchor(*args):
        result = original(*args)
        def get_block(number):
            block = original_block(number)
            if number == 80:
                return type(block)(dict(block, hash=b"x" * 32))
            return block
        w3.eth.get_block = get_block
        return result
    monkeypatch.setattr(review, "review_project_payer", replace_anchor)
    with pytest.raises(ValueError, match="^ETHEREUM_PROJECT_SNAPSHOT_REORG$"):
        review.review_bootstrap_intent(w3, intent, bundle, 80)
