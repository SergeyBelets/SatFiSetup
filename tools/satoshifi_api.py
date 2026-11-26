#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Client for the SatoshiFi monitoring API (wrapper spec 5.2-5.3).

Participants:  bind (work address -> personal wallet), report (heartbeat loop), event.
Operator:      op-deployment, op-event, op-state, op-status.

Signs requests with the key in an SDK identity file (JSON with "private_key").
Needs only eth-account, which the SDK already installs.

Config file (JSON):
  {"api_base": "https://api.btcyield.online", "deployment": "s13",
   "identity_file": "/path/identity.json",
   "rpc_url": "https://...",            # report only: public Ethereum RPC
   "min_balance_wei": "50000000000000000",  # report only
   "report_period_s": 300}              # report only
"""

import argparse
import json
import os
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

VERSION = "1.0.0"
CONFIG_KEYS = {"api_base", "deployment", "identity_file"}
REPORT_KEYS = {"rpc_url", "min_balance_wei", "report_period_s"}


def fail(message):
    print(json.dumps({"status": "ERROR", "message": message}), file=sys.stderr)
    sys.exit(2)


def load_config(path, extra=()):
    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    needed = CONFIG_KEYS | set(extra)
    missing = needed - set(config)
    if missing:
        fail("config is missing %s" % sorted(missing))
    return config


def load_account(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            fail("identity file must be a regular file readable only by its owner")
        return Account.from_key(json.load(stream)["private_key"])


def signed_call(config, account, method, path, payload=None):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    nonce = "0x" + secrets.token_hex(16)
    text = "\n".join(["SatoshiFi api v1", "%s %s" % (method, path), str(ts), nonce, "0x" + keccak(body).hex()])
    signature = account.sign_message(encode_defunct(text=text)).signature.hex()
    headers = {
        "Content-Type": "application/json",
        "X-SatoshiFi-Address": account.address,
        "X-SatoshiFi-Timestamp": str(ts),
        "X-SatoshiFi-Nonce": nonce,
        "X-SatoshiFi-Signature": signature if signature.startswith("0x") else "0x" + signature,
    }
    request = urllib.request.Request(config["api_base"].rstrip("/") + path,
                                     data=body if payload is not None else None,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}") | {"http": exc.code}


def eth_balance(rpc_url, address):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                          "params": [address, "latest"]}).encode()
    request = urllib.request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    if "result" not in result:
        raise RuntimeError("eth_getBalance failed: %s" % result.get("error", {}).get("message", "no result"))
    return int(result["result"], 16)


WSIG_HEADER = "SatoshiFi work binding v1"
BIND_KEYS = {"bind_page", "chain_id"}


def wsig_message(deployment, chain_id, work, code, exp):
    """Twin of backend messages.wsig_message and site satoshifi-formats.js."""
    return "\n".join([WSIG_HEADER, "Deployment: %s" % deployment, "Chain ID: %d" % chain_id,
                      "Work address: %s" % work, "Code: %s" % code, "Expires: %d" % exp])


def bind_link(config, account, ttl):
    code = secrets.token_hex(32)
    exp = int(time.time()) + ttl
    text = wsig_message(config["deployment"], int(config["chain_id"]), account.address, code, exp)
    wsig = account.sign_message(encode_defunct(text=text)).signature.hex()
    wsig = wsig if wsig.startswith("0x") else "0x" + wsig
    query = urllib.parse.urlencode([("bind", code), ("deployment", config["deployment"]),
                                    ("work", account.address), ("exp", exp), ("wsig", wsig)])
    return code, exp, config["bind_page"] + "?" + query


def binding_status(config, code):
    request = urllib.request.Request(config["api_base"].rstrip("/") + "/v1/bindings/" + code, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}") | {"http": exc.code}


def cmd_bind(args):
    config = load_config(args.config, BIND_KEYS)
    account = load_account(config["identity_file"])
    while True:
        code, exp, url = bind_link(config, account, args.ttl)
        print("BIND_LINK " + url, flush=True)
        if args.print_only:
            return
        while time.time() < exp:
            try:
                result = binding_status(config, code)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print("BIND_WAIT api unreachable: %s" % type(exc).__name__, file=sys.stderr, flush=True)
                time.sleep(args.poll)
                continue
            state = result.get("binding")
            if state == "ACCEPTED":
                print("BIND_ACCEPTED " + account.address, flush=True)
                return
            if state == "REJECTED":
                print("BIND_REJECTED %s" % result.get("reason"), flush=True)
                sys.exit(3)
            time.sleep(args.poll)
        print("BIND_EXPIRED new link follows", flush=True)


def event_payload(config, args):
    return {"deployment": config["deployment"], "kind": args.kind, "verdict": args.verdict,
            "reason": args.reason, "action": args.action,
            "dedup_key": args.dedup_key or "%s:%s" % (args.kind, args.verdict), "ts": int(time.time())}


def cmd_report(args):
    config = load_config(args.config, REPORT_KEYS)
    account = load_account(config["identity_file"])
    while True:
        verdict, last_error, balance = "OK", None, 0
        try:
            balance = eth_balance(config["rpc_url"], account.address)
            if balance < int(config["min_balance_wei"]):
                verdict, last_error = "LOW_BALANCE", "ETH balance below minimum"
        except Exception as exc:
            verdict, last_error = "RPC_ERROR", type(exc).__name__
        payload = {"deployment": config["deployment"], "version": VERSION, "state": "RUNNING",
                   "stage": args.stage, "verdict": verdict, "last_error": last_error,
                   "proof_height": None, "eth_balance_wei": str(balance), "ts": int(time.time())}
        try:
            print(json.dumps(signed_call(config, account, "POST", "/v1/reports", payload)), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "ERROR", "message": "report not delivered: %s" % type(exc).__name__}), flush=True)
        if args.once:
            return
        time.sleep(int(config["report_period_s"]))


def cmd_event(args):
    config = load_config(args.config)
    account = load_account(config["identity_file"])
    print(json.dumps(signed_call(config, account, "POST", "/v1/events", event_payload(config, args))))


def cmd_op_deployment(args):
    config = load_config(args.config)
    account = load_account(config["identity_file"])
    with open(args.members, encoding="utf-8") as stream:
        members = json.load(stream)
    payload = {"status": args.status, "participants_chat_id": args.chat_id, "members": members}
    print(json.dumps(signed_call(config, account, "PUT", "/v1/operator/deployments/" + config["deployment"], payload)))


def cmd_op_event(args):
    config = load_config(args.config)
    account = load_account(config["identity_file"])
    print(json.dumps(signed_call(config, account, "POST", "/v1/operator/events", event_payload(config, args))))


def cmd_op_state(args):
    config = load_config(args.config)
    account = load_account(config["identity_file"])
    with open(args.snapshot, encoding="utf-8") as stream:
        snapshot = json.load(stream)
    payload = {"deployment": config["deployment"], "snapshot": snapshot, "ts": int(time.time())}
    print(json.dumps(signed_call(config, account, "PUT", "/v1/operator/state", payload)))


def cmd_op_status(args):
    config = load_config(args.config)
    account = load_account(config["identity_file"])
    print(json.dumps(signed_call(config, account, "GET", "/v1/operator/status?deployment=" + config["deployment"]),
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="satoshifi_api")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bind", help="bind the work address to a personal wallet (participant)")
    p.add_argument("--config", required=True)
    p.add_argument("--ttl", type=int, default=3600, help="link lifetime, seconds")
    p.add_argument("--poll", type=int, default=10)
    p.add_argument("--print-only", action="store_true", help="print one link and exit")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("report", help="send heartbeat reports (participant)")
    p.add_argument("--config", required=True)
    p.add_argument("--stage", default="service")
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_report)

    for name, func, helptext in (("event", cmd_event, "send an event to the participants chat"),
                                 ("op-event", cmd_op_event, "send an operator event (NEEDS_HUMAN also emails)")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", required=True)
        p.add_argument("--kind", required=True)
        p.add_argument("--verdict", required=True)
        p.add_argument("--reason", default="")
        p.add_argument("--action", default="")
        p.add_argument("--dedup-key")
        p.set_defaults(func=func)

    p = sub.add_parser("op-deployment", help="register or update a deployment (operator)")
    p.add_argument("--config", required=True)
    p.add_argument("--status", choices=["ACTIVE", "CLOSED"], required=True)
    p.add_argument("--chat-id", type=int)
    p.add_argument("--members", required=True, help='JSON file: [{"work_address": "0x..", "label": ".."}]')
    p.set_defaults(func=cmd_op_deployment)

    p = sub.add_parser("op-state", help="send a ceremony state snapshot (operator)")
    p.add_argument("--config", required=True)
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_op_state)

    p = sub.add_parser("op-status", help="show last reports of all members (operator)")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_op_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
