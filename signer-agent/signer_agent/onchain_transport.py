"""EVM-only discovery, enrollment and HPKE ciphertext transport; no external relay."""

import json
from eth_account import Account
from web3 import Web3
from .bootstrap import artifact, Bootstrap
from .operations import canonical
from .dkg_transport import sign, recover
from .dkg_protocol import Manifest

ZERO = "0x" + "00" * 20
KINDS = {"key": 0, "round1": 1, "envelope": 2}
MAX_BATCH_PACKETS = 16
MAX_BATCH_BYTES = 16384


def can_publish_intent(board, sender, number=None):
    """Public transport permission, never financial authorization.

    Keep the operator fast path for historical non-delegated profiles. Any
    delegated publisher must be admitted by the canonical board at this snapshot;
    unavailable permission evidence fails closed.
    """
    if sender is None:
        return False
    options = {} if number is None else dict(block_identifier=number)
    return (board.functions.operator().call(**options) == sender
            or bool(board.functions.canPublishIntent(sender).call(**options)))


def _transport_block(w3, confirmations=None):
    from .evm_network import read_block
    depth = (12 if w3.eth.chain_id == 1 else 1) if confirmations is None else confirmations
    return read_block(w3, depth)


def _canonical_snapshot(w3, block):
    if bytes(w3.eth.get_block(block["number"])["hash"]) != bytes(block["hash"]):
        raise ValueError("ONCHAIN_ORIGIN_REORG")


def _board_and_invitation(w3, address, block, *, birth_hint=None):
    from .onboarding import validate_invite
    from .contract_provenance import prove_creation

    address = Web3.to_checksum_address(address)
    a = artifact("DKGOnchainBoard")
    expected = bytes.fromhex(a["deployedBytecode"]["object"].removeprefix("0x"))
    if bytes(w3.eth.get_code(address, block_identifier=block["number"])) != expected:
        raise ValueError("BOARD_BUILD_MISMATCH")
    board = w3.eth.contract(address=address, abi=a["abi"])

    def read(method, *args):
        return getattr(board.functions, method)(*args).call(block_identifier=block["number"])

    raw_invite = bytes(read("invitation"))
    try:
        invite = validate_invite(json.loads(raw_invite.decode()))
    except (ValueError, AttributeError, KeyError, TypeError, IndexError, RecursionError):
        # Discovery must skip hostile event payloads just as it skips invalid
        # JSON or invalid values; their container shapes are untrusted too.
        raise ValueError("BOARD_INVITATION_SCHEMA") from None
    if (str(w3.eth.chain_id) != invite["chain_id"]
            or read("operator") != invite["operator"]
            or bytes(read("invitationHash")) != Web3.keccak(raw_invite)):
        raise ValueError("BOARD_INVITATION_MISMATCH")
    if not all(read("invited", member) for member in invite["participants"]):
        raise ValueError("BOARD_ROSTER_MISMATCH")
    # Runtime/getters alone cannot authenticate constructor-initialized storage.
    # Exact constructor args also exclude extra invited addresses not in the JSON.
    proof = prove_creation(w3, address, "DKGOnchainBoard",
                           [raw_invite, invite["participants"]], block["number"],
                           **({"birth_hint": birth_hint} if birth_hint is not None else {}))
    if Web3.to_checksum_address(proof["sender"]) != invite["operator"]:
        raise ValueError("BOARD_CREATOR_MISMATCH")
    if invite["version"] in ("4", "5", "6"):
        safe = _transport_block(w3, int(invite["evm_confirmations"]))
        # safe already subtracts the enrolled depth (and caps at finalized on
        # chain 1); do not subtract that depth a second time from creation.
        if int(proof["block_number"]) > safe["number"]:
            raise ValueError("EVM_CONFIRMATIONS_PENDING")
        if block["number"] > safe["number"]:
            return _board_and_invitation(w3, address, safe, birth_hint=birth_hint)
    _canonical_snapshot(w3, block)
    return board, invite


def board_at(w3, address, block=None):
    return _board_and_invitation(w3, address, block or _transport_block(w3))[0]


def registry_at(w3, address, block=None):
    """Authenticate the no-argument registry constructor before trusting its state."""
    from .contract_provenance import prove_creation

    block = block or _transport_block(w3)
    address = Web3.to_checksum_address(address)
    a = artifact("DKGCoordinator")
    expected = bytes.fromhex(a["deployedBytecode"]["object"].removeprefix("0x"))
    if bytes(w3.eth.get_code(address, block_identifier=block["number"])) != expected:
        raise ValueError("REGISTRY_BUILD_MISMATCH")
    prove_creation(w3, address, "DKGCoordinator", [], block["number"])
    _canonical_snapshot(w3, block)
    return w3.eth.contract(address=address, abi=a["abi"])


