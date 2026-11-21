import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from signer_agent import deposit_intent as m

ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / "rust/zkspv/derivation-qualification/public-vector.json").read_text())
RAW = bytes.fromhex(VECTOR["intent_bytes"])
EVM_VECTORS = json.loads((ROOT / "web3/test/fixtures/deposit-derivation-v1.json").read_text())["vectors"]


def test_canonical_intent_is_byte_identical_to_frozen_native_vector():
    intent = m.DepositIntentV1.decode(RAW)
    assert intent.encode() == RAW
    assert intent.intent_id.hex() == VECTOR["intent_id"]
    assert intent.evm_chain_id == 31337
    assert intent.key_epoch == 9
    assert intent.recipient != intent.requester


def test_all_context_fields_change_the_intent_and_only_wallet_fields_change_family():
    intent = m.DepositIntentV1.decode(RAW)
    excluded = {"pool", "mp_token", "requester", "recipient", "request_nonce", "service_policy_hash"}
    for field in fields(intent):
        old = getattr(intent, field.name)
        changed = old + 1 if type(old) is int else bytes([old[0] ^ 128]) + old[1:]
        variant = replace(intent, **{field.name: changed})
        assert variant.intent_id != intent.intent_id
        assert (variant.wallet_context == intent.wallet_context) == (field.name in excluded)


@pytest.mark.parametrize("raw", [b"", RAW[:-1], RAW + b"\0", b"x" + RAW[1:], RAW[:26] + b"\1" + RAW[27:]])
def test_noncanonical_intents_fail(raw):
    with pytest.raises(ValueError):
        m.DepositIntentV1.decode(raw)


def test_invalid_shapes_and_integer_types_fail():
    intent = m.DepositIntentV1.decode(RAW)
    for field in fields(intent):
        bad = -1 if type(getattr(intent, field.name)) is int else bytes(len(getattr(intent, field.name)))
        with pytest.raises(ValueError):
            replace(intent, **{field.name: bad}).encode()
    for epoch in [True, 1.0, 2**64, "9"]:
        with pytest.raises(ValueError):
            replace(intent, key_epoch=epoch).encode()
    with pytest.raises(ValueError):
        replace(intent, evm_chain_id=0).encode()


def test_native_ipc_descriptor_matches_frozen_vector_and_remains_unready():
    result = m.DepositIntentV1.decode(RAW).derive(bytes.fromhex(VECTOR["base_group"]))
    for field in ("profile", "intent_id", "base_group", "leaf_script", "merkle_root", "tweak", "output_key",
                  "output_parity_odd", "script_pubkey", "binding_commitment"):
        assert result[field] == VECTOR[field]
    assert result["ready"] is False
    assert result["input_key"]["mode"] == "DERIVED"


@pytest.mark.parametrize("vector", EVM_VECTORS, ids=[f"native-evm-{i}" for i in range(len(EVM_VECTORS))])
def test_all_frozen_evm_vectors_match_current_native_ipc(vector):
    result = m.DepositIntentV1.decode(bytes.fromhex(vector["intent"])).derive(bytes.fromhex(vector["base_group"]))
    assert result == {key: value for key, value in vector.items() if key != "intent"}


@pytest.mark.parametrize("field,value", [
    ("ready", True), ("binding_commitment", "00" * 32), ("output_parity_odd", 0),
    ("merkle_root", "00" * 32), ("wallet_context", "00" * 32), ("script_pubkey", "5120" + "00" * 32),
])
def test_native_response_tampering_never_activates_an_address(monkeypatch, field, value):
    intent = m.DepositIntentV1.decode(RAW)
    base = bytes.fromhex(VECTOR["base_group"])
    result = intent.derive(base)
    result[field] = value
    monkeypatch.setattr(m.crypto, "call", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="DEPOSIT_DERIVATION_RESPONSE"):
        intent.derive(base)
