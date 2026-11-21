import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from eth_utils import to_checksum_address
from web3 import Web3
from web3.exceptions import ContractLogicError

from .config import Config
from .frost_math import Point


SESSION_NONE = 0
SESSION_OPENED = 1
SESSION_FINALIZED = 2
SESSION_ABORTED = 3
PURPOSE_DKG = 6

FROST_AUTHORIZATION_ABI = [
    {
        "type": "function",
        "name": "coordinator",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "groupPubkey",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes"}],
    },
    {
        "type": "function",
        "name": "operationTargets",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "authorizeOperation",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "sessionId", "type": "uint256"},
            {"name": "operationHash", "type": "bytes32"},
            {"name": "target", "type": "address"},
        ],
        "outputs": [],
    },
]


@dataclass(frozen=True)
class Session:
    session_id: int
    creator: str
    group_pubkey: bytes
    message_hash: bytes
    message_bound: bool
    threshold: int
    total: int
    deadline: int
    enforce_shares_check: bool
    verifier_override: str
    state: int
    commits_count: int
    shares_count: int
    refusal_count: int
    purpose: int
    origin_contract: str
    origin_id: int
    network_id: int
    pool_id: bytes
    dkg_shares_count: int

    @classmethod
    def from_contract(cls, values: Sequence[Any]) -> "Session":
        return cls(
            session_id=int(values[0]),
            creator=to_checksum_address(values[1]),
            group_pubkey=bytes(values[2]),
            message_hash=bytes(values[3]),
            message_bound=bool(values[4]),
            threshold=int(values[5]),
            total=int(values[6]),
            deadline=int(values[7]),
            enforce_shares_check=bool(values[8]),
            verifier_override=to_checksum_address(values[9]),
            state=int(values[10]),
            commits_count=int(values[11]),
            shares_count=int(values[12]),
            refusal_count=int(values[13]),
            purpose=int(values[14]),
            origin_contract=to_checksum_address(values[15]),
            origin_id=int(values[16]),
            network_id=int(values[17]),
            pool_id=bytes(values[18]),
            dkg_shares_count=int(values[19]),
        )


