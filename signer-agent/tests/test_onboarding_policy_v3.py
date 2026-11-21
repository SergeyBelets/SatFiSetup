"""Signed V3 policy, explicit bootstrap inputs, and pre-transaction admission."""

from copy import deepcopy
import json
from types import SimpleNamespace

from eth_account import Account
import pytest
from web3 import Web3

from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_policy import NUMBER_BOUNDS, POLICY_FIELDS
from signer_agent.dkg_protocol import PROTOCOL_ID
from signer_agent.onboarding import (
    ROLES,
    assemble,
    chain_main,
    fingerprint,
    join,
    main,
    read_public,
    validate_invite,
    verify_profile,
)


ZERO = "0x" + "00" * 20


def address(number):
    return Account.from_key(number.to_bytes(32, "big")).address


def invitation(n=4, threshold=3, min_custodians=2, version="3"):
    participants = [address(i) for i in range(1, n + 1)]
    proposal = dict(
        version=version,
        chain_id="31337",
        bitcoin_network="regtest",
        mode="integration",
        deployment_id="explicit-policy",
        operator=participants[0],
        treasury=address(32),
        approval_deadline="4102444800",
        federation_id="explicit-policy-fed",
        confederation_id="explicit-policy-conf",
        participants=participants,
        threshold=str(threshold),
        epoch="1",
        sessions={role: i.to_bytes(32, "big").hex() for i, role in enumerate(ROLES, 1)},
        protocol_id=PROTOCOL_ID.hex(),
        phase_duration="3600",
    )
    if version == "1":
        proposal["evm_rpc"] = "http://127.0.0.1:8545"
    if version == "3":
        # Deliberately differ from common 294/330 examples: policy is caller input.
        proposal.update(
            min_p2wpkh_sats="297", min_p2tr_sats="401",
            min_custodians=str(min_custodians),
            bitcoin_confirmations="7",
            wrap_fee_bps="17", unwrap_fee_bps="23",
            wrap_staker_bps="1234", deploy_staker_bps="6789",
            converter_numerator="3", converter_denominator="9999",
            converter_funding="7654321", dao_token_recipient=address(31),
            dao_use_reserve=True, integration_fee_exemption=True,
        )
    return proposal


def direct_profile(proposal=None):
    """The explicit public-config path does not require an invitation or a board."""
    proposal = proposal or invitation()
    keys = (
        "chain_id", "bitcoin_network", "mode", "deployment_id", "operator",
        "treasury", "approval_deadline", "federation_id", "confederation_id",
        *POLICY_FIELDS,
    )
    profile = {key: deepcopy(proposal[key]) for key in keys if key in proposal}
    profile["evm_rpc"] = "http://127.0.0.1:8545"
    for offset, role in enumerate(ROLES):
        profile[role] = dict(
            id="0x" + proposal["sessions"][role],
            participants=list(proposal["participants"]),
            threshold=proposal["threshold"],
            epoch=proposal["epoch"],
            transport_keys=[
                "0x" + (offset * 32 + i).to_bytes(32, "big").hex()
                for i in range(1, len(proposal["participants"]) + 1)
            ],
        )
    return profile


def enroll(proposal, directory):
    entries, files = [], []
    for i in range(1, len(proposal["participants"]) + 1):
        identity = directory / ("identity-%s.json" % i)
        identity.write_text(json.dumps({"private_key": "%064x" % i}))
        identity.chmod(0o600)
        participant_dir = directory / ("participant-%s" % i)
        entry = join(proposal, fingerprint(proposal), identity, participant_dir)
        assert join(proposal, fingerprint(proposal), identity, participant_dir) == entry
        entries.append(entry)
        files.append(participant_dir / "enrollment.json")
    return entries, files


