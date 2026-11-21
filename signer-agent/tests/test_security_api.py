"""Private recovery state must never become a reader-facing API response."""

import hashlib
import json
import os
import stat
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from signer_agent.operations import Journal
from signer_agent.operational_api import handler


def test_api_scopes_operations_and_redacts_recovery_state(tmp_path):
    journal = Journal(tmp_path / "ops.sqlite")
    spec = dict(
        deployment="visible",
        artifact="CustodianDAOStaking",
        method="claim",
        contract="staking",
    )
    policy = dict(
        principals=[dict(role="reader", sha256=hashlib.sha256(b"reader").hexdigest())],
        deployments=["visible"],
        actions={"claim": spec},
    )
    journal.put(
        "visible",
        {"profile": {"evm_rpc": "https://provider.invalid/RPC_CREDENTIAL_MARKER"}},
    )
    journal.put("visible:deploy", {"kind": "deploy"})
    journal.update(
        "visible:deploy",
        "SIGNED",
        {"raw_transaction": "RAW_SIGNED_TX_MARKER", "tx_hash": "0x1234"},
    )
    journal.update(
        "visible",
        "VERIFIED",
        dict(
            chain_id="31337",
            contracts={"staking": {"address": "0xABCD"}},
            steps=[journal.get("visible:deploy")],
        ),
    )
    journal.put("claim-1", dict(action="claim", args=[["1"]], spec=spec))
    journal.update(
        "claim-1",
        "VERIFIED",
        dict(status="VERIFIED", manifest=journal.get("visible")["result"]),
    )
    journal.put(
        "hidden-operation",
        dict(action="claim", args=[["2"]], spec=dict(spec, deployment="hidden")),
    )
    with journal.db() as conn:
        conn.execute(
            "CREATE TABLE indexed_logs(chain_id TEXT,height INTEGER,block_hash TEXT,tx_hash TEXT,log_index INTEGER,address TEXT,topics TEXT,data TEXT,canonical INTEGER)"
        )
        for chain, address in (
            ("31337", "0xabcd"),
            ("31337", "0xffff"),
            ("1", "0xabcd"),
        ):
            conn.execute(
                "INSERT INTO indexed_logs VALUES (?,1,'block','tx',0,?,'[]','0x',1)",
                (chain, address),
            )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(policy, journal))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(path):
        req = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            headers={"Authorization": "Bearer reader"},
        )
        try:
            with urlopen(req) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        status, listing = get("/v1/operations")
        assert status == 200
        assert [row["id"] for row in listing["operations"]] == ["claim-1"]
        assert get("/v1/operations/visible")[0] == 404
        assert get("/v1/operations/visible:deploy")[0] == 404
        assert get("/v1/operations/hidden-operation")[0] == 404
        for path in (
            "/v1/operations",
            "/v1/operations/claim-1",
            "/v1/deployments/visible",
        ):
            code, response = get(path)
            assert code == 200
            assert "RPC_CREDENTIAL_MARKER" not in json.dumps(response)
            assert "RAW_SIGNED_TX_MARKER" not in json.dumps(response)
        assert get("/metrics")[1]["operations_by_status"] == {"VERIFIED": 1}
        events = get("/v1/events")[1]["events"]
        assert len(events) == 1
        assert events[0]["chain_id"] == "31337" and events[0]["address"] == "0xabcd"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_journal_is_private_even_with_public_umask(tmp_path):
    old = os.umask(0o022)
    try:
        journal = Journal(tmp_path / "new-directory" / "ops.sqlite")
        assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700
        with journal.db() as conn:
            conn.execute(
                "INSERT INTO events(operation,status) VALUES ('test','PREPARED')"
            )
            for suffix in ("-wal", "-shm"):
                assert (
                    stat.S_IMODE(os.stat(str(journal.path) + suffix).st_mode) == 0o600
                )
    finally:
        os.umask(old)


def test_journal_rejects_symlink(tmp_path):
    real = tmp_path / "real.sqlite"
    Journal(real)
    link = tmp_path / "link.sqlite"
    link.symlink_to(real)
    with pytest.raises((OSError, ValueError)):
        Journal(link)


def test_journal_restricts_existing_database_and_rejects_shared_directory(tmp_path):
    path = tmp_path / "existing.sqlite"
    Journal(path)
    os.chmod(path, 0o644)
    Journal(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(ValueError, match="JOURNAL_DIRECTORY_PERMISSIONS"):
        Journal(shared / "ops.sqlite")
