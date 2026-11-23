import logging
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from eth_utils import to_checksum_address
from coincurve import PublicKeyXOnly
from . import crypto

from .config import Config
from .coordinator import (
    PURPOSE_DKG, SESSION_ABORTED, SESSION_FINALIZED, SESSION_OPENED,
    CoordinatorClient, Session,
)
from .frost_math import (
    NoncePair,
    Point,
    SECP256K1_ORDER,
    aggregate_nonce,
    bip340_challenge,
    binding_factor,
    build_transcript,
    lagrange_coefficient,
    negate_point,
    parse_signing_share,
    participant_nonce,
    random_nonce_pair,
    scalar_base_mult,
    signature_share,
    verify_signature_share,
)
from .secure_state import SecureState, requires_independent_nonce_ledger
from .participant_errors import participant_failure
from .safe_errors import emit_debug


LOG = logging.getLogger(__name__)

# The initial review and each private/retransmit boundary use the same guards.
SIGNING_GUARDS = (
    "guard_verifier", "guard_mint_session", "guard_gas_session", "guard_bitcoin_session",
    "guard_address_registration_session", "guard_btc_fee_session",
    "guard_withdrawal_fee_session", "guard_ulr_treasury_session", "guard_token_metadata_session", "guard_custody_session",
    "guard_nft_session", "guard_fee_sale_session",
)


