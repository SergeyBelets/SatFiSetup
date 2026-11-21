"""Explicit Bitcoin network trust and bounded mainnet relay maintenance.

Live mainnet checks intentionally reject historical/header-only fixture nodes. The
policy is public reviewed configuration, never inferred from a remote RPC server.
Hashes in the policy and Core RPC are display order; contract hashes are wire order.
"""

import hashlib
import re
import time


MAINNET_GENESIS = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
TESTNET4_GENESIS = "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"
PUBLIC_NETWORKS = {"mainnet": ("main", MAINNET_GENESIS, "MainnetHeaderRelay"),
                   "testnet4": ("testnet4", TESTNET4_GENESIS, "Testnet4HeaderRelay")}
POW_LIMIT = (1 << 224) - 1
POLICY_KEYS = frozenset((
    "version", "network", "genesis_hash", "checkpoint_header", "checkpoint_height",
    "checkpoint_chainwork", "checkpoint_predecessors", "max_reorg_depth",
    "max_tip_age_seconds", "max_relay_lag_blocks", "max_headers_per_sync",
))


def is_public_network(network):
    return isinstance(network, str) and network in PUBLIC_NETWORKS


def relay_artifact(network):
    if network == "regtest":
        return "RegtestHeaderRelay"
    if not is_public_network(network):
        raise ValueError("BITCOIN_NETWORK_UNSUPPORTED")
    return PUBLIC_NETWORKS[network][2]


