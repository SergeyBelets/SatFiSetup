import hashlib, json, threading
from contextlib import contextmanager
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote
from http.server import ThreadingHTTPServer
import pytest
from signer_agent.operations import Journal, OperationJournal, public_data
from signer_agent.operational_api import handler
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(policy, journal))
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


def test_secret_and_amount_policy():
    with pytest.raises(ValueError):
        public_data({"nested": {"private_key": "x"}})
    with pytest.raises(ValueError):
        public_data({"amount": 1})
    assert encode_args(
        [{"type": "uint256"}, {"type": "uint256[]"}], ["123", ["456"]]
    ) == [123, [456]]


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