def invite_args(proposal, path):
    return [
        "invite", "--participants", *proposal["participants"],
        "--threshold", proposal["threshold"], "--treasury", proposal["treasury"],
        *[item for field in POLICY_FIELDS for item in (
            "--" + field.replace("_", "-"), str(proposal[field]).lower()
            if isinstance(proposal[field], bool) else proposal[field]
        )],
        "--chain-id", proposal["chain_id"], "--deadline", proposal["approval_deadline"],
        "--deployment-id", proposal["deployment_id"], "--output", str(path),
    ]


def assert_plan_policy(plan, proposal):
    steps = {step["id"]: step for step in plan["steps"]}
    escrow = steps["escrow"]
    assert escrow["artifact"] == "BitcoinWithdrawalEscrow"
    assert escrow["args"] == [
        "$batches", "$relay", proposal["bitcoin_confirmations"],
        proposal["min_p2wpkh_sats"], proposal["min_p2tr_sats"]
    ]
    escrow_checks = {check["method"]: check["equals"] for check in escrow["checks"]}
    assert escrow_checks["minP2WPKHSats"] == proposal["min_p2wpkh_sats"]
    assert escrow_checks["minP2TRSats"] == proposal["min_p2tr_sats"]
    assert escrow_checks["depth"] == proposal["bitcoin_confirmations"]
    assert steps["deposits"]["args"][2] == proposal["bitcoin_confirmations"]
    assert steps["fee_manager"]["args"] == [
        "$conf_auth", "$validator", proposal["treasury"],
        ["0", proposal["wrap_fee_bps"], "0", "0"],
        ["0", proposal["unwrap_fee_bps"], "0", "0"],
    ]
    assert steps["fee_router"]["args"] == [
        "$vault", proposal["treasury"], proposal["wrap_staker_bps"], proposal["deploy_staker_bps"],
    ]
    assert steps["converter"]["args"] == [
        "$mp", "$usd", proposal["converter_numerator"], proposal["converter_denominator"],
    ]
    assert steps["fund_integration_converter"]["args"] == ["$converter", proposal["converter_funding"]]
    assert steps["dao_token"]["args"][2] == proposal["dao_token_recipient"]
    assert steps["controller"]["args"][2] is proposal["dao_use_reserve"]
    for factory in ("federations", "confederations"):
        assert steps[factory + "_explicit_integration_fee_exemption"]["args"] == [
            ZERO, "0", proposal["integration_fee_exemption"],
        ]
    settlement = steps["settlement"]
    assert settlement["artifact"] == "CustodianDAODailySettlement"
    assert settlement["args"] == [
        "$usd", "$staking", "$controller", "$conf_auth", "$twap", "$vault",
        proposal["treasury"],
    ]
    assert dict(
        contract="settlement", method="treasury", args=[], equals=proposal["treasury"]
    ) in settlement["checks"]
    admission = steps["adopt_confederation"]
    assert admission["args"][3:5] == [proposal["threshold"], proposal["min_custodians"]]
    assert [member[1] for member in admission["args"][2]] == proposal["participants"][1:]


