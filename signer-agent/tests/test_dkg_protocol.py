import json
from pathlib import Path
import pytest
from eth_abi import encode
from eth_utils import keccak
from signer_agent import crypto
from signer_agent.dkg_protocol import Manifest, round1, commitment, hpke_context
from signer_agent.dkg_transport import Mailbox, sign, recover
from eth_account import Account


def fixture():
    return json.loads(
        (Path(__file__).resolve().parents[2] / "tests/vectors/dkg-v1.json").read_text()
    )


def test_golden_preimages_and_context_substitution():
    data = fixture()
    m = Manifest.from_dict(data["manifest"])
    assert m.context.hex() == data["vectors"][0]["hash"]
    for field, value in [
        ("chain_id", "1"),
        ("epoch", 8),
        ("registry", "0x" + "66" * 20),
        ("purpose", "BTC_SPEND"),
    ]:
        altered = dict(data["manifest"])
        altered[field] = value
        assert Manifest.from_dict(altered).context != m.context
    altered = dict(data["manifest"])
    altered["participants"] = list(reversed(altered["participants"]))
    assert Manifest.from_dict(altered).context != m.context
    for vector in data["vectors"]:
        assert keccak(bytes.fromhex(vector["preimage"])).hex() == vector["hash"]


def test_hpke_confidentiality_and_context_authentication():
    key = crypto.call("transport_key")
    out = crypto.call(
        "seal", public=key["public"], plaintext="11" * 80, info="aabb", aad="ccdd"
    )
    assert "11" * 40 not in out["ciphertext"]
    assert (
        crypto.call(
            "open",
            secret=key["secret"],
            enc=out["enc"],
            ciphertext=out["ciphertext"],
            info="aabb",
            aad="ccdd",
        )["plaintext"]
        == "11" * 80
    )
    with pytest.raises(RuntimeError):
        crypto.call(
            "open",
            secret=key["secret"],
            enc=out["enc"],
            ciphertext=out["ciphertext"],
            info="aabc",
            aad="ccdd",
        )


def test_round1_corruption_is_rejected():
    out = crypto.call("part1", index=1, n=3, t=2)
    other = crypto.call("part1", index=2, n=3, t=2)
    third = crypto.call("part1", index=3, n=3, t=2)
    corrupted = other["package"][:-2] + (
        "00" if other["package"][-2:] != "00" else "01"
    )
    with pytest.raises(RuntimeError):
        crypto.call(
            "part2",
            secret=out["secret"],
            round1={"2": corrupted, "3": third["package"]},
        )


def test_evm_adapter_matches_python_scalar_equation():
    from signer_agent.frost_math import signature_share, NoncePair

    params = dict(share=17, hiding=23, binding=29, rho=31, lagrange=37, challenge=41)
    for negate in (False, True):
        actual = crypto.call(
            "evm_share_v1",
            **{k: "%064x" % v for k, v in params.items()},
            negate_nonce=negate
        )
        expected = signature_share(17, NoncePair(23, 29), 31, 37, 41, negate)
        assert int(actual["share"], 16) == expected


def test_transport_signature_roles_and_conflicting_retry(tmp_path):
    original = fixture()["manifest"]
    accounts = [Account.from_key(i.to_bytes(32, "big")) for i in (1, 2, 3)]
    original["participants"] = [a.address for a in accounts]
    m = Manifest.from_dict(original)
    box = Mailbox(tmp_path / "mail.sqlite")
    a, b = accounts[:2]
    signature = sign(
        m,
        "DKGTransportKey",
        a.key,
        participant=a.address,
        transportPublicKey=m.transport_keys[0],
    )
    box.put(m, "key", a.address, "", dict(signature=signature))
    box.put(m, "key", a.address, "", dict(signature=signature))
    wrong = sign(
        m,
        "DKGTransportKey",
        b.key,
        participant=a.address,
        transportPublicKey=m.transport_keys[0],
    )
    with pytest.raises(ValueError, match="SENDER_MISMATCH"):
        box.put(m, "key", a.address, "", dict(signature=wrong))
    receipt = sign(
        m,
        "DKGReceipt",
        b.key,
        sender=a.address,
        recipient=b.address,
        shareHash=b"1" * 32,
        verified=True,
    )
    assert (
        recover(
            m,
            "DKGReceipt",
            receipt,
            sender=a.address,
            recipient=b.address,
            shareHash=b"1" * 32,
            verified=True,
        )
        == b.address
    )
    box.put(m, "round1", a.address, "", dict(package="abcd"))
    with pytest.raises(ValueError, match="CONFLICTING_RETRY"):
        box.put(m, "round1", a.address, "", dict(package="abce"))


def test_library_maximum_roster_and_group_parity_preserve_each_public_share():
    from coincurve import PublicKey
    from signer_agent.operations import canonical

    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    observed_raw_odd = False
    observed_share_odd = False
    # First exercise the declared maximum; subsequent small ceremonies cover random parity.
    for attempt in range(64):
        n, t = (16, 16) if attempt == 0 else (3, 2)
        first = {
            str(i): crypto.call("part1", index=i, n=n, t=t) for i in range(1, n + 1)
        }
        second = {
            i: crypto.call(
                "part2",
                secret=first[i]["secret"],
                round1={j: v["package"] for j, v in first.items() if j != i},
            )
            for i in first
        }
        results = []
        for i in first:
            public = dict(
                package=first[i]["package"], commitments=first[i]["commitments"]
            )
            assert len(canonical(public).encode()) <= 16384
            result = crypto.call(
                "part3",
                secret=second[i]["secret"],
                round1={j: v["package"] for j, v in first.items() if j != i},
                round2={j: v["packages"]["%064x" % int(i)] for j, v in second.items() if j != i},
            )
            results.append(result)
            assert (
                PublicKey.from_valid_secret(bytes.fromhex(result["signing_share"]))
                .format(compressed=True)
                .hex()
                == result["public_share"]
            )
            original = PublicKey(bytes.fromhex(result["original_group"]))
            if result["normalization_negated"]:
                original = original.multiply((order - 1).to_bytes(32, "big"))
            assert original.format(compressed=True).hex() == result["group"]
            assert result["group"].startswith("02")
            observed_raw_odd |= result["raw_dkg_group"].startswith("03")
            observed_share_odd |= result["public_share"].startswith("03")
        assert len({r["group"] for r in results}) == 1
        assert all(r["public_shares"] == results[0]["public_shares"] for r in results)
        if observed_raw_odd and observed_share_odd:
            break
    assert observed_raw_odd and observed_share_odd
