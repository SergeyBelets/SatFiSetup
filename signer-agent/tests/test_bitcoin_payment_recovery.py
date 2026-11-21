"""Operational outage recovery must preserve both paid proofs and spend guards."""

import json
from types import SimpleNamespace

import pytest
import web3

from signer_agent.bitcoin_rpc import BitcoinRPCError
from signer_agent.bitcoin_worker import BitcoinWorker

BLOCK = "ab" * 32
ORPHAN = "ba" * 32


class Bitcoin:
    def __init__(self):
        self.failed_method = None
        self.confirmations = 8
        self.canonical = BLOCK
        self.proof_valid = True
        self.override_response = {}
        self.sent = []

    def check_network(self):
        return None

    def call(self, method, *args):
        if method == self.failed_method:
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", -28)
        if method in self.override_response:
            return self.override_response[method]
        if method == "getrawtransaction":
            return dict(txid=args[0], blockhash=BLOCK, confirmations=self.confirmations, hex="exact-signed-transaction")
        if method == "getblockheader":
            return dict(hash=args[0], height=100, confirmations=8)
        if method == "getblockhash":
            return self.canonical
        if method == "gettxoutproof":
            return "merkle-proof"
        if method == "verifytxoutproof":
            return ["txid"] if self.proof_valid else []
        if method == "getmempoolentry":
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", -5)
        self.sent.append((method, args))
        pytest.fail("recovery must not broadcast or mutate chain state")


@pytest.fixture
def worker(tmp_path):
    rpc = Bitcoin()
    worker = BitcoinWorker(tmp_path / "worker.sqlite", rpc)
    with worker.db() as db:
        db.execute("INSERT INTO payments VALUES(?,?,?,?,?,NULL)",
                   ("batch", "exact-signed-transaction", "txid", "wtxid", "BROADCAST"))
    return worker


@pytest.mark.parametrize("method", ["getrawtransaction", "getblockheader", "getblockhash", "gettxoutproof", "verifytxoutproof"])
def test_each_post_payment_rpc_outage_is_retryable_and_preserves_proof(worker, method):
    original = worker.confirmation("batch", depth=7)
    assert original["status"] == "CONFIRMED"
    worker.rpc.failed_method = method
    failure = worker.confirmation("batch")
    assert failure == dict(status="OBSERVATION_UNAVAILABLE", error_code="BITCOIN_RPC_REJECTED", rpc_error_code=-28)
    with worker.db() as db:
        status, proof = db.execute("SELECT status,proof FROM payments WHERE id='batch'").fetchone()
    assert status == "CONFIRMED"
    assert json.loads(proof) == original
    worker.rpc.failed_method = None
    assert worker.confirmation("batch")["depth"] == 7
    with worker.db() as db:
        assert not db.execute("SELECT 1 FROM payment_observations").fetchone()
        audit = json.loads(db.execute("SELECT detail FROM payment_recoveries WHERE id='batch'").fetchone()[0])
    assert audit["previous"] == failure
    assert audit["manually_reviewed"] is False
    assert worker.rpc.sent == []


@pytest.mark.parametrize("changed", [
    dict(confirmations=0, canonical=ORPHAN), dict(canonical=ORPHAN), dict(proof_valid=False),
])
def test_real_reorg_stays_paused_until_explicit_verified_recovery(worker, changed):
    assert worker.confirmation("batch")["status"] == "CONFIRMED"
    for key, value in changed.items():
        setattr(worker.rpc, key, value)
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "RECOVERY_REQUIRED"
    worker.rpc.confirmations, worker.rpc.canonical, worker.rpc.proof_valid = 8, BLOCK, True
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "CONFIRMED"
    with worker.db() as db:
        audit = json.loads(db.execute("SELECT detail FROM payment_recoveries WHERE id='batch'").fetchone()[0])
    assert audit["manually_reviewed"] is True
    assert worker.rpc.sent == []


def test_a_later_rpc_failure_does_not_downgrade_a_reorg_pause(worker):
    worker.confirmation("batch")
    worker.rpc.canonical = ORPHAN
    worker.confirmation("batch")
    worker.rpc.failed_method = "getrawtransaction"
    failure = worker.confirmation("batch", recover=True)
    assert failure["status"] == "RECOVERY_REQUIRED"
    assert failure["rpc_error_code"] == -28


def test_outage_blocks_new_broadcast_until_original_payment_rechecked(worker):
    worker.confirmation("batch")
    worker.rpc.failed_method = "getblockheader"
    worker.confirmation("batch")
    with pytest.raises(ValueError, match="BITCOIN_OBSERVATION_UNAVAILABLE"):
        worker.broadcast("new-batch", dict(signed="new", txid="new-id", wtxid="new-witness"))
    with worker.db() as db:
        assert db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    assert worker.rpc.sent == []


def test_legacy_recovery_marker_has_checked_recovery_without_resetting_signed_bytes(worker):
    with worker.db() as db:
        db.execute("UPDATE payments SET status='RECOVERY_REQUIRED' WHERE id='batch'")
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "CONFIRMED"
    with worker.db() as db:
        assert db.execute("SELECT signed,txid FROM payments WHERE id='batch'").fetchone() == ("exact-signed-transaction", "txid")


