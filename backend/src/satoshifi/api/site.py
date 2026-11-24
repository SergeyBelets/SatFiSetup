# SPDX-License-Identifier: Apache-2.0
"""Page-facing routes of btcyield.online/confederation.html and the operator
counterparts: SIWE session, context, ceremony dashboard, confederation requests,
work-address bindings and the allowlist (wrapper spec 4, 5.2, 5.3, 6)."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from botocore.exceptions import ClientError

from satoshifi.api.monitoring import (deployment_id, load_deployment, parse_json, send_chat, text)
from satoshifi.common import auth, messages, notify, settings, sigs, store
from satoshifi.common.http import ApiError, ok
from satoshifi.common.messages import FormatError, require_checksum

log = logging.getLogger()

SESSION_NONCE_TTL_S = 300
REQUEST_KEYS = {"body", "issued_at", "signature"}
REQUEST_BODY_KEYS = {"initiator_address", "name", "email", "telegram", "organization", "participants"}
PARTICIPANT_KEYS = {"address", "label", "contact"}
BINDING_KEYS = {"deployment", "work_address", "code", "exp", "wsig", "siwe_message", "siwe_signature"}
ALLOWLIST_KEYS = {"deployment", "entries"}
REQUEST_PATCH_KEYS = {"status", "deployment"}
REQUEST_STATUSES = ("NEW", "IN_WORK", "CLOSED")
EVENTS_SHOWN = 20
MAX_REQUEST_BODY = 64 * 1024


def short(address: str) -> str:
    return address[:6] + "…" + address[-4:]


def json_body(event, keys: set, limit: int = auth.MAX_BODY) -> dict:
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    if len(raw) > limit:
        raise ApiError("PAYLOAD_TOO_LARGE")
    return parse_json(raw, keys)


# --- session tokens (5.3: address, expiry, HMAC-SHA256 on session_secret) --------

def make_token(address: str, exp: int) -> str:
    payload = ("%s.%d" % (address, exp)).encode("ascii")
    mac = hmac.new(settings.session_secret(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=") + "." + mac


def session_address(event) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    value = headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise ApiError("FORBIDDEN", "session token required")
    try:
        encoded, mac = value[len("Bearer "):].split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        address, exp = payload.decode("ascii").rsplit(".", 1)
        exp = int(exp)
    except (ValueError, UnicodeDecodeError):
        raise ApiError("FORBIDDEN", "malformed session token")
    expected = hmac.new(settings.session_secret(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise ApiError("FORBIDDEN", "bad session token")
    if exp <= int(time.time()):
        raise ApiError("FORBIDDEN", "session expired, sign in again")
    return address


def is_operator(address: str) -> bool:
    return address == settings.runtime()["operator_address"]


# --- POST /v1/session/nonce, POST /v1/session --------------------------------------

def post_session_nonce(event):
    nonce = secrets.token_hex(16)
    now = int(time.time())
    store.nonces().put_item(Item={"scope": "session", "nonce": nonce, "issued_at": now,
                                  "ttl": now + SESSION_NONCE_TTL_S})
    return ok(nonce=nonce, expires_in=SESSION_NONCE_TTL_S)


def check_siwe_common(fields: dict, now: int) -> None:
    if fields["domain"] != settings.site_domain():
        raise ApiError("BAD_REQUEST", "SIWE domain must be %s" % settings.site_domain())
    if not (fields["uri"] == settings.site_origin() or fields["uri"].startswith(settings.site_origin() + "/")):
        raise ApiError("BAD_REQUEST", "SIWE uri must be on %s" % settings.site_origin())
    if fields["chain_id"] != settings.runtime()["chain_id"]:
        raise ApiError("BAD_REQUEST", "SIWE chain id must be %d" % settings.runtime()["chain_id"])
    skew = settings.runtime()["clock_skew_s"]
    if fields["issued_at"] > now + skew:
        raise ApiError("STALE_TIMESTAMP", "SIWE issued in the future")
    if fields["expiration"] <= now:
        raise ApiError("STALE_TIMESTAMP", "SIWE message expired")


def post_session(event):
    data = json_body(event, {"message", "signature"})
    try:
        fields = messages.siwe_parse(data["message"])
        signer = sigs.recover(data["message"], data["signature"])
    except (FormatError, sigs.SignatureError) as exc:
        raise ApiError("BAD_SIGNATURE", str(exc))
    now = int(time.time())
    check_siwe_common(fields, now)
    if fields["statement"] != messages.login_statement() or fields["resources"]:
        raise ApiError("BAD_REQUEST", "not a sign-in message")
    if fields["expiration"] > now + SESSION_NONCE_TTL_S + settings.runtime()["clock_skew_s"]:
        raise ApiError("BAD_REQUEST", "SIWE expiration must be within 5 minutes")
    if signer != fields["address"]:
        raise ApiError("BAD_SIGNATURE", "signer does not match the SIWE address")
    try:
        store.nonces().delete_item(Key={"scope": "session", "nonce": fields["nonce"]},
                                   ConditionExpression="attribute_exists(nonce) AND #t > :now",
                                   ExpressionAttributeNames={"#t": "ttl"},
                                   ExpressionAttributeValues={":now": now})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError("REPLAY", "sign-in nonce is unknown, used or expired")
        raise
    exp = now + settings.runtime()["session_ttl_s"]
    return ok(token=make_token(signer, exp), address=signer, expires_at=exp,
              role="operator" if is_operator(signer) else "participant")


# --- context ---------------------------------------------------------------------

def snapshot(item: dict) -> dict:
    raw = item.get("snapshot")
    return json.loads(raw) if raw else {}


def phase_of(item: dict) -> str:
    """CONFEDERATION once the operator's snapshot carries the verified manifest."""
    return "CONFEDERATION" if snapshot(item).get("manifest") else "CEREMONY"


