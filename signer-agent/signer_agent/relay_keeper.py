"""Permissionless public-Bitcoin relay keeper; no DKG or Bitcoin spending keys.

Only the gas-paying Ethereum identity can sign, through DurableRelayBatch's
shared nonce journal. Provider failures never clear that journal or choose a new
nonce. An explicit pinned relay/policy is checked against a fully validating Core.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import threading
import time

from requests.exceptions import ConnectionError, HTTPError, Timeout
from eth_account import Account
from web3 import Web3
from .evm_rpc import evm_web3

from .bitcoin_network import relay_artifact, relay_constructor_args, sync_relay, validate_policy
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError, observation_error
from .bitcoin_trust import checked_runtime
from .bootstrap import artifact
from .contract_provenance import prove_creation
from .evm_network import read_block
from .participant_errors import participant_exception_code
from .relay_batch import DurableRelayBatch
from .safe_errors import DiagnosticArgumentParser, emit_debug
from .secure_state import SecureState
from .verified_cache import cache_state


RETRY_CODES = frozenset((
    "BITCOIN_RPC_FAILED", "ETHEREUM_FINALITY_UNAVAILABLE", "EVM_CONFIRMATIONS_PENDING",
    "MAINNET_CORE_NOT_SYNCED", "MAINNET_TXINDEX_NOT_SYNCED", "MAINNET_TIP_STALE",
    "MAINNET_CORE_REORG_DURING_CHECK", "MAINNET_CORE_CHAINWORK_BEHIND",
    "TRANSACTION_UNCONFIRMED", "EVM_RPC_UNAVAILABLE", "BITCOIN_RPC_WARMUP",
    "BITCOIN_RPC_COOKIE_UNAVAILABLE",
))


def _provenance_state(identity_file):
    """Authenticate public proofs with a key separate from transaction signing.

    Only the domain-separated derived key reaches SecureState. The gas identity
    remains in its existing private file, and is never serialized into evidence.
    """
    from .onboarding import read_private

    path = Path(identity_file).expanduser()
    account = Account.from_key(json.loads(read_private(path))["private_key"])
    secret = hmac.new(bytes(account.key), b"satoshifi/relay-keeper/provenance/v1",
        hashlib.sha256).hexdigest()
    return SecureState(str(path.resolve()) + ".relay-provenance.enc", secret)


def error_code(error):
    if type(error) is BitcoinRPCError and error.rpc_code == -28:
        return "BITCOIN_RPC_WARMUP"
    if type(error) is BitcoinRPCError and error.http_status in (401, 403):
        return "BITCOIN_RPC_AUTH_REJECTED"
    if isinstance(error, (ConnectionError, Timeout)):
        return "EVM_RPC_UNAVAILABLE"
    if isinstance(error, HTTPError) and error.response is not None and error.response.status_code in (429, 502, 503, 504):
        return "EVM_RPC_UNAVAILABLE"
    if type(error) is ValueError and error.args in (("RELAY_RUNTIME_PIN_MISMATCH",), ("BITCOIN_RPC_COOKIE_UNAVAILABLE",)):
        return error.args[0]
    return participant_exception_code(error)


class RelayKeeper:
    def __init__(self, args):
        self.args = args
        self.policy = validate_policy(json.loads(Path(args.bitcoin_policy).read_text()))
        if self.policy["version"] != "1":
            raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
        self.w3 = evm_web3(args.rpc, timeout=30)
        self.relay = self.w3.eth.contract(address=Web3.to_checksum_address(args.relay),
            abi=artifact(relay_artifact(self.policy["network"]))["abi"])
        self.origin_anchor = None
        self.sender = None
        self.provenance_state = _provenance_state(args.identity_file)

    def tick(self):
        # Limit cache authority to this invocation, including when an RPC error
        # interrupts the tick. A fresh keeper process reads the same authenticated
        # evidence; prove_creation still checks canonical anchors/current code.
        token = cache_state.set(self.provenance_state)
        try:
            return self._tick()
        finally:
            cache_state.reset(token)

    def _tick(self):
        a, w3 = self.args, self.w3
        if w3.eth.chain_id != a.chain_id:
            raise ValueError("CHAIN_MISMATCH")
        block = read_block(w3, a.evm_confirmations)
        kind = relay_artifact(self.policy["network"])
        code_hash = checked_runtime(w3, self.relay.address, kind, block.number)
        if code_hash.removeprefix("0x") != a.relay_code_hash.removeprefix("0x").lower():
            raise ValueError("RELAY_RUNTIME_PIN_MISMATCH")
        if self.origin_anchor is None:
            prove_creation(w3, self.relay.address, kind, relay_constructor_args(self.policy), block.number,
                birth_hint=getattr(a, "relay_creation_block", None))
            self.origin_anchor = (block.number, bytes(block.hash))
        if bytes(w3.eth.get_block(self.origin_anchor[0]).hash) != self.origin_anchor[1]:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        if bytes(w3.eth.get_block(block.number).hash) != bytes(block.hash):
            raise ValueError("BTC_REGISTRY_READ_REORG")
        # Read the cookie afresh: Core rotates it on restart. Never copy it into
        # config, the journal, argv, or JSON diagnostics.
        try:
            bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie,
                network=self.policy["network"], policy=self.policy)
        except ValueError as error:
            if error.args == ("BITCOIN_RPC_COOKIE_INVALID",):
                try:
                    os.lstat(a.bitcoin_cookie)
                except FileNotFoundError:
                    raise ValueError("BITCOIN_RPC_COOKIE_UNAVAILABLE") from None
            raise
        if self.sender is None:
            self.sender = DurableRelayBatch.for_rpc(a.identity_file, w3, self.relay.address,
                self.policy, a.evm_confirmations)
        return sync_relay(bitcoin, self.relay, self.policy, None,
            send_headers=self.sender, evm_confirmations=a.evm_confirmations)


def run(keeper, *, interval, max_backoff, timeout, once, stop, emit, monotonic=time.monotonic):
    deadline = monotonic() + timeout if timeout else None
    failures = 0
    while not stop.is_set():
        try:
            result = keeper.tick()
            failures = 0
        except Exception as error:
            code = error_code(error)
            emit_debug(error, code=code)
            diagnostic = observation_error(error)
            metadata = {key: value for key, value in diagnostic.items() if key in ("rpc_error_code", "http_status")}
            if code not in RETRY_CODES:
                emit(dict(status="STOPPED", code=code, retryable=False, **metadata))
                return 78
            failures += 1
            result = dict(status="WAITING", code=code, retryable=True, **metadata)
        # Emit every poll, including caught-up health. Supervisors can detect a
        # silent/dead process; records contain no endpoints, keys or Core cookie.
        emit(result)
        if once:
            return 0 if result.get("status") == "MAINNET_RELAY_READY" else 2
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            emit(dict(status="TIMEOUT", retryable=True))
            return 2
        delay = min(max_backoff, interval * 2 ** min(failures, 10))
        stop.wait(delay if remaining is None else min(delay, remaining))
    emit(dict(status="STOPPED", reason="SIGNAL"))
    return 0


def main(argv=None):
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    p = DiagnosticArgumentParser(description=__doc__)
    p.add_argument("--rpc", default=os.environ.get("SATOSHIFI_KEEPER_EVM_RPC"))
    p.add_argument("--bitcoin-rpc", default=os.environ.get("SATOSHIFI_KEEPER_BITCOIN_RPC"))
    p.add_argument("--bitcoin-cookie", required=True)
    p.add_argument("--bitcoin-policy", required=True)
    p.add_argument("--relay", required=True)
    p.add_argument("--relay-code-hash", required=True, help="keccak256 of the verified deployed runtime")
    p.add_argument("--relay-creation-block", type=int,
        help="optional deployment block locator; constructor and receipt are still verified")
    p.add_argument("--chain-id", type=int, choices=(1, 11155111, 31337), required=True)
    p.add_argument("--evm-confirmations", type=int, required=True)
    p.add_argument("--identity-file", required=True, help="separate gas-paying EVM identity; no DKG shares")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--max-backoff", type=int, default=300)
    p.add_argument("--timeout", type=int, default=0, help="0 means keep running until SIGINT/SIGTERM")
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    if not a.rpc or not a.bitcoin_rpc:
        p.error("RPC endpoints are required as flags or SATOSHIFI_KEEPER_*_RPC environment variables")
    if not 1 <= a.interval <= a.max_backoff <= 3600 or a.timeout < 0:
        p.error("require 1 <= interval <= max-backoff <= 3600 and timeout >= 0")
    if a.relay_creation_block is not None and a.relay_creation_block < 1:
        p.error("relay-creation-block must be positive")
    try:
        if len(bytes.fromhex(a.relay_code_hash.removeprefix("0x"))) != 32:
            raise ValueError()
    except ValueError:
        p.error("relay-code-hash must contain exactly 32 hex bytes")
    stop = threading.Event()
    prior = {kind: signal.signal(kind, lambda *_: stop.set()) for kind in (signal.SIGINT, signal.SIGTERM)}
    emit = lambda value: print(json.dumps(value, sort_keys=True), flush=True)
    try:
        keeper = RelayKeeper(a)
        return run(keeper, interval=a.interval, max_backoff=a.max_backoff,
            timeout=a.timeout, once=a.once, stop=stop, emit=emit)
    except Exception as error:
        code = error_code(error)
        emit_debug(error, code=code)
        emit(dict(status="STOPPED", code=code, retryable=False))
        return 78
    finally:
        for kind, previous in prior.items():
            signal.signal(kind, previous)


if __name__ == "__main__":
    raise SystemExit(main())
