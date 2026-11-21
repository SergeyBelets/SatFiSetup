# SPDX-License-Identifier: Apache-2.0
"""Signed-text formats against tests/vectors/vectors.json, positive and negative."""

import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from satoshifi.common import canon, messages, sigs

V = json.loads((Path(__file__).parent / "vectors" / "vectors.json").read_text(encoding="utf-8"))
P = V["params"]
A = V["accounts"]


def other_key():
    return Account.from_key(keccak(text="satoshifi test key: participant-other"))


# --- canonical JSON ----------------------------------------------------------

@pytest.mark.parametrize("case", V["canon"], ids=[c["name"] for c in V["canon"]])
def test_canon_vectors(case):
    raw = canon.jcs(case["value"])
    assert raw.decode("utf-8") == case["jcs"]
    assert canon.keccak_hex(raw) == case["keccak"]


@pytest.mark.parametrize("bad", [
    {"x": 1.5}, {"x": 2**53}, {"x": -(2**53)}, {1: "a"}, {"x": "\ud800"}, {"x": object()},
])
def test_canon_rejects(bad):
    with pytest.raises(canon.CanonError):
        canon.jcs(bad)


def test_canon_key_order_independent():
    assert canon.jcs({"a": 1, "b": 2}) == canon.jcs({"b": 2, "a": 1})


# --- request -----------------------------------------------------------------

def test_request_vector():
    r = V["request"]
    assert messages.request_hash(r["body"]) == r["hash"]
    assert messages.request_message(P["domain"], P["chain_id"], r["issued_at"], r["body"]) == r["message"]
    sigs.verify(r["message"], r["signature"], r["signer"])


def test_request_body_change_breaks_signature():
    r = V["request"]
    body = json.loads(json.dumps(r["body"]))
    body["participants"][1]["label"] = "Changed"
    msg = messages.request_message(P["domain"], P["chain_id"], r["issued_at"], body)
    with pytest.raises(sigs.SignatureError):
        sigs.verify(msg, r["signature"], r["signer"])


@pytest.mark.parametrize("domain,chain_id,issued", [
    ("evil.example", 1, None), ("btcyield.online", 11155111, None), ("btcyield.online", 1, 1),
])
def test_request_other_context_breaks_signature(domain, chain_id, issued):
    r = V["request"]
    msg = messages.request_message(domain, chain_id, issued or r["issued_at"] + 1, r["body"])
    with pytest.raises(sigs.SignatureError):
        sigs.verify(msg, r["signature"], r["signer"])


# --- wsig and binding SIWE ---------------------------------------------------

def test_wsig_vector():
    w = V["wsig"]
    assert messages.wsig_message(w["deployment"], P["chain_id"], w["work"], w["code"], w["exp"]) == w["message"]
    sigs.verify(w["message"], w["signature"], w["signer"])


@pytest.mark.parametrize("field,value", [
    ("deployment", "bad id!"), ("work", A["work"].lower()), ("code", "00" * 31),
    ("code", "AB" * 32), ("exp", -1),
])
def test_wsig_rejects_bad_fields(field, value):
    w = dict(V["wsig"])
    w[field] = value
    with pytest.raises(messages.FormatError):
        messages.wsig_message(w["deployment"], P["chain_id"], w["work"], w["code"], w["exp"])


def test_binding_siwe_vector():
    b, w = V["binding_siwe"], V["wsig"]
    fields = messages.siwe_parse(b["message"])
    assert fields["domain"] == P["domain"]
    assert fields["address"] == b["signer"]
    assert fields["statement"] == messages.binding_statement(w["deployment"], w["work"])
    assert fields["nonce"] == w["code"]
    assert fields["expiration"] == w["exp"]
    assert fields["resources"] == [messages.wsig_resource(w["signature"])] == [b["resource"]]
    sigs.verify(b["message"], b["signature"], b["signer"])


