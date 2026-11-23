"""Loopback operations API. Bearer hashes and explicit role/method allowlists.
This process has no access path to participant state, exports, passwords or private keys.
"""

import hashlib
import hmac
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib.parse import urlsplit, parse_qs, unquote
from .safe_errors import DiagnosticArgumentParser
from .operations import Journal, OperationJournal, public_data, prepare_operation
from .bootstrap import Bootstrap, artifact
from .bitcoin_rpc import BitcoinRPC


READINESS_TTL_SECONDS = 15
HTTP_BODY_LIMIT = 64 * 1024
HTTP_REQUEST_LIMIT = 128 * 1024
HTTP_REQUEST_READ_SECONDS = 10
HTTP_CONNECTION_LIMIT = 16


class RequestLimitExceeded(Exception):
    pass


class RequestInput(io.RawIOBase):
    """Enforce one deadline and byte budget across the request line, headers and body."""

    def __init__(self, connection):
        self.connection = connection
        self.deadline = monotonic() + HTTP_REQUEST_READ_SECONDS
        self.remaining = HTTP_REQUEST_LIMIT

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.remaining == 0:
            raise RequestLimitExceeded()
        timeout = self.deadline - monotonic()
        if timeout <= 0:
            raise TimeoutError()
        self.connection.settimeout(timeout)
        try:
            size = self.connection.recv_into(buffer, min(self.remaining, len(buffer)))
        finally:
            self.connection.settimeout(HTTP_REQUEST_READ_SECONDS)
        self.remaining -= size
        return size


