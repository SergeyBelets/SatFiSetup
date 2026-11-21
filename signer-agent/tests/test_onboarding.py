"""First-use invitation binding, private file boundaries and typed API arguments."""

import json
import os
import time
import pytest
from eth_account import Account
from signer_agent.onboarding import (
    join,
    assemble,
    fingerprint,
    validate_invite,
    password_file,
)
from signer_agent.dkg_protocol import PROTOCOL_ID
from signer_agent.bootstrap import encode_args


def invite():
    addresses = [Account.from_key(i.to_bytes(32, "big")).address for i in (1, 2, 3)]
    return dict(
        version="1",
        chain_id="31337",
        evm_rpc="http://127.0.0.1:8545",
        bitcoin_network="regtest",
        mode="integration",
        deployment_id="friends",
        operator=addresses[0],
        treasury=addresses[2],
        approval_deadline=str(int(time.time()) + 86400),
        federation_id="friends-fed",
        confederation_id="friends-conf",
        participants=addresses,
        threshold="2",
        epoch="1",
        sessions={
            r: os.urandom(32).hex()
            for r in ("evm_ceremony", "conf_ceremony", "btc_ceremony")
        },
        protocol_id=PROTOCOL_ID.hex(),
        phase_duration="3600",
    )


@pytest.mark.parametrize("version", ["1", "2"])
def test_enrollment_all_members_no_secret_collection_or_substitution(tmp_path, version):
    proposal = invite()
    proposal["version"] = version
    if version == "2":
        proposal.pop("evm_rpc")
    entries = []
    for i in (1, 2, 3):
        identity = tmp_path / str(i)
        identity.write_text(json.dumps({"private_key": "%064x" % i}))
        identity.chmod(0o600)
        directory = tmp_path / ("friend" + str(i))
        entry = join(proposal, fingerprint(proposal), identity, directory)
        assert entry == join(proposal, fingerprint(proposal), identity, directory)
        assert "secret" not in json.dumps(entry) and "private_key" not in json.dumps(
            entry
        )
        entries.append(entry)
    profile = assemble(proposal, entries)
    from signer_agent.onboarding import verify_profile

    routed = dict(profile, evm_rpc="https://example.invalid/private-local-route")
    assert verify_profile(routed) == proposal
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(dict(routed, treasury=proposal["participants"][0]))
    assert profile["conf_ceremony"]["participants"] == proposal["participants"]
    with pytest.raises(ValueError, match="INCOMPLETE"):
        assemble(proposal, entries[:2])
    altered = json.loads(json.dumps(entries))
    altered[0]["transport_keys"]["btc_ceremony"] = "a1" * 32
    with pytest.raises(ValueError, match="SIGNATURE"):
        assemble(proposal, altered)
    altered = dict(proposal, threshold="3")
    with pytest.raises(ValueError, match="CONTEXT"):
        assemble(altered, entries)
    with pytest.raises(ValueError, match="FINGERPRINT"):
        join(proposal, "0" * 64, identity, tmp_path / "other")
    with pytest.raises(ValueError, match="CONTEXT"):
        assemble(proposal, entries + [entries[0]])


def test_v2_invitation_cannot_contain_rpc_credentials():
    proposal = invite()
    proposal["version"] = "2"
    proposal.pop("evm_rpc")
    assert validate_invite(proposal) == proposal
    with pytest.raises(ValueError, match="INVITATION_SCHEMA"):
        validate_invite(
            dict(proposal, evm_rpc="https://user:token@example.invalid/key")
        )


def test_legacy_invitation_publication_rejected_before_rpc_or_identity(tmp_path):
    from types import SimpleNamespace
    from signer_agent.onboarding import chain_main

    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(invite()))
    with pytest.raises(ValueError, match="PUBLICATION_REQUIRES_V3"):
        chain_main(SimpleNamespace(action="publish", invite=path))


def test_local_secret_files_fail_closed(tmp_path):
    path = tmp_path / "password"
    path.write_text("short")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="32_RANDOM_BYTES"):
        password_file(path)
    path.write_text(os.urandom(32).hex())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="PERMISSIONS"):
        password_file(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        password_file(link)


@pytest.mark.parametrize(
    "typ,value",
    [
        ("uint8", "256"),
        ("uint256", "-1"),
        ("int8", "128"),
        ("bool", "false"),
        ("bool", 1),
        ("bytes32", "0xab"),
        ("uint256[2]", ["1"]),
        ("uint256[]", "12"),
    ],
)
def test_invalid_abi_values_rejected_before_queueing(typ, value):
    with pytest.raises((ValueError, TypeError)):
        encode_args([dict(type=typ)], [value])


def test_exact_abi_boundaries_and_nested_arrays():
    assert encode_args(
        [dict(type="uint8"), dict(type="bool"), dict(type="uint256[2][]")],
        ["255", False, [["1", "2"]]],
    ) == [255, False, [[1, 2]]]


def test_simultaneous_first_joins_share_one_password_and_enrollment(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from signer_agent import onboarding as module
    proposal=invite();identity=tmp_path/'identity.json'
    identity.write_text(json.dumps(dict(private_key=f'{1:064x}')));identity.chmod(0o600)
    directory=tmp_path/'participant';start=Barrier(2);writes=[];original=module.atomic_private
    def slow_password(path,data):
        if path.name=='password':writes.append(path);time.sleep(.05)
        return original(path,data)
    monkeypatch.setattr(module,'atomic_private',slow_password)
    def attempt(_):
        start.wait(timeout=10)
        return module.join(proposal,module.fingerprint(proposal),identity,directory)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert results[0]==results[1] and len(writes)==1
    assert module.join(proposal,module.fingerprint(proposal),identity,directory)==results[0]
