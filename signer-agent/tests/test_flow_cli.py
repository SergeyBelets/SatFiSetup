"""Polling must never turn pending state into permission or hide terminal errors."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from signer_agent import flow_cli as flow


def enrolled(tmp_path):
    for role in flow.ROLES:
        (tmp_path / (role + ".enc")).write_bytes(b"encrypted")
    return SimpleNamespace(directory=str(tmp_path), board="board", rpc="rpc",
                           identity_file="identity", bitcoin_rpc=None, bitcoin_cookie=None)


def test_one_pending_role_does_not_starve_the_other_ceremonies(tmp_path, monkeypatch):
    a = enrolled(tmp_path)
    calls = []
    def invoke(group, args):
        calls.append((group, args))
        if group == "setup":
            return dict(ceremonies={r: dict(status="IN_PROGRESS") for r in flow.ROLES})
        role = args[args.index("--role") + 1]
        return dict(status="WAITING" if role == flow.ROLES[0] else "ACTIVE")
    monkeypatch.setattr(flow, "invoke", invoke)
    result, complete = flow.dkg_tick(a)
    assert not complete and len(calls) == 4
    assert result["ceremonies"][flow.ROLES[-1]]["status"] == "ACTIVE"
    assert len({str(args[args.index("--state")+1]) for group, args in calls if group == "dkg"}) == 3
    assert all(args[0] == "run-once" for group, args in calls if group == "dkg")


@pytest.mark.parametrize("phase", ["FAILED", "EXPIRED"])
def test_terminal_ceremony_stops_instead_of_restarting(tmp_path, monkeypatch, phase):
    a = enrolled(tmp_path)
    monkeypatch.setattr(flow, "invoke", lambda *args: dict(ceremonies={r: dict(status=phase) for r in flow.ROLES}))
    with pytest.raises(ValueError, match="CEREMONY_TERMINAL_FAILURE"):
        flow.dkg_tick(a)


def test_missing_enrollment_does_not_create_state(tmp_path, monkeypatch):
    a = enrolled(tmp_path)
    (tmp_path / (flow.ROLES[0] + ".enc")).unlink()
    monkeypatch.setattr(flow, "invoke", lambda *args: dict(ceremonies={r: dict(status="IN_PROGRESS") for r in flow.ROLES}))
    with pytest.raises(ValueError, match="FLOW_ENROLLED_STATE_REQUIRED"):
        flow.dkg_tick(a)


def test_signer_progress_is_bound_to_explicit_batch_and_board(monkeypatch):
    a = SimpleNamespace(actor="signer", board="invitation", rpc="rpc", identity_file="identity",
        state="key", password_file="password", signing_board="signing", batch="01"*32,
        bitcoin_rpc="btc", bitcoin_cookie="cookie")
    calls = []
    def invoke(group, args):
        calls.append((group, args))
        return dict(status="WAITING" if len(calls) == 1 else "COMMITTED" if len(calls) == 2 else "SIGNED")
    monkeypatch.setattr(flow, "invoke", invoke)
    _, phase, done = flow.payment_tick(a, 0)
    assert phase == 0 and not done
    _, phase, done = flow.payment_tick(a, phase)
    assert phase == 1 and not done
    _, phase, done = flow.payment_tick(a, phase)
    assert phase == 2 and done
    assert [args[0] for _, args in calls] == ["btc-commit", "btc-commit", "btc-sign"]
    assert all(args[args.index("--batch")+1] == a.batch and args[args.index("--signing-board")+1] == a.signing_board for _, args in calls)


@pytest.mark.parametrize("code,pending", [("BTC_TRANSACTION_UNCONFIRMED", True),
    ("EVM_REORG_RECOVERY_REQUIRED", False), ("NONCE_STATE_ROLLBACK_RECOVERY_REQUIRED", False),
    ("ETHEREUM_FINALITY_UNAVAILABLE", False), ("SECRET_CREDENTIAL", False)])
def test_only_enumerated_pending_errors_retry_without_leaking(monkeypatch, code, pending):
    from signer_agent import cli
    def main(args):
        print(json.dumps(dict(status="ERROR", code=code)))
        return 2
    monkeypatch.setattr(cli, "main", main)
    if pending:
        assert flow.invoke("dkg", ["run-once"])["status"] == "WAITING"
    else:
        with pytest.raises(ValueError) as error:
            flow.invoke("dkg", ["run-once"])
        if code == "SECRET_CREDENTIAL":
            assert code not in str(error.value)


def test_timeout_is_pending_not_success_and_never_signs_intents(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(flow, "invoke", lambda group, args: calls.append((group, args)) or dict(status="AWAITING_PARTICIPANTS"))
    moments = iter([100, 101])
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(moments))
    assert flow.main(["bootstrap", "--profile", "public.json", "--state", "journal", "--identity-file", "identity", "--timeout", "1"]) == 2
    assert calls == [("bootstrap", ["resume", "--profile", "public.json", "--state", "journal", "--identity-file", "identity"])]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "TIMEOUT"


def test_operator_never_restarts_nonce_generation(monkeypatch):
    a = SimpleNamespace(actor="operator", board="board", rpc="rpc", identity_file="identity", state="state",
        password_file="password", signing_board="signing", batch="02"*32, bitcoin_rpc="btc", bitcoin_cookie="cookie")
    calls = []
    def invoke(group, args):
        calls.append(args[0])
        return dict(status="BROADCAST" if len(calls) == 1 else "BTC_PAYMENT_SETTLED")
    monkeypatch.setattr(flow, "invoke", invoke)
    _, phase, done = flow.payment_tick(a, 0)
    assert not done
    _, phase, done = flow.payment_tick(a, phase)
    assert done and calls == ["btc-broadcast", "btc-settle"]


def test_operator_waits_for_first_block_depth_and_settlement(monkeypatch, capsys):
    from signer_agent import cli
    actions = []
    responses = iter([dict(status="BROADCAST"),
        dict(status="ERROR", code="PAYMENT_NOT_CONFIRMED"),
        dict(status="ERROR", code="PAYMENT_DEPTH"), dict(status="BTC_PAYMENT_SETTLED")])
    def cli_main(args):
        actions.append(args[1])
        result = next(responses)
        print(json.dumps(result))
        return 2 if result["status"] == "ERROR" else 0
    monkeypatch.setattr(cli, "main", cli_main)
    monkeypatch.setattr(flow.time, "sleep", lambda _: None)
    assert flow.main(["payment", "--actor", "operator", "--board", "board", "--rpc", "rpc",
        "--identity-file", "identity", "--state", "state", "--password-file", "password",
        "--signing-board", "signing", "--batch", "01"*32,
        "--bitcoin-rpc", "btc", "--bitcoin-cookie", "cookie"]) == 0
    assert actions == ["btc-broadcast", "btc-settle", "btc-settle", "btc-settle"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "BTC_PAYMENT_SETTLED"


@pytest.mark.parametrize("retry,code", [(True, "BITCOIN_RPC_FAILED"),
    (False, "BITCOIN_RPC_COOKIE_INVALID")])
def test_payment_flow_repeats_only_retryable_observations(monkeypatch, retry, code):
    args = SimpleNamespace(actor="operator", board="board", rpc="rpc", identity_file="operator",
                           state="s", password_file="pw", signing_board="sb", batch="12"*32,
                           bitcoin_rpc="btc", bitcoin_cookie="cookie")
    pending = dict(status="OBSERVATION_UNAVAILABLE", error_code=code, retryable=retry)
    monkeypatch.setattr(flow, "invoke", lambda *a: pending)
    if retry:
        assert flow.payment_tick(args, 1) == (pending, 1, False)
    else:
        with pytest.raises(ValueError, match=code):
            flow.payment_tick(args, 1)


def test_operator_restart_completes_from_confirmed_settlement_at_broadcast(monkeypatch):
    args = SimpleNamespace(actor="operator", board="board", rpc="rpc", identity_file="operator",
                           state="s", password_file="pw", signing_board="sb", batch="12"*32,
                           bitcoin_rpc="btc", bitcoin_cookie="cookie")
    paid = dict(status="PAID", bitcoin_observation="CONFIRMED")
    monkeypatch.setattr(flow, "invoke", lambda *a: paid)
    assert flow.payment_tick(args, 0) == (paid, 2, True)
    paid["bitcoin_observation"] = "NOT_CHECKED"
    assert flow.payment_tick(args, 0) == (paid, 0, False)