def allowlist_rows(dep: str) -> list:
    return store.query_all(store.allowlist(), KeyConditionExpression="deployment_id = :d",
                           ExpressionAttributeValues={":d": dep})


def binding_rows(dep: str) -> list:
    rows = store.query_all(store.bindings(), KeyConditionExpression="deployment_id = :d",
                           ExpressionAttributeValues={":d": dep})
    return [r for r in rows if not r["personal_address"].startswith("work#")]


def member_deployments(address: str) -> list:
    """ACTIVE deployments where address is the operator, an allowlisted personal address
    or a bound work address."""
    if is_operator(address):
        return [i for i in store.scan_all(store.deployments()) if i.get("status") == "ACTIVE"]
    ids = {r["deployment_id"] for r in store.query_all(
        store.allowlist(), IndexName="by_personal", KeyConditionExpression="personal_address = :a",
        ExpressionAttributeValues={":a": address})}
    ids |= {r["deployment_id"] for r in store.query_all(
        store.bindings(), IndexName="by_work", KeyConditionExpression="work_address = :a",
        ExpressionAttributeValues={":a": address})}
    items = [store.deployments().get_item(Key={"deployment_id": d}).get("Item") for d in sorted(ids)]
    return [i for i in items if i and i.get("status") == "ACTIVE"]


def get_me(event):
    address = session_address(event)
    deps = member_deployments(address)
    mine = store.query_all(store.requests(), IndexName="by_initiator",
                           KeyConditionExpression="initiator_address = :a",
                           ExpressionAttributeValues={":a": address})
    return ok(address=address, role="operator" if is_operator(address) else "participant",
              ceremonies=[i["deployment_id"] for i in deps if phase_of(i) == "CEREMONY"],
              confederations=[i["deployment_id"] for i in deps if phase_of(i) == "CONFEDERATION"],
              requests=[{"request_id": r["request_id"], "created_at": int(r["created_at"]),
                         "participants": len(json.loads(r["body"])["participants"]),
                         "status": r["status"]} for r in mine if r["status"] != "CLOSED"])


def next_step(row, report, needs_human: bool) -> str:
    if row is None:
        return "Install the SatoshiFi client and bind your work address."
    if report is None:
        return "Start the SatoshiFi client: it has not reported yet."
    if needs_human:
        return "Your client needs a decision: run satoshifi-check in the terminal."
    if int(report["report"].get("eth_balance_wei") or "0") < 10 ** 17:
        return "Top up your work address: keep at least 0.1 ETH."
    return "Nothing to do: your client works on its own."


