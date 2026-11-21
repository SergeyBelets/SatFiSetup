# SPDX-License-Identifier: Apache-2.0
"""Generate tests/vectors/vectors.json.

Keys below are derived from public strings and are TEST-ONLY: anyone can
recompute them. Never fund these addresses or use them outside tests.
Run from backend/:  python tests/vectors/make_vectors.py
"""

import json
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from satoshifi.common import canon, messages

OUT = Path(__file__).with_name("vectors.json")


def test_key(label: str):
    return Account.from_key(keccak(text="satoshifi test key: " + label))


def sign(acct, text: str) -> str:
    return "0x" + acct.sign_message(encode_defunct(text=text)).signature.hex().removeprefix("0x")


operator = test_key("operator")
initiator = test_key("initiator")
personal = test_key("participant-personal")
work = test_key("participant-work")
other = test_key("participant-other")

DOMAIN = "btcyield.online"
URI = "https://btcyield.online/create-confederation.html"
CHAIN_ID = 1
T0 = 1795000000  # fixed moment for reproducible vectors

canon_cases = [
    {"name": "nested-order", "value": {"b": [3, {"z": 1, "a": None}], "a": True, "c": False}},
    {"name": "unicode-and-escapes", "value": {"text": "Проверка \"q\" \\ /\n\t\u0001 ✓"}},
    {"name": "utf16-key-order", "value": {"": 1, "\U0001F600": 2, "a": 3}},
    {"name": "integers", "value": {"max": 9007199254740991, "min": -9007199254740991, "zero": 0}},
    {"name": "empty", "value": {"o": {}, "l": []}},
]
for case in canon_cases:
    raw = canon.jcs(case["value"])
    case["jcs"] = raw.decode("utf-8")
    case["keccak"] = canon.keccak_hex(raw)

request_body = {
    "initiator_address": initiator.address,
    "initiator_name": "Test Initiator",
    "email": "initiator@example.org",
    "organization": "Example Org",
    "participants": [
        {"address": initiator.address, "label": "Initiator"},
        {"address": personal.address, "label": "Участник", "contact": "@participant"},
    ],
}
request_msg = messages.request_message(DOMAIN, CHAIN_ID, T0, request_body)

code = keccak(text="satoshifi test code").hex()
deployment = "s13"
exp = T0 + 86400
wsig_msg = messages.wsig_message(deployment, CHAIN_ID, work.address, code, exp)
wsig_sig = sign(work, wsig_msg)
binding_msg = messages.siwe_message(
    DOMAIN, personal.address, messages.binding_statement(deployment, work.address), URI,
    CHAIN_ID, code, T0, exp, [messages.wsig_resource(wsig_sig)])

login_nonce = keccak(text="satoshifi test login nonce").hex()[:32]
login_msg = messages.siwe_message(
    DOMAIN, initiator.address, messages.login_statement(), URI, CHAIN_ID, login_nonce, T0, T0 + 300)

report_body = json.dumps({
    "deployment": deployment, "version": "1.0.0", "state": "RUNNING", "stage": "zk",
    "verdict": "OK", "last_error": None, "proof_height": 925000,
    "eth_balance_wei": "120000000000000000", "ts": T0}, separators=(",", ":"))
api_nonce = "0x" + keccak(text="satoshifi test api nonce").hex()[:32]
api_post = messages.api_string("POST", "/v1/reports", T0, api_nonce, report_body.encode())
api_get = messages.api_string("GET", "/v1/operator/status?deployment=s13", T0, api_nonce, b"")

vectors = {
    "note": "TEST-ONLY keys derived from public strings; never fund these addresses.",
    "params": {"domain": DOMAIN, "uri": URI, "chain_id": CHAIN_ID, "t0": T0},
    "accounts": {name: acct.address for name, acct in
                 [("operator", operator), ("initiator", initiator), ("personal", personal),
                  ("work", work), ("other", other)]},
    "canon": canon_cases,
    "request": {"body": request_body, "issued_at": T0, "hash": messages.request_hash(request_body),
                "message": request_msg, "signer": initiator.address,
                "signature": sign(initiator, request_msg)},
    "wsig": {"deployment": deployment, "work": work.address, "code": code, "exp": exp,
             "message": wsig_msg, "signer": work.address, "signature": wsig_sig},
    "binding_siwe": {"message": binding_msg, "signer": personal.address,
                     "signature": sign(personal, binding_msg),
                     "resource": messages.wsig_resource(wsig_sig)},
    "login_siwe": {"nonce": login_nonce, "message": login_msg, "signer": initiator.address,
                   "signature": sign(initiator, login_msg)},
    "api": [
        {"method": "POST", "path": "/v1/reports", "timestamp": T0, "nonce": api_nonce,
         "body": report_body, "message": api_post, "signer": work.address,
         "signature": sign(work, api_post)},
        {"method": "GET", "path": "/v1/operator/status?deployment=s13", "timestamp": T0,
         "nonce": api_nonce, "body": "", "message": api_get, "signer": operator.address,
         "signature": sign(operator, api_get)},
    ],
}

OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("wrote", OUT)