@pytest.mark.parametrize("chain_id", ["31337", "11155111"])
def test_v3_cli_to_signed_enrollment_to_explicit_constructor_plan(tmp_path, capsys, chain_id):
    requested = invitation()
    requested["chain_id"] = chain_id
    path = tmp_path / "invitation.json"
    assert main(invite_args(requested, path)) == 0
    result = json.loads(capsys.readouterr().out)
    proposal = read_public(path)
    assert result == dict(status="INVITED", fingerprint=fingerprint(proposal), invitation=proposal)
    assert proposal["version"] == "6"
    assert "evm_rpc" not in proposal
    for key in (*POLICY_FIELDS, "treasury", "threshold", "chain_id"):
        assert proposal[key] == requested[key]
    assert len(set(proposal["sessions"].values())) == 3
    assert validate_invite(proposal) == proposal

    entries, enrollment_files = enroll(proposal, tmp_path)
    assert {entry["invitation_hash"] for entry in entries} == {fingerprint(proposal)}
    output = tmp_path / "profile.json"
    rpc = "https://participant-route.example.invalid/rpc"
    assert main([
        "assemble", "--rpc", rpc, "--invite", str(path),
        "--enrollments", *[str(f) for f in reversed(enrollment_files)],
        "--output", str(output),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PROFILE_READY"
    profile = read_public(output)
    assert verify_profile(profile) == proposal
    assert profile["evm_rpc"] == rpc
    assert profile["evm_confirmations"] == "1"
    for role in ROLES:
        assert profile[role]["participants"] == proposal["participants"]
        assert profile[role]["transport_keys"] == [
            "0x" + entry["transport_keys"][role] for entry in entries
        ]
    before = deepcopy(profile)
    assert_plan_policy(build(profile), proposal)
    assert profile == before


@pytest.mark.parametrize("missing", (*POLICY_FIELDS, "treasury"))
def test_v3_and_direct_bootstrap_never_supply_missing_policy(missing):
    proposal = invitation()
    config = direct_profile(proposal)
    del proposal[missing]
    del config[missing]
    original_invite, original_config = deepcopy(proposal), deepcopy(config)
    with pytest.raises(ValueError, match="INVITATION_SCHEMA"):
        validate_invite(proposal)
    with pytest.raises(ValueError, match="MISSING_PUBLIC_BOOTSTRAP_INPUTS:.*" + missing):
        build(config)
    assert proposal == original_invite
    assert config == original_config
    assert "steps" not in config


@pytest.mark.parametrize("flag", [
    *["--" + field.replace("_", "-") for field in POLICY_FIELDS], "--treasury", "--threshold",
])
def test_invite_cli_requires_each_policy_argument_before_writing(tmp_path, capsys, flag):
    output = tmp_path / "invitation.json"
    args = invite_args(invitation(), output)
    index = args.index(flag)
    del args[index:index + 2]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("key", [key for key, bounds in NUMBER_BOUNDS.items() if bounds[0] > 0])
@pytest.mark.parametrize("value", [
    None, True, 294, "", "0", "-1", "+1", "1.5", "0x1", " 294", "294 ", "1e3", str(2**256),
    "0294", "02", "٢٩٤", "２９４",
])
def test_policy_rejects_non_positive_non_decimal_and_uint256_overflow(key, value):
    proposal = invitation()
    proposal[key] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER: " + key):
        validate_invite(proposal)
    with pytest.raises(ValueError, match="INVALID_EXPLICIT_PARAMETER: " + key):
        build(direct_profile(proposal))


@pytest.mark.parametrize("key", ["wrap_fee_bps", "unwrap_fee_bps", "wrap_staker_bps", "deploy_staker_bps"])
@pytest.mark.parametrize("value", [None, True, 100, "-1", "+1", "01", "1.5", "10001", "0x10"])
def test_basis_points_are_explicit_canonical_decimal_values_between_zero_and_10000(key, value):
    proposal = invitation()
    proposal[key] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER: " + key):
        validate_invite(proposal)
    with pytest.raises(ValueError, match="INVALID_EXPLICIT_PARAMETER: " + key):
        build(direct_profile(proposal))


@pytest.mark.parametrize("basis_points", ["0", "10000"])
@pytest.mark.parametrize("use_reserve", [False, True])
def test_explicit_zero_or_full_fee_and_boolean_policy_survive_plan(basis_points, use_reserve):
    proposal = invitation()
    proposal.update({key: basis_points for key in (
        "wrap_fee_bps", "unwrap_fee_bps", "wrap_staker_bps", "deploy_staker_bps",
    )})
    proposal["dao_use_reserve"] = use_reserve
    assert validate_invite(proposal) == proposal
    assert_plan_policy(build(direct_profile(proposal)), proposal)


@pytest.mark.parametrize("value", ["1", "5", str(2**32)])
def test_bitcoin_depth_rejects_unsafe_or_uint32_overflow(value):
    proposal = invitation()
    proposal["bitcoin_confirmations"] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER: bitcoin_confirmations"):
        validate_invite(proposal)
    with pytest.raises(ValueError, match="INVALID_EXPLICIT_PARAMETER: bitcoin_confirmations"):
        build(direct_profile(proposal))


@pytest.mark.parametrize("key,value", [
    *[(key, value) for key in ("dao_use_reserve", "integration_fee_exemption")
      for value in (None, 0, 1, "true", "false")],
    ("integration_fee_exemption", False),
    *[("dao_token_recipient", value) for value in (ZERO, "", "0x0", None, 1)],
])
def test_boolean_and_recipient_policy_cannot_be_implicitly_coerced(key, value):
    proposal = invitation()
    proposal[key] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER: " + key):
        validate_invite(proposal)
    with pytest.raises(ValueError, match="INVALID_EXPLICIT_PARAMETER: " + key):
        build(direct_profile(proposal))


@pytest.mark.parametrize("field,value", [
    ("wrap_fee_bps", "10001"), ("converter_funding", "0"),
    ("converter_denominator", "-1"), ("bitcoin_confirmations", "5"),
    ("integration_fee_exemption", "false"), ("dao_token_recipient", ZERO),
])
def test_invalid_cli_policy_does_not_write_invitation(tmp_path, field, value):
    output = tmp_path / "invitation.json"
    args = invite_args(invitation(), output)
    args[args.index("--" + field.replace("_", "-")) + 1] = value
    with pytest.raises(ValueError, match="INVITATION_PARAMETER: " + field):
        main(args)
    assert not output.exists()


@pytest.mark.parametrize("p2wpkh,p2tr", [("294", "330"), (str(2**256 - 1), str(2**256 - 1))])
def test_payout_policy_keeps_dust_floors_and_explicit_uint256_upper_boundaries(p2wpkh, p2tr):
    proposal = invitation()
    proposal.update(min_p2wpkh_sats=p2wpkh, min_p2tr_sats=p2tr)
    assert validate_invite(proposal) == proposal
    assert_plan_policy(build(direct_profile(proposal)), proposal)


@pytest.mark.parametrize("value", ["1", str(2**256 - 1)])
def test_converter_policy_retains_explicit_uint256_boundaries(value):
    proposal = invitation()
    proposal.update(converter_numerator=value, converter_denominator=value, converter_funding=value)
    assert validate_invite(proposal) == proposal
    assert_plan_policy(build(direct_profile(proposal)), proposal)


@pytest.mark.parametrize("value", ["6", str(2**32 - 1)])
def test_bitcoin_depth_uint32_boundaries_are_passed_to_both_contracts(value):
    proposal = invitation()
    proposal["bitcoin_confirmations"] = value
    assert validate_invite(proposal) == proposal
    assert_plan_policy(build(direct_profile(proposal)), proposal)


@pytest.mark.parametrize("treasury", [ZERO, "", "0x0", "not-an-address", "0x" + "11" * 21])
def test_invalid_treasury_is_rejected_before_bootstrap(treasury):
    proposal = invitation()
    proposal["treasury"] = treasury
    with pytest.raises(ValueError):
        validate_invite(proposal)
    config = direct_profile(proposal)
    with pytest.raises(ValueError, match="INVALID_TREASURY"):
        build(config)
    assert "steps" not in config


@pytest.mark.parametrize("n,threshold,minimum", [
    (2, 2, 1), (3, 3, 2), (4, 3, 2), (4, 3, 3), (7, 5, 4), (10, 7, 6),
    (16, 12, 11), (16, 16, 15),
])
def test_confederation_threshold_boundary_is_valid_for_invitation_and_direct_config(n, threshold, minimum):
    proposal = invitation(n, threshold, minimum)
    assert validate_invite(proposal) == proposal
    assert_plan_policy(build(direct_profile(proposal)), proposal)


@pytest.mark.parametrize("n,threshold,minimum", [
    (3, 2, 1), (4, 2, 1), (7, 4, 3), (10, 6, 5), (16, 11, 10),
    (4, 5, 3), (4, 3, 1), (4, 3, 4), (2, 1, 1), (1, 1, 1), (17, 12, 11),
])
def test_invalid_threshold_or_custodian_composition_fails_in_both_entry_paths(n, threshold, minimum):
    proposal = invitation(n, threshold, minimum)
    with pytest.raises(ValueError, match="INVALID_THRESHOLD|INVITATION_CONFEDERATION_THRESHOLD"):
        validate_invite(proposal)
    config = direct_profile(proposal)
    with pytest.raises(ValueError, match="INVALID_THRESHOLD|BOOTSTRAP_CONFEDERATION_THRESHOLD"):
        build(config)
    assert "steps" not in config


@pytest.fixture(scope="module")
def enrolled_v3(tmp_path_factory):
    proposal = invitation()
    entries, _ = enroll(proposal, tmp_path_factory.mktemp("enrolled-policy-v3"))
    return proposal, entries, assemble(proposal, entries, "http://127.0.0.1:8545")


@pytest.mark.parametrize("key,value", [
    ("min_p2wpkh_sats", "298"), ("min_p2tr_sats", "402"),
    ("min_custodians", "3"), ("treasury", address(33)),
    ("bitcoin_confirmations", "8"), ("wrap_fee_bps", "18"), ("unwrap_fee_bps", "24"),
    ("wrap_staker_bps", "1235"), ("deploy_staker_bps", "6790"),
    ("converter_numerator", "4"), ("converter_denominator", "10000"),
    ("converter_funding", "7654322"), ("dao_token_recipient", address(30)),
    ("dao_use_reserve", False),
])
def test_enrollment_commits_policy_and_local_profiles_cannot_substitute_it(enrolled_v3, key, value):
    proposal, entries, profile = deepcopy(enrolled_v3)
    changed_invite = dict(proposal, **{key: value})
    assert validate_invite(changed_invite) == changed_invite
    assert fingerprint(changed_invite) != fingerprint(proposal)
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(changed_invite, entries)
    profile[key] = value
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(profile)
    # The direct local bootstrap path must enforce the signed policy too.
    assert "onchain_board" not in profile
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        build(profile)


def test_coordinator_admin_is_renounced_after_binding_before_v1_ceremonies():
    config = direct_profile()
    steps = build(config)["steps"]
    ids = [step["id"] for step in steps]
    bind = steps[ids.index("bind_dkg")]
    renounce = steps[ids.index("renounce_coordinator_admin")]
    role = "0x" + bytes(Web3.keccak(text="ADMIN_ROLE")).hex()
    assert bind["method"] == "bindDKGRegistry"
    assert bind["checks"] == [
        dict(contract="coordinator", method="dkgRegistry", args=[], equals="$dkg")
    ]
    assert renounce["contract"] == "coordinator"
    assert renounce["kind"] == "call"
    assert renounce["method"] == "renounceRole"
    assert renounce["args"] == [role, config["operator"]]
    assert renounce["checks"] == [
        dict(contract="coordinator", method="hasRole", args=[role, config["operator"]], equals=False)
    ]
    assert ids.index("bind_dkg") < ids.index("renounce_coordinator_admin")
    for ceremony in ROLES:
        assert ids.index("renounce_coordinator_admin") < ids.index(ceremony + "_create")
    assert not any(step.get("method") == "grantRole" for step in steps)


@pytest.mark.parametrize("kind", ["ordered-roster", "different-member", "threshold"])
def test_direct_config_rejects_btc_wallet_without_matching_confederation_result(kind):
    config = direct_profile()
    btc = config["btc_ceremony"]
    if kind == "ordered-roster":
        btc["participants"][1:3] = reversed(btc["participants"][1:3])
    elif kind == "different-member":
        btc["participants"][-1] = address(31)
    else:
        btc["threshold"] = "4"
    with pytest.raises(ValueError, match="BOOTSTRAP_BITCOIN_ROSTER_THRESHOLD"):
        build(config)


def test_direct_confederation_roster_starts_with_the_actual_federation_representative():
    config = direct_profile()
    for role in ("conf_ceremony", "btc_ceremony"):
        config[role]["participants"][:2] = reversed(config[role]["participants"][:2])
    with pytest.raises(ValueError, match="BOOTSTRAP_FEDERATION_REPRESENTATIVE"):
        build(config)


@pytest.mark.parametrize("kind", ["duplicate-member", "zero-member", "duplicate-session", "zero-session", "operator"])
def test_direct_config_rejects_inadmissible_dkg_inputs_before_any_plan(kind):
    config = direct_profile()
    if kind == "duplicate-member":
        config["conf_ceremony"]["participants"][-1] = config["operator"]
    elif kind == "zero-member":
        config["conf_ceremony"]["participants"][-1] = ZERO
    elif kind == "duplicate-session":
        config["btc_ceremony"]["id"] = config["conf_ceremony"]["id"]
    elif kind == "zero-session":
        config["btc_ceremony"]["id"] = "0x" + "00" * 32
    else:
        config["operator"] = address(31)
    with pytest.raises(ValueError, match="INVALID_ROSTER|INVALID_SESSION|BOOTSTRAP_CEREMONY_SESSIONS|BOOTSTRAP_OPERATOR_NOT_PARTICIPANT"):
        build(config)
    assert "steps" not in config


@pytest.mark.parametrize("version", ["1", "2"])
def test_legacy_invites_remain_readable_but_publication_and_implicit_policy_upgrade_fail(tmp_path, capsys, version):
    proposal = invitation(n=3, threshold=2, version=version)
    path = tmp_path / ("invitation-v%s.json" % version)
    path.write_text(json.dumps(proposal))
    assert validate_invite(read_public(path)) == proposal
    assert main(["inspect", "--invite", str(path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["invitation"] == proposal
    assert inspected["fingerprint"] == fingerprint(proposal)
    assert all(key not in inspected["invitation"] for key in POLICY_FIELDS)
    # Deliberately omit RPC and identity attributes: version rejection precedes both.
    with pytest.raises(ValueError, match="PUBLICATION_REQUIRES_V3"):
        chain_main(SimpleNamespace(action="publish", invite=path))
    entries, _ = enroll(proposal, tmp_path)
    legacy_profile = assemble(proposal, entries, "http://127.0.0.1:8545")
    assert verify_profile(legacy_profile) == proposal
    assert all(key not in legacy_profile for key in POLICY_FIELDS)
    with pytest.raises(ValueError, match="MISSING_PUBLIC_BOOTSTRAP_INPUTS:.*min_p2wpkh_sats.*min_p2tr_sats.*min_custodians"):
        build(legacy_profile)


@pytest.mark.parametrize("version", ["1", "2"])
def test_legacy_enrollment_cannot_authorize_added_constructor_policy(tmp_path, version):
    # This roster already meets today's confederation policy, so rejection cannot
    # be caused by an obsolete threshold or by missing constructor arguments.
    proposal = invitation(n=4, threshold=3, min_custodians=2, version=version)
    entries, _ = enroll(proposal, tmp_path)
    legacy_profile = assemble(proposal, entries, "http://127.0.0.1:8545")
    added_policy = {key: invitation()[key] for key in POLICY_FIELDS}
    legacy_profile.update(added_policy)

    # Legacy reading and signature verification remain supported. The new
    # parameters, however, are absent from the participants' signed invitation.
    assert verify_profile(legacy_profile) == proposal
    assert all(key not in proposal for key in POLICY_FIELDS)

    # A deliberately explicit, unenrolled configuration is still a valid input.
    explicit_profile = {k: deepcopy(v) for k, v in legacy_profile.items() if k != "onboarding"}
    assert_plan_policy(build(explicit_profile), dict(proposal, **added_policy))

    with pytest.raises(ValueError, match="BOOTSTRAP_REQUIRES_V3_INVITATION"):
        build(legacy_profile)
    assert "steps" not in legacy_profile

