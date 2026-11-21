import hashlib
import json
import pytest
from signer_agent.funding_envelope import inspect_funding


def compact(number):
    if number < 253: return bytes([number])
    if number < 65536: return b"\xfd" + number.to_bytes(2, "little")
    return b"\xfe" + number.to_bytes(4, "little")


def transaction(inputs=1, outputs=1, witness_size=0):
    vin = compact(inputs) + b"".join(bytes.fromhex("11" * 32) + index.to_bytes(4, "little")
        + b"\x01\x51\xff\xff\xff\xff" for index in range(inputs))
    vout = compact(outputs) + (1000 .to_bytes(8, "little") + b"\x01\x51") * outputs
    stripped = b"\x02\x00\x00\x00" + vin + vout + bytes(4)
    if witness_size:
        raw = b"\x02\x00\x00\x00\x00\x01" + vin + vout + (
            b"\x01" + compact(witness_size) + b"\x01" * witness_size) * inputs + bytes(4)
    else:
        raw = stripped
    txid = hashlib.sha256(hashlib.sha256(stripped).digest()).digest()[::-1].hex()
    return raw.hex(), stripped.hex(), txid


@pytest.mark.parametrize("inputs,outputs", [(1,33), (1,256), (1,257), (257,1)])
def test_real_pinned_parser_handles_external_counts(inputs, outputs):
    raw, stripped, txid = transaction(inputs, outputs)
    result = inspect_funding(raw, txid)
    assert result["stripped"] == stripped and result["raw"] == raw
    assert len(result["inputs"]) == inputs and len(result["outputs"]) == outputs
    assert result["parser"] == "rust-bitcoin-0.32.7"


def test_real_parser_cli_preserves_large_witness_and_stripped_txid(tmp_path, capsys):
    from signer_agent.bitcoin_cli import main
    raw, stripped, txid = transaction(witness_size=100001)
    path = tmp_path / "funding.hex"; path.write_text(raw)
    assert main(["inspect-funding", "--raw-file", str(path), "--txid", txid]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["raw"] == raw and result["stripped"] == stripped
    assert int(result["raw_bytes"]) > 100000 and result["txid"] != result["wtxid"]
    with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
        inspect_funding(raw, result["wtxid"])
