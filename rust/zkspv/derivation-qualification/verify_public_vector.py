#!/usr/bin/env python3
"""Check public fixture hashes/encoding independently; no curve or secret code."""
from hashlib import sha256
import json
from pathlib import Path


def digest(value):
    return sha256(value).digest()


def tagged(tag, value):
    prefix = digest(tag)
    return digest(prefix + prefix + value)


def check():
    vector = json.loads(Path(__file__).with_name("public-vector.json").read_text())
    assert vector["test_keys_only"] is True
    assert vector["profile"] == "BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1"
    intent = b"SATOHSI_DEPOSIT_INTENT_V1\0" + b"\0\1\0\1"
    intent += bytes([1]) * 32 + (31337).to_bytes(32, "big")
    intent += bytes([2]) * 20 + bytes([3]) * 20 + bytes([4]) * 20
    intent += bytes([5]) * 32 + bytes([6]) * 20 + bytes([7]) * 32 + bytes([8]) * 32
    intent += (9).to_bytes(8, "big") + bytes([10]) * 20 + bytes([11]) * 20
    intent += bytes([12]) * 32 + bytes([13]) * 32
    assert len(intent) == 382 and intent.hex() == vector["intent_bytes"]
    intent_id = digest(intent)
    assert intent_id.hex() == vector["intent_id"]
    leaf = b"\x6a\x20" + intent_id
    assert leaf.hex() == vector["leaf_script"] and vector["leaf_version"] == "c0"
    root = tagged(b"TapLeaf", b"\xc0\x22" + leaf)
    assert root.hex() == vector["merkle_root"]
    base = bytes.fromhex(vector["base_group"])
    assert len(base) == 33 and base[0] == 2
    tweak = tagged(b"TapTweak", base[1:] + root)
    assert tweak.hex() == vector["tweak"]
    output = bytes.fromhex(vector["output_key"])
    assert len(output) == 32
    assert vector["derived_group_even_y"] == "02" + output.hex()
    script = b"\x51\x20" + output
    assert script.hex() == vector["script_pubkey"]
    assert type(vector["output_parity_odd"]) is bool
    binding = b"SATOHSI_DEPOSIT_BINDING_V1\0" + intent + base + leaf + root + tweak
    binding += output + bytes([vector["output_parity_odd"]]) + script
    assert digest(binding).hex() == vector["binding_commitment"]
    print("PASS: frozen intent encoding, TapLeaf/TapTweak hashes, script and binding; no curve claim")


if __name__ == "__main__":
    check()