class OperationalHTTPServer(ThreadingHTTPServer):
    request_queue_size = 32

    def __init__(self, address, request_handler):
        self._connections = BoundedSemaphore(HTTP_CONNECTION_LIMIT)
        super().__init__(address, request_handler)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            try:
                request.setblocking(False)
                request.send(
                    b"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Retry-After: 1\r\nCache-Control: no-store\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


def add_connection_options(parser):
    parser.add_argument("--rpc", help="Override the local EVM RPC")
    parser.add_argument("--bitcoin-rpc", help="Local configuration for the public Bitcoin Core RPC endpoint")
    parser.add_argument("--bitcoin-cookie", help="Local path to the Bitcoin Core authentication file")


def connection_options(args):
    if bool(args.bitcoin_rpc) != bool(args.bitcoin_cookie):
        raise ValueError("BITCOIN_RPC_CREDENTIALS_REQUIRED")
    return dict(evm_rpc=args.rpc, bitcoin_rpc=args.bitcoin_rpc, bitcoin_cookie=args.bitcoin_cookie)


def bitcoin_for_profile(profile, bitcoin_rpc, bitcoin_cookie):
    # A transaction-only or local profile has no Bitcoin freshness checkpoint.
    if not any(step["kind"] == "bitcoin_ready" for step in profile["steps"]):
        return None
    if not bitcoin_rpc or not bitcoin_cookie:
        raise ValueError("MAINNET_CORE_REQUIRED")
    return BitcoinRPC(bitcoin_rpc, bitcoin_cookie, profile["bitcoin_network"], profile["bitcoin_policy"])


def deployment_runner(profile, journal, *, evm_rpc=None, bitcoin_rpc=None, bitcoin_cookie=None, evm_graph_only=False):
    core = None if evm_graph_only else bitcoin_for_profile(profile, bitcoin_rpc, bitcoin_cookie)
    return Bootstrap(profile, journal.path, bitcoin=core, evm_rpc_override=evm_rpc)


def public_result(result):
    """Allowlist recovery metadata; raw transactions and profiles stay local."""
    safe = {
        k: result[k]
        for k in (
            "status",
            "step",
            "code",
            "tx_hash",
            "nonce",
            "block_number",
            "block_hash",
        )
        if k in result
    }
    if "manifest" in result:
        safe["manifest"] = public_manifest(result["manifest"])
    return safe


def public_manifest(manifest):
    safe = {
        k: manifest[k]
        for k in (
            "version",
            "deployment_id",
            "chain_id",
            "mode",
            "bitcoin_network",
            "contracts",
            "values",
        )
        if k in manifest
    }
    safe["steps"] = [
        dict(id=row["id"], status=row["status"], result=public_result(row["result"]))
        for row in manifest.get("steps", [])
    ]
    return safe


def operation_visible(policy, entry):
    request = entry["request"]
    spec = policy.get("actions", {}).get(request.get("action"))
    return bool(
        spec
        and spec.get("deployment") in policy.get("deployments", [])
        and request.get("spec") == spec
    )


def public_operation(policy, entry):
    if not operation_visible(policy, entry):
        raise KeyError(entry["id"])
    return dict(
        id=entry["id"],
        status=entry["status"],
        request={k: entry["request"][k] for k in ("action", "args", "spec")},
        result=public_result(entry["result"]),
    )


class DeploymentObservations:
    """Reuse recent HTTP verification results without queuing concurrent refreshes."""

    def __init__(self, journal, connections):
        self.journal = journal
        self.connections = connections
        self.checked = {}
        self.refresh = Lock()

    def recent(self, deployment):
        saved = self.checked.get(deployment)
        if saved is not None:
            timestamp, runner, ready = saved
            age = monotonic() - timestamp
            if age < READINESS_TTL_SECONDS:
                return runner, ready, int(age)

    def observe(self, deployment):
        saved = self.recent(deployment)
        if saved is not None:
            return saved
        if not self.refresh.acquire(blocking=False):
            raise ValueError("API_VERIFICATION_IN_PROGRESS")
        try:
            saved = self.recent(deployment)
            if saved is not None:
                return saved
            profile = self.journal.get(deployment)["request"]["profile"]
            runner = deployment_runner(profile, self.journal, **self.connections)
            ready = runner.apply(verify=True)["status"] == "VERIFIED"
            self.checked[deployment] = (monotonic(), runner, ready)
            return runner, ready, 0
        except KeyError:
            raise
        except Exception:
            self.checked[deployment] = (monotonic(), None, False)
            raise
        finally:
            self.refresh.release()


def read_view(policy, journal, name, args=None, *, observations=None, **connections):
    spec = dict(policy.get("views", {})[name])
    if spec.get("deployment") not in policy.get("deployments", []):
        raise KeyError(spec.get("deployment"))
    if args is not None:
        if not spec.get("parameters"):
            raise ValueError("VIEW_ARGUMENTS_FIXED")
        spec["args"] = public_data(args)
    profile = journal.get(spec["deployment"])["request"]["profile"]
    if observations is None:
        b = deployment_runner(profile, journal, **connections)
        ready = b.apply(verify=True)["status"] == "VERIFIED"
        age = 0
    else:
        b, ready, age = observations.observe(spec["deployment"])
    if not ready:
        raise ValueError("DEPLOYMENT_INCOMPLETE")
    fn = b.fn(spec)
    if fn.abi["stateMutability"] not in ("view", "pure"):
        raise ValueError("READ_ONLY_METHOD_REQUIRED")
    block = b.w3.eth.get_block("latest")
    value = fn.call(block_identifier=block.number)
    if b.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return dict(
        chain_id=str(profile["chain_id"]),
        bitcoin_network=profile["bitcoin_network"],
        block_number=str(block.number),
        block_hash=block.hash.hex(),
        deployment_check_age_seconds=age,
        value=b.wire(value),
        units=b.resolve(spec.get("units")),
    )


def handler(policy, journal, **connections):
    requests = OperationJournal(journal.path)
    observations = DeploymentObservations(journal, connections)

    class API(BaseHTTPRequestHandler):
        server_version = "SatohsiOps/1"

        def setup(self):
            super().setup()
            self.rfile.close()
            self.rfile = io.BufferedReader(RequestInput(self.connection))

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except RequestLimitExceeded:
                self.close_connection = True

        def log_message(self, *args):
            pass  # Never log credentials, query strings or request bodies.

        def reply(self, status, data):
            raw = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def role(self):
            supplied = self.headers.get("Authorization", "")
            digest = (
                hashlib.sha256(supplied[7:].encode()).hexdigest()
                if supplied.startswith("Bearer ")
                else ""
            )
            for principal in policy["principals"]:
                if hmac.compare_digest(digest, principal["sha256"]):
                    return principal["role"]
            return None

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if path == "/health":
                return self.reply(200, {"status": "UP"})
            if not self.role():
                return self.reply(401, {"error": "AUTHENTICATION_REQUIRED"})
            try:
                if path == "/v1/events":
                    scopes = []
                    for deployment in policy.get("deployments", []):
                        manifest = journal.get(deployment)["result"]
                        scopes.extend(
                            (manifest["chain_id"], item["address"].lower())
                            for item in manifest.get("contracts", {}).values()
                        )
                    if not scopes:
                        return self.reply(200, {"events": []})
                    with journal.db() as c:
                        rows = c.execute(
                            "SELECT chain_id,height,block_hash,tx_hash,log_index,address,topics,data,canonical FROM indexed_logs WHERE "
                            + " OR ".join(
                                "(chain_id=? AND lower(address)=?)" for _ in scopes
                            )
                            + " ORDER BY height DESC LIMIT 200",
                            [v for scope in scopes for v in scope],
                        ).fetchall()
                    return self.reply(
                        200,
                        {
                            "events": [
                                dict(
                                    r,
                                    chain_id=str(r["chain_id"]),
                                    height=str(r["height"]),
                                    log_index=str(r["log_index"]),
                                )
                                for r in rows
                            ]
                        },
                    )
                if path == "/v1/operations":
                    return self.reply(
                        200,
                        {
                            "operations": [
                                public_operation(policy, row)
                                for row in requests.list(deployments=policy.get("deployments", []))
                                if operation_visible(policy, row)
                            ]
                        },
                    )
                if path.startswith("/v1/operations/"):
                    deployments = policy.get("deployments", [])
                    query = parse_qs(url.query, keep_blank_values=True)
                    if query:
                        if set(query) != {"deployment"} or len(query["deployment"]) != 1 or not query["deployment"][0]:
                            raise ValueError("API_OPERATION_DEPLOYMENT_QUERY")
                        deployments = [name for name in deployments if name == query["deployment"][0]]
                    entry = requests.get(
                        unquote(path.rsplit("/", 1)[-1]), deployments=deployments,
                        visible=lambda row: operation_visible(policy, row),
                    )
                    return self.reply(200, public_operation(policy, entry))
                if path == "/v1/deployments":
                    return self.reply(
                        200, {"deployments": policy.get("deployments", [])}
                    )
                if path.startswith("/v1/deployments/"):
                    deployment = path.split("/")[-1]
                    if deployment not in policy.get("deployments", []):
                        raise KeyError(deployment)
                    entry = journal.get(deployment)
                    return self.reply(
                        200,
                        {
                            "status": entry["status"],
                            "manifest": public_manifest(entry["result"]),
                        },
                    )
                if path == "/v1/actions":
                    actions = {}
                    for name, spec in policy.get("actions", {}).items():
                        if spec.get("deployment") not in policy.get("deployments", []):
                            continue
                        method = next(
                            x
                            for x in artifact(spec["artifact"])["abi"]
                            if x.get("type") == "function"
                            and x["name"] == spec["method"]
                        )
                        actions[name] = dict(
                            spec, inputs=method["inputs"], requires_role="operator"
                        )
                    return self.reply(
                        200,
                        {
                            "actions": actions,
                            "views": {
                                name: spec
                                for name, spec in policy.get("views", {}).items()
                                if spec.get("deployment")
                                in policy.get("deployments", [])
                            },
                        },
                    )
                if path == "/readiness":
                    checks = []
                    for deployment in policy.get("deployments", []):
                        _, ready, age = observations.observe(deployment)
                        checks.append(
                            {
                                "deployment": deployment,
                                "ready": ready,
                                "age_seconds": age,
                            }
                        )
                    ready = bool(checks) and all(c["ready"] for c in checks)
                    return self.reply(
                        200 if ready else 503,
                        {"status": "READY" if ready else "NOT_READY", "checks": checks},
                    )
                if path == "/metrics":
                    entries = [
                        row for row in requests.list(deployments=policy.get("deployments", [])) if operation_visible(policy, row)
                    ]
                    counts = {}
                    for e in entries:
                        counts[e["status"]] = counts.get(e["status"], 0) + 1
                    observed = {}
                    for name, metric in policy.get("metrics", {}).items():
                        try:
                            observed[name] = read_view(
                                policy, journal, metric["view"], metric.get("args"), observations=observations
                            )
                        except Exception:
                            observed[name] = {"error": "METRIC_UNAVAILABLE"}
                    return self.reply(
                        200, {"operations_by_status": counts, "onchain": observed}
                    )
                if path == "/openapi.json":
                    return self.reply(
                        200,
                        json.loads(
                            (
                                Path(__file__).resolve().parents[2]
                                / "docs/openapi.json"
                            ).read_text()
                        ),
                    )
                # Named reads expose pools, DKG public status, fees, DAO reports, proposals and next actions.
                if path.startswith("/v1/views/"):
                    spec = policy.get("views", {}).get(path.split("/")[-1])
                    if not spec:
                        return self.reply(404, {"error": "VIEW_NOT_FOUND"})
                    query = parse_qs(urlsplit(self.path).query)
                    encoded = query.get("args", [None])[0]
                    if encoded is not None and len(encoded) > 8192:
                        raise ValueError("VIEW_ARGUMENTS_SIZE")
                    args = None if encoded is None else json.loads(encoded)
                    return self.reply(
                        200, read_view(policy, journal, path.split("/")[-1], args, observations=observations)
                    )
                return self.reply(404, {"error": "NOT_FOUND"})
            except KeyError:
                return self.reply(404, {"error": "NOT_FOUND"})
            except ValueError as exc:
                code = str(exc)
                if code == "API_OPERATION_DEPLOYMENT_REQUIRED":
                    return self.reply(409, {"error": code})
                if code == "API_VERIFICATION_IN_PROGRESS":
                    return self.reply(503, {"error": code})
                if code in (
                    "API_OPERATION_DEPLOYMENT_QUERY",
                    "INVALID_IDEMPOTENCY_KEY",
                    "VIEW_ARGUMENTS_FIXED",
                    "VIEW_ARGUMENTS_SIZE",
                    "INTEGER_ARGUMENT_STRING_REQUIRED",
                    "ARGUMENT_COUNT",
                ):
                    return self.reply(400, {"error": code})
                return self.reply(503, {"error": "STATE_VERIFICATION_FAILED"})
            except Exception:
                return self.reply(503, {"error": "STATE_VERIFICATION_FAILED"})

        def do_POST(self):
            if self.role() not in ("operator", "admin"):
                return self.reply(403, {"error": "OPERATOR_REQUIRED"})
            if urlsplit(self.path).path != "/v1/operations":
                return self.reply(404, {"error": "NOT_FOUND"})
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or "Transfer-Encoding" in self.headers:
                    return self.reply(400, {"error": "REQUEST_BODY_FRAMING"})
                length = int(lengths[0]) if lengths else 0
                if length <= 0 or length > HTTP_BODY_LIMIT:
                    return self.reply(413, {"error": "BODY_SIZE"})
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.reply(400, {"error": "INVALID_REQUEST"})
                request = json.loads(body)
                result = prepare_operation(
                    policy, requests, self.headers.get("Idempotency-Key", ""), request
                )
                return self.reply(202, public_operation(policy, result))
            except ValueError as e:
                return self.reply(
                    (
                        409
                        if str(e) == "IDEMPOTENCY_CONFLICT"
                        else (403 if str(e) == "ACTION_NOT_ALLOWED" else 400)
                    ),
                    {
                        "error": (
                            str(e)
                            if str(e).replace("_", "").isalnum()
                            else "INVALID_REQUEST"
                        )
                    },
                )
            except RequestLimitExceeded:
                return self.reply(413, {"error": "BODY_SIZE"})
            except TimeoutError:
                return self.reply(408, {"error": "REQUEST_TIMEOUT"})
            except Exception:
                return self.reply(400, {"error": "INVALID_REQUEST"})

    return API


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("--policy", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--port", type=int, default=8787)
    add_connection_options(p)
    a = p.parse_args(argv)
    connections = connection_options(a)
    policy = public_data(json.loads(Path(a.policy).read_text()))
    if not policy.get("principals"):
        raise ValueError("AUTH_POLICY_REQUIRED")
    for principal in policy["principals"]:
        if (
            principal["role"] not in ("reader", "operator", "admin")
            or len(principal["sha256"]) != 64
        ):
            raise ValueError("AUTH_POLICY_INVALID")
    server = OperationalHTTPServer(
        ("127.0.0.1", a.port), handler(policy, Journal(a.state), **connections)
    )
    server.serve_forever()