@dataclass(frozen=True)
class ActionResult:
    session_id: int
    status: str
    detail: str
    tx_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class SigningService:
    def __init__(
        self,
        config: Config,
        client: Optional[CoordinatorClient] = None,
        state: Optional[SecureState] = None,
        *,
        reviewed_session_id: Optional[int] = None,
    ):
        config.validate(require_signing=True)
        self.config = config
        self.client = client or CoordinatorClient(config)
        self.reviewed_session_id = reviewed_session_id
        self.state = state or SecureState(config.state_path, config.state_key, nonce_ledger=config.nonce_ledger)
        if config.nonce_ledger is not None:
            self.state.require_nonce_ledger(expected_path=config.nonce_ledger)
        self.signing_share = parse_signing_share(config.signing_share_hex)
        self.public_key_share = scalar_base_mult(self.signing_share)

    def pending(self, session_id: Optional[int] = None) -> List[Session]:
        return self.client.pending_sessions(session_id)

    def describe(self, session: Session) -> Dict[str, object]:
        return {
            "session_id": session.session_id,
            "message_hash": "0x" + session.message_hash.hex(),
            "origin_contract": session.origin_contract,
            "origin_id": session.origin_id,
            "purpose": session.purpose,
            "network_id": session.network_id,
            "threshold": session.threshold,
            "total": session.total,
            "commits": session.commits_count,
            "shares": session.shares_count,
            "refusals": session.refusal_count,
            "deadline": session.deadline,
            "seconds_remaining": max(0, session.deadline - int(time.time())),
            "enforced": session.enforce_shares_check,
        }

    def reject(self, session: Session, reason: str) -> ActionResult:
        if not reason.strip():
            raise ValueError("rejection reason must not be empty")
        tx_hash = self.client.reject(session.session_id, reason.strip())
        return ActionResult(session.session_id, "rejected", reason.strip(), tx_hash)

    def approve(self, session: Session) -> ActionResult:
        with self.state.exclusive():
            return self._approve_locked(session)

    def _approve_locked(self, session: Session) -> ActionResult:
        terminal = self._terminal_result(session)
        if terminal is not None:
            return terminal
        if session.purpose == PURPOSE_DKG:
            return ActionResult(session.session_id, "skipped", "DKG is not a signing request")
        chain_id = self.client.chain_id
        if chain_id != 31337 and self.reviewed_session_id != session.session_id:
            raise ValueError("PUBLIC_EVM_TYPED_SIGNING_REQUIRED")
        graph = getattr(self.client, "bitcoin_graph", {})
        if requires_independent_nonce_ledger(chain_id, graph.get("bitcoin_network")):
            self.state.require_nonce_ledger(expected_path=self.config.nonce_ledger)
        for name in SIGNING_GUARDS:
            guard = getattr(self.client, name, None)
            if guard is not None:
                guard(session)
        if not session.enforce_shares_check:
            return ActionResult(
                session.session_id,
                "unsupported",
                "legacy hash-only sessions cannot carry a verifiable FROST transcript",
            )
        if not session.message_bound:
            return ActionResult(session.session_id, "waiting", "message hash is not bound")
        if len(session.group_pubkey) != 32:
            return ActionResult(session.session_id, "unsupported", "only x-only Schnorr sessions are supported")

        participants = self.client.participants(session.session_id)
        sender = to_checksum_address(self.client.sender)
        if sender not in participants:
            return ActionResult(session.session_id, "skipped", "configured EVM sender is not a participant")

        onchain_public, registered = self.client.public_key_share(session.session_id, sender)
        if not registered:
            return ActionResult(session.session_id, "waiting", "participant public-key share is not registered")
        if onchain_public != self.public_key_share:
            return ActionResult(
                session.session_id,
                "blocked",
                "configured signing share does not match the registered public-key share",
            )

        commitments = self.client.all_nonce_commitments(session.session_id, participants)
        if not self.client.has_committed(session.session_id, sender):
            return self._commit(session, participants, commitments)
        if self.client.has_submitted_share(session.session_id, sender):
            if self.config.auto_finalize:
                finalized = self.try_finalize(session, participants, commitments)
                if finalized is not None:
                    return finalized
            return ActionResult(session.session_id, "complete", "signature share already submitted")
        return self._submit_share(session, participants, commitments)

    @staticmethod
    def _terminal_result(session: Session) -> Optional[ActionResult]:
        """Honor the caller's confirmed snapshot before touching nonce state.

        Repeating an approved CLI command after another signer finalized the
        session is a read-only success. An aborted, absent, or unknown session
        must never produce a new commitment or retransmit a signature share.
        """
        if session.state == SESSION_FINALIZED:
            return ActionResult(session.session_id, "finalized", "session already finalized")
        if session.state == SESSION_ABORTED:
            return ActionResult(session.session_id, "blocked", "session is aborted")
        if session.state != SESSION_OPENED:
            return ActionResult(session.session_id, "blocked", "session is not open")
        return None

    def _active_committee(
        self,
        session: Session,
        participants: Sequence[str],
        commitments: Dict[str, Tuple[Point, Point]],
    ) -> List[str]:
        committed = [address for address in participants if to_checksum_address(address) in commitments]
        if len(committed) >= session.threshold:
            return committed

        active = list(committed)
        for participant in participants:
            participant = to_checksum_address(participant)
            if participant in active or self.client.has_refused(session.session_id, participant):
                continue
            active.append(participant)
            if len(active) == session.threshold:
                break
        return active

    def _guard_bitcoin(self, session):
        if hasattr(self.client,'continuity_context'):self.client.continuity_context=None
        for name in SIGNING_GUARDS:
            guard = getattr(self.client, name, None)
            if guard is not None: guard(session)
        from .bitcoin_continuity import evm_guard
        evm_guard(self.client,self.state,session)

    def _commit(
        self,
        session: Session,
        participants: Sequence[str],
        commitments: Dict[str, Tuple[Point, Point]],
    ) -> ActionResult:
        sender = to_checksum_address(self.client.sender)
        committee = self._active_committee(session, participants, commitments)
        if len(commitments) >= session.threshold:
            return ActionResult(session.session_id, "standby", "the signing committee already has enough commitments")
        if sender not in committee:
            return ActionResult(session.session_id, "standby", "waiting as a fallback signer")

        key = SecureState.session_key(self.client.chain_id, self.client.address, session.session_id)
        stored = self.state.get(key)
        if stored and stored.get("status") == "nonce-created":
            if stored.get("message_hash") != session.message_hash.hex():
                return ActionResult(session.session_id, "blocked", "stored nonce belongs to another message hash")
            self.state.assert_unspent(key)
            nonces = NoncePair(int(stored["hiding"], 16), int(stored["binding"], 16))
        elif stored:
            return ActionResult(session.session_id, "blocked", "nonce already spent; start a new session")
        else:
            self._guard_bitcoin(session)
            self.state.assert_unspent(key)
            nonces = random_nonce_pair()
            self.state.put(
                key,
                {
                    "status": "nonce-created",
                    "message_hash": session.message_hash.hex(),
                    "hiding": hex(nonces.hiding),
                    "binding": hex(nonces.binding),
                },
            )

        self._guard_bitcoin(session)
        tx_hash = self.client.submit_nonce_commitments(
            session.session_id, nonces.hiding_point, nonces.binding_point
        )
        return ActionResult(session.session_id, "committed", "D/E nonce commitments submitted", tx_hash)

    def _submit_share(
        self,
        session: Session,
        participants: Sequence[str],
        commitments: Dict[str, Tuple[Point, Point]],
    ) -> ActionResult:
        if len(commitments) < session.threshold:
            return ActionResult(session.session_id, "waiting", "not enough nonce commitments")

        sender = to_checksum_address(self.client.sender)
        key = SecureState.session_key(self.client.chain_id, self.client.address, session.session_id)
        stored = self.state.get(key)
        if not stored or stored.get("status") not in {"nonce-created", "share-prepared", "consumed"}:
            return ActionResult(
                session.session_id,
                "blocked",
                "on-chain commitment exists but its secret nonce is unavailable; do not regenerate it",
            )
        if stored.get("message_hash") != session.message_hash.hex():
            return ActionResult(session.session_id, "blocked", "stored nonce message hash mismatch")

        transcript = build_transcript(
            self.client.chain_id,
            self.client.address,
            session.session_id,
            session.group_pubkey,
            session.message_hash,
            participants,
            commitments,
        )
        if stored.get("status") in {"share-prepared", "consumed"}:
            if stored.get("transcript") != transcript.hex() or not stored.get("share"):
                return ActionResult(session.session_id, "blocked", "spent nonce belongs to another signing package")
            self.state.assert_spent(key, transcript.hex())
            self._guard_bitcoin(session)
            tx_hash = self.client.submit_signature_share(session.session_id, int(stored["share"], 16))
            self.state.mark_consumed(key, tx_hash)
            return ActionResult(session.session_id, "signed", "persisted share retransmitted", tx_hash)
        self.state.assert_unspent(key)
        nonces = NoncePair(int(stored["hiding"], 16), int(stored["binding"], 16))
        if commitments.get(sender) != (nonces.hiding_point, nonces.binding_point):
            return ActionResult(session.session_id, "blocked", "stored nonce does not match on-chain commitments")

        aggregate = aggregate_nonce(transcript, session.session_id, participants, commitments)
        nonce_negated = bool(aggregate.y & 1)
        normalized_aggregate = negate_point(aggregate) if nonce_negated else aggregate

        locked, locked_transcript, locked_aggregate, _ = self.client.nonce_set_status(session.session_id)
        if locked and (locked_transcript != transcript or locked_aggregate != normalized_aggregate):
            return ActionResult(session.session_id, "blocked", "local transcript differs from the locked on-chain set")

        rho = binding_factor(transcript, session.session_id, sender)
        lagrange = lagrange_coefficient(sender, participants, commitments.keys())
        challenge = bip340_challenge(
            aggregate.x, int.from_bytes(session.group_pubkey, "big"), session.message_hash
        )
        # Secret scalar arithmetic lives in the pinned local Rust adapter.
        self._guard_bitcoin(session)
        graph = getattr(self.client, "bitcoin_graph", {})
        with crypto.require_pin(requires_independent_nonce_ledger(self.client.chain_id, graph.get("bitcoin_network"))):
            share = int(crypto.call(
                "evm_share_v1", share="%064x" % self.signing_share,
                hiding="%064x" % nonces.hiding, binding="%064x" % nonces.binding,
                rho="%064x" % rho, lagrange="%064x" % lagrange, challenge="%064x" % challenge,
                negate_nonce=nonce_negated,
            )["share"], 16)
        nonce_point = participant_nonce(
            nonces.hiding_point, nonces.binding_point, rho, negate=nonce_negated
        )
        if not verify_signature_share(
            share, self.public_key_share, nonce_point, lagrange, challenge
        ):
            return ActionResult(session.session_id, "blocked", "locally generated signature share is invalid")

        self.state.prepare_share(key, transcript.hex(), share)
        self._guard_bitcoin(session)
        tx_hash = self.client.submit_signature_share(session.session_id, share)
        self.state.mark_consumed(key, tx_hash)
        result = ActionResult(session.session_id, "signed", "verifiable scalar share submitted", tx_hash)
        if self.config.auto_finalize:
            try:
                refresh = getattr(self.client, "finalization_snapshot", None)
                finalized = (self.try_finalize(refresh(session)) if refresh is not None
                             else self.try_finalize(session, participants, commitments))
                if finalized is not None:
                    return finalized
            except Exception as exc:
                failure = participant_failure(exc)
                emit_debug(exc, code=failure["code"])
                LOG.warning("share submitted; automatic finalization code=%s. %s", failure["code"], failure["message"])
        return result

    def try_finalize(
        self,
        session: Session,
        participants: Optional[Sequence[str]] = None,
        commitments: Optional[Dict[str, Tuple[Point, Point]]] = None,
    ) -> Optional[ActionResult]:
        terminal = self._terminal_result(session)
        if terminal is not None:
            return terminal
        participants = list(participants or self.client.participants(session.session_id))
        commitments = commitments or self.client.all_nonce_commitments(session.session_id, participants)
        shares = []
        for participant in participants:
            if to_checksum_address(participant) not in commitments:
                continue
            share = self.client.verified_share(session.session_id, participant)
            if share is None:
                return None
            shares.append(share)
        if len(shares) < session.threshold:
            return None

        locked, _, aggregate, _ = self.client.nonce_set_status(session.session_id)
        if not locked:
            return None
        z = sum(shares) % SECP256K1_ORDER
        signature = aggregate.x.to_bytes(32, "big") + z.to_bytes(32, "big")
        if not PublicKeyXOnly(session.group_pubkey).verify(signature, session.message_hash):
            return ActionResult(session.session_id, "blocked", "independent BIP340 verification failed")
        tx_hash = self.client.finalize(session.session_id, signature, session.message_hash)
        if tx_hash is None:
            return self._terminal_result(self.client.session(session.session_id)) or ActionResult(
                session.session_id, "waiting", "another finalization is awaiting confirmation")
        return ActionResult(session.session_id, "finalized", "aggregate signature finalized", tx_hash)