class CoordinatorClient:
    REQUIRED_FUNCTIONS = {
        "getSession",
        "getSessionParticipants",
        "hasCommitted",
        "hasSubmittedShare",
        "hasRefused",
        "getParticipantPublicKeyShare",
        "getNonceCommitmentPoints",
        "getVerifiedShareStatus",
        "getNonceSetStatus",
        "nextSessionId",
        "getResult",
        "submitNonceCommitments",
        "submitVerifiableSignatureShare",
        "rejectSignatureRequest",
        "finalizeSession",
    }

    def __init__(self, config: Config):
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(config.rpc_url, request_kwargs={"timeout": 30}))
        if not self.w3.is_connected():
            raise RuntimeError("cannot connect to EVM RPC")
        self.sender = to_checksum_address(config.sender)
        key_address = to_checksum_address(self.w3.eth.account.from_key(config.private_key).address)
        if key_address != self.sender:
            raise ValueError("EVM_PRIVATE_KEY does not belong to EVM_SENDER")
        self.address = to_checksum_address(config.coordinator_address)
        raw_abi = json.loads(Path(config.coordinator_abi_path).expanduser().read_text(encoding="utf-8"))
        abi = raw_abi["abi"] if isinstance(raw_abi, dict) and "abi" in raw_abi else raw_abi
        self._validate_abi(abi)
        self.contract = self.w3.eth.contract(address=self.address, abi=abi)
        if not self.w3.eth.get_code(self.address):
            raise RuntimeError("FROST coordinator address has no runtime code")
        try:
            self._read_call(self.contract.functions.nextSessionId())
        except Exception as exc:
            raise RuntimeError("FROST coordinator ABI/address mismatch") from exc

    def _validate_abi(self, abi: Sequence[Dict[str, Any]]) -> None:
        names = {entry.get("name") for entry in abi if entry.get("type") == "function"}
        missing = sorted(self.REQUIRED_FUNCTIONS - names)
        if missing:
            raise ValueError("FROST ABI is outdated; missing: %s" % ", ".join(missing))

    @property
    def chain_id(self) -> int:
        return int(self.w3.eth.chain_id)

    def _confirmation_graph(self):
        graph = dict(getattr(self, "bitcoin_graph", {}))
        local = int(getattr(getattr(self, "config", None), "confirmations", 1))
        if local > int(graph.get("evm_confirmations", "1")):
            graph["evm_confirmations"] = str(local)
        return graph

    def pin_read_snapshot(self):
        """Use the strongest enrolled/local depth and mainnet finality for one round."""
        from .bitcoin_trust import confirmed_block

        graph = self._confirmation_graph()
        self._read_snapshot = confirmed_block(self.w3, graph) if (
            self.w3.eth.chain_id == 1 or "evm_confirmations" in graph) else None
        self.check_read_snapshot()

    def check_read_snapshot(self):
        block = getattr(self, "_read_snapshot", None)
        if block is not None and self.w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")

    def _read_call(self, call):
        block = getattr(self, "_read_snapshot", None)
        return call.call(block_identifier=block.number) if block is not None else call.call()

    def guard_mint_session(self, session):
        from .mint_admission import guard_signing_session
        guard_signing_session(self, session)

    def guard_verifier(self, session):
        """Authenticate the actual coordinator/verifier before private work.

        ABI compatibility and a nonempty runtime are insufficient. A session
        override is never part of the supported signing authorization path.
        """
        from .bootstrap import artifact
        from .bitcoin_trust import checked_runtime, confirmed_block
        from .contract_provenance import prove_creation

        if session.verifier_override != "0x" + "00" * 20:
            raise ValueError("FROST_CANONICAL_VERIFIER_REQUIRED")
        initial = getattr(self, "_read_snapshot", None)
        if initial is None:
            initial = confirmed_block(self.w3, self._confirmation_graph())
        coordinator = self.w3.eth.contract(address=self.address, abi=artifact("FROSTCoordinator")["abi"])
        expected = None
        for block in (initial, self.w3.eth.get_block("latest")):
            checked_runtime(self.w3, self.address, "FROSTCoordinator", block.number)
            verifier = coordinator.functions.defaultVerifier().call(block_identifier=block.number)
            checked_runtime(self.w3, verifier, "FROSTVerifier", block.number)
            prove_creation(self.w3, verifier, "FROSTVerifier", [], block.number)
            prove_creation(self.w3, self.address, "FROSTCoordinator", [verifier], block.number)
            if expected is not None and verifier != expected:
                raise ValueError("FROST_CANONICAL_VERIFIER_REQUIRED")
            expected = verifier
            if self.w3.eth.get_block(block.number).hash != block.hash:
                raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
        self.check_read_snapshot()

    def guard_gas_session(self, session):
        from .gas_governance import guard_signing_session
        guard_signing_session(self, session)

    def guard_bitcoin_session(self, session):
        review = getattr(self, "bitcoin_plan_review", None)
        if review is not None:
            expected, callback = review
            if session.message_hash.hex() != expected:
                raise ValueError("BTC_PUBLIC_PLAN_OPERATION_MISMATCH")
            callback()

    def guard_address_registration_session(self, session):
        from .address_registration import guard_signing_session
        guard_signing_session(self, session)

    def guard_btc_fee_session(self, session):
        from .btc_fee_policy import guard_signing_session
        guard_signing_session(self, session)

    def guard_withdrawal_fee_session(self, session):
        from .withdrawal_fee_policy import guard_signing_session
        guard_signing_session(self, session)

    def guard_token_metadata_session(self, session):
        from .bootstrap_tokens import guard_signing_session
        guard_signing_session(self, session)

    def guard_custody_session(self, session):
        from .bootstrap_custody import guard_signing_session
        guard_signing_session(self, session)

    def guard_ulr_treasury_session(self, session):
        from .ulr_treasury import guard_signing_session
        guard_signing_session(self, session)

    def guard_nft_session(self, session):
        from .nft_governance import guard_signing_session
        guard_signing_session(self, session)

    def session(self, session_id: int) -> Session:
        return Session.from_contract(self._read_call(self.contract.functions.getSession(session_id)))

    def participants(self, session_id: int) -> List[str]:
        return [to_checksum_address(value) for value in self._read_call(self.contract.functions.getSessionParticipants(session_id))]

    def is_participant(self, session_id: int, address: Optional[str] = None) -> bool:
        participant = to_checksum_address(address or self.sender)
        return participant in self.participants(session_id)

    def has_committed(self, session_id: int, address: Optional[str] = None) -> bool:
        return bool(self._read_call(self.contract.functions.hasCommitted(session_id, address or self.sender)))

    def has_submitted_share(self, session_id: int, address: Optional[str] = None) -> bool:
        return bool(self._read_call(self.contract.functions.hasSubmittedShare(session_id, address or self.sender)))

    def has_refused(self, session_id: int, address: Optional[str] = None) -> bool:
        return bool(self._read_call(self.contract.functions.hasRefused(session_id, address or self.sender)))

    def public_key_share(self, session_id: int, address: Optional[str] = None) -> Tuple[Point, bool]:
        x, y, registered = self._read_call(self.contract.functions.getParticipantPublicKeyShare(
            session_id, address or self.sender
        ))
        return Point(int(x), int(y)), bool(registered)

    def nonce_commitment(self, session_id: int, address: str) -> Optional[Tuple[Point, Point]]:
        d_x, d_y, e_x, e_y, submitted = self._read_call(self.contract.functions.getNonceCommitmentPoints(
            session_id, address
        ))
        if not submitted:
            return None
        return Point(int(d_x), int(d_y)), Point(int(e_x), int(e_y))

    def all_nonce_commitments(
        self, session_id: int, participants: Iterable[str]
    ) -> Dict[str, Tuple[Point, Point]]:
        result: Dict[str, Tuple[Point, Point]] = {}
        for participant in participants:
            commitment = self.nonce_commitment(session_id, participant)
            if commitment is not None:
                result[to_checksum_address(participant)] = commitment
        return result

    def verified_share(self, session_id: int, address: str) -> Optional[int]:
        verified, share = self._read_call(self.contract.functions.getVerifiedShareStatus(session_id, address))
        return int(share) if verified else None

    def nonce_set_status(self, session_id: int) -> Tuple[bool, bytes, Point, int]:
        locked, transcript, aggregate_x, aggregate_y, verified_count = self._read_call(self.contract.functions.getNonceSetStatus(
            session_id
        ))
        return bool(locked), bytes(transcript), Point(int(aggregate_x), int(aggregate_y)), int(verified_count)

    def pending_sessions(self, session_id: Optional[int] = None) -> List[Session]:
        session_ids = [session_id] if session_id is not None else self._discover_session_ids()
        now = int(time.time())
        pending: List[Session] = []
        for current_id in session_ids:
            current = self.session(int(current_id))
            if current.state != SESSION_OPENED or current.deadline < now:
                continue
            if not self.is_participant(current.session_id):
                continue
            if self.has_refused(current.session_id):
                continue
            pending.append(current)
        return sorted(pending, key=lambda item: item.session_id)

    def _discover_session_ids(self) -> List[int]:
        latest = self.w3.eth.block_number - self.config.confirmations + 1
        if latest < self.config.deployment_block:
            return []
        result = set()
        start = self.config.deployment_block
        while start <= latest:
            end = min(start + self.config.scan_chunk_size - 1, latest)
            event = self.contract.events.SessionCreated
            try:
                entries = event.get_logs(from_block=start, to_block=end)
            except TypeError:
                entries = event.get_logs(fromBlock=start, toBlock=end)
            result.update(int(entry["args"]["sessionId"]) for entry in entries)
            start = end + 1
        return sorted(result)

    def submit_nonce_commitments(self, session_id: int, hiding: Point, binding: Point) -> str:
        return self._send(
            self.contract.functions.submitNonceCommitments(
                session_id, hiding.x, hiding.y, binding.x, binding.y
            )
        )

    def submit_signature_share(self, session_id: int, share: int) -> str:
        return self._send(self.contract.functions.submitVerifiableSignatureShare(session_id, share))

    def reject(self, session_id: int, reason: str) -> str:
        return self._send(self.contract.functions.rejectSignatureRequest(session_id, reason))

    def finalize(self, session_id: int, signature: bytes, message_hash: bytes) -> Optional[str]:
        def finalized():
            latest = Session.from_contract(self.contract.functions.getSession(session_id).call())
            return latest.state == SESSION_FINALIZED and latest.message_hash == message_hash

        from .finalize_recovery import reconcile_session
        from .transaction_wait import TransactionRecoveryRequired
        if finalized():
            reconcile_session(self, session_id, signature, message_hash, terminal=True)
            return None
        call = self.contract.functions.finalizeSession(session_id, signature, message_hash)
        try:
            call.call({"from": self.sender})
        except ContractLogicError:
            if finalized():
                reconcile_session(self, session_id, signature, message_hash, terminal=True)
                return None
            raise
        try:
            return self._send(call)
        except ContractLogicError:
            if finalized():
                reconcile_session(self, session_id, signature, message_hash, terminal=True)
                return None
            raise
        except TransactionRecoveryRequired:
            if reconcile_session(self, session_id, signature, message_hash):
                return None
            raise

    def result(self, session_id: int) -> Tuple[bytes, bytes]:
        signature, message_hash = self._read_call(self.contract.functions.getResult(session_id))
        return bytes(signature), bytes(message_hash)

    def relay_authorization(self, session_id: int, authorization_address: str, target: str) -> str:
        session = self.session(session_id)
        if session.state != SESSION_FINALIZED:
            raise ValueError("session is not finalized")
        _, operation_hash = self.result(session_id)
        authorization_address = to_checksum_address(authorization_address)
        target = to_checksum_address(target)
        zero = to_checksum_address("0x0000000000000000000000000000000000000000")
        if not session.enforce_shares_check:
            raise ValueError("authorization requires an enforced coordinator session")
        if session.verifier_override != zero:
            raise ValueError("authorization rejects per-session verifier overrides")
        if session.origin_contract != target:
            raise ValueError("session origin contract and requested target differ")
        if not self.w3.eth.get_code(authorization_address):
            raise ValueError("FROSTAuthorization address has no runtime code")

        authorization = self.w3.eth.contract(address=authorization_address, abi=FROST_AUTHORIZATION_ABI)
        configured_coordinator = to_checksum_address(self._read_call(authorization.functions.coordinator()))
        if configured_coordinator != self.address:
            raise ValueError("authorization is bound to another coordinator")
        if bytes(self._read_call(authorization.functions.groupPubkey())) != session.group_pubkey:
            raise ValueError("coordinator session and authorization group keys differ")

        existing_target = to_checksum_address(self._read_call(authorization.functions.operationTargets(operation_hash)))
        if existing_target not in {zero, target}:
            raise ValueError("operation hash is already bound to another target")
        return self._send(
            authorization.functions.authorizeOperation(session_id, operation_hash, target)
        )

    def _send(self, call: Any) -> str:
        self.check_read_snapshot()
        if getattr(self, 'identity_file', None):
            from .bootstrap import Bootstrap
            from .bitcoin_trust import transaction_profile
            from .operations import canonical
            values=Bootstrap.wire(list(call.args))
            operation=Web3.keccak(text=canonical([call.address,call.fn_name,values])).hex()
            profile=dict(deployment_id='signer-'+operation,chain_id=str(self.chain_id),evm_rpc=self.config.rpc_url,
                         **transaction_profile(getattr(self, 'bitcoin_graph', {})),
                         contracts={'target':dict(address=call.address,artifact='FROSTCoordinator',runtime_hash=Web3.keccak(self.w3.eth.get_code(call.address)).hex())},
                         steps=[dict(id='send',kind='call',contract='target',method=call.fn_name,args=values)])
            floor = int(self._confirmation_graph().get("evm_confirmations", "1"))
            b=Bootstrap(profile,self.transaction_journal,evm_confirmation_floor=floor)
            if b.apply_confirmed(self.identity_file)['status']!='VERIFIED': raise ValueError('TRANSACTION_UNCONFIRMED')
            return b.journal.get(profile['deployment_id']+':send')['result']['tx_hash']
        if self.chain_id == 1:
            raise ValueError("ETHEREUM_DURABLE_IDENTITY_REQUIRED")
        tx: Dict[str, Any] = {
            "from": self.sender,
            "nonce": self.w3.eth.get_transaction_count(self.sender, "pending"),
            "chainId": self.chain_id,
        }
        if self.config.gas_limit:
            tx["gas"] = self.config.gas_limit
        else:
            tx["gas"] = int(call.estimate_gas({"from": self.sender}) * 1.2)

        latest = self.w3.eth.get_block("latest")
        if latest.get("baseFeePerGas") is not None:
            priority = self.w3.to_wei(self.config.priority_fee_gwei, "gwei")
            configured = (
                self.w3.to_wei(self.config.max_fee_gwei, "gwei")
                if self.config.max_fee_gwei is not None
                else int(latest["baseFeePerGas"] * 2 + priority)
            )
            tx.update({"maxFeePerGas": configured, "maxPriorityFeePerGas": priority})
        else:
            tx["gasPrice"] = self.w3.eth.gas_price

        built = call.build_transaction(tx)
        signed = self.w3.eth.account.sign_transaction(built, private_key=self.config.private_key)
        raw = getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", None))
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=self.config.receipt_timeout, poll_latency=2
        )
        if int(receipt.status) != 1:
            raise RuntimeError("transaction reverted: %s" % self.w3.to_hex(tx_hash))
        return self.w3.to_hex(tx_hash)