def get_dashboard(event):
    address = session_address(event)
    dep = deployment_id((event.get("queryStringParameters") or {}).get("deployment"))
    item = load_deployment(dep)
    if item.get("status") != "ACTIVE" or dep not in {i["deployment_id"] for i in member_deployments(address)}:
        raise ApiError("FORBIDDEN", "not a participant of this deployment")
    allow = {r["personal_address"]: r for r in allowlist_rows(dep)}
    bound = {r["personal_address"]: r for r in binding_rows(dep)}
    reports = {r["work_address"]: r for r in store.query_all(
        store.reports(), KeyConditionExpression="deployment_id = :d", ExpressionAttributeValues={":d": dep})}
    now = int(time.time())
    silence = settings.runtime()["silence_s"]
    members, mine = [], None
    for personal, row in sorted(allow.items(), key=lambda kv: int(kv[1]["order"])):
        binding = bound.get(personal)
        work = binding["work_address"] if binding else None
        report = reports.get(work) if work else None
        age = now - int(report["received_at"]) if report else None
        entry = {"label": row["label"], "personal": short(personal), "work": short(work) if work else None,
                 "bound": binding is not None,
                 "verdict": report["verdict"] if report else None,
                 "stage": report["report"].get("stage") if report else None,
                 "report_age_s": age, "silent": age is not None and age > silence,
                 "me": address in (personal, work)}
        members.append(entry)
        if entry["me"]:
            mine = (binding, report)
    events = store.query_all(store.events(), KeyConditionExpression="deployment_id = :d",
                             ExpressionAttributeValues={":d": dep})
    events.sort(key=lambda e: int(e["last_sent"]), reverse=True)
    step = None
    if mine is not None:
        needs = mine[1] is not None and mine[1]["verdict"] == "NEEDS_HUMAN"
        step = next_step(mine[0], mine[1], needs)
    snap = snapshot(item)
    return ok(deployment=dep, phase=phase_of(item), snapshot=snap,
              snapshot_ts=int(item["snapshot_ts"]) if item.get("snapshot_ts") is not None else None,
              members=members, my_step=step,
              events=[{"text": e["text"], "verdict": e["verdict"], "at": int(e["last_sent"])}
                      for e in events[:EVENTS_SHOWN]])


# --- POST /v1/requests (P-05…P-08) -----------------------------------------------