def _decimal(value, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError("MAINNET_POLICY_INTEGER")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError("MAINNET_POLICY_RANGE")
    return number


def _hex(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        raise ValueError("MAINNET_POLICY_HEX")
    return bytes.fromhex(value)


def header_hash(raw):
    """Return the raw SHA256d digest used by the relay."""
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def _header(raw_hex):
    raw = _hex(raw_hex, 160)
    bits = int.from_bytes(raw[72:76], "little")
    exponent, mantissa = bits >> 24, bits & 0x7fffff
    if bits & 0x800000 or not mantissa or exponent > 34:
        raise ValueError("MAINNET_HEADER_TARGET")
    target = (mantissa >> (8 * (3 - exponent))) if exponent <= 3 else (mantissa << (8 * (exponent - 3)))
    if not 0 < target <= POW_LIMIT:
        raise ValueError("MAINNET_HEADER_TARGET")
    # Reject alternate non-canonical compact encodings, as the relay does.
    size = (target.bit_length() + 7) // 8
    compact = target << (8 * (3 - size)) if size <= 3 else target >> (8 * (size - 3))
    if compact & 0x800000:
        compact >>= 8
        size += 1
    if bits != (size << 24) | compact:
        raise ValueError("MAINNET_HEADER_TARGET")
    digest = header_hash(raw)
    if int.from_bytes(digest, "little") > target:
        raise ValueError("MAINNET_HEADER_POW")
    return raw, digest, (1 << 256) // (target + 1)


def validate_policy(policy, expected_network=None):
    """Validate offline structure/PoW. This does NOT establish a live Core anchor."""
    if not isinstance(policy, dict):
        raise ValueError("MAINNET_POLICY_FIELDS")
    zk = policy.get("version") == "2"
    if set(policy) != POLICY_KEYS | ({"zk_spv"} if zk else set()):
        raise ValueError("MAINNET_POLICY_FIELDS")
    network = policy["network"]
    if (policy["version"] not in ("1", "2") or not is_public_network(network)
            or policy["genesis_hash"] != PUBLIC_NETWORKS[network][1]
            or expected_network is not None and network != expected_network):
        raise ValueError("MAINNET_POLICY_NETWORK")
    height = _decimal(policy["checkpoint_height"], 2016, (1 << 64) - 1)
    if height % 2016:
        raise ValueError("MAINNET_POLICY_CHECKPOINT_BOUNDARY")
    work = int.from_bytes(_hex(policy["checkpoint_chainwork"], 64), "big")
    checkpoint, _, minimum_work = _header(policy["checkpoint_header"])
    if work < minimum_work:
        raise ValueError("MAINNET_POLICY_CHAINWORK")
    prior = policy["checkpoint_predecessors"]
    if not isinstance(prior, list) or len(prior) != 10:
        raise ValueError("MAINNET_POLICY_PREDECESSORS")
    previous = None
    for value in [*prior, policy["checkpoint_header"]]:
        raw, digest, _ = _header(value)
        if previous is not None and raw[4:36] != previous:
            raise ValueError("MAINNET_POLICY_PREDECESSORS")
        previous = digest
    # Operational limits bound RPC work and EVM transactions, not Bitcoin consensus.
    _decimal(policy["max_reorg_depth"], 1, 144)
    _decimal(policy["max_tip_age_seconds"], 1, 86400)
    _decimal(policy["max_relay_lag_blocks"], 0, 144)
    _decimal(policy["max_headers_per_sync"], 1, 64)
    if zk:
        from .zk_spv import validate_zk_policy
        validate_zk_policy(policy)
    return dict(policy, checkpoint_predecessors=list(prior))


def relay_constructor_args(policy):
    policy = validate_policy(policy)
    if policy["version"] != "1":
        raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
    return [bytes.fromhex(policy["checkpoint_header"]), int(policy["checkpoint_height"]),
            int(policy["checkpoint_chainwork"], 16),
            [bytes.fromhex(value) for value in policy["checkpoint_predecessors"]],
            int(policy["max_reorg_depth"])]


def checkpoint_policy(bitcoin, *, checkpoint_height, max_reorg_depth,
                      max_tip_age_seconds, max_relay_lag_blocks, max_headers_per_sync, network=None):
    """Build a public review candidate from the operator's fully validated Core.

    A candidate must still be independently compared/reviewed before it is included
    in an invitation. This helper never sends transactions or chooses policy values.
    """
    height = _decimal(checkpoint_height, 2016, (1 << 64) - 1)
    if height % 2016:
        raise ValueError("MAINNET_POLICY_CHECKPOINT_BOUNDARY")
    network = network or getattr(bitcoin, "network", "mainnet")
    if getattr(bitcoin, "network", network) != network:
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    if not is_public_network(network) or bitcoin.call("getblockchaininfo").get("chain") != PUBLIC_NETWORKS[network][0]:
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    checkpoint, raw = _core_header(bitcoin, height)
    prior = []
    for candidate in range(height - 10, height):
        block_hash = bitcoin.call("getblockhash", candidate)
        prior.append(bitcoin.call("getblockheader", block_hash, False))
    policy = validate_policy(dict(
        version="1", network=network, genesis_hash=PUBLIC_NETWORKS[network][1],
        checkpoint_header=raw, checkpoint_height=checkpoint_height,
        checkpoint_chainwork=checkpoint["chainwork"], checkpoint_predecessors=prior,
        max_reorg_depth=max_reorg_depth, max_tip_age_seconds=max_tip_age_seconds,
        max_relay_lag_blocks=max_relay_lag_blocks, max_headers_per_sync=max_headers_per_sync,
    ))
    check_core(bitcoin, network, policy)
    return policy


def _rpc_int(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("MAINNET_CORE_RESPONSE")
    return value


def _fresh(timestamp, policy, now):
    age = now - _rpc_int(timestamp)
    if age > int(policy["max_tip_age_seconds"]) or age < -7200:
        raise ValueError("MAINNET_TIP_STALE")


def _core_header(bitcoin, height):
    display_hash = bitcoin.call("getblockhash", height)
    _hex(display_hash, 64)
    verbose = bitcoin.call("getblockheader", display_hash, True)
    raw_hex = bitcoin.call("getblockheader", display_hash, False)
    raw, digest, _ = _header(raw_hex)
    if (digest[::-1].hex() != display_hash or verbose.get("hash") != display_hash
            or _rpc_int(verbose.get("height")) != height
            or _rpc_int(verbose.get("confirmations"), 1) < 1
            or verbose.get("time") != int.from_bytes(raw[68:72], "little")
            or verbose.get("bits") != raw[72:76][::-1].hex()):
        raise ValueError("MAINNET_CORE_HEADER_MISMATCH")
    _hex(verbose.get("chainwork"), 64)
    return verbose, raw_hex


def check_core(bitcoin, network="regtest", policy=None, *, now=None):
    """Fail closed before mainnet signing/broadcast/proof or relay maintenance."""
    for attempt in range(3):
        try:
            return _check_core(bitcoin, network, policy, now=now)
        except ValueError as error:
            if attempt == 2 or str(error) not in (
                "MAINNET_CORE_NOT_SYNCED", "MAINNET_TXINDEX_NOT_SYNCED",
            ):
                raise
            time.sleep(0.5 * (attempt + 1))


def _check_core(bitcoin, network, policy, *, now):
    if network != "regtest" and not is_public_network(network):
        raise ValueError("BITCOIN_NETWORK_UNSUPPORTED")
    if is_public_network(network):
        policy = validate_policy(policy, network)
    elif policy is not None:
        raise ValueError("REGTEST_POLICY_FORBIDDEN")
    info = bitcoin.call("getblockchaininfo")
    if info.get("chain") != (PUBLIC_NETWORKS[network][0] if is_public_network(network) else "regtest"):
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    if network == "regtest":
        return info
    if bitcoin.call("getblockhash", 0) != PUBLIC_NETWORKS[network][1]:
        raise ValueError("MAINNET_GENESIS_MISMATCH")
    height = _rpc_int(info.get("blocks"))
    if info.get("initialblockdownload") is not False or _rpc_int(info.get("headers")) != height:
        raise ValueError("MAINNET_CORE_NOT_SYNCED")
    if info.get("pruned") is not False:
        raise ValueError("MAINNET_CORE_PRUNED")
    index = bitcoin.call("getindexinfo", "txindex").get("txindex", {})
    if index.get("synced") is not True or _rpc_int(index.get("best_block_height")) != height:
        raise ValueError("MAINNET_TXINDEX_NOT_SYNCED")
    checkpoint_height = int(policy["checkpoint_height"])
    if height < checkpoint_height + int(policy["max_reorg_depth"]):
        raise ValueError("MAINNET_CHECKPOINT_TOO_RECENT")
    checkpoint, raw = _core_header(bitcoin, checkpoint_height)
    if raw != policy["checkpoint_header"] or checkpoint["chainwork"] != policy["checkpoint_chainwork"]:
        raise ValueError("MAINNET_CHECKPOINT_MISMATCH")
    # The full predecessor bytes are trusted deployment inputs: compare all ten to Core.
    for offset, expected in enumerate(policy["checkpoint_predecessors"]):
        prior_hash = bitcoin.call("getblockhash", checkpoint_height - 10 + offset)
        if bitcoin.call("getblockheader", prior_hash, False) != expected:
            raise ValueError("MAINNET_CHECKPOINT_MISMATCH")
    tip, _ = _core_header(bitcoin, height)
    if (tip["hash"] != info.get("bestblockhash") or tip["chainwork"] != info.get("chainwork")
            or int(tip["chainwork"], 16) < int(policy["checkpoint_chainwork"], 16)):
        raise ValueError("MAINNET_CORE_TIP_MISMATCH")
    _fresh(tip["time"], policy, int(time.time()) if now is None else now)
    if bitcoin.call("getblockhash", height) != info["bestblockhash"]:
        raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
    return info


def _read(relay, method, *args, block_identifier="latest"):
    return getattr(relay.functions, method)(*args).call(block_identifier=block_identifier)


def _relay_snapshot(relay, policy, block_identifier):
    def read(method, *args):
        return _read(relay, method, *args, block_identifier=block_identifier)

    if read("testHelpersEnabled") is not False:
        raise ValueError("MAINNET_RELAY_TEST_HELPERS")
    if read("halted") is not False:
        raise ValueError("MAINNET_RELAY_HALTED")
    checkpoint = header_hash(bytes.fromhex(policy["checkpoint_header"]))
    if (bytes(read("checkpointHash")) != checkpoint
            or read("checkpointHeight") != int(policy["checkpoint_height"])
            or read("checkpointChainWork") != int(policy["checkpoint_chainwork"], 16)
            or read("maxReorgDepth") != int(policy["max_reorg_depth"])):
        raise ValueError("MAINNET_RELAY_POLICY_MISMATCH")
    head = bytes(read("getMainchainHead"))
    data = read("blocks", head)
    if len(head) != 32 or head == bytes(32) or data[3] != read("getMainchainHeight"):
        raise ValueError("MAINNET_RELAY_HEAD_MISMATCH")
    return head, data


def inspect_relay(bitcoin, relay, policy, *, require_fresh=True, block_identifier="latest", now=None):
    """Require relay to agree with the locally validated Core chain and reviewed policy."""
    policy = validate_policy(policy)
    if policy["version"] != "1":
        raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
    core = check_core(bitcoin, policy["network"], policy, now=now)
    # A newly observed halt blocks spending immediately, even before EVM finality.
    if _read(relay, "halted"):
        raise ValueError("MAINNET_RELAY_HALTED")
    head, data = _relay_snapshot(relay, policy, block_identifier)
    height = int(data[3])
    if height > core["blocks"]:
        raise ValueError("MAINNET_RELAY_AHEAD_OF_CORE")
    verbose, _ = _core_header(bitcoin, height)
    if bytes.fromhex(verbose["hash"])[::-1] != head:
        raise ValueError("MAINNET_RELAY_REORG_REQUIRED")
    if int(verbose["chainwork"], 16) != int(data[2]) or int(data[4]) != verbose["time"]:
        raise ValueError("MAINNET_RELAY_WORK_MISMATCH")
    lag = core["blocks"] - height
    if require_fresh:
        if lag > int(policy["max_relay_lag_blocks"]):
            raise ValueError("MAINNET_RELAY_STALE")
        try:
            _fresh(int(data[4]), policy, int(time.time()) if now is None else now)
        except ValueError:
            raise ValueError("MAINNET_RELAY_STALE") from None
    if bitcoin.call("getblockhash", core["blocks"]) != core["bestblockhash"]:
        raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
    return dict(status="MAINNET_RELAY_READY", network=policy["network"], height=str(height), core_height=str(core["blocks"]),
                head=head.hex(), core_head=core["bestblockhash"], chainwork=verbose["chainwork"], lag=str(lag))


def _confirmed_snapshot(relay, confirmations):
    if type(confirmations) is not int or not 1 <= confirmations <= 256:
        raise ValueError("MAINNET_EVM_CONFIRMATIONS")
    if relay.w3.eth.chain_id == 1:
        from .evm_network import read_block
        block = read_block(relay.w3, confirmations)
        return block["number"], (block["number"], bytes(block["hash"]))
    number = relay.w3.eth.block_number - confirmations + 1
    if number < 0:
        raise ValueError("MAINNET_EVM_CONFIRMATIONS")
    block = relay.w3.eth.get_block(number)
    return number, (number, bytes(block.hash))


def _check_evm_snapshot(relay, anchor):
    if anchor is not None and bytes(relay.w3.eth.get_block(anchor[0]).hash) != anchor[1]:
        raise ValueError("BTC_REGISTRY_READ_REORG")


def sync_relay(bitcoin, relay, policy, send_header, *, now=None, evm_confirmations=1, send_headers=None):
    """Submit at most the reviewed bound, through a caller-supplied durable sender.

    send_header(raw_hex) must journal the EVM transaction before sending and wait
    for its canonical receipt. A pending callback is detected from contract state.
    Re-running resumes from stored headers; no new nonce or signing data is used.
    """
    policy = validate_policy(policy)
    if policy["version"] != "1":
        raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
    core = check_core(bitcoin, policy["network"], policy, now=now)
    if _read(relay, "halted"):
        raise ValueError("MAINNET_RELAY_HALTED")
    recover = getattr(send_headers, "recover_pending", None)
    if recover is not None:
        # A competing keeper may have completed every header while our signed
        # transaction was dropped. Settle that exact nonce reservation before
        # returning READY, even when the next canonical chunk would be empty.
        recovered = recover()
        if recovered is not None and (not isinstance(recovered, dict)
                or recovered.get("status") not in (1, "VERIFIED", "CONFIRMED")):
            return dict(status="IN_PROGRESS", submitted="0", reason="EVM_HEADER_PENDING")
    block_identifier, anchor = _confirmed_snapshot(relay, evm_confirmations)
    head, data = _relay_snapshot(relay, policy, block_identifier)
    height, checkpoint = int(data[3]), int(policy["checkpoint_height"])
    if int(data[2]) > int(core["chainwork"], 16):
        raise ValueError("MAINNET_CORE_CHAINWORK_BEHIND")
    floor = max(checkpoint, height - int(policy["max_reorg_depth"]))
    common = None
    for candidate in range(min(height, core["blocks"]), floor - 1, -1):
        current = bytes(_read(relay, "height2hash", candidate, block_identifier=block_identifier))
        if current == bytes.fromhex(bitcoin.call("getblockhash", candidate))[::-1]:
            common = candidate
            break
    if common is None:
        raise ValueError("MAINNET_RELAY_REORG_LIMIT")
    _check_evm_snapshot(relay, anchor)
    # Verify chainwork before extending any selected or competing branch.
    common_header, _ = _core_header(bitcoin, common)
    common_data = _read(relay, "blocks", bytes.fromhex(common_header["hash"])[::-1])
    if int(common_data[2]) != int(common_header["chainwork"], 16):
        raise ValueError("MAINNET_RELAY_WORK_MISMATCH")
    submitted = 0
    queued = []
    # Previously stored fork headers must not consume this call's submission budget;
    # otherwise a fork longer than the batch limit could never make forward progress.
    limit = int(policy["max_headers_per_sync"])
    if send_headers is not None:
        # The invitation is an upper bound, not a requirement to fill one tx.
        # Ancestry pointer writes make a 64-header batch exceed Ethereum's
        # transaction gas cap. Bound fresh chunks before any nonce reservation;
        # the sender still estimates with margin and enforces the local cap.
        limit = min(limit, 16)
    end = min(core["blocks"], common + int(policy["max_reorg_depth"]) + limit)
    for candidate in range(common + 1, end + 1):
        if _read(relay, "halted"):
            raise ValueError("MAINNET_RELAY_HALTED")
        # A concurrent reorg requires rebuilding this bounded batch from a new snapshot.
        if bitcoin.call("getblockhash", core["blocks"]) != core["bestblockhash"]:
            raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
        verbose, raw = _core_header(bitcoin, candidate)
        digest = bytes.fromhex(verbose["hash"])[::-1]
        stored = _read(relay, "blocks", digest)
        if stored[3] and not _read(relay, "blocks", digest, block_identifier=block_identifier)[3]:
            # A mined header is not yet a consensus input. Do not start a new
            # growing-tip chunk while the prior receipt awaits EVM finality.
            return dict(status="IN_PROGRESS", submitted="0", reason="EVM_HEADER_PENDING")
        if not stored[3]:
            if submitted >= limit:
                break
            if send_headers is not None:
                queued.append((raw, digest, candidate, int(verbose["chainwork"], 16)))
                submitted += 1
                continue
            result = send_header(raw)
            submitted += 1
            if _read(relay, "halted"):
                raise ValueError("MAINNET_RELAY_HALTED")
            if isinstance(result, dict) and result.get("status") not in (None, 1, "VERIFIED", "CONFIRMED"):
                return dict(status="IN_PROGRESS", submitted=str(submitted), reason="EVM_HEADER_PENDING")
            stored = _read(relay, "blocks", digest)
            if not stored[3]:
                return dict(status="IN_PROGRESS", submitted=str(submitted), reason="EVM_HEADER_PENDING")
        if int(stored[3]) != candidate or int(stored[2]) != int(verbose["chainwork"], 16):
            raise ValueError("MAINNET_RELAY_WORK_MISMATCH")
    if queued:
        # One bounded atomic EVM transaction. Reorg protection and the canonical
        # postconditions are identical to the single-header path.
        if bitcoin.call("getblockhash", core["blocks"]) != core["bestblockhash"]:
            raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
        result = send_headers([entry[0] for entry in queued])
        if _read(relay, "halted"):
            raise ValueError("MAINNET_RELAY_HALTED")
        if isinstance(result, dict) and result.get("status") not in (None, 1, "VERIFIED", "CONFIRMED"):
            return dict(status="IN_PROGRESS", submitted=str(submitted), reason="EVM_HEADER_PENDING")
        for _, digest, candidate, work in queued:
            stored = _read(relay, "blocks", digest)
            if not stored[3]:
                return dict(status="IN_PROGRESS", submitted=str(submitted), reason="EVM_HEADER_PENDING")
            if int(stored[3]) != candidate or int(stored[2]) != work:
                raise ValueError("MAINNET_RELAY_WORK_MISMATCH")
    try:
        block_identifier, anchor = _confirmed_snapshot(relay, evm_confirmations)
        checked = inspect_relay(bitcoin, relay, policy, now=now, block_identifier=block_identifier)
        _check_evm_snapshot(relay, anchor)
    except ValueError as exc:
        if str(exc) not in ("MAINNET_RELAY_STALE", "MAINNET_RELAY_REORG_REQUIRED"):
            raise
        return dict(status="IN_PROGRESS", submitted=str(submitted), reason="RELAY_NOT_FRESH")
    return dict(checked, submitted=str(submitted))
