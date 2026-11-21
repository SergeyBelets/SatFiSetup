"""Operational outage recovery must preserve both paid proofs and spend guards."""

import json

import pytest

from signer_agent.bitcoin_rpc import BitcoinRPCError
from signer_agent.bitcoin_worker import BitcoinWorker


TXID = "70" * 32
BLOCK_HASH = "ab" * 32


class Bitcoin:
    def __init__(self):
        self.failed_method = None
        self.confirmations = 8
        self.canonical = BLOCK_HASH
        self.proof_valid = True
        self.sent = []

    def check_network(self):
        return None

    def call(self, method, *args):
        if method == self.failed_method:
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", -28)
        if method == "getrawtransaction":
            return dict(txid=args[0], blockhash=BLOCK_HASH, confirmations=self.confirmations, hex="exact-signed-transaction")
        if method == "getblockheader":
            return dict(height=100, hash=args[0])
        if method == "getblockhash":
            return self.canonical
        if method == "gettxoutproof":
            return "aa"
        if method == "verifytxoutproof":
            return [TXID] if self.proof_valid else []
        self.sent.append((method, args))
        pytest.fail("recovery must not broadcast or mutate chain state")


@pytest.fixture
def worker(tmp_path):
    rpc = Bitcoin()
    worker = BitcoinWorker(tmp_path / "worker.sqlite", rpc)
    with worker.db() as db:
        db.execute("INSERT INTO payments VALUES(?,?,?,?,?,NULL)",
                   ("batch", "exact-signed-transaction", TXID, "wtxid", "BROADCAST"))
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


@pytest.mark.parametrize("changed", [dict(confirmations=0), dict(canonical="ba" * 32), dict(proof_valid=False)])
def test_real_reorg_stays_paused_until_explicit_verified_recovery(worker, changed):
    assert worker.confirmation("batch")["status"] == "CONFIRMED"
    for key, value in changed.items():
        setattr(worker.rpc, key, value)
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "RECOVERY_REQUIRED"
    worker.rpc.confirmations, worker.rpc.canonical, worker.rpc.proof_valid = 8, BLOCK_HASH, True
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "CONFIRMED"
    with worker.db() as db:
        audit = json.loads(db.execute("SELECT detail FROM payment_recoveries WHERE id='batch'").fetchone()[0])
    assert audit["manually_reviewed"] is True
    assert worker.rpc.sent == []


def test_a_later_rpc_failure_does_not_downgrade_a_reorg_pause(worker):
    worker.confirmation("batch")
    worker.rpc.confirmations = 0
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
        assert db.execute("SELECT signed,txid FROM payments WHERE id='batch'").fetchone() == ("exact-signed-transaction", TXID)


def test_recovery_cannot_reduce_original_confirmation_depth(worker):
    worker.rpc.confirmations = 12
    worker.confirmation("batch", depth=12)
    worker.rpc.confirmations = 0
    worker.confirmation("batch")
    worker.rpc.confirmations = 6
    assert worker.confirmation("batch", depth=1, recover=True)["status"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("changes", [
    {}, {"txid": BLOCK_HASH, "blockhash": BLOCK_HASH, "confirmations": 12},
    {"txid": TXID, "blockhash": BLOCK_HASH},
    {"txid": TXID, "confirmations": 0},
])
def test_incomplete_or_unrelated_rpc_reply_preserves_paid_proof(worker, monkeypatch, changes):
    original = worker.confirmation("batch")
    original_call = worker.rpc.call
    with monkeypatch.context() as patch:
        patch.setattr(worker.rpc, "call", lambda method, *args:
                      {"hex": "exact-signed-transaction", **changes}
                      if method == "getrawtransaction" else original_call(method, *args))
        observed = worker.confirmation("batch")
        assert observed["status"] == "OBSERVATION_UNAVAILABLE"
        assert observed["error_code"] == "BITCOIN_RPC_RESPONSE_INVALID"
        with worker.db() as db:
            assert json.loads(db.execute("SELECT proof FROM payments WHERE id='batch'").fetchone()[0]) == original
    assert worker.confirmation("batch") == original


def test_mempool_entry_rechecks_the_exact_txid_and_witness(worker, monkeypatch):
    worker.confirmation("batch")
    original_call = worker.rpc.call
    def mempool(method, *args):
        if method == "getrawtransaction":
            return {"hex": "exact-signed-transaction", "txid": args[0], "hash": "ba" * 32}
        if method == "getmempoolentry":
            assert args == (TXID,)
            return {"wtxid": "ba" * 32}
        return original_call(method, *args)
    with monkeypatch.context() as patch:
        patch.setattr(worker.rpc, "call", mempool)
        assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch")["status"] == "RECOVERY_REQUIRED"
    assert worker.confirmation("batch", recover=True)["status"] == "CONFIRMED"


def test_broadcast_lookup_outage_never_attempts_a_send(worker):
    worker.rpc.failed_method = "getrawtransaction"
    with pytest.raises(ValueError, match="BITCOIN_OBSERVATION_UNAVAILABLE"):
        worker.broadcast("new-batch", dict(signed="00", txid="ee"*32, wtxid="ff"*32))
    with worker.db() as db:
        assert db.execute("SELECT status,signed FROM payments WHERE id='new-batch'").fetchone() == ("SIGNED", "00")
    assert worker.rpc.sent == []
