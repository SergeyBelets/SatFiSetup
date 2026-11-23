import hashlib, json, threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote
import pytest
from signer_agent.operations import Journal, OperationJournal, public_data
from signer_agent.operational_api import OperationalHTTPServer, handler
from signer_agent.bootstrap import encode_args


def test_atomic_idempotency_across_connections(tmp_path):
    path = tmp_path / "ops.sqlite"
    a = Journal(path)
    b = Journal(path)
    assert a.put("one", {"amount": "9007199254740993000"}) == b.put(
        "one", {"amount": "9007199254740993000"}
    )
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        b.put("one", {"amount": "2"})
    a.update("one", "SIGNED", {"tx_hash": "public"})
    assert b.get("one")["status"] == "SIGNED"


def test_journal_reads_each_view_in_one_connection(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "ops.sqlite")
    for key in ("test%_:first", "testXX:first", "test%_:second"):
        journal.put(key, {"args": [key]})
    original = journal.db
    opens = []

    @contextmanager
    def counted():
        opens.append(True)
        with original() as db:
            yield db

    monkeypatch.setattr(journal, "db", counted)
    assert [row["id"] for row in journal.list()] == ["test%_:second", "testXX:first", "test%_:first"]
    assert list(journal.steps_for("test%_")) == ["test%_:first", "test%_:second"]
    assert journal.put("test%_:first", {"args": ["test%_:first"]})["status"] == "PREPARED"
    assert len(opens) == 3


def test_indexer_distinguishes_missing_blocks_and_bounds_confirmed_rollback(tmp_path):
    from types import SimpleNamespace
    from hexbytes import HexBytes
    from web3.exceptions import BlockNotFound
    from signer_agent.indexer import Indexer

    journal = Journal(tmp_path / "index.sqlite")
    headers = {height: HexBytes(bytes([height]) * 32) for height in range(1, 5)}

    def get_block(height):
        if height not in headers:
            raise BlockNotFound("block temporarily unavailable")
        return SimpleNamespace(number=height, hash=headers[height])

    def get_logs(query):
        # RPC observation must run outside the indexer's write transaction.
        with journal.db() as db:
            db.execute("BEGIN IMMEDIATE")
        height = query["fromBlock"]
        digest = headers[height]
        return [SimpleNamespace(blockHash=digest, transactionHash=digest, logIndex=0,
                                address="0x" + "11" * 20, topics=[], data=HexBytes("0x"))]

    eth = SimpleNamespace(chain_id=31337, block_number=4, get_block=get_block, get_logs=get_logs)
    index = Indexer(SimpleNamespace(eth=eth), journal, 31337)

    def canonical_heights():
        with journal.db() as db:
            return [row[0] for row in db.execute(
                "SELECT height FROM indexed_logs WHERE canonical=1 ORDER BY height")]

    assert index.poll([], 1, confirmations=1, limit=4)["checkpoint"] == "4"
    eth.block_number = 3
    assert index.poll([], 1, confirmations=1)["status"] == "AWAITING_EVM_RPC"
    eth.block_number = 5
    headers[5] = HexBytes(bytes([5]) * 32)
    headers.pop(4)
    assert index.poll([], 1, confirmations=1)["status"] == "AWAITING_EVM_RPC"
    assert canonical_heights() == [1, 2, 3, 4]

    for height in range(2, 5):
        headers[height] = HexBytes(bytes([height + 32]) * 32)
    assert index.poll([], 1, confirmations=1, limit=2) == {
        "status": "RECOVERY_REQUIRED", "checkpoint": "2", "lag": "3",
    }
    assert canonical_heights() == [1, 2]
    assert index.poll([], 1, confirmations=1, limit=2)["checkpoint"] == "2"
    assert index.poll([], 1, confirmations=1, limit=4)["checkpoint"] == "5"
    assert canonical_heights() == [1, 2, 3, 4, 5]
    with journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM indexed_logs WHERE canonical=0").fetchone()[0] == 3


