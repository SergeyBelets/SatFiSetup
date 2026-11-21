"""The same canonical public-byte fixtures feed Python/Rust and Solidity tests."""

import hashlib
import json

import pytest

from funding_vectors import (MANIFEST, PROFILE, ROOT, check_rust, compact_size,
                             expected_artifacts, rust_binary, vectors)


CASES = vectors()


def test_checked_in_fixture_artifacts_are_reproducible():
    for path, expected in expected_artifacts().items():
        assert path.read_text() == expected, f"regenerate fixture: {path}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_fixture_hash_order_and_public_outpoint_binding(case):
    document = json.loads(MANIFEST.read_text())
    assert document["schema_version"] == 1
    assert document["profile"] == PROFILE
    fixture = next(item for item in document["vectors"] if item["name"] == case.name)
    raw_file = (ROOT / "web3" / fixture["raw_file"]).read_text()
    assert raw_file.startswith("0x") and not raw_file.endswith("\n")
    raw = bytes.fromhex(raw_file[2:])
    expected = fixture["expected"]
    metadata_file = (ROOT / "web3" / fixture["raw_file"]).with_suffix(".json")
    metadata = json.loads(metadata_file.read_text())
    assert metadata == {**fixture, "expected": {key: value for key, value in expected.items() if key != "inputs"}}
    raw_hash = hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    assert raw == case.serialize()
    assert raw_hash.hex() == expected["raw_sha256d"]
    assert raw_hash[::-1].hex() == expected["txid"]
    assert raw_hash.hex() != expected["txid"]  # fixtures reveal byte-order mistakes
    assert 0 < len(raw) <= 100_000
    assert len(set((item.txid_le, item.vout) for item in case.inputs)) == len(case.inputs)
    target = case.outputs[fixture["target_vout"]]
    assert expected["target_value"] == target.value
    assert expected["target_script_hex"] == target.script.hex()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_pinned_rust_inspector_matches_shared_fixture(case):
    if not rust_binary().is_file():
        pytest.skip("build the pinned frost-crypto CLI locally to run the independent parser check")
    check_rust(case)


def test_envelope_and_compact_size_boundaries_are_present():
    by_name = {case.name: case for case in CASES}
    assert [len(by_name[f"outputs_{count}"].outputs) for count in (33, 256, 257)] == [33, 256, 257]
    assert len(by_name["inputs_257"].inputs) == 257
    assert len(by_name["max_inputs_2438"].inputs) == 2438
    assert len(by_name["max_outputs_11105"].outputs) == 11105
    assert len(by_name["script_65536"].outputs[0].script) == 65536
    assert len(by_name["stripped_100000"].serialize()) == 100_000
    # The next minimal input/output would exceed the profile byte envelope.
    assert len(by_name["max_inputs_2438"].serialize()) + 41 > 100_000
    assert len(by_name["max_outputs_11105"].serialize()) + 9 > 100_000
    assert compact_size(252) == b"\xfc"
    assert compact_size(253) == b"\xfd\xfd\x00"
    assert compact_size(65535) == b"\xfd\xff\xff"
    assert compact_size(65536) == b"\xfe\x00\x00\x01\x00"