def validate_request_body(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) != REQUEST_BODY_KEYS:
        raise ApiError("BAD_REQUEST", "request body fields: %s" % sorted(REQUEST_BODY_KEYS))
    try:
        require_checksum(body["initiator_address"], "initiator_address")
    except FormatError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    name = text(body, "name", 120)
    email = text(body, "email", 200)
    if not name or "@" not in email or " " in email:
        raise ApiError("BAD_REQUEST", "name and a valid e-mail are required")
    text(body, "telegram", 64, optional=True)
    text(body, "organization", 120, optional=True)
    parts = body["participants"]
    limit = settings.runtime()["max_participants"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= limit:
        raise ApiError("BAD_REQUEST", "participants: 1…%d entries" % limit)
    seen = {settings.runtime()["operator_address"]}
    for entry in parts:
        if not isinstance(entry, dict) or set(entry) != PARTICIPANT_KEYS:
            raise ApiError("BAD_REQUEST", "participant fields: %s" % sorted(PARTICIPANT_KEYS))
        try:
            require_checksum(entry["address"], "participant address")
        except FormatError as exc:
            raise ApiError("BAD_REQUEST", str(exc))
        if entry["address"] in seen:
            raise ApiError("BAD_REQUEST", "duplicate participant or the operator address: %s" % entry["address"])
        seen.add(entry["address"])
        if not text(entry, "label", 64):
            raise ApiError("BAD_REQUEST", "participant label is required")
        text(entry, "contact", 200, optional=True)
    return body


def post_request(event):
    session = session_address(event)
    data = json_body(event, REQUEST_KEYS, MAX_REQUEST_BODY)
    body = validate_request_body(data["body"])
    issued = data["issued_at"]
    now = int(time.time())
    if not isinstance(issued, int) or isinstance(issued, bool) or abs(now - issued) > settings.runtime()["clock_skew_s"]:
        raise ApiError("STALE_TIMESTAMP")
    if body["initiator_address"] != session:
        raise ApiError("FORBIDDEN", "initiator must be the signed-in address")
    try:
        message = messages.request_message(settings.site_domain(), settings.runtime()["chain_id"], issued, body)
        sigs.verify(message, data["signature"], session)
    except (FormatError, sigs.SignatureError) as exc:
        raise ApiError("BAD_SIGNATURE", str(exc))
    rhash = messages.request_hash(body)
    request_id = "R-" + rhash[2:14]
    try:
        store.requests().put_item(Item={
            "request_id": request_id, "initiator_address": session, "hash": rhash,
            "body": json.dumps(body, ensure_ascii=False), "signature": data["signature"],
            "issued_at": issued, "created_at": now, "status": "NEW", "deployment_id": None},
            ConditionExpression="attribute_not_exists(request_id)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return ok(request_id=request_id, duplicate=True)
        raise
    summary = "Новая заявка %s: %s, %d участн., %s" % (request_id, body["name"], len(body["participants"]), body["email"])
    send_chat(settings.operator_chat_id(), summary)
    lines = [summary, "Telegram: %s" % (body["telegram"] or "-"), "Организация: %s" % (body["organization"] or "-"),
             "Инициатор: %s" % session, ""]
    lines += ["%s · %s · %s" % (p["address"], p["label"], p["contact"] or "-") for p in body["participants"]]
    try:
        notify.operator_email("SatoshiFi заявка %s" % request_id, "\n".join(lines))
    except Exception as exc:
        log.error("SES delivery failed: %s", type(exc).__name__)
    return ok(request_id=request_id, duplicate=False)


# --- POST /v1/bindings, GET /v1/bindings/{code} (4.6, DB-01) -----------------------

def post_binding(event):
    data = json_body(event, BINDING_KEYS)
    dep = deployment_id(data["deployment"])
    chain = settings.runtime()["chain_id"]
    now = int(time.time())
    try:
        work = require_checksum(data["work_address"], "work_address")
        code = messages.require_match(messages.CODE_RE, data["code"], "code")
        exp = messages.require_unix(data["exp"], "exp")
        wsig_text = messages.wsig_message(dep, chain, work, code, exp)
        sigs.verify(wsig_text, data["wsig"], work)
        fields = messages.siwe_parse(data["siwe_message"])
        personal = sigs.recover(data["siwe_message"], data["siwe_signature"])
    except (FormatError, sigs.SignatureError) as exc:
        raise ApiError("BAD_SIGNATURE", str(exc))
    if exp <= now:
        raise ApiError("CODE_EXPIRED")
    check_siwe_common(fields, now)
    if (personal != fields["address"] or fields["statement"] != messages.binding_statement(dep, work)
            or fields["nonce"] != code or fields["expiration"] != exp
            or fields["resources"] != [messages.wsig_resource(data["wsig"])]):
        raise ApiError("BAD_SIGNATURE", "binding SIWE does not match code, work address or wsig")
    load_deployment(dep)
    allowed = store.allowlist().get_item(Key={"deployment_id": dep, "personal_address": personal}).get("Item")
    if not allowed:
        reject(code, dep, work, exp, "NOT_ALLOWLISTED")
        raise ApiError("NOT_ALLOWLISTED")
    try:
        # The resource-attached client serializes plain Python values itself.
        store.bindings().meta.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": settings.env("T_CODES"),
                     "Item": {"code": code, "deployment_id": dep, "work_address": work, "exp": exp,
                              "status": "ACCEPTED", "reason": "", "ttl": exp + 7 * 86400},
                     "ConditionExpression": "attribute_not_exists(code)"}},
            {"Put": {"TableName": settings.env("T_BINDINGS"),
                     "Item": {"deployment_id": dep, "personal_address": personal, "work_address": work,
                              "siwe_message": data["siwe_message"], "siwe_signature": data["siwe_signature"],
                              "wsig": data["wsig"], "wsig_message": wsig_text, "code": code, "bound_at": now},
                     "ConditionExpression": "attribute_not_exists(personal_address)"}},
            {"Put": {"TableName": settings.env("T_BINDINGS"),
                     "Item": {"deployment_id": dep, "personal_address": "work#" + work, "owner": personal},
                     "ConditionExpression": "attribute_not_exists(personal_address)"}},
        ])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
        if reasons and reasons[0] == "ConditionalCheckFailed":
            raise ApiError("CODE_USED")
        raise ApiError("ALREADY_BOUND")
    label = allowed["label"]
    store.deployments().update_item(Key={"deployment_id": dep}, UpdateExpression="SET members.#w = :l",
                                    ExpressionAttributeNames={"#w": work}, ExpressionAttributeValues={":l": label})
    send_chat(settings.operator_chat_id(), "Привязка принята · %s · %s → %s" % (dep, label, short(work)))
    return ok(accepted=True, deployment=dep, label=label)


