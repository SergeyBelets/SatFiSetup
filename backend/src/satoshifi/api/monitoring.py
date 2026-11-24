# SPDX-License-Identifier: Apache-2.0
"""Monitoring routes: deployments, participant reports and events, operator
events and state, Telegram /status (spec 5.2, T-01…T-09, M-01, M-02)."""

import json
import logging
import re
import time

from botocore.exceptions import ClientError

from satoshifi.common import auth, notify, settings, store
from satoshifi.common.http import ApiError, ok
from satoshifi.common.messages import DEPLOYMENT_RE, FormatError, require_checksum

log = logging.getLogger()

REPORT_KEYS = {"deployment", "version", "state", "stage", "verdict", "last_error",
               "proof_height", "eth_balance_wei", "ts"}
EVENT_KEYS = {"deployment", "kind", "verdict", "reason", "action", "dedup_key", "ts"}
STATE_KEYS = {"deployment", "snapshot", "ts"}
DEPLOYMENT_KEYS = {"status", "participants_chat_id", "members"}
WORD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_MEMBERS = 101


# --- validation helpers --------------------------------------------------------

def parse_json(body: bytes, keys: set) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("BAD_REQUEST", "body must be JSON")
    if not isinstance(data, dict):
        raise ApiError("BAD_REQUEST", "body must be a JSON object")
    if set(data) != keys:
        raise ApiError("BAD_REQUEST", "fields: missing %s, unknown %s"
                       % (sorted(keys - set(data)), sorted(set(data) - keys)))
    return data


def text(data: dict, key: str, limit: int, optional: bool = False):
    value = data[key]
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit or "\n" in value and limit <= 64:
        raise ApiError("BAD_REQUEST", "%s must be a string up to %d characters" % (key, limit))
    return value