def test_recovery_cannot_reduce_original_confirmation_depth(worker):
    worker.rpc.confirmations = 12
    worker.confirmation("batch", depth=12)
    worker.rpc.canonical = ORPHAN
    worker.confirmation("batch")
    worker.rpc.canonical = BLOCK
    worker.rpc.confirmations = 6
    assert worker.confirmation("batch", depth=1, recover=True)["status"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("method,response", [
    ("getrawtransaction", {}),
    ("getrawtransaction", dict(txid="txid", hex="signed")),
    ("getrawtransaction", dict(txid="txid", hex="signed", confirmations=8)),
    ("getrawtransaction", dict(txid="txid", hex="signed", confirmations=None, blockhash=None)),
    ("getblockheader", {}),
    ("getblockhash", None),
    ("gettxoutproof", None),
    ("verifytxoutproof", None),
    ("verifytxoutproof", [None]),
])
def test_incomplete_proxy_response_is_not_reorg_evidence(worker, method, response):
    original = worker.confirmation("batch")
    worker.rpc.override_response[method] = response
    assert worker.confirmation("batch") == dict(
        status="OBSERVATION_UNAVAILABLE", error_code="BITCOIN_OBSERVATION_UNAVAILABLE")
    worker.rpc.override_response.clear()
    assert worker.confirmation("batch") == original


@pytest.mark.parametrize("canonical,status", [
    (BLOCK, "OBSERVATION_UNAVAILABLE"), (ORPHAN, "RECOVERY_REQUIRED"),
])
def test_mempool_membership_requires_original_anchor_conflict_for_reorg(worker, canonical, status):
    worker.confirmation("batch")
    worker.rpc.canonical = canonical
    worker.rpc.override_response.update({
        "getrawtransaction": dict(txid="txid", hex="signed"),
        "getmempoolentry": dict(vsize=110),
    })
    assert worker.confirmation("batch")["status"] == status


def test_same_block_at_lower_rpc_depth_waits_and_recovers_automatically(worker):
    original = worker.confirmation("batch", depth=7)
    worker.rpc.confirmations = 4
    assert worker.confirmation("batch") == dict(
        status="OBSERVATION_UNAVAILABLE", error_code="BITCOIN_PAYMENT_DEPTH_PENDING")
    with worker.db() as db:
        row = db.execute("SELECT status,proof FROM payments WHERE id='batch'").fetchone()
    assert row[0] == "CONFIRMED" and json.loads(row[1]) == original
    worker.rpc.confirmations = 9
    assert worker.confirmation("batch") == original
    with worker.db() as db:
        assert db.execute("SELECT COUNT(*) FROM payment_observations").fetchone()[0] == 0
    assert worker.rpc.sent == []


@pytest.mark.parametrize("recover", [False, True])
def test_in_flight_confirmation_preserves_a_newer_pause(worker, monkeypatch, recover):
    worker.confirmation("batch")
    observer = BitcoinWorker(worker.path, Bitcoin())
    observer.rpc.canonical = ORPHAN
    if recover:
        assert observer.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    original_call = worker.rpc.call

    def checked_call(method, *args):
        if method == "verifytxoutproof":
            assert observer.confirmation("batch", recover=recover)["status"] == "RECOVERY_REQUIRED"
        return original_call(method, *args)

    monkeypatch.setattr(worker.rpc, "call", checked_call)
    assert worker.confirmation("batch", recover=recover)["status"] == "RECOVERY_REQUIRED"
    assert observer.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    monkeypatch.setattr(worker.rpc, "call", original_call)
    assert worker.confirmation("batch", recover=True)["status"] == "CONFIRMED"


@pytest.mark.parametrize("canonical,status", [(BLOCK, "OBSERVATION_UNAVAILABLE"), (ORPHAN, "RECOVERY_REQUIRED")])
def test_missing_confirmed_transaction_checks_stored_block(worker, monkeypatch, canonical, status):
    worker.confirmation("batch")
    worker.rpc.canonical = canonical
    original_call = worker.rpc.call

    def call(method, *args):
        if method == "getrawtransaction":
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", -5)
        return original_call(method, *args)

    monkeypatch.setattr(worker.rpc, "call", call)
    assert worker.confirmation("batch")["status"] == status


def test_standalone_recovery_checks_relay_identity_but_not_freshness(worker, monkeypatch, tmp_path, capsys):
    from signer_agent import bitcoin_cli

    worker.confirmation("batch")
    worker.rpc.canonical = ORPHAN
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    worker.rpc.canonical = BLOCK
    policy_file = tmp_path / "policy.json"
    policy_file.write_text('{"network":"mainnet"}')
    monkeypatch.setattr(bitcoin_cli, "validate_policy", lambda value, **_: value)
    monkeypatch.setattr(bitcoin_cli, "BitcoinRPC", lambda *_, **__: worker.rpc)
    class ChainView:
        HTTPProvider = staticmethod(lambda _: None)

        def __init__(self, _provider):
            self.eth = SimpleNamespace(chain_id=1)

    monkeypatch.setattr(web3, "Web3", ChainView)
    pins = []
    monkeypatch.setattr(bitcoin_cli, "_checked_mainnet_relay", lambda _, address, policy: pins.append((address, policy)))
    monkeypatch.setattr(bitcoin_cli, "_inspect_mainnet_relay", lambda *args: pytest.fail("recovery requires no relay catch-up"))
    assert bitcoin_cli.main(["recover", "--bitcoin-rpc", "core", "--bitcoin-cookie", "cookie",
        "--bitcoin-network", "mainnet", "--bitcoin-policy", str(policy_file), "--evm-confirmations", "12",
        "--chain-id", "1", "--rpc", "evm", "--relay", "pinned-relay", "--state", worker.path, "--batch", "batch"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "CONFIRMED"
    assert pins == [("pinned-relay", {"network": "mainnet"})]