def transact(w3, identity_file, state, board, method, args):
    # Reuse the durable relayer nonce lock and signed-before-broadcast journal.
    _, invite = invitation(w3, board.address)
    operation = Web3.keccak(text=canonical([board.address, method, args])).hex()
    profile = dict(
        deployment_id="board-" + operation,
        chain_id=str(w3.eth.chain_id),
        evm_rpc=w3.provider.endpoint_uri,
        bitcoin_network=invite["bitcoin_network"],
        mode=invite["mode"],
        evm_confirmations=invite.get("evm_confirmations", "1"),
        contracts={
            "board": dict(
                address=board.address,
                artifact="DKGOnchainBoard",
                runtime_hash=Web3.keccak(w3.eth.get_code(board.address)).hex(),
            )
        },
        steps=[
            dict(id="send", kind="call", contract="board", method=method, args=args)
        ],
    )
    result = Bootstrap(profile, state).apply_confirmed(identity_file)
    if result["status"] != "VERIFIED":
        raise ValueError("BOARD_TRANSACTION_UNCONFIRMED")
    return result


def invitation(w3, address, *, birth_hint=None):
    return _board_and_invitation(w3, address, _transport_block(w3), birth_hint=birth_hint)


def assembled_profile(w3, address):
    from .onboarding import assemble

    board, invite = invitation(w3, address)
    block = _transport_block(w3, int(invite.get("evm_confirmations", "12" if w3.eth.chain_id == 1 else "1")))
    enrollments = []
    for address in invite["participants"]:
        data = board.functions.enrollment(address).call(block_identifier=block["number"])
        if not data:
            raise ValueError("ENROLLMENT_INCOMPLETE")
        entry = json.loads(bytes(data).decode())
        if entry["participant"] != address:
            raise ValueError("ENROLLMENT_IDENTITY_MISMATCH")
        enrollments.append(entry)
    profile = assemble(invite, enrollments, w3.provider.endpoint_uri)
    profile["onchain_board"] = board.address
    profile["contracts"] = {
        "onchain_board": dict(
            address=board.address,
            artifact="DKGOnchainBoard",
            runtime_hash=Web3.keccak(w3.eth.get_code(board.address)).hex(),
        )
    }
    return profile


def ceremony_bundle(w3, address, role):
    from .onboarding import make_manifest

    board, invite = invitation(w3, address)
    snapshot = _transport_block(w3, int(invite.get("evm_confirmations", "12" if w3.eth.chain_id == 1 else "1")))
    registry = board.functions.registry().call(block_identifier=snapshot["number"])
    if registry == ZERO:
        raise ValueError("REGISTRY_NOT_BOUND")
    registry_at(w3, registry, snapshot)
    profile = assembled_profile(w3, address)
    deployment = dict(
        contracts={
            "dkg": dict(
                address=registry,
                runtime_hash=Web3.keccak(w3.eth.get_code(registry, block_identifier=snapshot["number"])).hex(),
            )
        }
    )
    bundle = make_manifest(profile, deployment, role, w3.provider.endpoint_uri)
    binding_number = board.functions.registryBlock().call(block_identifier=snapshot["number"])
    if binding_number > snapshot["number"]:
        raise ValueError("EVM_CONFIRMATIONS_PENDING")
    block = w3.eth.get_block(binding_number)
    _canonical_snapshot(w3, snapshot)
    bundle["anchor"] = dict(number=str(block.number), hash=block.hash.hex())
    bundle["onchain_board"] = board.address
    return bundle


def bootstrap_status(w3, address):
    """Public coordination state, including ceremonies not yet created."""
    from .onboarding import ROLES

    board, invite = invitation(w3, address)
    block = _transport_block(w3, int(invite.get("evm_confirmations", "12" if w3.eth.chain_id == 1 else "1")))
    enrolled = {
        a: bool(board.functions.enrollment(a).call(block_identifier=block["number"])) for a in invite["participants"]
    }
    registry_address = board.functions.registry().call(block_identifier=block["number"])
    ceremonies = {role: dict(status="NOT_STARTED") for role in ROLES}
    if registry_address != ZERO:
        registry = registry_at(w3, registry_address, block)
        now = block["timestamp"]
        for role in ROLES:
            sid = bytes.fromhex(invite["sessions"][role])
            if not any(registry.functions.ceremonies(sid).call(block_identifier=block["number"])[0]):
                continue
            info = registry.functions.getSessionInfo(sid).call(block_identifier=block["number"])
            phase = int(info[4])
            status = (
                "COMPLETED"
                if info[9]
                else (
                    "FAILED"
                    if phase in (5, 6)
                    else "EXPIRED" if now > info[5] else "IN_PROGRESS"
                )
            )
            ceremonies[role] = dict(
                status=status,
                phase=phase,
                deadline=str(info[5]),
                commitments=str(info[6]),
                shares=str(info[7]),
                public_shares=str(info[8]),
            )
    _canonical_snapshot(w3, block)
    result = dict(
        board=board.address,
        enrolled=enrolled,
        registry=registry_address,
        ceremonies=ceremonies,
    )
    if "bitcoin_custody" in invite:
        from .bootstrap_custody import role_inputs
        result["role_participants"] = {role: role_inputs(invite, role)[0] for role in ROLES}
    if invite["version"] in ("3", "4", "5", "6"):
        from .bootstrap_policy import effective_fee_policy
        result["fees"] = effective_fee_policy(invite)
    return result


