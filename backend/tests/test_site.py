# SPDX-License-Identifier: Apache-2.0
"""Page-facing routes against moto DynamoDB/SSM: sign-in, context, dashboard,
requests and bindings, positive and negative."""

import json
import secrets
import time

import boto3
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

moto = pytest.importorskip("moto")

ORIGIN = "https://btcyield.online"
OPERATOR = Account.from_key(keccak(text="site test operator"))
ALICE = Account.from_key(keccak(text="site test alice personal"))
ALICE_WORK = Account.from_key(keccak(text="site test alice work"))
BOB = Account.from_key(keccak(text="site test bob personal"))
STRANGER = Account.from_key(keccak(text="site test stranger"))
DEP = "satfi-t1"

TABLES = {
    "T_REQUESTS": ([("request_id", "S")], [("request_id", "HASH")], [("by_initiator", "initiator_address")]),
    "T_DEPLOYMENTS": ([("deployment_id", "S")], [("deployment_id", "HASH")], []),
    "T_ALLOWLIST": ([("deployment_id", "S"), ("personal_address", "S")],
                    [("deployment_id", "HASH"), ("personal_address", "RANGE")], [("by_personal", "personal_address")]),
    "T_CODES": ([("code", "S")], [("code", "HASH")], []),
    "T_BINDINGS": ([("deployment_id", "S"), ("personal_address", "S"), ("work_address", "S")],
                   [("deployment_id", "HASH"), ("personal_address", "RANGE")], [("by_work", "work_address")]),
    "T_REPORTS": ([("deployment_id", "S"), ("work_address", "S")],
                  [("deployment_id", "HASH"), ("work_address", "RANGE")], []),
    "T_EVENTS": ([("deployment_id", "S"), ("dedup_key", "S")],
                 [("deployment_id", "HASH"), ("dedup_key", "RANGE")], []),
    "T_NONCES": ([("scope", "S"), ("nonce", "S")], [("scope", "HASH"), ("nonce", "RANGE")], []),
}


@pytest.fixture
def api(monkeypatch):
    with moto.mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
        monkeypatch.setenv("SITE_ORIGIN", ORIGIN)
        monkeypatch.setenv("SSM_CONFIG", "/satoshifi/test/config")
        monkeypatch.setenv("SSM_SESSION_SECRET", "/satoshifi/test/session_secret")
        monkeypatch.setenv("SSM_TELEGRAM_PREFIX", "/satoshifi/telegram")
        monkeypatch.setenv("OPERATOR_CHAT_KEY", "test")
        ddb = boto3.client("dynamodb")
        if "T_REQUESTS" in TABLES:
            for env_name, (attrs, keys, indexes) in TABLES.items():
                name = env_name.lower()
                monkeypatch.setenv(env_name, name)
                kwargs = dict(TableName=name, BillingMode="PAY_PER_REQUEST",
                              AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs]
                              + ([{"AttributeName": i[1], "AttributeType": "S"} for i in indexes
                                  if i[1] not in [a for a, _ in attrs]]),
                              KeySchema=[{"AttributeName": a, "KeyType": k} for a, k in keys])
                if indexes:
                    kwargs["GlobalSecondaryIndexes"] = [
                        {"IndexName": n, "KeySchema": [{"AttributeName": a, "KeyType": "HASH"}],
                         "Projection": {"ProjectionType": "ALL"}} for n, a in indexes]
                ddb.create_table(**kwargs)
        ssm = boto3.client("ssm")
        ssm.put_parameter(Name="/satoshifi/test/config", Type="String", Value=json.dumps({
            "operator_address": OPERATOR.address, "chain_id": 1, "report_period_s": 300, "silence_s": 900,
            "repeat_s": 1800, "clock_skew_s": 300, "session_ttl_s": 3600, "max_participants": 100}))
        ssm.put_parameter(Name="/satoshifi/test/session_secret", Type="SecureString", Value="s" * 32)
        ssm.put_parameter(Name="/satoshifi/telegram/chats", Type="String", Value=json.dumps({"test": -1}))
        from satoshifi.common import notify, settings, store
        for f in (settings.ssm_value, settings.runtime, settings.operator_chat_id, settings._ssm, store.table):
            f.cache_clear()
        sent = []
        monkeypatch.setattr(notify, "telegram", lambda chat, text: sent.append((chat, text)))
        monkeypatch.setattr(notify, "operator_email", lambda subject, body: sent.append(("email", subject)))
        from satoshifi.api import handler
        store.deployments().put_item(Item={"deployment_id": DEP, "status": "ACTIVE", "participants_chat_id": None,
                                           "members": {OPERATOR.address: "operator"}})
        yield handler, sent