def test_api_has_roles_no_export_and_string_amounts(tmp_path):
    tokens = {"read": "reader", "write": "operator"}
    policy = {
        "deployments": ["local", "other"],
        "principals": [
            dict(sha256=hashlib.sha256(t.encode()).hexdigest(), role=r)
            for t, r in tokens.items()
        ],
        "actions": {
            "claim": {
                "artifact": "CustodianDAOStaking",
                "method": "claim",
                "deployment": "local",
                "contract": "staking",
            }
        },
    }
    policy["actions"]["claim-other"] = dict(policy["actions"]["claim"], deployment="other")
    journal = Journal(tmp_path / "ops.sqlite")
    server = OperationalHTTPServer(("127.0.0.1", 0), handler(policy, journal))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def call(path, token=None, body=None, key="test"):
        headers = {"Authorization": "Bearer " + token} if token else {}
        headers["Idempotency-Key"] = key
        req = Request(
            "http://127.0.0.1:" + str(server.server_port) + path,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            with urlopen(req) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            return e.code, json.load(e)

    try:
        assert call("/health")[0] == 200
        assert call("/v1/operations")[0] == 401
        assert (
            call("/v1/operations", "read", {"action": "claim", "args": [["1"]]})[0]
            == 403
        )
        assert (
            call("/v1/operations", "write", {"action": "claim", "args": [[1]]})[0]
            == 400
        )
        assert (
            call("/v1/operations", "write", {"action": "claim", "args": [["1"]]})[0]
            == 202
        )
        assert (
            call("/v1/operations", "write", {"action": "claim", "args": [["2"]]})[0]
            == 409
        )
        assert call("/v1/export", "write")[0] == 404
        assert call("/v1/operations", "write", {"private_key": "forbidden"})[0] == 400
        key = "hidden-deployment:deploy_token"
        valid = {"action": "claim", "args": [["1"]]}
        assert call("/v1/operations", "write", valid, key)[0] == 202
        step = {"id": "deploy_token", "kind": "deploy"}
        assert journal.put(key, step)["request"] == step
        assert call("/v1/operations", "write", valid, key)[0] == 202
        assert call("/v1/operations/" + key, "read")[1]["request"]["action"] == "claim"
        journal.put("other-deployment", {"profile": {"steps": []}})
        assert call("/v1/operations", "write", valid, "other-deployment")[0] == 202
        assert journal.get("other-deployment")["request"] == {"profile": {"steps": []}}
        status, response = call("/v1/operations", "read")
        assert status == 200
        assert "forbidden" not in json.dumps(response)

        shared = "shared/claim"
        url = "/v1/operations/" + quote(shared, safe="")
        other = dict(valid, action="claim-other")
        assert call("/v1/operations", "write", valid, shared)[0] == 202
        status, prepared = call("/v1/operations", "write", other, shared)
        assert status == 202
        assert call("/v1/operations", "write", other, shared) == (202, prepared)
        assert call(url + "?deployment=other", "read") == (200, prepared)
        assert call(url, "read") == (409, {"error": "API_OPERATION_DEPLOYMENT_REQUIRED"})
        for query in ("deployment=", "deployment=local&deployment=other", "unknown=other"):
            assert call(url + "?" + query, "read") == (400, {"error": "API_OPERATION_DEPLOYMENT_QUERY"})
        assert call(url + "?deployment=unlisted", "read")[0] == 404
        del policy["actions"]["claim-other"]
        assert call(url, "read")[1]["request"]["action"] == "claim"
        assert call(url + "?deployment=other", "read")[0] == 404
        assert call("/metrics", "read")[1]["operations_by_status"] == {"PREPARED": 4}
        assert {row["request"]["action"] for row in call("/v1/operations", "read")[1]["operations"]} == {"claim"}
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_http_request_input_uses_one_deadline_and_byte_budget(monkeypatch):
    from types import SimpleNamespace as NS
    from signer_agent import operational_api as api

    now = [0]
    monkeypatch.setattr(api, "monotonic", lambda: now[0])
    monkeypatch.setattr(api, "HTTP_REQUEST_LIMIT", 4)
    timeouts, sizes = [], []

    def receive(buffer, size):
        sizes.append(size)
        buffer[:size] = b"x" * size
        return size

    reader = api.RequestInput(NS(settimeout=timeouts.append, recv_into=receive))
    assert reader.readinto(bytearray(3)) == 3
    now[0] = api.HTTP_REQUEST_READ_SECONDS - 1
    assert reader.readinto(bytearray(3)) == 1
    assert timeouts[2] == 1
    assert sizes == [3, 1]
    with pytest.raises(api.RequestLimitExceeded):
        reader.readinto(bytearray(1))
    reader = api.RequestInput(NS(settimeout=timeouts.append, recv_into=receive))
    now[0] += api.HTTP_REQUEST_READ_SECONDS
    with pytest.raises(TimeoutError):
        reader.readinto(bytearray(1))
    assert sizes == [3, 1]


def test_http_server_rejects_work_above_connection_limit(tmp_path, monkeypatch):
    from signer_agent import operational_api as api

    monkeypatch.setattr(api, "HTTP_CONNECTION_LIMIT", 1)
    entered, finished = threading.Event(), threading.Event()
    journal = Journal(tmp_path / "ops.sqlite")

    class FirstRequest(handler({"principals": []}, journal)):
        def do_GET(self):
            entered.set()
            if finished.wait(timeout=3):
                super().do_GET()

    with OperationalHTTPServer(("127.0.0.1", 0), FirstRequest) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/health"

        def status():
            with urlopen(url, timeout=4) as response:
                return response.status

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(status)
                try:
                    assert entered.wait(timeout=2)
                    with pytest.raises(HTTPError) as overloaded:
                        status()
                    assert overloaded.value.code == 503
                    assert overloaded.value.headers["Retry-After"] == "1"
                    overloaded.value.close()
                finally:
                    finished.set()
                assert response.result(timeout=2) == 200
            assert server._connections.acquire(timeout=2)
            server._connections.release()
            assert status() == 200
        finally:
            server.shutdown()
            worker.join()


def test_http_post_validates_framing_before_preparing_an_operation(tmp_path):
    from email.message import Message
    from io import BytesIO

    api = handler({"principals": []}, Journal(tmp_path / "ops.sqlite"))
    request = api.__new__(api)
    request.path = "/v1/operations"
    request.role = lambda: "operator"
    request.reply = lambda status, result: (status, result)
    request.headers = Message()
    request.headers.add_header("Content-Length", "10")
    request.headers.add_header("Content-Length", "10")
    assert request.do_POST() == (400, {"error": "REQUEST_BODY_FRAMING"})
    del request.headers["Content-Length"]
    request.headers.add_header("Transfer-Encoding", "chunked")
    assert request.do_POST() == (400, {"error": "REQUEST_BODY_FRAMING"})
    del request.headers["Transfer-Encoding"]
    request.headers.add_header("Content-Length", "10")
    request.rfile = BytesIO(b"{}")
    assert request.do_POST() == (400, {"error": "INVALID_REQUEST"})


def test_migrate_api_records_preserves_id_and_status_and_releases_old_key(tmp_path):
    journal = Journal(tmp_path / "ops.sqlite")
    key = "hidden:step"
    spec = dict(artifact="ULRStaking", contract="staking", method="claim", deployment="visible")
    prepared = dict(action="claim", args=[["100"]], spec=spec)
    journal.put(key, prepared)
    journal.update(key, "VERIFIED", {"tx_hash": "old"})
    old = journal.get(key)
    requests = OperationJournal(journal.path)
    assert requests.get(key) == old
    with pytest.raises(KeyError):
        journal.get(key)
    step = dict(kind="deploy")
    journal.put(key, step)
    assert OperationJournal(journal.path).get(key) == old
    assert Journal(journal.path).get(key)["request"] == step
    with requests.db() as db:
        assert [r[0] for r in db.execute("SELECT status FROM api_operation_events ORDER BY sequence")] == [
            "PREPARED", "VERIFIED"]
        assert [r[0] for r in db.execute("SELECT status FROM events")] == ["PREPARED"]


def test_api_operation_keys_are_independent_per_deployment(tmp_path):
    requests = OperationJournal(tmp_path / "ops.sqlite")
    request = dict(action="claim", args=[["100"]], spec=dict(deployment="first"))
    first = requests.put("same", request)
    second = requests.put("same", dict(request, spec=dict(deployment="second")))
    assert first["id"] == second["id"] == "same"
    with pytest.raises(ValueError, match="API_OPERATION_DEPLOYMENT_REQUIRED"):
        requests.get("same")
    with pytest.raises(ValueError, match="INVALID_IDEMPOTENCY_KEY"):
        requests.get(None)
    requests.update("same", "VERIFIED", {}, deployments=["first"])
    assert requests.get("same", deployments=["first"])["status"] == "VERIFIED"
    assert requests.list(deployments=["second"]) == [second]
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        requests.put("same", dict(request, args=[["101"]]))


def test_api_execution_uses_separate_state_and_legacy_retry_keeps_binding(tmp_path):
    from signer_agent.operation_worker import execution_profile

    journal = Journal(tmp_path / "ops.sqlite")
    spec = dict(artifact="ULRStaking", contract="staking", method="claim", deployment="visible")
    prepared = dict(action="claim", args=[["100"]], spec=spec)
    journal.put("key", prepared)
    requests = OperationJournal(journal.path)
    operation = requests.find("key", ["visible"])
    base = dict(mode="integration", chain_id="31337")
    contracts = {"staking": {"artifact": "ULRStaking", "address": "synthetic-staking"}}
    state, profile = execution_profile(journal, operation, base, contracts)
    assert state != journal.path
    assert profile["deployment_id"] != "operation-key"
    journal.put(profile["deployment_id"], {"profile": {"steps": []}})
    assert execution_profile(journal, operation, base, contracts) == (state, profile)
    legacy = dict(profile, deployment_id="operation-key")
    journal.put("operation-key", dict(profile=legacy, version="1"))
    assert execution_profile(journal, operation, base, contracts) == (journal.path, legacy)
    requests.put("key", dict(prepared, spec=dict(spec, deployment="second")))
    new_operation = requests.find("key", ["second"])
    new_state, new_profile = execution_profile(journal, new_operation, base, contracts)
    assert new_state == state
    assert new_profile["deployment_id"] != legacy["deployment_id"]
    with pytest.raises(ValueError, match="API_LEGACY_OPERATION_PROFILE_MISMATCH"):
        execution_profile(journal, operation, dict(base, chain_id="11155111"), contracts)
    with pytest.raises(ValueError, match="API_OPERATION_ARTIFACT_MISMATCH"):
        execution_profile(journal, operation, base, {"staking": {"artifact": "Different"}})


def test_api_signed_receipt_can_be_observed_without_bitcoin_or_signer(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from eth_account import Account
    from hexbytes import HexBytes
    from web3 import Web3

    from signer_agent import operation_worker as worker

    journal = Journal(tmp_path / "ops.sqlite")
    requests = OperationJournal(journal.path)
    spec = dict(artifact="ULRStaking", contract="staking", method="claim", deployment="base")
    base = dict(deployment_id="base", mode="backed", chain_id="11155111",
                bitcoin_network="testnet4", steps=[dict(kind="bitcoin_ready")])
    journal.put("base", dict(profile=base))
    recipient = "0x" + "11" * 20
    code = bytes([0x60, 0])
    contracts = dict(staking=dict(artifact="ULRStaking", address=recipient, runtime_hash=Web3.keccak(code).hex()))
    journal.update("base", "VERIFIED", dict(contracts=contracts))
    requests.put("op", dict(action="claim", args=[["100"]], spec=spec))
    operation = requests.find("op", ["base"])
    state, profile = worker.execution_profile(journal, operation, base, contracts)
    execution = profile["deployment_id"]
    step = profile["steps"][0]
    source = Journal(state)
    source.put(execution, dict(profile=profile, version="1"))
    source.put(execution + ":execute", step)
    calldata = Web3.keccak(text="claim(uint256[])")[:4] + Web3().codec.encode(["uint256[]"], [[100]])
    signed = Account.create().sign_transaction(dict(to=recipient, data=calldata, value=0, nonce=4, gas=80_000,
                                                    gasPrice=1, chainId=11155111))
    source.update(execution + ":execute", "BROADCAST",
                  dict(tx_hash=signed.hash.hex(), raw_transaction=signed.raw_transaction.hex(), nonce="4"))
    checkpoint = HexBytes(bytes([9]) * 32)
    receipt = NS(transactionHash=signed.hash, blockNumber=9, blockHash=checkpoint, status=1)
    flags = dict(code=code, calldata=calldata)
    http = NS(get_transaction_receipt=lambda _txid: receipt,
              get_block=lambda h: NS(number=10 if h == "latest" else h, hash=checkpoint),
              get_code=lambda _address, **_options: flags["code"], chain_id=11155111)
    runner = NS(w3=NS(eth=http), evm_confirmations=2,
                fn=lambda _step: NS(_encode_transaction_data=lambda: flags["calldata"]))
    monkeypatch.setattr(worker, "Bootstrap", lambda got, db, *, evm_rpc_override: runner)

    observed = worker.operation_receipt(journal, operation, "https://sepolia.invalid")
    assert observed == dict(status="RECEIPT_CONFIRMED", tx_hash=signed.hash.hex(), block_number="9",
                            block_hash=checkpoint.hex())
    assert source.get(execution + ":execute")["status"] == "BROADCAST"
    assert requests.get("op", deployments=["base"])["status"] == "PREPARED"
    receipt.status = 0
    assert worker.operation_receipt(journal, operation, None)["status"] == "RECEIPT_REVERTED"
    receipt.blockNumber = 10
    assert worker.operation_receipt(journal, operation, None)["status"] == "AWAITING_EVM_RECEIPT"
    flags["calldata"] = calldata[:4]
    with pytest.raises(ValueError, match="^EVM_SIGNED_TRANSACTION_BINDING$"):
        worker.operation_receipt(journal, operation, None)


def test_secret_and_amount_policy():
    with pytest.raises(ValueError):
        public_data({"nested": {"private_key": "x"}})
    with pytest.raises(ValueError):
        public_data({"amount": 1})
    assert encode_args(
        [{"type": "uint256"}, {"type": "uint256[]"}], ["123", ["456"]]
    ) == [123, [456]]


@pytest.mark.parametrize("difference", [None, "method", "deployment"])
def test_worker_rechecks_policy_after_building_unsigned_transaction(tmp_path, monkeypatch, difference):
    from signer_agent import operation_worker as worker

    spec = dict(artifact="ULRStaking", contract="staking", method="claim", deployment="visible")
    operation = dict(request=dict(action="claim", args=[["100"]], spec=spec))
    policy = dict(deployments=["visible"], actions={"claim": spec})
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    assert worker.require_current_policy(policy_path, operation) is None
    tx = {"nonce": 3}

    def build(*_a):
        # Policy changed during the RPC work to estimate gas.
        if difference == "method":
            policy["actions"]["claim"] = dict(spec, method="newMethod")
        elif difference == "deployment":
            policy["deployments"].clear()
        policy_path.write_text(json.dumps(policy))
        return tx

    monkeypatch.setattr(worker.Bootstrap, "build_transaction", build)
    runner = worker.OperationBootstrap.__new__(worker.OperationBootstrap)
    runner.operation, runner.policy_path = operation, policy_path
    runner.contracts = {"staking": {"artifact": "ULRStaking"}}
    step = {"contract": "staking", "method": "claim"}
    if difference:
        with pytest.raises(ValueError, match="^ACTION_NOT_ALLOWED$"):
            runner.build_transaction(step, object(), 3)
    else:
        assert runner.build_transaction(step, object(), 3) is tx


@pytest.mark.parametrize("backend", ["relay", "zk-state-v2"])
def test_ops_readiness_and_worker_use_same_local_core_without_persisting_connection(tmp_path, monkeypatch, backend):
    from types import SimpleNamespace as NS
    from signer_agent import operational_api as api, operation_worker as worker

    journal = Journal(tmp_path / "ops.sqlite")
    requests = OperationJournal(journal.path)
    deployment = "backed"
    policy = dict(backend=backend)
    profile = dict(deployment_id=deployment, chain_id="11155111", bitcoin_network="testnet4",
                   bitcoin_policy=policy, steps=[dict(kind="bitcoin_ready")])
    journal.put(deployment, dict(profile=profile))
    spec = dict(artifact="ULRStaking", contract="staking", method="claim", deployment=deployment)
    settle = dict(artifact="BitcoinWithdrawalEscrow", contract="escrow", method="settle",
                  deployment=deployment, evm_graph_only=True)
    requests.put("op", dict(action="claim", args=[["100"]], spec=spec))
    requests.put("btc-op", dict(action="settle", args=[], spec=settle))
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(dict(deployments=[deployment], actions={"claim": spec, "settle": settle})))
    local = dict(evm_rpc="https://alternate.invalid/rpc", bitcoin_rpc="https://core.invalid/private",
                 bitcoin_cookie="/local/auth")
    assert not any(value in str(journal.get(deployment)) for value in local.values())

    core = object()
    made = []
    status = {"ready": True, "evm_ready": True, "anchor_matches": True, "runtime_hash": "0x" + "01" * 32}
    def client(rpc, cookie, network, configured):
        assert (rpc, cookie, network, configured) == (local["bitcoin_rpc"], local["bitcoin_cookie"], "testnet4", policy)
        made.append(core)
        return core
    monkeypatch.setattr(api, "BitcoinRPC", client)
    contract = dict(artifact="ULRStaking", address="0x" + "02" * 20, runtime_hash="0x" + "01" * 32)

    def bootstrap(current, state, *, bitcoin, evm_rpc_override):
        assert (current, state, evm_rpc_override) == (profile, journal.path, local["evm_rpc"])

        def verify(*, verify, evm_graph_only=False):
            assert verify
            if evm_graph_only:
                assert bitcoin is None
                outcome = "EVM_GRAPH_VERIFIED" if status["evm_ready"] else "INCOMPLETE"
            else:
                assert bitcoin is core
                outcome = "VERIFIED" if status["ready"] else "AWAITING_BITCOIN_RELAY"
            return dict(status=outcome,
                        manifest=dict(contracts={"staking": contract, "escrow": {"artifact": settle["artifact"]}}))

        number, digest = 6, b"\xa1" * 32
        def block(tag):
            return NS(number=number, hash=digest if tag == "latest" or status["anchor_matches"] else b"\xcc" * 32)

        fn = NS(abi={"stateMutability": "view"}, call=lambda **_kw: 42)
        return NS(apply=verify, fn=lambda _spec: fn, resolve=lambda value: value, wire=str,
                  evm_confirmations=1, w3=NS(eth=NS(chain_id=11155111, get_block=block)))

    monkeypatch.setattr(api, "Bootstrap", bootstrap)
    def runtime(_w3, address, artifact, block):
        assert (address, artifact, block) == (contract["address"], "ULRStaking", 6)
        return status["runtime_hash"]

    monkeypatch.setattr(worker, "checked_runtime", runtime)

    with pytest.raises(ValueError, match="^MAINNET_CORE_REQUIRED$"):
        api.deployment_runner(profile, journal)
    read_policy = dict(deployments=[deployment], views={"stake": dict(spec, method="totalStaked")})
    view = api.read_view(read_policy, journal, "stake", **local)
    assert view["value"] == "42"
    assert not any(value in str(view) for value in local.values())

    clock = [100]
    monkeypatch.setattr(api, "monotonic", lambda: clock[0])
    request_handler = api.handler(read_policy, journal, **local)
    request = request_handler.__new__(request_handler)
    request.role = lambda: "reader"
    request.path = "/readiness"
    request.reply = lambda code, body: (code, body)
    assert request.do_GET() == (200, dict(status="READY", checks=[dict(deployment=deployment, ready=True, age_seconds=0)]))
    clock[0] += 3
    request.path = "/v1/views/stake"
    code, view = request.do_GET()
    assert code == 200 and view["deployment_check_age_seconds"] == 3
    assert made == [core] * 2
    status["ready"] = False
    clock[0] += api.READINESS_TTL_SECONDS
    request.path = "/readiness"
    assert request.do_GET() == (503, dict(status="NOT_READY", checks=[dict(deployment=deployment, ready=False, age_seconds=0)]))

    def operation_bootstrap(operation_profile, _state, **options):
        assert not status["ready"]
        assert operation_profile["steps"] == [dict(id="execute", kind="call",
            contract="staking", method="claim", args=[["100"]])]
        assert options["evm_rpc_override"] == local["evm_rpc"]
        return NS(apply_confirmed=lambda _identity: dict(status="VERIFIED"))
    monkeypatch.setattr(worker, "OperationBootstrap", operation_bootstrap)
    args = NS(id="btc-op", policy=policy_file, rpc=local["evm_rpc"], bitcoin_rpc=local["bitcoin_rpc"],
               bitcoin_cookie=local["bitcoin_cookie"], dry_run=False, identity_file="identity")
    with pytest.raises(ValueError, match="^DEPLOYMENT_NOT_READY$"):
        worker.execute(journal, requests, args, [deployment])
    assert requests.get("btc-op", deployments=[deployment])["status"] == "PREPARED"

    args.id, args.bitcoin_rpc, args.bitcoin_cookie = "op", None, None
    status["evm_ready"] = False
    with pytest.raises(ValueError, match="^DEPLOYMENT_NOT_READY$"):
        worker.execute(journal, requests, args, [deployment])
    status["evm_ready"] = True
    status["runtime_hash"] = "0x" + "03" * 32
    with pytest.raises(ValueError, match="^CODE_HASH_MISMATCH$"):
        worker.execute(journal, requests, args, [deployment])
    status["runtime_hash"] = contract["runtime_hash"]
    status["anchor_matches"] = False
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        worker.execute(journal, requests, args, [deployment])
    assert requests.get("op", deployments=[deployment])["status"] == "PREPARED"
    status["anchor_matches"] = True
    assert worker.execute(journal, requests, args, [deployment]) == dict(status="VERIFIED")
    assert made == [core] * 4
    assert journal.get(deployment)["request"]["profile"] == profile
    assert not any(value in str(requests.get("op", deployments=[deployment])) for value in local.values())


@pytest.mark.parametrize("kind,method", [("ULRStaking", "stake"), ("BitcoinMintAdmission", "openMint")])
def test_operations_require_current_artifact_for_new_non_exit_signing(monkeypatch, kind, method):
    from signer_agent import operation_worker as worker

    instance = worker.OperationBootstrap.__new__(worker.OperationBootstrap)
    instance.contracts = {"target": dict(artifact=kind, artifact_hash="archive-pin")}
    instance.operation, instance.policy_path = {}, "policy"
    checked = []
    monkeypatch.setattr(worker, "pinned_artifact", lambda item: (None, True))
    monkeypatch.setattr(worker, "require_current_policy", lambda *args: checked.append("policy"))
    monkeypatch.setattr(worker.Bootstrap, "build_transaction", lambda *args: checked.append("build"))

    with pytest.raises(ValueError, match="^API_LEGACY_ARTIFACT_EXIT_ONLY$"):
        instance.build_transaction(dict(contract="target", method=method), None, 1)
    assert checked == []
    if kind == "ULRStaking":
        instance.build_transaction(dict(contract="target", method="unstake"), None, 1)
        assert checked == ["build", "policy"]


def test_evm_graph_review_preserves_bitcoin_checkpoints_and_rechecks_evm(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS
    from signer_agent import bootstrap

    btc = dict(id="bitcoin_ready", kind="bitcoin_ready")
    binding = dict(id="bind", kind="call")
    profile = dict(deployment_id="backed", mode="backed", chain_id="11155111", bitcoin_network="testnet4",
                   evm_rpc="https://alternate.invalid/rpc", steps=[btc, binding])
    monkeypatch.setattr(bootstrap, "evm_web3", lambda *_args, **_kw: NS(eth=NS(chain_id=11155111)))
    b = bootstrap.Bootstrap(profile, tmp_path / "ops.sqlite")
    b.journal.put("backed:bitcoin_ready", btc)
    b.journal.update("backed:bitcoin_ready", "AWAITING_BITCOIN_RELAY", {"depth": "0"})
    saved_bitcoin = b.journal.get("backed:bitcoin_ready")
    saved_deployment = b.journal.get("backed")
    assert b.apply(verify=True, evm_graph_only=True) == dict(status="INCOMPLETE", step="bind")
    checks = []
    monkeypatch.setattr(b, "verify_checks", lambda step, receipt: checks.append((step, receipt)))
    monkeypatch.setattr(b, "verify_receipt", lambda result: ("verified", result["tx_hash"]))
    b.journal.update("backed:bind", "CONFIRMED", {"tx_hash": "receipt"})
    assert b.apply(verify=True, evm_graph_only=True)["status"] == "EVM_GRAPH_VERIFIED"
    assert checks == [(binding, ("verified", "receipt"))]
    assert b.journal.get("backed:bitcoin_ready") == saved_bitcoin
    assert b.journal.get("backed") == saved_deployment
    with pytest.raises(ValueError, match="^MAINNET_CORE_REQUIRED$"):
        b.apply(verify=True)
    with pytest.raises(ValueError, match="^API_EVM_GRAPH_VERIFY_ONLY$"):
        b.apply(evm_graph_only=True)


def test_bootstrap_rejects_mainnet_before_rpc_or_identity(tmp_path):
    from signer_agent.bootstrap import Bootstrap
    with pytest.raises(ValueError, match="INTEGRATION_NETWORK_NOT_ALLOWED"):
        Bootstrap(dict(mode="integration", chain_id="1", bitcoin_network="regtest"), tmp_path / "ops.sqlite")


def test_bootstrap_plan_has_real_waits_and_no_secret_shortcut():
    from signer_agent.bootstrap_plan import build
    from signer_agent.bootstrap import artifact
    from eth_utils import to_checksum_address

    members = [to_checksum_address("0x" + ("%040x" % i)) for i in (1, 2, 3)]
    config = dict(
        deployment_id="test",
        mode="integration",
        chain_id="31337",
        bitcoin_network="regtest",
        evm_rpc="http://127.0.0.1:8545",
        operator=members[0],
        treasury=members[2],
        min_p2wpkh_sats="294",
        min_p2tr_sats="330",
        min_custodians="2",
        bitcoin_confirmations="6",
        wrap_fee_bps="100", unwrap_fee_bps="100",
        wrap_staker_bps="2500", deploy_staker_bps="5000",
        converter_numerator="1", converter_denominator="10000000000",
        converter_funding="1000000000", dao_token_recipient=members[0],
        dao_use_reserve=False, integration_fee_exemption=True,
        approval_deadline="9999999999",
        federation_id="fed",
        confederation_id="conf",
    )
    for i, name in enumerate(("evm_ceremony", "conf_ceremony", "btc_ceremony")):
        config[name] = dict(
            id="0x" + ("%064x" % (i + 1)),
            participants=members,
            threshold="3",
            epoch="1",
            transport_keys=["0x" + ("%064x" % n) for n in (1, 2, 3)],
        )
    plan = build(config)
    assert len({s["id"] for s in plan["steps"]}) == len(plan["steps"])
    assert any(
        s.get("pending_status") == "AWAITING_CUSTODIAN_APPROVALS" for s in plan["steps"]
    )
    assert any(s.get("method") == "registerCustodianDAO" for s in plan["steps"])
    types = {s["id"]: s["artifact"] for s in plan["steps"] if s["kind"] == "deploy"}
    for step in plan["steps"]:
        for out in step.get("outputs", []):
            if out.get("artifact"):
                types[out["name"]] = out["artifact"]
    for step in plan["steps"]:
        if step["kind"] == "deploy":
            abi = artifact(step["artifact"])["abi"]
            ctor = next((a for a in abi if a["type"] == "constructor"), {"inputs": []})
            assert len(step["args"]) == len(ctor["inputs"]), step["id"]
        if step["kind"] in ("call", "wait"):
            abi = artifact(types[step["contract"]])["abi"]
            f = [
                a
                for a in abi
                if a.get("type") == "function" and a.get("name") == step["method"]
            ]
            assert len(f) == 1, step["id"]
            assert len(step["args"]) == len(f[0]["inputs"]), step["id"]
        for check in step.get("checks", []):
            abi = artifact(types[check["contract"]])["abi"]
            getter = next(
                a
                for a in abi
                if a.get("type") == "function" and a["name"] == check["method"]
            )
            assert len(getter["inputs"]) == len(check["args"])
        for out in step.get("outputs", []):
            if out.get("event"):
                event = next(
                    a
                    for a in artifact(types[out["contract"]])["abi"]
                    if a.get("type") == "event" and a["name"] == out["event"]
                )
                assert out["field"] in [a["name"] for a in event["inputs"]]


def test_example_policy_is_public_and_matches_contract_abis():
    from pathlib import Path
    from signer_agent.bootstrap import artifact

    policy = public_data(
        json.loads(
            (
                Path(__file__).resolve().parents[2] / "config/api-policy.example.json"
            ).read_text()
        )
    )
    assert policy["principals"] == []
    artifacts = {
        spec["contract"]: spec["artifact"] for spec in policy["actions"].values()
    }
    artifacts.update(dkg="DKGCoordinator", usd="DAOTestUSD")
    for collection in ("actions", "views"):
        for spec in policy[collection].values():
            methods = [
                x
                for x in artifact(artifacts[spec["contract"]])["abi"]
                if x.get("type") == "function" and x["name"] == spec["method"]
            ]
            assert len(methods) == 1
            if collection == "views":
                assert methods[0]["stateMutability"] in ("view", "pure")
                assert spec.get("parameters") or len(methods[0]["inputs"]) == len(
                    spec["args"]
                )
