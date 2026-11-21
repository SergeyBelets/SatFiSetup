"""Public invitations and local enrollment; never collect participant private keys."""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from .safe_errors import DiagnosticArgumentParser
from . import crypto
from .dkg import atomic_private, Registry
from .dkg_protocol import Manifest, PROTOCOL_ID
from .operations import canonical, public_data, Journal
from .secure_state import SecureState
from .bootstrap_policy import (ADDRESS_FIELDS, BOOLEAN_FIELDS, NUMBER_BOUNDS,
    POLICY_FIELDS, MAINNET_FIELDS, validate_policy, validate_mainnet_policy, validate_network_policy)
from .bitcoin_network import is_public_network

ROLES = ("evm_ceremony", "conf_ceremony", "btc_ceremony")
DISCOVERY_RECENT_BLOCKS = 8192


def validate_phase_duration(chain_id, value, version="6"):
    expected = "86400" if chain_id == "1" else "3600"
    if value == expected:
        return
    if (version != "6" or chain_id not in ("31337", "11155111") or not isinstance(value, str)
            or not re.fullmatch(r"[1-9][0-9]{3,4}", value) or not 3600 <= int(value) <= 86400):
        raise ValueError("DKG_PHASE_DURATION")
INVALID_INVITATION_CANDIDATE = frozenset((
    "BOARD_BUILD_MISMATCH", "BOARD_INVITATION_SCHEMA", "BOARD_INVITATION_MISMATCH",
    "BOARD_ROSTER_MISMATCH", "BOARD_CREATOR_MISMATCH", "BTC_CONSTRUCTOR_INPUT_MISMATCH",
    "BTC_CANONICAL_CREATION_NOT_FOUND", "BTC_CONTRACT_BUILD_MISMATCH",
))


def discover_invitations(w3, operator, *, from_block=None, recent_blocks=DISCOVERY_RECENT_BLOCKS,
                         board_address=None, page_size=2000):
    """Bound first discovery; event coordinates locate, but never authenticate, a board."""
    from .evm_log_paging import log_pages
    from .onchain_transport import invitation

    latest = w3.eth.block_number
    if isinstance(recent_blocks, bool) or not isinstance(recent_blocks, int) or recent_blocks < 1:
        raise ValueError("DISCOVERY_RANGE_INVALID")
    if from_block is None:
        first = max(0, latest - recent_blocks + 1)
    elif isinstance(from_block, bool) or not isinstance(from_block, int) or not 0 <= from_block <= latest:
        raise ValueError("DISCOVERY_RANGE_INVALID")
    else:
        first = from_block
    anchor = w3.eth.get_block(latest)
    operator = Web3.to_checksum_address(operator)
    if board_address is not None:
        board_address = Web3.to_checksum_address(board_address)
    topic = Web3.keccak(text="InvitationPublished(address,bytes32)")
    found, seen = [], set()
    for logs in log_pages(w3, first, latest, address=board_address, initial_span=page_size,
            topics=[topic, "0x" + "00" * 12 + operator[2:]]):
        for log in logs:
            number, address = log.get("blockNumber"), log.get("address")
            if (type(number) is not int or not first <= number <= latest or not Web3.is_address(address)
                    or board_address is not None and Web3.to_checksum_address(address) != board_address):
                raise ValueError("DISCOVERY_EVENT_SCOPE")
            try:
                # A successful constructor proof is still required. The event's
                # block is an untrusted locator, not evidence of authentic code.
                board, invite = invitation(w3, address, birth_hint=number)
            except ValueError as error:
                if (type(error) is ValueError and len(error.args) == 1
                        and isinstance(error.args[0], str)
                        and error.args[0] in INVALID_INVITATION_CANDIDATE):
                    continue
                # A failed RPC page/proof, inconsistent receipt, missing anchor
                # or pending finality must never become an empty successful search.
                raise
            if invite["operator"] == operator and board.address not in seen:
                found.append(dict(board=board.address, block=str(number),
                                  fingerprint=fingerprint(invite), invitation=invite))
                seen.add(board.address)
    if bytes(w3.eth.get_block(latest)["hash"]) != bytes(anchor["hash"]):
        raise ValueError("ONCHAIN_ORIGIN_REORG")
    return dict(status="FOUND" if found else "NOT_FOUND" if first == 0 else "NOT_FOUND_IN_RANGE",
                invitations=found, searched_from_block=str(first), searched_to_block=str(latest),
                complete_history=first == 0)


def read_private(path,*,max_bytes=None):
    # Open without following links, then inspect the opened inode (no stat/read race).
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise ValueError("PRIVATE_FILE_PERMISSIONS")
        if max_bytes is not None:
            if type(max_bytes) is not int or not 1<=max_bytes<=64*1024**2 or info.st_size>max_bytes:raise ValueError('PRIVATE_FILE_TOO_LARGE')
            value=stream.read(max_bytes+1)
            if len(value.encode())>max_bytes:raise ValueError('PRIVATE_FILE_TOO_LARGE')
            return value
        return stream.read(65537)