def reject(code: str, dep: str, work: str, exp: int, reason: str) -> None:
    try:
        store.codes().put_item(Item={"code": code, "deployment_id": dep, "work_address": work, "exp": exp,
                                     "status": "REJECTED", "reason": reason, "ttl": exp + 7 * 86400},
                               ConditionExpression="attribute_not_exists(code)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def get_binding(event):
    try:
        code = messages.require_match(messages.CODE_RE, (event.get("pathParameters") or {}).get("code"), "code")
    except FormatError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    item = store.codes().get_item(Key={"code": code}).get("Item")
    if not item:
        return ok(binding="PENDING")
    return ok(binding=item["status"], reason=item.get("reason") or None, deployment=item["deployment_id"])


# --- operator: allowlist, bindings, requests ---------------------------------------

def put_allowlist(event):
    signer, body = auth.verify_signed(event)
    auth.require_operator(signer)
    data = parse_json(body, ALLOWLIST_KEYS)
    dep = deployment_id(data["deployment"])
    load_deployment(dep)
    entries = data["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= settings.runtime()["max_participants"] + 1:
        raise ApiError("BAD_REQUEST", "entries: 1…%d" % (settings.runtime()["max_participants"] + 1))
    rows, seen = [], set()
    for order, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"personal_address", "label"}:
            raise ApiError("BAD_REQUEST", "entries need personal_address and label")
        try:
            personal = require_checksum(entry["personal_address"], "personal_address")
        except FormatError as exc:
            raise ApiError("BAD_REQUEST", str(exc))
        if personal in seen or not text(entry, "label", 64):
            raise ApiError("BAD_REQUEST", "addresses must be unique and labels non-empty")
        seen.add(personal)
        rows.append({"deployment_id": dep, "personal_address": personal, "label": entry["label"],
                     "order": order, "loaded_at": int(time.time()), "loaded_by": signer})
    old = {r["personal_address"] for r in allowlist_rows(dep)}
    with store.allowlist().batch_writer() as batch:
        for personal in old - seen:
            batch.delete_item(Key={"deployment_id": dep, "personal_address": personal})
        for row in rows:
            batch.put_item(Item=row)
    return ok(deployment=dep, entries=len(rows), removed=sorted(old - seen))


def get_operator_bindings(event):
    signer, _ = auth.verify_signed(event)
    auth.require_operator(signer)
    dep = deployment_id((event.get("queryStringParameters") or {}).get("deployment"))
    allowed = {r["personal_address"] for r in allowlist_rows(dep)}
    rows = []
    for r in binding_rows(dep):
        row = {k: store.plain(v) for k, v in r.items()}
        row["allowlisted"] = r["personal_address"] in allowed
        rows.append(row)
    return ok(deployment=dep, bindings=rows)


def get_operator_requests(event):
    signer, _ = auth.verify_signed(event)
    auth.require_operator(signer)
    rows = store.scan_all(store.requests())
    rows.sort(key=lambda r: int(r["created_at"]), reverse=True)
    return ok(requests=[{**store.plain(r), "body": json.loads(r["body"])} for r in rows])


def patch_operator_request(event):
    signer, body = auth.verify_signed(event)
    auth.require_operator(signer)
    request_id = (event.get("pathParameters") or {}).get("id") or ""
    data = parse_json(body, REQUEST_PATCH_KEYS)
    if data["status"] not in REQUEST_STATUSES:
        raise ApiError("BAD_REQUEST", "status must be one of %s" % (REQUEST_STATUSES,))
    dep = deployment_id(data["deployment"]) if data["deployment"] is not None else None
    try:
        store.requests().update_item(Key={"request_id": request_id}, UpdateExpression="SET #s=:s, deployment_id=:d",
                                     ConditionExpression="attribute_exists(request_id)",
                                     ExpressionAttributeNames={"#s": "status"},
                                     ExpressionAttributeValues={":s": data["status"], ":d": dep})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError("BAD_REQUEST", "unknown request id")
        raise
    return ok(request_id=request_id, status=data["status"])


ROUTES = {
    "POST /v1/session/nonce": post_session_nonce,
    "POST /v1/session": post_session,
    "GET /v1/me": get_me,
    "GET /v1/dashboard": get_dashboard,
    "POST /v1/requests": post_request,
    "POST /v1/bindings": post_binding,
    "GET /v1/bindings/{code}": get_binding,
    "PUT /v1/operator/allowlist": put_allowlist,
    "GET /v1/operator/bindings": get_operator_bindings,
    "GET /v1/operator/requests": get_operator_requests,
    "PATCH /v1/operator/requests/{id}": patch_operator_request,
}