def call(handler, route, body=None, token=None, path=None, query=None, raw=None):
    method, path_template = route.split(" ", 1)
    event = {"routeKey": route, "rawPath": path_template, "headers": {},
             "requestContext": {"http": {"method": method}}, "pathParameters": path or {},
             "queryStringParameters": query or {}, "body": raw if raw is not None else
             (json.dumps(body) if body is not None else "")}
    if token:
        event["headers"]["Authorization"] = "Bearer " + token
    response = handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def sign(account, text):
    return Account.sign_message(encode_defunct(text=text), account.key).signature.hex()


def sig0x(account, text):
    s = sign(account, text)
    return s if s.startswith("0x") else "0x" + s


def login(handler, account, **overrides):
    from satoshifi.common import messages
    status, data = call(handler, "POST /v1/session/nonce")
    assert status == 200
    now = int(time.time())
    fields = dict(domain="btcyield.online", address=account.address, statement="Sign in to SatoshiFi.",
                  uri=ORIGIN + "/confederation.html", chain_id=1, nonce=data["nonce"], issued_at=now,
                  expiration=now + 240)
    fields.update(overrides)
    text = messages.siwe_message(**fields)
    return call(handler, "POST /v1/session", {"message": text, "signature": sig0x(account, text)})


def allow(*rows):
    from satoshifi.common import store
    for order, (account, label) in enumerate(rows):
        store.allowlist().put_item(Item={"deployment_id": DEP, "personal_address": account.address,
                                         "label": label, "order": order})


def binding_body(personal, work, code=None, exp=None, **siwe):
    from satoshifi.common import messages
    code = code or secrets.token_hex(32)
    exp = exp or int(time.time()) + 3600
    wsig_text = messages.wsig_message(DEP, 1, work.address, code, exp)
    wsig = sig0x(work, wsig_text)
    fields = dict(domain="btcyield.online", address=personal.address,
                  statement=messages.binding_statement(DEP, work.address), uri=ORIGIN + "/confederation.html",
                  chain_id=1, nonce=code, issued_at=int(time.time()), expiration=exp,
                  resources=[messages.wsig_resource(wsig)])
    fields.update(siwe)
    text = messages.siwe_message(**fields)
    return {"deployment": DEP, "work_address": work.address, "code": code, "exp": exp, "wsig": wsig,
            "siwe_message": text, "siwe_signature": sig0x(personal, text)}


# --- sign-in -----------------------------------------------------------------------

def test_login_and_context(api):
    handler, _ = api
    allow((ALICE, "Alice"))
    status, data = login(handler, ALICE)
    assert status == 200 and data["role"] == "participant"
    status, me = call(handler, "GET /v1/me", token=data["token"])
    assert status == 200 and me["ceremonies"] == [DEP] and me["confederations"] == []
    status, data = login(handler, STRANGER)
    status, me = call(handler, "GET /v1/me", token=data["token"])
    assert me["ceremonies"] == [] and me["confederations"] == [] and me["requests"] == []
    status, data = login(handler, OPERATOR)
    status, me = call(handler, "GET /v1/me", token=data["token"])
    assert data["role"] == "operator" and me["ceremonies"] == [DEP]