def password_file(path):
    value = read_private(path).strip()
    if len(value) != 64:
        raise ValueError("STATE_KEY_REQUIRES_32_RANDOM_BYTES_HEX")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError("STATE_KEY_REQUIRES_32_RANDOM_BYTES_HEX") from None
    return value


def read_public(path):
    if Path(path).stat().st_size > 1048576:
        raise ValueError("PUBLIC_FILE_TOO_LARGE")
    return public_data(json.loads(Path(path).read_text()))


def write_public(path, data):
    atomic_private(path, (json.dumps(public_data(data), indent=2) + "\n").encode())


def fingerprint(invite):
    return hashlib.sha256(canonical(invite).encode()).hexdigest()


def validate_invite(invite):
    required = {
        "version",
        "chain_id",
        "bitcoin_network",
        "mode",
        "deployment_id",
        "operator",
        "treasury",
        "approval_deadline",
        "federation_id",
        "confederation_id",
        "participants",
        "threshold",
        "epoch",
        "sessions",
        "protocol_id",
        "phase_duration",
    }
    # V1 is readable for existing enrollments, but must never be published again:
    # its RPC URL can contain credentials. V2 commits only shared public inputs.
    if invite.get("version") == "1":
        required.add("evm_rpc")
    if invite.get("version") in ("3", "4", "5", "6"):
        required.update(POLICY_FIELDS)
    if invite.get("version") in ("4", "5"):
        required.update(MAINNET_FIELDS)
    if invite.get("version") == "5":
        required.add("deployment_fee")
    if invite.get("version") == "6":
        required.add("evm_confirmations")
        if "bitcoin_custody" in invite:
            from .bootstrap_custody import validate as validate_custody
            validate_custody(invite["bitcoin_custody"], invite["participants"], invite["min_custodians"])
            required.add("bitcoin_custody")
        if "token_metadata" in invite:
            from .bootstrap_tokens import resolve
            resolve(invite)
            required.add("token_metadata")
        if "system_bootstrap" in invite:
            from .bootstrap_system import validate
            validate(invite)
            required.add("system_bootstrap")
        if "ulr" in invite:
            from .bootstrap_ulr import validate
            validate(invite)
            required.add("ulr")
        if "withdrawal_fee" in invite:
            from .bootstrap_withdrawal_fee import validate
            validate(invite["withdrawal_fee"])
            required.add("withdrawal_fee")
        if is_public_network(invite.get("bitcoin_network")):
            required.add("bitcoin_policy")
        if invite.get("chain_id") == "1" or is_public_network(invite.get("bitcoin_network")):
            required.update(("reward_token",) if "ulr" in invite else ("reward_token", "btc_usd_feed"))
        if invite.get("chain_id") == "1":
            required.add("deployment_fee")
    if set(invite) != required or invite["version"] not in ("1", "2", "3", "4", "5", "6"):
        raise ValueError("INVITATION_SCHEMA")
    mainnet = invite["version"] in ("4", "5")
    if invite["version"] == "6":
        validate_network_policy(invite, "INVITATION_PARAMETER: ")
    elif (
        invite["mode"] != ("backed" if mainnet else "integration")
        or invite["bitcoin_network"] != ("mainnet" if mainnet else "regtest")
        or invite["chain_id"] not in (("1",) if invite["version"] == "5" else ("31337", "11155111"))
    ):
        raise ValueError("INTEGRATION_NETWORK_NOT_ALLOWED")
    validate_phase_duration(invite["chain_id"], invite["phase_duration"], invite["version"])
    if invite["protocol_id"] != PROTOCOL_ID.hex():
        raise ValueError("INVITATION_PROTOCOL")
    addresses = [Web3.to_checksum_address(a) for a in invite["participants"]]
    if addresses != invite["participants"] or invite["operator"] != addresses[0]:
        raise ValueError("INVITATION_ROSTER")
    if (
        set(invite["sessions"]) != set(ROLES)
        or len(set(invite["sessions"].values())) != 3
    ):
        raise ValueError("INVITATION_SESSIONS")
    for role in ROLES:
        from .bootstrap_custody import role_inputs
        roster, role_threshold = role_inputs(invite, role)
        Manifest(
            int(invite["chain_id"]),
            invite["treasury"],
            bytes.fromhex(invite["sessions"][role]),
            int(invite["epoch"]),
            "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH",
            tuple(roster),
            tuple(i.to_bytes(32, "big") for i in range(1, len(roster) + 1)),
            int(role_threshold),
        )
    if invite["version"] in ("3", "4", "5", "6"):
        validate_policy(invite, "INVITATION_PARAMETER: ")
        f, c = 1, len(addresses) - 1
        threshold = int(invite["threshold"])
        if (c < 1 or threshold < (7 * len(addresses) + 9) // 10 or not threshold > f
                or "bitcoin_custody" not in invite and not threshold-f <= int(invite["min_custodians"]) <= c):
            raise ValueError("INVITATION_CONFEDERATION_THRESHOLD")
    if mainnet:
        validate_mainnet_policy(invite, "INVITATION_PARAMETER: ")
    return invite


