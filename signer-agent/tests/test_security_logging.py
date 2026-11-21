"""Synthetic credentials must never reach signer console output or logs."""

from types import SimpleNamespace
from unittest.mock import Mock
from dataclasses import fields
import json
import logging

import pytest
from requests.exceptions import HTTPError

from signer_agent import bootstrap, bootstrap_create, cli, dao_cli, launch_configurator


SENSITIVE = "https://synthetic-user:synthetic-password@rpc.invalid/v3/synthetic-token"


@pytest.fixture(autouse=True)
def restore_library_logging():
    names = ("urllib3", "requests", "web3", "aiohttp", "httpx", "httpcore")
    previous = [
        (
            logging.getLogger(name),
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
        )
        for name in names
    ]
    yield
    for logger, handlers, propagate in previous:
        logger.handlers = handlers
        logger.propagate = propagate


def fail(*args, **kwargs):
    raise ValueError(SENSITIVE)


def assert_operator_error(output, expected):
    result = json.loads(output)
    assert result["status"] == "ERROR" and result["code"] == expected
    assert result["stage"] and result["message"] and result["next_action"]


def assert_redacted(capsys, caplog):
    capture = capsys.readouterr()
    output = capture.out + capture.err + caplog.text
    assert "ValueError" in output
    assert "synthetic" not in output
    assert "Traceback" not in output
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("stage", ["parse", "construct"])
def test_configuration_errors_do_not_print_credentials(
    monkeypatch, capsys, caplog, stage
):
    monkeypatch.setattr(
        cli.Config, "from_env", fail if stage == "parse" else lambda: object()
    )
    monkeypatch.setattr(cli, "SigningService", fail)
    assert cli.main(["doctor"]) == 2
    assert_redacted(capsys, caplog)


@pytest.mark.parametrize(
    "arguments",
    [
        ["pending"],
        ["decide", "1", "approve"],
        ["watch"],
        ["relay", "1", "authorization", "target"],
        ["finalize", "1"],
    ],
)
def test_all_legacy_command_boundaries_redact_rpc_errors(
    monkeypatch, capsys, caplog, arguments
):
    service = SimpleNamespace(
        pending=fail, client=SimpleNamespace(relay_authorization=fail, session=fail)
    )
    monkeypatch.setattr(
        cli.Config, "from_env", lambda: SimpleNamespace(poll_interval=1, auto_sign=True)
    )
    monkeypatch.setattr(cli, "SigningService", lambda _: service)
    assert cli.main(arguments) in (1, 2)
    assert_redacted(capsys, caplog)


def test_session_failure_never_logs_exception_traceback(monkeypatch, capsys, caplog):
    session = SimpleNamespace(session_id=1)
    service = SimpleNamespace(pending=lambda _: [session], approve=fail)
    monkeypatch.setattr(cli, "_show", lambda *args: None)
    assert cli.run_once(service, approve_all=True) == 1
    assert_redacted(capsys, caplog)


def test_decision_failure_is_caught_after_session_lookup(monkeypatch, capsys, caplog):
    service = SimpleNamespace(pending=lambda _: [object()], approve=fail)
    monkeypatch.setattr(cli.Config, "from_env", lambda: object())
    monkeypatch.setattr(cli, "SigningService", lambda _: service)
    assert cli.main(["decide", "1", "approve"]) == 2
    assert_redacted(capsys, caplog)


def test_debug_does_not_enable_http_transport_logging(monkeypatch):
    monkeypatch.setattr(cli.Config, "from_env", fail)
    configure = Mock()
    monkeypatch.setattr(cli.logging, "basicConfig", configure)
    assert cli.main(["--log-level", "DEBUG", "doctor"]) == 2
    assert configure.call_args.kwargs["level"] == cli.logging.WARNING


@pytest.mark.parametrize("level", [logging.DEBUG, logging.WARNING])
@pytest.mark.parametrize("debug", [[], ["--debug"]])
def test_transport_retry_messages_are_suppressed_before_dispatch(
    monkeypatch, capsys, caplog, level, debug
):
    def command(_):
        # A child logger with an explicit DEBUG level must not bypass suppression.
        logger = logging.getLogger("urllib3.connectionpool")
        with monkeypatch.context() as patch:
            patch.setattr(logger, "level", logging.DEBUG)
            logger.log(level, "retrying %s", SENSITIVE)
        raise ValueError(SENSITIVE)

    monkeypatch.setattr(cli, "_main", command)
    assert cli.main([*debug, "pending"]) == 2
    assert_redacted(capsys, caplog)


