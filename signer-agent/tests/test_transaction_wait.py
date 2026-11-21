"""A slow or lost receipt cannot cause new bytes/nonces on direct CLI retries."""
import pytest
from types import SimpleNamespace
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError, SSLError
from web3.exceptions import ProviderConnectionError
from signer_agent.transaction_wait import apply_confirmed
from signer_agent.transaction_wait import TransactionPending, TransactionRecoveryRequired, nonblocking_transactions
from test_bootstrap_parallel import fixture


def test_pending_receipt_then_finality_waits_for_original_signed_transaction(tmp_path, monkeypatch):
    runner, identity = fixture(tmp_path, count=1)
    runner.w3.eth.automine = False
    polls = []
    def progress(seconds):
        tx = next(iter(runner.w3.eth.transactions))
        polls.append(tx)
        if len(polls) == 1:
            runner.w3.eth.mine(tx)
        else:
            runner.w3.eth.block_number = 30
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", progress)
    assert runner.apply_confirmed(identity)["status"] == "VERIFIED"
    assert len(polls) == 2 and polls[0] == polls[1]
    assert len(runner.w3.eth.sent) == 1
    assert runner.prepared == ["leaf0"]


def test_timeout_retains_signed_transaction_for_later_resume(tmp_path):
    runner, identity = fixture(tmp_path, count=1)
    runner.w3.eth.automine = False
    with pytest.raises(TransactionPending) as error:
        runner.apply_confirmed(identity, timeout=0)
    assert error.value.result["status"] == "TRANSACTION_PENDING"
    tx = next(iter(runner.w3.eth.transactions))
    runner.w3.eth.mine(tx)
    runner.w3.eth.block_number = 30
    assert runner.apply_confirmed(identity)["status"] == "VERIFIED"
    assert len(runner.w3.eth.sent) == 1 and runner.prepared == ["leaf0"]


def test_parallel_dkg_polling_keeps_independent_ceremonies_nonblocking(tmp_path, monkeypatch):
    runner, identity = fixture(tmp_path, count=1)
    runner.w3.eth.automine = False
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda _: pytest.fail("flow must not wait"))
    with nonblocking_transactions():
        assert runner.apply_confirmed(identity)["status"] == "BROADCAST"


def test_reverted_transaction_never_waits_or_signs_a_replacement(tmp_path, monkeypatch):
    runner, identity = fixture(tmp_path, count=1)
    runner.w3.eth.fail_nonce = 0
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda _: pytest.fail("revert is terminal"))
    with pytest.raises(TransactionRecoveryRequired) as error:
        runner.apply_confirmed(identity)
    assert error.value.result["reason"] == "TRANSACTION_REVERTED"
    assert error.value.result["step"] == "leaf0"
    assert len(runner.w3.eth.sent) == 1


@pytest.mark.parametrize("error", [ReadTimeout("private-rpc-url"), ConnectionError("private-token"),
    ProviderConnectionError("private-url"), *[HTTPError("private-body", response=SimpleNamespace(status_code=code))
    for code in (429, 502, 503, 504)]])
def test_pending_transport_failure_resumes_exact_durable_transaction(tmp_path, monkeypatch, capsys, error):
    runner, identity = fixture(tmp_path, count=1)
    runner.profile["steps"][0].pop("parallel_group")
    runner.w3.eth.automine = False
    original = runner.apply
    attempts, waits = [], []
    def apply(value):
        attempts.append(value)
        if len(attempts) == 2:
            raise error
        return original(value)
    runner.apply = apply
    def progress(seconds):
        tx = next(iter(runner.w3.eth.transactions))
        waits.append(tx)
        if len(waits) == 2:
            runner.w3.eth.mine(tx)
            runner.w3.eth.block_number = 30
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", progress)
    assert runner.apply_confirmed(identity)["status"] == "VERIFIED"
    assert len(attempts) == 3 and attempts == [identity] * 3
    assert len(set(waits)) == 1
    assert len(runner.w3.eth.sent) == 1 and runner.prepared == ["leaf0"]
    output = capsys.readouterr().err
    assert '"error_code"' in output
    assert "private" not in output


def test_transport_timeout_returns_pending_with_original_hash(monkeypatch):
    now, calls = [0], []
    txid = "ab" * 32
    def apply(identity):
        calls.append(identity)
        if len(calls) == 1:
            return dict(status="BROADCAST", tx_hash=txid, step="send")
        raise ReadTimeout("private-provider-data")
    monkeypatch.setattr("signer_agent.transaction_wait.time.monotonic", lambda: now[0])
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(TransactionPending) as caught:
        apply_confirmed(SimpleNamespace(apply=apply), "same-identity", timeout=10, poll_interval=5)
    assert caught.value.result == dict(status="TRANSACTION_PENDING", step="send", tx_hash=txid,
        reason="RPC_UNAVAILABLE", message="The RPC endpoint could not confirm the journaled transaction. Retry with the same account and journal.")
    assert now[0] == 10 and len(calls) == 3


@pytest.mark.parametrize("error", [ReadTimeout("secret"), HTTPError("secret", response=SimpleNamespace(status_code=503))])
def test_transport_failure_before_a_known_send_does_not_retry(monkeypatch, error):
    calls = []
    def apply(identity):
        calls.append(identity)
        raise error
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda _: pytest.fail("must not wait"))
    with pytest.raises(type(error)) as caught:
        apply_confirmed(SimpleNamespace(apply=apply), "identity")
    assert caught.value is error and calls == ["identity"]


@pytest.mark.parametrize("error", [HTTPError("secret", response=SimpleNamespace(status_code=401)),
    HTTPError("secret", response=SimpleNamespace(status_code=403)), SSLError("certificate failure secret"),
    ValueError("TRANSACTION_REVERTED"), ValueError("EVM_REORG_RECOVERY_REQUIRED"), RuntimeError("unknown")])
def test_semantic_auth_and_unknown_errors_never_retry_after_pending(monkeypatch, error):
    calls = []
    def apply(identity):
        calls.append(identity)
        if len(calls) == 1:
            return dict(status="BROADCAST", tx_hash="ab" * 32)
        raise error
    sleeps = []
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", sleeps.append)
    with pytest.raises(type(error)) as caught:
        apply_confirmed(SimpleNamespace(apply=apply), "identity")
    assert caught.value is error and len(calls) == 2 and len(sleeps) == 1


def test_nonblocking_mode_never_retries_transient_errors(monkeypatch):
    def apply(identity):
        raise ReadTimeout("secret")
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda _: pytest.fail("flow cannot block"))
    with nonblocking_transactions(), pytest.raises(ReadTimeout):
        apply_confirmed(SimpleNamespace(apply=apply), "identity")


def test_new_unidentified_pending_stage_cannot_reuse_an_older_transaction_hash(monkeypatch):
    results = iter([dict(status="BROADCAST", tx_hash="ab" * 32),
                    dict(status="CONFIRMING", pending=["different-stage"])])
    def apply(identity):
        try:
            return next(results)
        except StopIteration:
            raise ReadTimeout("private-data") from None
    monkeypatch.setattr("signer_agent.transaction_wait.time.sleep", lambda _: None)
    with pytest.raises(ReadTimeout):
        apply_confirmed(SimpleNamespace(apply=apply), "identity")