def enrollment_message(body):
    return encode_defunct(text="SATOHSI_BOOTSTRAP_ENROLLMENT_V1\n" + canonical(body))


def verify_enrollments(invite, enrollments):
    validate_invite(invite)
    found = {}
    for entry in enrollments:
        if set(entry) != {
            "invitation_hash",
            "participant",
            "transport_keys",
            "signature",
        }:
            raise ValueError("ENROLLMENT_SCHEMA")
        body = {k: v for k, v in entry.items() if k != "signature"}
        address = entry["participant"]
        if (
            entry["invitation_hash"] != fingerprint(invite)
            or address not in invite["participants"]
            or address in found
            or set(entry["transport_keys"]) != set(ROLES)
        ):
            raise ValueError("ENROLLMENT_CONTEXT")
        if (
            Account.recover_message(
                enrollment_message(body), signature=entry["signature"]
            )
            != address
        ):
            raise ValueError("ENROLLMENT_SIGNATURE")
        found[address] = entry
    if set(found) != set(invite["participants"]):
        raise ValueError("ENROLLMENT_INCOMPLETE")
    # Separate transport keys per purpose/role. Every result has its own random DKG.
    keys = [e["transport_keys"][r] for e in found.values() for r in ROLES]
    if len(set(keys)) != len(keys) or any(
        len(bytes.fromhex(k)) != 32 or int(k, 16) == 0 for k in keys
    ):
        raise ValueError("ENROLLMENT_TRANSPORT_KEYS")
    return found


def assemble(invite, enrollments, rpc=None):
    found = verify_enrollments(invite, enrollments)
    p = {
        k: invite[k]
        for k in (
            "chain_id",
            "bitcoin_network",
            "mode",
            "deployment_id",
            "operator",
            "treasury",
            "approval_deadline",
            "federation_id",
            "confederation_id",
        )
    }
    if invite["version"] in ("3", "4", "5", "6"):
        p.update({k: invite[k] for k in POLICY_FIELDS})
    if invite["version"] in ("4", "5"):
        p.update({k: invite[k] for k in MAINNET_FIELDS})
    if invite["version"] == "6":
        p.update({k: invite[k] for k in MAINNET_FIELDS if k in invite})
        if invite["phase_duration"] != ("86400" if invite["chain_id"] == "1" else "3600"):
            p["phase_duration"] = invite["phase_duration"]
        if "bitcoin_custody" in invite:
            p["bitcoin_custody"] = invite["bitcoin_custody"]
        if "token_metadata" in invite:
            p["token_metadata"] = invite["token_metadata"]
        if "system_bootstrap" in invite:
            p["system_bootstrap"] = invite["system_bootstrap"]
        if "ulr" in invite:
            p["ulr"] = invite["ulr"]
        if "withdrawal_fee" in invite:
            p["withdrawal_fee"] = invite["withdrawal_fee"]
    if invite["chain_id"] == "1":
        p["deployment_fee"] = invite["deployment_fee"]
    p.update(
        evm_rpc=rpc if rpc is not None else invite.get("evm_rpc", ""),
        evm_confirmations=invite.get("evm_confirmations", "12" if invite["chain_id"] == "11155111" else "1"),
        onboarding=dict(invitation=invite, enrollments=enrollments),
    )
    for role in ROLES:
        from .bootstrap_custody import role_inputs
        roster, role_threshold = role_inputs(invite, role)
        p[role] = dict(
            id="0x" + invite["sessions"][role],
            participants=roster,
            threshold=role_threshold,
            epoch=invite["epoch"],
            transport_keys=[
                "0x" + found[a]["transport_keys"][role] for a in roster
            ],
        )
    return p


def verify_profile(profile):
    data = profile["onboarding"]
    expected = assemble(data["invitation"], data["enrollments"])
    if any((field in profile) != (field in expected) for field in ("withdrawal_fee", "ulr", "system_bootstrap", "token_metadata", "bitcoin_custody", "phase_duration")):
        raise ValueError("ENROLLED_PROFILE_MISMATCH")
    for k, v in expected.items():
        if k == "evm_rpc":
            # Routing is participant-local; it is not an enrolled consensus input.
            continue
        if profile.get(k) != v:
            raise ValueError("ENROLLED_PROFILE_MISMATCH")
    from .bootstrap_tokens import resolve
    resolve(profile)
    return data["invitation"]