def test_login_siwe_vector():
    s = V["login_siwe"]
    fields = messages.siwe_parse(s["message"])
    assert fields["statement"] == messages.login_statement()
    assert fields["nonce"] == s["nonce"]
    assert fields["resources"] == []
    assert fields["expiration"] - fields["issued_at"] == 300
    sigs.verify(s["message"], s["signature"], s["signer"])


@pytest.mark.parametrize("mutate", [
    lambda m: m.replace("Version: 1", "Version: 2"),
    lambda m: m + "\n",
    lambda m: m.replace("\n\n", "\n", 1),
    lambda m: m.replace("Z\nExpiration", "+00:00\nExpiration"),
    lambda m: m.replace("Chain ID: 1", "Chain ID: 01"),
    lambda m: m.replace("Nonce: ", "Nonce: !"),
    lambda m: m.replace("URI: https://", "URI: http://"),
    lambda m: m.replace("\nExpiration Time", "\nNot Before: 2026-01-01T00:00:00Z\nExpiration Time"),
])
def test_siwe_parse_rejects(mutate):
    with pytest.raises(messages.FormatError):
        messages.siwe_parse(mutate(V["binding_siwe"]["message"]))


def test_siwe_foreign_domain_is_visible_to_caller():
    # Layout is valid; the caller must compare domain with SIWE_DOMAIN and reject.
    fields = messages.siwe_parse(V["binding_siwe"]["message"].replace("btcyield.online wants", "evil.example wants"))
    assert fields["domain"] == "evil.example" != P["domain"]


def test_siwe_lowercase_address_rejected():
    msg = V["login_siwe"]["message"]
    addr = V["login_siwe"]["signer"]
    with pytest.raises(messages.FormatError):
        messages.siwe_parse(msg.replace(addr, addr.lower()))


def test_wsig_resource_rejects_garbage():
    with pytest.raises(messages.FormatError):
        messages.wsig_resource("0x1234")


# --- signed API requests -----------------------------------------------------

@pytest.mark.parametrize("case", V["api"], ids=[c["method"] for c in V["api"]])
def test_api_vectors(case):
    msg = messages.api_string(case["method"], case["path"], case["timestamp"], case["nonce"],
                              case["body"].encode("utf-8"))
    assert msg == case["message"]
    sigs.verify(msg, case["signature"], case["signer"])


def test_api_body_byte_change_breaks_signature():
    case = V["api"][0]
    tampered = case["body"].replace('"OK"', '"FAIL"').encode("utf-8")
    msg = messages.api_string(case["method"], case["path"], case["timestamp"], case["nonce"], tampered)
    with pytest.raises(sigs.SignatureError):
        sigs.verify(msg, case["signature"], case["signer"])


@pytest.mark.parametrize("args", [
    ("TRACE", "/v1/reports", 1, "0x" + "0" * 32, b""),
    ("POST", "/v2/reports", 1, "0x" + "0" * 32, b""),
    ("POST", "/v1/reports x", 1, "0x" + "0" * 32, b""),
    ("POST", "/v1/reports", 1, "0x" + "0" * 31, b""),
    ("POST", "/v1/reports", 1, "0x" + "0" * 32, "text"),
])
def test_api_string_rejects(args):
    with pytest.raises(messages.FormatError):
        messages.api_string(*args)


# --- signature recovery ------------------------------------------------------

def test_wrong_signer_rejected():
    r = V["request"]
    forged = other_key().sign_message(encode_defunct(text=r["message"])).signature.hex()
    with pytest.raises(sigs.SignatureError):
        sigs.verify(r["message"], "0x" + forged.removeprefix("0x"), r["signer"])


@pytest.mark.parametrize("sig", ["", "0x", "0x" + "00" * 65, "0x" + "zz" * 65, "0x" + "11" * 64])
def test_malformed_signature_rejected(sig):
    with pytest.raises(sigs.SignatureError):
        sigs.recover(V["request"]["message"], sig)