def word(data: dict, key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not WORD_RE.match(value):
        raise ApiError("BAD_REQUEST", "%s must match %s" % (key, WORD_RE.pattern))
    return value


def deployment_id(value) -> str:
    if not isinstance(value, str) or not DEPLOYMENT_RE.match(value):
        raise ApiError("BAD_REQUEST", "bad deployment id")
    return value


def fresh_ts(data: dict) -> int:
    ts = data["ts"]
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise ApiError("BAD_REQUEST", "ts must be an integer")
    if abs(int(time.time()) - ts) > settings.runtime()["clock_skew_s"]:
        raise ApiError("STALE_TIMESTAMP")
    return ts


def load_deployment(dep: str) -> dict:
    item = store.deployments().get_item(Key={"deployment_id": dep}).get("Item")
    if not item:
        raise ApiError("UNKNOWN_DEPLOYMENT")
    return item


def member_label(item: dict, work: str) -> str:
    members = item.get("members") or {}
    if work not in members:
        raise ApiError("FORBIDDEN", "address is not a member of this deployment")
    return members[work]


def participants_chat(item: dict) -> int:
    chat = item.get("participants_chat_id")
    return int(chat) if chat is not None else settings.operator_chat_id()


def send_chat(chat_id: int, message: str) -> str:
    try:
        notify.telegram(chat_id, message)
        return "SENT"
    except notify.NotifyError as exc:
        log.error("telegram delivery failed: %s", exc)
        return "FAILED"


# --- operator: deployments -----------------------------------------------------

def put_deployment(event):
    signer, body = auth.verify_signed(event)
    auth.require_operator(signer)
    dep = deployment_id((event.get("pathParameters") or {}).get("id"))
    data = parse_json(body, DEPLOYMENT_KEYS)
    if data["status"] not in ("ACTIVE", "CLOSED"):
        raise ApiError("BAD_REQUEST", "status must be ACTIVE or CLOSED")
    chat = data["participants_chat_id"]
    if chat is not None and (not isinstance(chat, int) or isinstance(chat, bool)):
        raise ApiError("BAD_REQUEST", "participants_chat_id must be an integer or null")
    members = data["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ApiError("BAD_REQUEST", "members must list 1…%d entries" % MAX_MEMBERS)
    mapping = {}
    for entry in members:
        if not isinstance(entry, dict) or set(entry) != {"work_address", "label"}:
            raise ApiError("BAD_REQUEST", "member entries need work_address and label")
        try:
            work = require_checksum(entry["work_address"], "work_address")
        except FormatError as exc:
            raise ApiError("BAD_REQUEST", str(exc))
        label = text(entry, "label", 64)
        if not label or work in mapping:
            raise ApiError("BAD_REQUEST", "labels must be non-empty and addresses unique")
        mapping[work] = label
    now = int(time.time())
    store.deployments().update_item(
        Key={"deployment_id": dep},
        UpdateExpression="SET #s=:s, participants_chat_id=:c, members=:m, updated_at=:t, updated_by=:b",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": data["status"], ":c": chat, ":m": mapping, ":t": now, ":b": signer})
    return ok(deployment=dep, members=len(mapping))


# --- participants: reports and events ------------------------------------------

def post_report(event):
    signer, body = auth.verify_signed(event)
    data = parse_json(body, REPORT_KEYS)
    dep = deployment_id(data["deployment"])
    ts = fresh_ts(data)
    report = {
        "version": text(data, "version", 32),
        "state": word(data, "state"),
        "stage": text(data, "stage", 64),
        "verdict": word(data, "verdict"),
        "last_error": text(data, "last_error", 500, optional=True),
        "proof_height": data["proof_height"],
        "eth_balance_wei": data["eth_balance_wei"],
        "ts": ts,
    }
    if report["proof_height"] is not None and (not isinstance(report["proof_height"], int)
                                               or isinstance(report["proof_height"], bool)):
        raise ApiError("BAD_REQUEST", "proof_height must be an integer or null")
    if not isinstance(report["eth_balance_wei"], str) or not report["eth_balance_wei"].isdigit():
        raise ApiError("BAD_REQUEST", "eth_balance_wei must be a decimal string")
    item = load_deployment(dep)
    label = member_label(item, signer)
    now = int(time.time())
    previous = store.reports().get_item(Key={"deployment_id": dep, "work_address": signer}).get("Item") or {}
    store.reports().put_item(Item={
        "deployment_id": dep, "work_address": signer, "label": label, "report": report,
        "verdict": report["verdict"], "received_at": now, "silence_alerted": False})
    changed = previous.get("verdict") != report["verdict"]
    recovered = bool(previous.get("silence_alerted"))
    if changed or recovered:
        reason = report["last_error"] or report["stage"]
        prefix = "связь восстановлена · " if recovered else ""
        send_chat(participants_chat(item), prefix + notify.line(label, report["verdict"], reason, ""))
    return ok(accepted=True)


def deliver_event(dep: str, item: dict, source_label: str, data: dict, chat_id: int, email: bool):
    key = word(data, "dedup_key")
    verdict = word(data, "verdict")
    message = notify.line(source_label, verdict, text(data, "reason", 300), text(data, "action", 300))
    now = int(time.time())
    repeat = settings.runtime()["repeat_s"]
    try:
        store.events().put_item(
            Item={"deployment_id": dep, "dedup_key": key, "text": message, "kind": word(data, "kind"),
                  "verdict": verdict, "last_sent": now, "suppressed": 0, "delivery": "PENDING",
                  "ttl": now + 30 * 86400},
            ConditionExpression="attribute_not_exists(dedup_key) OR last_sent < :edge",
            ExpressionAttributeValues={":edge": now - repeat})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        store.events().update_item(
            Key={"deployment_id": dep, "dedup_key": key},
            UpdateExpression="ADD suppressed :one", ExpressionAttributeValues={":one": 1})
        return ok(delivered=False, suppressed=True)
    status = send_chat(chat_id, message)
    if email:
        try:
            notify.operator_email("SatoshiFi %s: %s" % (dep, verdict), message)
        except Exception as exc:
            log.error("SES delivery failed: %s", type(exc).__name__)
            status += "+EMAIL_FAILED"
    store.events().update_item(
        Key={"deployment_id": dep, "dedup_key": key},
        UpdateExpression="SET delivery=:d", ExpressionAttributeValues={":d": status})
    return ok(delivered=status == "SENT", delivery=status)


def post_event(event):
    signer, body = auth.verify_signed(event)
    data = parse_json(body, EVENT_KEYS)
    dep = deployment_id(data["deployment"])
    fresh_ts(data)
    item = load_deployment(dep)
    label = member_label(item, signer)
    return deliver_event(dep, item, label, data, participants_chat(item), email=False)


# --- operator: events, state, status --------------------------------------------

def post_operator_event(event):
    signer, body = auth.verify_signed(event)
    auth.require_operator(signer)
    data = parse_json(body, EVENT_KEYS)
    dep = deployment_id(data["deployment"])
    fresh_ts(data)
    item = load_deployment(dep)
    needs_human = data["verdict"] == "NEEDS_HUMAN"
    return deliver_event(dep, item, "оператор " + dep, data, settings.operator_chat_id(), email=needs_human)


def put_operator_state(event):
    signer, body = auth.verify_signed(event)
    auth.require_operator(signer)
    data = parse_json(body, STATE_KEYS)
    dep = deployment_id(data["deployment"])
    ts = fresh_ts(data)
    if not isinstance(data["snapshot"], dict):
        raise ApiError("BAD_REQUEST", "snapshot must be an object")
    load_deployment(dep)
    store.deployments().update_item(
        Key={"deployment_id": dep},
        UpdateExpression="SET #snap=:s, snapshot_ts=:t, snapshot_received=:r",
        ExpressionAttributeNames={"#snap": "snapshot"},
        ExpressionAttributeValues={":s": json.dumps(data["snapshot"], ensure_ascii=False), ":t": ts,
                                   ":r": int(time.time())})
    return ok(accepted=True)


def get_operator_status(event):
    signer, _ = auth.verify_signed(event)
    auth.require_operator(signer)
    dep = deployment_id((event.get("queryStringParameters") or {}).get("deployment"))
    item = load_deployment(dep)
    rows = store.query_all(store.reports(), KeyConditionExpression="deployment_id = :d",
                           ExpressionAttributeValues={":d": dep})
    return ok(deployment=dep, status=item["status"], members=store.plain(item.get("members") or {}),
              reports=[store.plain(r) for r in rows])


# --- Telegram webhook -------------------------------------------------------------

def status_text(items) -> str:
    now = int(time.time())
    lines = []
    for item in items:
        dep = item["deployment_id"]
        lines.append("%s (%s)" % (dep, item.get("status")))
        rows = {r["work_address"]: r for r in store.query_all(
            store.reports(), KeyConditionExpression="deployment_id = :d",
            ExpressionAttributeValues={":d": dep})}
        for work, label in sorted((item.get("members") or {}).items(), key=lambda kv: kv[1]):
            row = rows.get(work)
            if not row:
                lines.append("  %s · нет отчётов" % label)
                continue
            age = now - int(row["received_at"])
            lines.append("  %s · %s · %s · %d мин назад" % (label, row["verdict"], row["report"].get("stage"), age // 60))
    return "\n".join(lines) or "Активных церемоний нет."


def post_telegram(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-telegram-bot-api-secret-token") != settings.telegram_webhook_secret():
        raise ApiError("FORBIDDEN")
    update = json.loads(auth.raw_body(event).decode("utf-8"))
    message = update.get("message") or {}
    command = (message.get("text") or "").split()
    chat = (message.get("chat") or {}).get("id")
    name = command[0].split("@")[0] if command else ""
    if chat is None or name not in ("/status", "/help"):
        return ok()
    if name == "/help":
        send_chat(chat, "/status — участники церемонии этого чата: вердикт, этап, возраст отчёта\n"
                        "/help — эта справка\nКабинет: %s/confederation.html" % settings.site_origin())
        return ok()
    active = [i for i in store.scan_all(store.deployments()) if i.get("status") == "ACTIVE"]
    if chat != settings.operator_chat_id():
        active = [i for i in active if i.get("participants_chat_id") is not None
                  and int(i["participants_chat_id"]) == chat]
    send_chat(chat, status_text(active))
    return ok()


ROUTES = {
    "PUT /v1/operator/deployments/{id}": put_deployment,
    "POST /v1/reports": post_report,
    "POST /v1/events": post_event,
    "POST /v1/operator/events": post_operator_event,
    "PUT /v1/operator/state": put_operator_state,
    "GET /v1/operator/status": get_operator_status,
    "POST /v1/tg": post_telegram,
}