def join(invite, expected_hash, identity_file, directory, *, bitcoin=None):
    validate_invite(invite)
    if expected_hash != fingerprint(invite):
        raise ValueError("INVITATION_FINGERPRINT_MISMATCH")
    if int(invite["approval_deadline"]) <= int(time.time()):
        raise ValueError("INVITATION_EXPIRED")
    if is_public_network(invite["bitcoin_network"]):
        if bitcoin is None:
            raise ValueError("MAINNET_CORE_REQUIRED")
        from .bitcoin_network import check_core
        check_core(bitcoin, invite["bitcoin_network"], invite["bitcoin_policy"])
    account = Account.from_key(json.loads(read_private(identity_file))["private_key"])
    if account.address not in invite["participants"]:
        raise ValueError("NOT_INVITED")
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("PARTICIPANT_DIRECTORY_PERMISSIONS")
    # Serialize the entire first enrollment, including password publication.
    # Per-role state locks alone do not protect concurrent password/enrollment
    # creation by two fresh join commands.
    import fcntl
    fd = os.open(root / ".enrollment.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        return _join_locked(invite, expected_hash, account, root)


def _join_locked(invite, expected_hash, account, root):
    enrollment = root / "enrollment.json"
    # A retry verifies the old enrollment, never silently replaces a key.
    if enrollment.exists():
        data = read_public(enrollment)
        if (
            data["participant"] != account.address
            or data["invitation_hash"] != expected_hash
        ):
            raise ValueError("ENROLLMENT_CONTEXT")
        return data
    if not (root / "password").exists():
        atomic_private(root / "password", (os.urandom(32).hex() + "\n").encode())
    password = password_file(root / "password")
    keys = {}
    for role in ROLES:
        state = SecureState(str(root / (role + ".enc")), password)
        with state.exclusive():
            binding = state.get("enrollment")
            if binding and binding != dict(
                invitation_hash=expected_hash, participant=account.address
            ):
                raise ValueError("ENROLLMENT_CONTEXT")
            state.put(
                "enrollment",
                dict(invitation_hash=expected_hash, participant=account.address),
            )
            key = state.get("transport")
            if not key:
                key = crypto.call("transport_key")
                state.put("transport", key)
            keys[role] = key["public"]
    body = dict(
        invitation_hash=expected_hash, participant=account.address, transport_keys=keys
    )
    result = dict(
        body,
        signature=Account.sign_message(
            enrollment_message(body), account.key
        ).signature.hex(),
    )
    write_public(enrollment, result)
    return result


def make_manifest(profile, deployment, role, rpc):
    invite = verify_profile(profile)
    c = profile[role]
    address = deployment["contracts"]["dkg"]["address"]
    m = Manifest(
        int(profile["chain_id"]),
        address,
        bytes.fromhex(c["id"][2:]),
        int(c["epoch"]),
        "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH",
        tuple(c["participants"]),
        tuple(bytes.fromhex(k[2:]) for k in c["transport_keys"]),
        int(c["threshold"]),
    )
    registry = Registry(rpc, address)
    from .onchain_transport import registry_at

    if invite["chain_id"] == "1":
        from .evm_network import read_block
        from .bitcoin_trust import invitation_options
        registry.bitcoin_graph = dict(version="2", chain_id=invite["chain_id"], **invitation_options(invite))
        block = read_block(registry.w3, int(invite["evm_confirmations"]))
        registry.read_block_identifier = block.number
    else:
        block = registry.w3.eth.get_block("latest")
    registry_at(registry.w3, address, block)
    registry.check(m)
    from .bootstrap import artifact

    expected_code = bytes.fromhex(
        artifact("DKGCoordinator")["deployedBytecode"]["object"].removeprefix("0x")
    )
    if bytes(registry.w3.eth.get_code(address, block_identifier=block.number)) != expected_code:
        raise ValueError("REGISTRY_BUILD_MISMATCH")
    code_hash = Web3.keccak(registry.w3.eth.get_code(address, block_identifier=block.number)).hex()
    if code_hash.removeprefix("0x") != deployment["contracts"]["dkg"][
        "runtime_hash"
    ].removeprefix("0x"):
        raise ValueError("REGISTRY_CODE_HASH_MISMATCH")
    if registry.read("PHASE_DURATION") != int(invite["phase_duration"]):
        raise ValueError("CEREMONY_TIMING_MISMATCH")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("ONCHAIN_ORIGIN_REORG")
    # Bundle contains only public data. Participant checks signed enrollments before part1.
    return dict(
        m.to_dict(),
        onboarding=profile["onboarding"],
        role=role,
        registry_code_hash=code_hash,
        anchor=dict(number=str(block.number), hash=block.hash.hex()),
        phase_duration=invite["phase_duration"],
    )


def validate_bundle(bundle, state, registry):
    from .onchain_transport import registry_at

    # Also authenticate direct/legacy manifests before the first private DKG round.
    m = Manifest.from_dict(bundle)
    if m.registry != registry.contract.address or m.chain_id != registry.w3.eth.chain_id:
        raise ValueError("MANIFEST_MISMATCH")
    registry_at(registry.w3, m.registry)
    if "onboarding" not in bundle:
        if state.get("enrollment"):
            raise ValueError("ENROLLED_MANIFEST_REQUIRED")
        return
    data = bundle["onboarding"]
    profile = assemble(data["invitation"], data["enrollments"])
    role = bundle["role"]
    c = profile[role]
    expected = Manifest(
        m.chain_id,
        m.registry,
        bytes.fromhex(c["id"][2:]),
        int(c["epoch"]),
        "BTC_SPEND" if role == "btc_ceremony" else "EVM_AUTH",
        tuple(c["participants"]),
        tuple(bytes.fromhex(k[2:]) for k in c["transport_keys"]),
        int(c["threshold"]),
    )
    binding = state.get("enrollment")
    if (
        m != expected
        or m.chain_id != int(profile["chain_id"])
        or not binding
        or binding["invitation_hash"] != fingerprint(data["invitation"])
        or binding["participant"] != registry.account.address
    ):
        raise ValueError("ENROLLED_MANIFEST_MISMATCH")
    if registry.read("PHASE_DURATION") != int(data["invitation"]["phase_duration"]):
        raise ValueError("CEREMONY_TIMING_MISMATCH")
    from .bootstrap import artifact

    expected_code = bytes.fromhex(
        artifact("DKGCoordinator")["deployedBytecode"]["object"].removeprefix("0x")
    )
    if bytes(registry.w3.eth.get_code(m.registry)) != expected_code:
        raise ValueError("REGISTRY_BUILD_MISMATCH")
    if (
        Web3.keccak(registry.w3.eth.get_code(m.registry)).hex()
        != bundle["registry_code_hash"]
    ):
        raise ValueError("REGISTRY_CODE_HASH_MISMATCH")
    if (
        registry.w3.eth.get_block(int(bundle["anchor"]["number"])).hash.hex()
        != bundle["anchor"]["hash"]
    ):
        raise ValueError("DKG_ANCHOR_REORG")


def main(argv):
    p = DiagnosticArgumentParser(prog="frost-signer setup")
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser(
        "invite", help="create an immutable public invitation from known addresses"
    )
    c.add_argument("--participants", nargs="+", required=True)
    c.add_argument("--threshold", type=int, required=True)
    c.add_argument("--treasury", required=True)
    for field, bounds in NUMBER_BOUNDS.items():
        c.add_argument("--" + field.replace("_", "-"), required=True,
                       help="explicit decimal integer, %s..%s" % bounds)
    for field in BOOLEAN_FIELDS:
        c.add_argument("--" + field.replace("_", "-"), choices=("true", "false"), required=True)
    for field in ADDRESS_FIELDS:
        c.add_argument("--" + field.replace("_", "-"), required=True)
    c.add_argument("--chain-id", choices=["1", "31337", "11155111"], help="legacy numeric EVM selection")
    c.add_argument("--phase-duration", help="seconds per testnet DKG phase (3600–86400)")
    c.add_argument("--ethereum-network", choices=("mainnet", "sepolia", "local"),
                   help="independent EVM network (default: local)")
    c.add_argument("--bitcoin-network", choices=("regtest", "mainnet", "testnet4"), default="regtest")
    c.add_argument("--bitcoin-policy", help="local public policy generated from your own Bitcoin Core; published inside V6 invitation")
    c.add_argument("--withdrawal-fee-policy", help="public opt-in personal fee bootstrap policy, committed by every V6 enrollment")
    c.add_argument("--ulr-policy", help="signed shared ULR bootstrap policy: create/reuse and relay/zk-state-v2")
    c.add_argument("--token-metadata", help="JSON names and symbols; committed before participant enrollment")
    c.add_argument("--bitcoin-custody-policy", help="custodian-only Bitcoin/EVM monetary epoch and pinned source graphs")
    c.add_argument("--system-policy", help="single enrolled Bitcoin + ULR policy, including canonical key family and address/signing boards")
    c.add_argument("--reward-token")
    c.add_argument("--reward-token-code-hash")
    c.add_argument("--reward-token-decimals")
    c.add_argument("--btc-usd-feed")
    c.add_argument("--btc-usd-feed-code-hash")
    c.add_argument("--evm-confirmations")
    c.add_argument("--deployment-fee-amount")
    c.add_argument("--deployment-fee-treasury")
    c.add_argument("--deployment-id", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--deadline", type=int, default=None)
    c = sub.add_parser(
        "inspect",
        help="display roster, thresholds, purposes and invitation fingerprint",
    )
    c.add_argument("--invite", required=True)
    c = sub.add_parser(
        "join", help="create own encrypted transport keys and sign enrollment"
    )
    c.add_argument("--invite", required=True)
    c.add_argument("--accept-fingerprint", required=True)
    c.add_argument("--identity-file", required=True)
    c.add_argument("--directory", required=True)
    c.add_argument("--bitcoin-rpc")
    c.add_argument("--bitcoin-cookie")
    c = sub.add_parser(
        "assemble", help="verify all EVM enrollments and create the bootstrap profile"
    )
    c.add_argument("--rpc", required=True)
    c.add_argument("--invite", required=True)
    c.add_argument("--enrollments", nargs="+", required=True)
    c.add_argument("--output", required=True)
    c = sub.add_parser(
        "manifest", help="export a checked ceremony bundle from a paused bootstrap"
    )
    c.add_argument("--profile", required=True)
    c.add_argument("--state", required=True)
    c.add_argument("--role", choices=ROLES, required=True)
    c.add_argument("--output", required=True)
    c = sub.add_parser(
        "publish", help="publish invitation on-chain, without an out-of-band channel"
    )
    c.add_argument("--rpc", required=True)
    c.add_argument("--invite", required=True)
    c.add_argument("--identity-file", required=True)
    c.add_argument("--state", required=True)
    c = sub.add_parser(
        "discover", help="find canonical invitations from a known friend EVM address"
    )
    c.add_argument("--operator", required=True)
    c.add_argument("--rpc", required=True)
    c.add_argument("--board", help="restrict eth_getLogs to this board address")
    c.add_argument("--log-page-size", type=int, default=2000, help="initial eth_getLogs page size in blocks")
    scope = c.add_mutually_exclusive_group()
    scope.add_argument("--from-block", type=int,
        help="explicit first block; 0 searches the full history")
    scope.add_argument("--recent-blocks", type=int, default=DISCOVERY_RECENT_BLOCKS,
        help="search only this many recent blocks (default: 8192)")
    scope.add_argument("--launch-config", type=Path,
        help="use the EVM network and invitation search floor from this launch config")
    c = sub.add_parser(
        "join-chain",
        help="read invitation and publish own signed transport keys on-chain",
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c.add_argument("--accept-fingerprint", required=True)
    c.add_argument("--identity-file", required=True)
    c.add_argument("--directory", required=True)
    c.add_argument("--bitcoin-rpc")
    c.add_argument("--bitcoin-cookie")
    c = sub.add_parser(
        "assemble-chain",
        help="read all enrollments directly from EVM into bootstrap profile",
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c.add_argument("--output", required=True)
    c = sub.add_parser(
        "manifest-chain", help="read and validate ceremony bundle directly from EVM"
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c.add_argument("--role", choices=ROLES, required=True)
    c.add_argument("--output", required=True)
    c = sub.add_parser(
        "status", help="read enrollments and DKG progress without an operator channel"
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c = sub.add_parser(
        "pending", help="read latest public signing intent directly from the board"
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c = sub.add_parser(
        "approve-custodian",
        help="approve reviewed on-chain intent as an actual custodian",
    )
    c.add_argument("--board", required=True)
    c.add_argument("--rpc", required=True)
    c.add_argument("--session", required=True, type=int)
    c.add_argument("--operation-hash", required=True)
    c.add_argument("--identity-file", required=True)
    c.add_argument("--state", required=True)
    c.add_argument('--ulr-config', help='Reviewed ULR pin for post-bootstrap custodian approvals')
    c.add_argument('--fee-sale-config', help='Independently reviewed fee sale and treasury pin')
    for action in ("join-chain", "assemble-chain", "manifest-chain", "status", "pending"):
        sub.choices[action].add_argument("--board-creation-block", type=int,
            help="untrusted creation block locator from discovery; constructor proof remains required")
    a = p.parse_args(argv)
    if a.action == "approve-custodian":
        from .custodian_cli import approve

        print(json.dumps(approve(a), indent=2))
        return 0
    if a.action in (
        "publish",
        "discover",
        "join-chain",
        "assemble-chain",
        "manifest-chain",
        "pending",
        "status",
    ):
        return chain_main(a)
    if a.action == "invite":
        system = None
        if a.system_policy:
            if a.ulr_policy or a.withdrawal_fee_policy:
                raise ValueError("SYSTEM_BOOTSTRAP_POLICY_CONFLICT")
            from .bootstrap_system import bundle
            system = bundle(read_public(a.system_policy))
        selected = {"mainnet": "1", "sepolia": "11155111", "local": "31337"}.get(a.ethereum_network)
        if selected is not None and a.chain_id is not None and selected != a.chain_id:
            raise ValueError("ETHEREUM_NETWORK_MISMATCH")
        a.chain_id = selected or a.chain_id or "31337"
        addresses = [Web3.to_checksum_address(x) for x in a.participants]
        public_bitcoin = is_public_network(a.bitcoin_network)
        backed = a.chain_id == "1" or public_bitcoin
        if a.chain_id == "1" and a.deadline is None:
            raise ValueError("ETHEREUM_MAINNET_DEADLINE_REQUIRED")
        mainnet_inputs = (a.reward_token, a.reward_token_code_hash, a.reward_token_decimals)
        if not (a.ulr_policy or system):
            mainnet_inputs += (a.btc_usd_feed, a.btc_usd_feed_code_hash)
        elif a.btc_usd_feed is not None or a.btc_usd_feed_code_hash is not None:
            raise ValueError("ULR_BTC_USD_FEED_UNUSED")
        if backed and not all(value is not None for value in mainnet_inputs):
            raise ValueError("MAINNET_INVITATION_INPUTS_REQUIRED")
        if not backed and any(value is not None for value in mainnet_inputs):
            raise ValueError("REGTEST_MAINNET_POLICY_MIXED")
        if public_bitcoin and not a.bitcoin_policy:
            raise ValueError("MAINNET_INVITATION_INPUTS_REQUIRED")
        if not public_bitcoin and a.bitcoin_policy is not None:
            raise ValueError("REGTEST_MAINNET_POLICY_MIXED")
        invite = dict(
            version="6",
            chain_id=a.chain_id,
            bitcoin_network=a.bitcoin_network,
            mode="backed" if backed else "integration",
            deployment_id=a.deployment_id,
            operator=addresses[0],
            treasury=Web3.to_checksum_address(a.treasury),
            approval_deadline=str(a.deadline or int(time.time()) + 7 * 86400),
            federation_id=a.deployment_id + "-fed",
            confederation_id=a.deployment_id + "-conf",
            participants=addresses,
            threshold=str(a.threshold),
            epoch="1",
            sessions={r: os.urandom(32).hex() for r in ROLES},
            protocol_id=PROTOCOL_ID.hex(),
            phase_duration=a.phase_duration or ("86400" if a.chain_id == "1" else "3600"),
            evm_confirmations=a.evm_confirmations or ("12" if a.chain_id == "1" else "1"),
        )
        invite.update({field: getattr(a, field) for field in NUMBER_BOUNDS})
        invite.update({field: getattr(a, field) == "true" for field in BOOLEAN_FIELDS})
        invite.update({field: Web3.to_checksum_address(getattr(a, field)) for field in ADDRESS_FIELDS})
        if a.withdrawal_fee_policy:
            invite["withdrawal_fee"] = read_public(a.withdrawal_fee_policy)
        if a.ulr_policy:
            invite["ulr"] = read_public(a.ulr_policy)
        if system:
            invite.update(system_bootstrap="1", ulr=system["ulr"], withdrawal_fee=system["withdrawal_fee"])
        if system and "token_metadata" in system:
            invite["token_metadata"] = system["token_metadata"]
        if system and "bitcoin_custody" in system:
            invite["bitcoin_custody"] = system["bitcoin_custody"]
        if a.bitcoin_custody_policy:
            custody = read_public(a.bitcoin_custody_policy)
            if "bitcoin_custody" in invite and custody != invite["bitcoin_custody"]:
                raise ValueError("BITCOIN_CUSTODY_CONFIG_CONFLICT")
            invite["bitcoin_custody"] = custody
        if a.token_metadata:
            names = read_public(a.token_metadata)
            if "token_metadata" in invite and names != invite["token_metadata"]:
                raise ValueError("TOKEN_METADATA_CONFIG_CONFLICT")
            invite["token_metadata"] = names
        if public_bitcoin:
            invite["bitcoin_policy"] = read_public(a.bitcoin_policy)
        if backed:
            invite["reward_token"] = dict(address=Web3.to_checksum_address(a.reward_token),
                runtime_hash=a.reward_token_code_hash, decimals=a.reward_token_decimals)
            if not (a.ulr_policy or system):
                invite["btc_usd_feed"] = dict(address=Web3.to_checksum_address(a.btc_usd_feed),
                    runtime_hash=a.btc_usd_feed_code_hash)
        if a.chain_id == "1":
            if not a.deployment_fee_amount or not a.deployment_fee_treasury:
                raise ValueError("ETHEREUM_MAINNET_INVITATION_INPUTS_REQUIRED")
            invite["deployment_fee"] = dict(amount=a.deployment_fee_amount,
                treasury=Web3.to_checksum_address(a.deployment_fee_treasury))
        elif a.deployment_fee_amount is not None or a.deployment_fee_treasury is not None:
            raise ValueError("ETHEREUM_MAINNET_POLICY_MIXED")
        validate_invite(invite)
        write_public(a.output, invite)
        result = dict(
            status="INVITED", fingerprint=fingerprint(invite), invitation=invite
        )
    elif a.action == "inspect":
        invite = validate_invite(read_public(a.invite))
        result = dict(
            status="REVIEW_REQUIRED", fingerprint=fingerprint(invite), invitation=invite
        )
    elif a.action == "join":
        invite = read_public(a.invite)
        result = join(
            invite, a.accept_fingerprint, a.identity_file, a.directory,
            bitcoin=local_bitcoin(a, invite),
        )
    elif a.action == "assemble":
        result = assemble(
            read_public(a.invite), [read_public(f) for f in a.enrollments], a.rpc
        )
        write_public(a.output, result)
        result = dict(status="PROFILE_READY", output=a.output)
    else:
        profile = read_public(a.profile)
        entry = Journal(a.state).get(profile["deployment_id"])
        bundle = make_manifest(profile, entry["result"], a.role, profile["evm_rpc"])
        write_public(a.output, bundle)
        result = dict(
            status="MANIFEST_READY",
            context=Manifest.from_dict(bundle).context.hex(),
            output=a.output,
        )
    print(json.dumps(result, indent=2))
    return 0


def local_bitcoin(args, invite):
    if not is_public_network(invite["bitcoin_network"]):
        if getattr(args, "bitcoin_rpc", None) or getattr(args, "bitcoin_cookie", None):
            raise ValueError("REGTEST_MAINNET_POLICY_MIXED")
        return None
    if not getattr(args, "bitcoin_rpc", None) or not getattr(args, "bitcoin_cookie", None):
        raise ValueError("MAINNET_CORE_REQUIRED")
    from .bitcoin_rpc import BitcoinRPC
    return BitcoinRPC(args.bitcoin_rpc, args.bitcoin_cookie, invite["bitcoin_network"], invite["bitcoin_policy"])


def chain_main(a):
    from .bootstrap import Bootstrap
    from .onchain_transport import (
        invitation,
        assembled_profile,
        ceremony_bundle,
        transact,
        bootstrap_status,
    )

    if a.action == "publish":
        invite = validate_invite(read_public(a.invite))
        from .platform_support import require_participant_prover
        require_participant_prover(invite)
        if invite["version"] not in ("3", "4", "5", "6"):
            raise ValueError("PUBLICATION_REQUIRES_V3_INVITATION_RECREATE_AND_REENROLL")
        account = Account.from_key(
            json.loads(read_private(a.identity_file))["private_key"]
        )
        if account.address != invite["operator"]:
            raise ValueError("INVITATION_OPERATOR_MISMATCH")
        profile = dict(
            deployment_id=invite["deployment_id"] + "-invitation",
            mode=invite["mode"],
            chain_id=invite["chain_id"],
            evm_rpc=a.rpc,
            bitcoin_network=invite["bitcoin_network"],
            evm_confirmations=invite.get("evm_confirmations", "1"),
            steps=[
                dict(
                    id="board",
                    kind="deploy",
                    artifact="DKGOnchainBoard",
                    args=[
                        "0x" + canonical(invite).encode().hex(),
                        invite["participants"],
                    ],
                )
            ],
        )
        result = Bootstrap(profile, a.state).apply(a.identity_file)
        if result["status"] == "VERIFIED":
            manifest = result["manifest"]
            deployment = next(step for step in manifest["steps"] if step["id"] == profile["deployment_id"] + ":board")
            result = dict(
                status=result["status"],
                board=manifest["contracts"]["board"]["address"],
                block=deployment["result"]["block_number"],
                fingerprint=fingerprint(invite),
            )
        else:
            # A caller-selected confirmation depth can leave a deployment
            # durably broadcast. Repeating publish resumes that exact tx.
            result = dict(status=result["status"], step=result.get("step"),
                          fingerprint=fingerprint(invite))
    else:
        w3 = Web3(Web3.HTTPProvider(a.rpc, request_kwargs={"timeout": 20}))
        chain_id = w3.eth.chain_id
        if chain_id not in (1, 31337, 11155111):
            raise ValueError("INTEGRATION_NETWORK_NOT_ALLOWED")
        if getattr(a, "board_creation_block", None) is not None:
            # Verify once with the public locator; later reads use the same
            # provider's authenticated proof cache, never trust the locator alone.
            invitation(w3, a.board, birth_hint=a.board_creation_block)
        if a.action == "discover":
            first = a.from_block
            if getattr(a, "launch_config", None) is not None:
                from .launch_configurator import CHAINS, validate

                launch = validate(json.loads(a.launch_config.read_text()))
                if CHAINS[launch["ethereum_network"]] != str(chain_id):
                    raise ValueError("CHAIN_MISMATCH")
                configured = launch["evm_discovery_from_block"]
                if configured is not None:
                    first = int(configured)
            result = discover_invitations(w3, a.operator, from_block=first,
                                          board_address=getattr(a, "board", None),
                                          page_size=getattr(a, "log_page_size", 2000),
                                          recent_blocks=getattr(a, "recent_blocks", DISCOVERY_RECENT_BLOCKS))
        elif a.action == "join-chain":
            board, invite = invitation(w3, a.board)
            from .platform_support import require_participant_prover
            require_participant_prover(invite)
            entry = join(invite, a.accept_fingerprint, a.identity_file, a.directory,
                         bitcoin=local_bitcoin(a, invite))
            transact(
                w3,
                a.identity_file,
                str(Path(a.directory) / "transactions.sqlite"),
                board,
                "enroll",
                ["0x" + canonical(entry).encode().hex()],
            )
            result = dict(
                status="ENROLLED_ONCHAIN",
                participant=entry["participant"],
                board=board.address,
                fingerprint=fingerprint(invite),
            )
        elif a.action == "status":
            result = bootstrap_status(w3, a.board)
        elif a.action == "pending":
            board, invite = invitation(w3, a.board)
            from .onchain_transport import _transport_block
            block = _transport_block(w3, int(invite.get("evm_confirmations", "12")))
            session = board.functions.latestSession().call(block_identifier=block.number)
            payload = board.functions.intents(session).call(block_identifier=block.number)
            result = dict(
                status="REVIEW_REQUIRED" if payload else "NO_SIGNING_INTENT",
                session=str(session),
                intent=json.loads(bytes(payload).decode()) if payload else None,
            )
        elif a.action == "assemble-chain":
            profile = assembled_profile(w3, a.board)
            # RPC endpoint is local routing, not a cryptographic identity.
            write_public(a.output, profile)
            result = dict(status="PROFILE_READY", output=a.output)
        else:
            bundle = ceremony_bundle(w3, a.board, a.role)
            write_public(a.output, bundle)
            result = dict(
                status="MANIFEST_READY",
                context=Manifest.from_dict(bundle).context.hex(),
                output=a.output,
            )
    print(json.dumps(result, indent=2))
    return 0
