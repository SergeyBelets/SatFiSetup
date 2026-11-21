"""Diagnostic identifiers that never stringify exceptions carrying credentials."""

import argparse
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sys


_debug = ContextVar("cli_debug", default=False)
_PACKAGE = Path(__file__).resolve().parent


class DiagnosticArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if not _debug.get():
            super().error(message)
        _emit(dict(status="DEBUG_ERROR", code="INVALID_CLI_ARGUMENTS", frames=[]))
        self.exit(2)


def exception_code(exc):
    name = type(exc).__name__
    return (
        name
        if name.isascii() and name.isidentifier() and len(name) <= 64
        else "CLIENT_ERROR"
    )


# Text is selected locally. Exception arguments, RPC responses and CLI arguments
# must never become operator-facing messages.
_OPERATOR_GUIDANCE = {
    "CONFEDERATION_DAO_CANDIDATE": (
        "Адрес уже в составе или кандидат не соответствует условиям добавления.",
        "Сверьте исходную DAO-операцию, членство и статус кандидата на подтверждённом блоке."),
    "CONFEDERATION_DAO_BINDING": (
        "Цель операции и проверяемый DAO указывают на разные контракты авторизации.",
        "Сверьте адреса цели, DAO и DKGAuthorization в исходной сессии."),
    "CONFEDERATION_DAO_CUSTODIAN": (
        "Адрес отсутствует в составе, из которого запрошено удаление.",
        "Сверьте исходную сессию, операцию и актуальный состав кастодианов."),
    "ETHEREUM_TRANSACTION_GAS_LIMIT": (
        "Требуемый объём газа для этого вызова выходит за допустимый предел.",
        "Сверьте оценку pending с запасом и SATOSHIFI_MAX_TX_GAS до повторной отправки."),
    "ETHEREUM_TRANSACTION_BUDGET_REQUIRED": (
        "Отсутствует корректный локальный бюджет транзакции Ethereum.",
        "Проверьте локальные пределы газа, максимальной комиссии и приоритета."),
    "ETHEREUM_TRANSACTION_BUDGET_INVALID": (
        "Пределы расходов Ethereum несовместимы.",
        "Сверьте предел газа и убедитесь, что priority fee не превышает max fee."),
    "EVM_SIGNED_TRANSACTION_BINDING": (
        "Подписанная транзакция расходится с записью журнала или отправителем.",
        "Сверьте кошелёк, сеть и журнал исходной операции по хешу транзакции."),
    "EVM_REORG_RECOVERY_REQUIRED": (
        "Сохранённый блок или nonce расходится с каноническим состоянием Ethereum.",
        "Проверьте исходную квитанцию и блок finalized перед возобновлением операции."),
    "RECOVERY_REVERT_NOT_FINALIZED": (
        "Пока не подтверждены финальный откат и расход nonce исходной транзакции.",
        "Повторите сверку исходного хеша после финализации блока."),
    "ULR_CUSTODIAN_OPERATION_RECEIPT": (
        "Контракт не подтвердил указанный адрес и действие изменения состава.",
        "Сверьте исходную сессию, адрес участника и действие добавления или удаления."),
    "INVALID_PAYMENT_PROOF": (
        "Проверка доказательства включения исходного платежа в блок Bitcoin не прошла.",
        "Сверьте исходный txid, блок и доказательство перед btc-recover."),
    "BTC_BATCH_NOT_SIGNING": (
        "Пакет вывода сейчас не находится в состоянии подписи.",
        "Проверьте исходный идентификатор пакета и подтверждённое состояние escrow."),
    "ValueError": (
        "Значение аргумента или состояние операции не прошло проверку.",
        "Сверьте аргументы на указанном этапе. Для диагностики повторите чтение с --debug."),
    "PRIOR_TRANSACTION_RECOVERY_REQUIRED": (
        "Ранее сохранённая транзакция этого кошелька требует проверки.",
        "Сверьте её хеш, исходный вызов и каноническую квитанцию в том же журнале."),
    "OPERATION_PARAMETERS_MISMATCH": (
        "Параметры операции расходятся с подписываемым хешем.",
        "Сверьте сеть, целевой контракт, исходную сессию, аргументы и nonce."),
    "POLICY_MISMATCH": (
        "Данные сессии не совпали с проверяемой операцией.",
        "Сверьте цель, хеш и исходную сессию до подписи."),
    "BITCOIN_RPC_AUTH_REJECTED": (
        "Bitcoin RPC отклонил учётные данные.",
        "Проверьте локальный cookie и права доступа на узле, затем повторите исходный запрос."),
    "BITCOIN_OBSERVATION_UNAVAILABLE": (
        "Сейчас не удалось независимо проверить состояние Bitcoin.",
        "Повторите наблюдение через тот же проверенный узел и исходный журнал."),
    "BITCOIN_PAYMENT_REORG": (
        "Исходный платёж потерял требуемые подтверждения или каноничность.",
        "Сверьте прежний txid, batch и escrow перед проверяемым восстановлением."),
    "BITCOIN_RPC_TLS_FAILED": (
        "Защищённое соединение с Bitcoin RPC не прошло проверку.",
        "Проверьте сертификат, имя узла и настройки TLS."),
    "BITCOIN_RPC_RESPONSE_INVALID": (
        "Bitcoin RPC вернул неполные или противоречивые данные.",
        "Проверьте узел и повторите чтение того же платежа."),
    "PAYMENTS_PAUSED_FOR_RECOVERY": (
        "Платёж приостановлен до проверки исходной операции.",
        "Проверьте исходный batch, глубину и каноничность выплаты перед btc-recover."),
    "TRANSACTION_PENDING": (
        "Окончательная квитанция сохранённой EVM-транзакции пока не получена.",
        "Проверьте tx_hash и финальность через исходный журнал. Повтор использует те же подписанные байты."),
}
_DEFAULT_GUIDANCE = (
    "Команда завершилась ошибкой при обработке запроса.",
    "Сохраните код и диагностическую запись --debug для разбора. Перед повтором проверьте состояние исходной операции.",
)