@pytest.mark.parametrize("override,code", [
    (dict(domain="evil.example"), "BAD_REQUEST"),
    (dict(chain_id=11155111), "BAD_REQUEST"),
    (dict(uri="https://evil.example/x"), "BAD_REQUEST"),
    (dict(statement="Something else."), "BAD_REQUEST"),
    (dict(expiration=int(time.time()) - 10), "STALE_TIMESTAMP"),
    (dict(expiration=int(time.time()) + 3600), "BAD_REQUEST"),
    (dict(nonce="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), "REPLAY"),
])
def test_login_rejections(api, override, code):
    handler, _ = api
    status, data = login(handler, ALICE, **override)
    assert data["code"] == code


def test_login_nonce_single_use_and_token_checks(api):
    handler, _ = api
    from satoshifi.common import messages
    status, data = call(handler, "POST /v1/session/nonce")
    now = int(time.time())
    text = messages.siwe_message("btcyield.online", ALICE.address, "Sign in to SatoshiFi.", ORIGIN, 1,
                                 data["nonce"], now, now + 200)
    body = {"message": text, "signature": sig0x(ALICE, text)}
    assert call(handler, "POST /v1/session", body)[0] == 200
    assert call(handler, "POST /v1/session", body)[1]["code"] == "REPLAY"
    forged = {"message": text, "signature": sig0x(BOB, text)}
    assert call(handler, "POST /v1/session", forged)[1]["code"] in ("BAD_SIGNATURE", "REPLAY")
    assert call(handler, "GET /v1/me")[1]["code"] == "FORBIDDEN"
    assert call(handler, "GET /v1/me", token="x.y")[1]["code"] == "FORBIDDEN"
    from satoshifi.api import site
    expired = site.make_token(ALICE.address, now - 1)
    assert call(handler, "GET /v1/me", token=expired)[1]["code"] == "FORBIDDEN"
    good = site.make_token(ALICE.address, now + 100)
    tampered = good[:-1] + ("1" if good[-1] == "0" else "0")
    assert call(handler, "GET /v1/me", token=tampered)[1]["code"] == "FORBIDDEN"


# --- bindings ------------------------------------------------------------------------

def test_binding_flow(api):
    handler, sent = api
    allow((ALICE, "Alice"), (BOB, "Bob"))
    body = binding_body(ALICE, ALICE_WORK)
    assert call(handler, "GET /v1/bindings/{code}", path={"code": body["code"]})[1]["binding"] == "PENDING"
    status, data = call(handler, "POST /v1/bindings", body)
    assert status == 200 and data["label"] == "Alice"
    assert call(handler, "GET /v1/bindings/{code}", path={"code": body["code"]})[1]["binding"] == "ACCEPTED"
    assert call(handler, "POST /v1/bindings", body)[1]["code"] == "CODE_USED"
    again = binding_body(ALICE, Account.create())
    assert call(handler, "POST /v1/bindings", again)[1]["code"] == "ALREADY_BOUND"
    stolen = binding_body(BOB, ALICE_WORK)
    assert call(handler, "POST /v1/bindings", stolen)[1]["code"] == "ALREADY_BOUND"
    from satoshifi.common import store
    assert store.deployments().get_item(Key={"deployment_id": DEP})["Item"]["members"][ALICE_WORK.address] == "Alice"
    assert any("Привязка принята" in str(m) for m in sent)
    status, data = login(handler, ALICE_WORK)
    assert call(handler, "GET /v1/me", token=data["token"])[1]["ceremonies"] == [DEP]


def test_binding_rejections(api):
    handler, _ = api
    allow((ALICE, "Alice"))
    stranger = binding_body(STRANGER, Account.create())
    assert call(handler, "POST /v1/bindings", stranger)[1]["code"] == "NOT_ALLOWLISTED"
    assert call(handler, "GET /v1/bindings/{code}", path={"code": stranger["code"]})[1]["binding"] == "REJECTED"
    expired = binding_body(ALICE, ALICE_WORK, exp=int(time.time()) - 5)
    assert call(handler, "POST /v1/bindings", expired)[1]["code"] in ("CODE_EXPIRED", "STALE_TIMESTAMP")
    body = binding_body(ALICE, ALICE_WORK)
    body["wsig"] = sig0x(BOB, "x")
    assert call(handler, "POST /v1/bindings", body)[1]["code"] == "BAD_SIGNATURE"
    wrong_nonce = binding_body(ALICE, ALICE_WORK, nonce="b" * 64)
    assert call(handler, "POST /v1/bindings", wrong_nonce)[1]["code"] == "BAD_SIGNATURE"
    wrong_statement = binding_body(ALICE, ALICE_WORK, statement="My work address for other is me.")
    assert call(handler, "POST /v1/bindings", wrong_statement)[1]["code"] == "BAD_SIGNATURE"
    extra = dict(binding_body(ALICE, ALICE_WORK), extra=1)
    assert call(handler, "POST /v1/bindings", extra)[1]["code"] == "BAD_REQUEST"


# --- dashboard ----------------------------------------------------------------------

def test_dashboard_access_and_content(api):
    handler, _ = api
    allow((ALICE, "Alice"), (BOB, "Bob"))
    call(handler, "POST /v1/bindings", binding_body(ALICE, ALICE_WORK))
    token = login(handler, ALICE)[1]["token"]
    status, data = call(handler, "GET /v1/dashboard", token=token, query={"deployment": DEP})
    assert status == 200 and data["phase"] == "CEREMONY"
    rows = {m["label"]: m for m in data["members"]}
    assert rows["Alice"]["bound"] and rows["Alice"]["me"] and not rows["Bob"]["bound"]
    assert "Start the SatoshiFi client" in data["my_step"]
    stranger = login(handler, STRANGER)[1]["token"]
    assert call(handler, "GET /v1/dashboard", token=stranger, query={"deployment": DEP})[1]["code"] == "FORBIDDEN"
    assert call(handler, "GET /v1/dashboard", token=token, query={"deployment": "nope"})[1]["code"] == "UNKNOWN_DEPLOYMENT"
    from satoshifi.common import store
    store.deployments().update_item(Key={"deployment_id": DEP}, UpdateExpression="SET #snap=:s, snapshot_ts=:t",
                                    ExpressionAttributeNames={"#snap": "snapshot"}, ExpressionAttributeValues={":s": json.dumps({"manifest": {"x": 1}}), ":t": 1})
    assert call(handler, "GET /v1/me", token=token)[1]["confederations"] == [DEP]


# --- requests ------------------------------------------------------------------------

def request_payload(account, participants=None, issued=None):
    from satoshifi.common import messages
    body = {"initiator_address": account.address, "name": "Init", "email": "init@example.com", "telegram": None,
            "organization": None, "participants": participants or [
                {"address": ALICE.address, "label": "Alice", "contact": None}]}
    issued = issued or int(time.time())
    text = messages.request_message("btcyield.online", 1, issued, body)
    return {"body": body, "issued_at": issued, "signature": sig0x(account, text)}


def test_request_flow(api):
    handler, sent = api
    token = login(handler, STRANGER)[1]["token"]
    payload = request_payload(STRANGER)
    status, data = call(handler, "POST /v1/requests", payload, token=token)
    assert status == 200 and data["request_id"].startswith("R-") and not data["duplicate"]
    assert call(handler, "POST /v1/requests", payload, token=token)[1]["duplicate"]
    me = call(handler, "GET /v1/me", token=token)[1]
    assert me["requests"][0]["request_id"] == data["request_id"]
    assert any(m[0] == "email" for m in sent) and any("Новая заявка" in str(m[1]) for m in sent)


def test_request_rejections(api):
    handler, _ = api
    token = login(handler, STRANGER)[1]["token"]
    other = request_payload(ALICE)
    assert call(handler, "POST /v1/requests", other, token=token)[1]["code"] == "FORBIDDEN"
    dup = request_payload(STRANGER, [{"address": ALICE.address, "label": "A", "contact": None}] * 2)
    assert call(handler, "POST /v1/requests", dup, token=token)[1]["code"] == "BAD_REQUEST"
    op = request_payload(STRANGER, [{"address": OPERATOR.address, "label": "op", "contact": None}])
    assert call(handler, "POST /v1/requests", op, token=token)[1]["code"] == "BAD_REQUEST"
    stale = request_payload(STRANGER, issued=int(time.time()) - 1000)
    assert call(handler, "POST /v1/requests", stale, token=token)[1]["code"] == "STALE_TIMESTAMP"
    forged = request_payload(STRANGER)
    forged["signature"] = sig0x(ALICE, "x")
    assert call(handler, "POST /v1/requests", forged, token=token)[1]["code"] == "BAD_SIGNATURE"
    assert call(handler, "POST /v1/requests", request_payload(STRANGER))[1]["code"] == "FORBIDDEN"
    big = call(handler, "POST /v1/requests", token=token, raw="x" * (65 * 1024))
    assert big[1]["code"] == "PAYLOAD_TOO_LARGE"