@pytest.mark.parametrize("rpc_code, expected", [
    (-38013, "CREATE_SIMULATION_REJECTED"),
    (-32601, "CREATE_SIMULATION_RPC_REQUIRED"),
    (SENSITIVE, "CREATE_SIMULATION_REJECTED"),
])
def test_bootstrap_debug_preserves_only_numeric_rpc_error(
    monkeypatch, tmp_path, capsys, rpc_code, expected
):
    profile = tmp_path / "synthetic-private-profile.json"
    profile.write_text('{"steps": []}')
    provider = Mock()
    provider.make_request.return_value = {
        "error": {"code": rpc_code, "message": SENSITIVE, "data": "synthetic-signed-transaction"}
    }

    def simulate(*args, **kwargs):
        bootstrap_create.simulate_group(
            SimpleNamespace(provider=provider), [], SimpleNamespace(number=10)
        )

    monkeypatch.setattr(bootstrap, "Bootstrap", simulate)
    argv = ["bootstrap", "verify", "--profile", str(profile)]
    assert cli.main([*argv, "--debug"]) == 2
    capture = capsys.readouterr()
    assert_operator_error(capture.out, expected)
    record = json.loads(capture.err)
    assert record["status"] == "DEBUG_ERROR"
    assert record["code"] == expected
    assert any(
        frame["module"] == "signer_agent.bootstrap_create" and frame["function"] == "simulate_group"
        for frame in record["frames"]
    )
    assert all(set(frame) == {"module", "function", "line"} for frame in record["frames"])
    if type(rpc_code) is int:
        assert record["causes"] == [{"code": "Web3RPCError", "frames": [], "rpc_error_code": rpc_code}]
    else:
        assert "causes" not in record
    assert "synthetic" not in capture.out + capture.err

    assert cli.main(argv) == 2
    ordinary = capsys.readouterr()
    assert ordinary.out == capture.out
    assert ordinary.err == ""


def test_flow_debug_keeps_inner_frames_and_http_status(monkeypatch, tmp_path, capsys):
    profile = tmp_path / "synthetic-profile.json"
    profile.write_text('{"steps": []}')

    def failed_provider(*args, **kwargs):
        try:
            raise HTTPError(SENSITIVE, response=SimpleNamespace(status_code=503, text=SENSITIVE))
        except HTTPError:
            raise ValueError("CREATE_SIMULATION_REJECTED") from None

    monkeypatch.setattr(bootstrap, "Bootstrap", failed_provider)
    assert cli.main([
        "--debug", "flow", "bootstrap", "--once", "--profile", str(profile),
        "--state", str(tmp_path / "synthetic-state.sqlite"), "--identity-file", SENSITIVE,
    ]) == 2
    capture = capsys.readouterr()
    assert_operator_error(capture.out, "CREATE_SIMULATION_REJECTED")
    records = [json.loads(line) for line in capture.err.splitlines()]
    inner = next(record for record in records if "causes" in record)
    assert inner["causes"] == [{"code": "HTTPError", "frames": [], "http_status": 503}]
    assert any(frame["module"] == "signer_agent.bootstrap" for frame in inner["frames"])
    assert "synthetic" not in capture.out + capture.err


@pytest.mark.parametrize("command", [["bootstrap"], ["bitcoin", "deposit-commitment"], ["dao"]])
def test_debug_parser_does_not_echo_invalid_argument(command, capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(["--debug", *command, SENSITIVE])
    assert failure.value.code == 2
    capture = capsys.readouterr()
    assert capture.out == ""
    assert json.loads(capture.err) == {
        "status": "DEBUG_ERROR", "code": "INVALID_CLI_ARGUMENTS", "frames": [],
    }


@pytest.mark.parametrize("module, target, argv, secret", [
    (dao_cli, "run", ["dao", "status"], "SYNTHETIC_ACCESS_TOKEN"),
    (launch_configurator, "_main", ["configure", "validate"], "LAUNCH_SYNTHETIC_ACCESS_TOKEN"),
])
def test_debug_only_emits_allowlisted_application_codes(monkeypatch, capsys, module, target, argv, secret):
    def unknown_code(*args):
        raise ValueError(secret)

    monkeypatch.setattr(module, target, unknown_code)
    assert cli.main(["--debug", *argv]) == 2
    capture = capsys.readouterr()
    assert_operator_error(capture.out, "ValueError")
    assert json.loads(capture.err)["code"] == "ValueError"
    assert secret not in capture.out + capture.err


def test_unwritable_debug_stream_preserves_command_result(monkeypatch, capsys):
    def write(_):
        raise BrokenPipeError()

    monkeypatch.setattr(cli, "_main", fail)
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys, "stderr", SimpleNamespace(write=write))
        assert cli.main(["--debug", "pending"]) == 2
    assert_operator_error(capsys.readouterr().out, "ValueError")


def test_configuration_repr_excludes_all_secret_bearing_fields():
    values = {f.name: "public-fixture" for f in fields(cli.Config)}
    for name in ("rpc_url", "private_key", "signing_share_hex", "state_key"):
        values[name] = SENSITIVE
    assert "synthetic" not in repr(cli.Config(**values))