class OnchainMailbox:
    def __init__(self, registry, address, identity_file, state):
        self.registry = registry
        self.board = board_at(registry.w3, address)
        if self.board.functions.registry().call() != registry.contract.address:
            raise ValueError("BOARD_REGISTRY_MISMATCH")
        registry_at(registry.w3, registry.contract.address)
        self.identity = identity_file
        self.state = state

    def _checked_snapshot(self, manifest):
        w3 = self.registry.w3
        block = _transport_block(w3)
        board, invite = _board_and_invitation(w3, self.board.address, block)
        # _board_and_invitation may validate discovery at an older enrolled
        # depth. Its returned contract is not a block-bound object: packet and
        # registry reads must independently use that same enrolled depth.
        if invite["version"] in ("4", "5", "6"):
            block = _transport_block(w3, int(invite["evm_confirmations"]))
            board, invite = _board_and_invitation(w3, self.board.address, block)
        registry_at(w3, self.registry.contract.address, block)
        if (board.functions.registry().call(block_identifier=block["number"])
                != self.registry.contract.address or manifest.registry != self.registry.contract.address):
            raise ValueError("BOARD_REGISTRY_MISMATCH")
        roles = [role for role, sid in invite["sessions"].items()
                 if bytes.fromhex(sid) == manifest.session_id]
        from .bootstrap_custody import role_inputs
        roster, threshold = role_inputs(invite, roles[0]) if len(roles) == 1 else ([], 0)
        if (len(roles) != 1 or tuple(roster) != manifest.participants
                or int(threshold) != manifest.threshold
                or int(invite["epoch"]) != manifest.epoch
                or int(invite["chain_id"]) != manifest.chain_id
                or manifest.purpose != ("BTC_SPEND" if roles[0] == "btc_ceremony" else "EVM_AUTH")):
            raise ValueError("BOARD_MANIFEST_MISMATCH")
        _canonical_snapshot(w3, block)
        return block

    def get(self, manifest, kind, sender, recipient=""):
        block = self._checked_snapshot(manifest)
        payload = self.board.functions.packet(
            manifest.session_id, KINDS[kind], sender, recipient or ZERO
        ).call(block_identifier=block["number"])
        _canonical_snapshot(self.registry.w3, block)
        return json.loads(bytes(payload).decode()) if payload else None

    def put(self, manifest, kind, sender, recipient, body):
        self.put_many(manifest, [(kind, sender, recipient, body)])

    def put_many(self, manifest, messages):
        """Publish deterministic bounded groups; retries retain the complete original group.

        Never regroup only missing entries: a still-unfinalized publication must
        resolve to its original durable operation and exact signed transaction.
        """
        expected = {
            "key": {"signature"},
            "round1": {"package", "commitments"},
            "envelope": {"envelope", "hash", "signature"},
        }
        if not messages or len(messages) > MAX_BATCH_PACKETS:
            raise ValueError("PUBLIC_PACKET_SCHEMA")
        packets = []
        seen = set()
        for kind, sender, recipient, body in messages:
            if sender != self.registry.account.address:
                raise ValueError("SENDER_MISMATCH")
            if kind not in expected or not isinstance(body, dict) or set(body) != expected[kind]:
                raise ValueError("PUBLIC_PACKET_SCHEMA")
            if ((kind == "envelope" and (recipient == sender or recipient not in manifest.participants))
                    or (kind != "envelope" and recipient)):
                raise ValueError("PUBLIC_PACKET_SCHEMA")
            key = (kind, recipient)
            if key in seen:
                raise ValueError("PUBLIC_PACKET_SCHEMA")
            seen.add(key)
            payload = canonical(body).encode()
            if not payload or len(payload) > MAX_BATCH_BYTES:
                raise ValueError("MESSAGE_TOO_LARGE")
            packets.append((kind, sender, recipient, body, payload))
        packets.sort(key=lambda p: (KINDS[p[0]], manifest.index(p[2]) if p[2] else 0))
        groups = []
        group, size = [], 0
        for packet in packets:
            if group and size + len(packet[4]) > MAX_BATCH_BYTES:
                groups.append(group)
                group, size = [], 0
            group.append(packet)
            size += len(packet[4])
        groups.append(group)
        block = self._checked_snapshot(manifest)
        unpublished = []
        for group in groups:
            missing = False
            for kind, sender, recipient, body, payload in group:
                old = bytes(self.board.functions.packet(
                    manifest.session_id, KINDS[kind], sender, recipient or ZERO
                ).call(block_identifier=block["number"]))
                if old and old != payload:
                    raise ValueError("CONFLICTING_RETRY")
                missing |= not old
            if missing:
                unpublished.append(group)
        _canonical_snapshot(self.registry.w3, block)
        for group in unpublished:
            transact(self.registry.w3, self.identity, self.state, self.board, "publishBatch", [
                "0x" + manifest.session_id.hex(),
                [str(KINDS[p[0]]) for p in group],
                [p[2] or ZERO for p in group],
                ["0x" + p[4].hex() for p in group],
            ])