def operator_error(code, stage, exc=None):
    from .evm_network import GasLimitExceeded

    message, next_action = _OPERATOR_GUIDANCE.get(code, _DEFAULT_GUIDANCE)
    result = dict(status="ERROR", code=code, stage=stage, message=message, next_action=next_action)
    if type(exc) is GasLimitExceeded:
        for key, value in (("required_gas", exc.required), ("max_tx_gas", exc.limit), ("estimated_gas", exc.estimate)):
            if type(value) is int and 0 < value < 2**256:
                result[key] = value
    return result


def pending_notice(result):
    message, next_action = _OPERATOR_GUIDANCE["TRANSACTION_PENDING"]
    return dict(result, message=message, next_action=next_action)


@contextmanager
def debug_diagnostics(enabled):
    token = _debug.set(enabled or _debug.get())
    try:
        yield
    finally:
        _debug.reset(token)


def _locations(trace):
    frames = deque(maxlen=12)
    while trace is not None:
        code = trace.tb_frame.f_code
        path = Path(code.co_filename)
        if path.is_relative_to(_PACKAGE) and path.suffix == ".py":
            parts = path.relative_to(_PACKAGE).with_suffix("").parts
            if all(part.isascii() and part.isidentifier() for part in parts):
                name = code.co_name
                frames.append(dict(
                    module=".".join(("signer_agent", *parts)),
                    function=name if name.isascii() and name.isidentifier() else "<module>",
                    line=trace.tb_lineno,
                ))
        trace = trace.tb_next
    return list(frames)


def _transport_codes(exc):
    from requests.exceptions import HTTPError
    from web3.exceptions import Web3RPCError
    from .bitcoin_rpc import BitcoinRPCError

    status = rpc_code = None
    if isinstance(exc, HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    elif type(exc) is BitcoinRPCError:
        status, rpc_code = exc.http_status, exc.rpc_code
    elif isinstance(exc, Web3RPCError):
        response = getattr(exc, "rpc_response", None)
        error = response.get("error") if type(response) is dict else None
        if type(error) is dict:
            rpc_code = error.get("code")
    result = {}
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    if type(rpc_code) is int and -40000 <= rpc_code < 0:
        result["rpc_error_code"] = rpc_code
    return result


def _emit(record):
    try:
        print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
    except OSError:
        # A failed diagnostic sink must not change the operation's result.
        pass


def emit_debug(exc, *, code=None):
    """Emit only error identifiers, SDK code locations and bounded numeric codes."""
    if not _debug.get():
        return
    record = dict(status="DEBUG_ERROR", code=code or exception_code(exc),
                  frames=_locations(exc.__traceback__), **_transport_codes(exc))
    seen = {id(exc)}
    causes = []
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen and len(causes) < 3:
        seen.add(id(cause))
        causes.append(dict(code=exception_code(cause), frames=_locations(cause.__traceback__),
                           **_transport_codes(cause)))
        cause = cause.__cause__ or cause.__context__
    if causes:
        record["causes"] = causes
    _emit(record)
