"""Pinned ULR treasury graphs and reproducible historical wallet+stake snapshots.

The inventory is reconciled against the canonical token's fixed supply at the
snapshot block. The shared FROST authority attests the resulting report.
"""

import hashlib
import json
import re
from pathlib import Path

from web3 import Web3
from web3._utils.events import get_event_data

from .bootstrap import artifact, encode_args
from .bitcoin_trust import checked_runtime, runtime_artifact
from .contract_provenance import prove_creation
from .dao_client import address, wire
from .operations import canonical, public_data
from .evm_network import read_block

KINDS = ("ULRCustodianSnapshots", "ULRConfederationTreasury")
MIN_ULR = 200_000 * 10**18
SUPPLY = 10_000_000 * 10**18
MAX_HOLDERS = 4096
MAX_BOARD_INTENT_BYTES = 32768
# Posting full intents writes each 32-byte word to a new storage slot. The
# board's size limit exceeds the usable space under the transaction gas cap.
MAX_INLINE_SNAPSHOT_BYTES = 16 * 1024
MAX_SNAPSHOT_INTENT_BYTES = 4 * 1024 * 1024
SNAPSHOT_METHOD = "getPublishDayOperationHash"
ACTIVATE_TREASURY_METHOD = "getActivateTreasuryOperationHash"
SNAPSHOT_INTENT_HASH = "ulr_snapshot_intent_sha256"


def requires_review(intent, bundle):
    """Derive mandatory ULR review from enrollment, not removable intent labels."""
    kind = intent.get("artifact")
    if kind in (*KINDS, "CustodianValidator"):
        return True
    if kind not in ("CustodianDAOFeeRouter", "CustodianDAODecisionBoard"):
        return False
    invite = bundle.get("onboarding", {}).get("invitation", {})
    enrolled = bundle.get("role") == "conf_ceremony" and isinstance(invite.get("ulr"), dict)
    if enrolled and "ulr_treasury" not in intent:
        raise ValueError("ULR_PUBLIC_INTENT_AND_PIN_REQUIRED")
    return enrolled or "ulr_treasury" in intent


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError("ULR_DECIMAL_STRING_REQUIRED")
    result = int(value)
    if result >= 2**256:
        raise ValueError("ULR_INTEGER_RANGE")
    return result


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value) or int(value, 16) == 0:
        raise ValueError("ULR_NONZERO_HASH_REQUIRED")
    return bytes.fromhex(value[2:])


def load_pin(path):
    pin = public_data(json.loads(Path(path).read_text()))
    if set(pin) != {"version", "chain_id", "staking", "snapshots", "confirmations", "treasuries"} or pin["version"] != "1":
        raise ValueError("ULR_PIN_REQUIRED")
    if pin["chain_id"] not in ("1", "31337", "11155111"):
        raise ValueError("ULR_CHAIN_POLICY")
    if not (12 if pin["chain_id"] == "1" else 1) <= number(pin["confirmations"]) <= 256:
        raise ValueError("ULR_FINALITY_POLICY")
    for field in ("staking", "snapshots"):
        if address(pin[field]) != pin[field]:
            raise ValueError("ULR_CHECKSUM_ADDRESS_REQUIRED")
    targets = pin["treasuries"]
    if not isinstance(targets, list) or len(targets) > 64 or len(set(targets)) != len(targets):
        raise ValueError("ULR_TREASURY_INVENTORY")
    for target in targets:
        if address(target) != target:
            raise ValueError("ULR_CHECKSUM_ADDRESS_REQUIRED")
    return pin


def contract(w3, kind, target):
    return w3.eth.contract(address=address(target), abi=artifact(kind)["abi"])


def read(target, at, method, *args):
    return getattr(target.functions, method)(*args).call(block_identifier=at)


def proven(w3, kind, target, at, args=None):
    target = contract(w3, kind, target)
    checked_runtime(w3, target.address, kind, at)
    prove_creation(w3, target.address, kind, args, at)
    return target


def authority_context(w3, target, at):
    auth = contract(w3, "DKGAuthorization", target)
    coordinator_address = read(auth, at, "coordinator")
    coordinator = contract(w3, "FROSTCoordinator", coordinator_address)
    verifier = read(coordinator, at, "defaultVerifier")
    proven(w3, "FROSTVerifier", verifier, at, [])
    proven(w3, "FROSTCoordinator", coordinator_address, at, [verifier])
    registry_address = read(auth, at, "registry")
    registry = proven(w3, "DKGCoordinator", registry_address, at, [])
    dkg_id = read(auth, at, "dkgId")
    completed = read(registry, at, "getCompletedResult", dkg_id)
    if (read(auth, at, "verifier") != verifier
            or read(coordinator, at, "dkgRegistry") != registry.address
            or bytes(completed[3]) != Web3.keccak(text="EVM_AUTH")
            or bytes(read(auth, at, "resultHash")) != bytes(completed[1])
            or bytes(read(auth, at, "groupPubkey")) != bytes(completed[4])
            or list(read(auth, at, "getParticipants")) != list(completed[5])
            or read(auth, at, "threshold") != completed[6]):
        raise ValueError("ULR_AUTHORIZATION_DKG_BINDING")
    proven(w3, "DKGAuthorization", target, at,
           [coordinator_address, dkg_id, bytes(completed[4]), list(completed[5]), int(completed[6])])
    return auth


def staking_args(staking, at):
    return [read(staking, at, name) for name in ("stakeToken", "rewardToken", "CUSTODIAN_THRESHOLD", "minimumStakeAmount")]


def treasury_args(treasury, at):
    return [read(treasury, at, name) for name in ("factory", "confederationId", "snapshots", "reserveFloor", "payoutBps")]


def configuration(w3, pin, at):
    if str(w3.eth.chain_id) != pin["chain_id"]:
        raise ValueError("ULR_CHAIN_MISMATCH")
    staking = contract(w3, "ULRStaking", pin["staking"])
    proven(w3, "ULRStaking", staking.address, at, staking_args(staking, at))
    registry = contract(w3, "ULRCustodianSnapshots", pin["snapshots"])
    authority = read(registry, at, "authorization")
    if read(registry, at, "staking") != staking.address:
        raise ValueError("ULR_STAKING_BINDING")
    proven(w3, "ULRCustodianSnapshots", registry.address, at, [staking.address, authority])
    authority_context(w3, authority, at)
    return registry, staking


def treasury_context(w3, pin, target, at, *, configured=None, require_active=True):
    registry, staking = configured or configuration(w3, pin, at)
    if target not in pin["treasuries"]:
        raise ValueError("ULR_TREASURY_NOT_PINNED")
    treasury = contract(w3, "ULRConfederationTreasury", target)
    factory = read(treasury, at, "factory")
    conf_id = read(treasury, at, "confederationId")
    proven(w3, "ULRConfederationTreasury", target, at, treasury_args(treasury, at))
    factory = proven(w3, "ConfederationFactory", factory, at)
    conf = read(factory, at, "getConfederation", conf_id)
    if ((require_active and not conf[-1]) or read(treasury, at, "snapshots") != registry.address
            or read(treasury, at, "staking") != staking.address
            or read(treasury, at, "authorization") != conf[7]
            or read(treasury, at, "validator") != conf[8]
            or read(treasury, at, "rewardToken") != read(staking, at, "rewardToken")):
        raise ValueError("ULR_CONFEDERATION_BINDING")
    authority_context(w3, conf[7], at)
    proven(w3, "CustodianValidator", conf[8], at)
    return registry, staking, treasury, factory, conf_id, conf


def snapshot_treasuries(w3, registry, day, anchor, *, pin, at):
    """Use the registry's enrollment history at the agreed UTC cutoff."""
    height = anchor.number
    _, reviewed = runtime_artifact(w3, registry.address, "ULRCustodianSnapshots", height)
    if "legacy_reference" not in reviewed:
        return set(read(registry, height, "snapshotTreasuries", day))

    from .evm_log_paging import log_pages

    # Historical logs in older registries may include enrollments outside the
    # approved factory graph. Derive trusted factories from reviewed pins first.
    factories, required = set(), set()
    for candidate in pin["treasuries"]:
        _, _, treasury, factory, _, _ = treasury_context(w3, pin, candidate, at, require_active=False)
        factories.add(factory.address)
        scope = read(treasury, at, "scope")
        if read(registry, height, "treasuries", scope) == treasury.address:
            required.add(treasury.address)

    cache = getattr(w3, "_ulr_treasury_history", None)
    if cache is None:
        cache = w3._ulr_treasury_history = {}
    key = registry.address, height, bytes(anchor.hash), frozenset(factories), frozenset(required)
    if key in cache:
        return cache[key]
    proof = prove_creation(w3, registry.address, "ULRCustodianSnapshots", None, height)
    event = next(item for item in reviewed["abi"] if item.get("name") == "TreasuryEnrolled")
    topic = Web3.to_hex(Web3.keccak(text="TreasuryEnrolled(bytes32,address,address,string)"))
    scopes, targets = set(), set()
    for page in log_pages(w3, int(proof["block_number"]), height, topics=[topic],
                          initial_span=100_000, address=registry.address):
        for log in page:
            decoded = get_event_data(w3.codec, event, log)
            if decoded["args"]["factory"] not in factories:
                continue
            scope, treasury = decoded["args"]["scope"], decoded["args"]["treasury"]
            number = decoded["blockNumber"]
            if (decoded["address"] != registry.address or log.get("removed")
                    or not int(proof["block_number"]) <= number <= height or scope in scopes or treasury in targets
                    or bytes(w3.eth.get_block(number).hash) != bytes(decoded["blockHash"])
                    or read(registry, height, "treasuries", scope) != treasury):
                raise ValueError("ULR_TREASURY_HISTORY_MISMATCH")
            scopes.add(scope)
            targets.add(treasury)
            if len(targets) > 64:
                raise ValueError("ULR_TREASURY_INVENTORY")
    if not required <= targets:
        raise ValueError("ULR_TREASURY_HISTORY_MISMATCH")
    if bytes(w3.eth.get_block(height).hash) != bytes(anchor.hash):
        raise ValueError("ULR_SNAPSHOT_REORG")
    cache[key] = frozenset(targets)
    return cache[key]


def snapshot_blocks(w3, staking, day, height, at):
    current = w3.eth.get_block(at)
    if day < read(staking, at, "firstEligibleDay") or day >= current.timestamp // 86400 or height >= current.number:
        raise ValueError("ULR_SNAPSHOT_DAY")
    anchor, following = w3.eth.get_block(height), w3.eth.get_block(height + 1)
    end = (day + 1) * 86400
    if (anchor.number != height or following.number != height + 1
            or bytes(following.parentHash) != bytes(anchor.hash)
            or not anchor.timestamp < end <= following.timestamp or following.number > current.number):
        raise ValueError("ULR_LAST_BLOCK_OF_DAY_REQUIRED")
    return current, anchor, following


def snapshot_arguments(w3, pin, inputs, at):
    """Recompute every reported weight at the last EVM block of a completed UTC day."""
    public_data(inputs)
    if set(inputs) != {"day", "snapshot_block", "inventory_evidence_hash", "owners", "treasuries"}:
        raise ValueError("ULR_SNAPSHOT_INPUTS")
    registry, staking = configuration(w3, pin, at)
    day, height = number(inputs["day"]), number(inputs["snapshot_block"])
    current, anchor, following = snapshot_blocks(w3, staking, day, height, at)
    digest(inputs["inventory_evidence_hash"])
    candidates = inputs["owners"]
    if not isinstance(candidates, list) or len(candidates) > MAX_HOLDERS or len(set(candidates)) != len(candidates):
        raise ValueError("ULR_HOLDER_INVENTORY")
    if len(set(address(x) for x in candidates)) != len(candidates):
        raise ValueError("ULR_HOLDER_INVENTORY")
    targets = inputs["treasuries"]
    if (not isinstance(targets, list) or not targets or len(targets) > 64 or len(set(targets)) != len(targets)):
        raise ValueError("ULR_TREASURY_INVENTORY")
    eligible_treasuries = snapshot_treasuries(w3, registry, day, anchor, pin=pin, at=at)
    if not eligible_treasuries <= set(pin["treasuries"]):
        raise ValueError("ULR_TREASURY_PIN_BEHIND")
    if set(targets) != eligible_treasuries:
        raise ValueError("ULR_COMPLETE_TREASURY_INVENTORY_REQUIRED")
    allocations, conf_evidence, roster_records, active = [], [], [], set()
    for target in targets:
        _, _, treasury, _, _, conf = treasury_context(
            w3, pin, target, height, configured=(registry, staking), require_active=False
        )
        scope = read(treasury, height, "scope")
        if read(registry, height, "treasuries", scope) != target:
            raise ValueError("ULR_TREASURY_NOT_ENROLLED")
        if conf[8] != read(treasury, height, "validator"):
            raise ValueError("ULR_HISTORICAL_CONFEDERATION_BINDING")
        validator = contract(w3, "CustodianValidator", conf[8])
        members = read(validator, height, "getCustodians") if conf[-1] else []
        active.update(members)
        roster_records.append((target, scope, validator.address, members))
    owners, weights, observations = [], [], []
    token = proven(w3, "CustodianDAOToken", read(staking, at, "stakeToken"), at)
    threshold = read(staking, at, "CUSTODIAN_THRESHOLD")
    supply = read(token, height, "totalSupply")
    staking_balance = read(token, height, "balanceOf", staking.address)
    total_staked = read(staking, height, "totalStaked")
    token_balance = read(token, height, "balanceOf", token.address)
    if supply != SUPPLY or not 0 <= total_staked <= staking_balance or not 0 <= token_balance <= supply - staking_balance:
        raise ValueError("ULR_HOLDER_INVENTORY_SUPPLY")
    # Tokens sent directly to the token or staking contract have no attributed owner.
    expected_total = supply - token_balance - (staking_balance - total_staked)
    inventory = set(address(x) for x in candidates) | active
    if len(inventory) > MAX_HOLDERS:
        raise ValueError("ULR_HOLDER_INVENTORY")
    observed_total = 0
    for owner in sorted(inventory, key=lambda x: int(x, 16)):
        if owner in (staking.address, token.address) or int(owner, 16) == 0:
            raise ValueError("ULR_SYSTEM_HOLDER_EXCLUDED")
        wallet = read(token, height, "balanceOf", owner)
        staked = read(staking, height, "stakedBalance", owner)
        weight = wallet + staked
        observed_total += weight
        observations.append(dict(owner=owner, wallet=str(wallet), staked=str(staked), weight=str(weight),
            active_custodian=owner in active))
        if weight >= threshold or (owner in active and weight > 0):
            owners.append(owner)
            weights.append(weight)
    unaccounted = expected_total - observed_total
    # Active custodians are already included from the pinned validators. The
    # remainder cannot contain any other eligible owner if it is below threshold.
    if not 0 <= unaccounted < threshold:
        raise ValueError("ULR_HOLDER_INVENTORY_INCOMPLETE")
    for target, scope, validator_address, members in roster_records:
        member_set = set(members)
        indices = [i for i, owner in enumerate(owners) if owner in member_set]
        roster_hash = Web3.keccak(w3.codec.encode(["address[]"], [members]))
        allocations.append([scope, b"".join(i.to_bytes(2, "big") for i in indices), roster_hash])
        conf_evidence.append(dict(treasury=target, scope=Web3.to_hex(scope), validator=validator_address,
                                  custodians=members, custodian_indices=indices))
    allocations.sort(key=lambda row: bytes(row[0]))
    evidence = canonical(dict(version="4", eligibility_policy="HOLDERS_THRESHOLD_PLUS_ACTIVE_RESIDUAL_V1",
        threshold_wei=str(threshold), attributed_supply_wei=str(expected_total),
        observed_wei=str(observed_total), unaccounted_wei=str(unaccounted),
        unattributed_staking_balance_wei=str(staking_balance - total_staked), token_balance_wei=str(token_balance),
        chain_id=pin["chain_id"], snapshots=registry.address,
        staking=staking.address, snapshot_block=str(height), snapshot_block_hash=Web3.to_hex(anchor.hash),
        snapshot_timestamp=str(anchor.timestamp), following_block_hash=Web3.to_hex(following.hash),
        inputs=inputs, holders=observations, confederations=conf_evidence)).encode()
    report = [day, height, anchor.hash, hashlib.sha256(evidence).digest()]
    if (w3.eth.get_block(height).hash != anchor.hash or w3.eth.get_block(height + 1).hash != following.hash
            or w3.eth.get_block(current.number).hash != current.hash):
        raise ValueError("ULR_SNAPSHOT_REORG")
    return [report, owners, weights, allocations], evidence


def snapshot_calldata_limit(w3, registry, args, operation, block):
    """Reject transactions whose calldata alone exceeds the network gas cap."""
    from .evm_network import MAX_TRANSACTION_GAS

    data = bytes.fromhex(registry.encode_abi("publishDay", args=[*args, operation]).removeprefix("0x"))
    tokens = len(data) + 3 * (len(data) - data.count(0))
    if 21_000 + 10 * tokens >= min(MAX_TRANSACTION_GAS, w3.eth.get_block(block).gasLimit):
        raise ValueError("ULR_SNAPSHOT_TRANSACTION_GAS")


def _snapshot_intent(intent):
    return intent.get("artifact") == "ULRCustodianSnapshots" and intent.get("hash_method") == SNAPSHOT_METHOD


def _snapshot_envelope(intent, encoded):
    envelope = {key: value for key, value in intent.items() if key not in ("hash_args", "ulr_snapshot_inputs")}
    envelope[SNAPSHOT_INTENT_HASH] = hashlib.sha256(encoded).hexdigest()
    return envelope


def board_intent(intent):
    if SNAPSHOT_INTENT_HASH in intent:
        raise ValueError("ULR_SNAPSHOT_INTENT_HASH_MISMATCH")
    encoded = canonical(public_data(intent)).encode()
    snapshot = _snapshot_intent(intent)
    if len(encoded) <= (MAX_INLINE_SNAPSHOT_BYTES if snapshot else MAX_BOARD_INTENT_BYTES):
        return intent
    if not snapshot or len(encoded) > MAX_SNAPSHOT_INTENT_BYTES:
        raise ValueError("ULR_PUBLIC_INTENT_TOO_LARGE")
    envelope = _snapshot_envelope(intent, encoded)
    if len(canonical(envelope).encode()) > MAX_BOARD_INTENT_BYTES:
        raise ValueError("ULR_PUBLIC_INTENT_TOO_LARGE")
    return envelope


def matches_board_intent(published, intent):
    if SNAPSHOT_INTENT_HASH in intent:
        return False
    if SNAPSHOT_INTENT_HASH not in published:
        return published == intent
    if not _snapshot_intent(intent):
        return False
    encoded = canonical(intent).encode()
    return len(encoded) <= MAX_SNAPSHOT_INTENT_BYTES and _snapshot_envelope(intent, encoded) == published


def resolve_board_intent(published, path, *, session):
    if SNAPSHOT_INTENT_HASH not in published:
        return published
    if published.get("session") != str(session):
        raise ValueError("ULR_SNAPSHOT_INTENT_HASH_MISMATCH")
    if path is None:
        raise ValueError("ULR_SNAPSHOT_INTENT_FILE_REQUIRED")
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_SNAPSHOT_INTENT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_INTENT_BYTES:
        raise ValueError("ULR_PUBLIC_INTENT_TOO_LARGE")
    intent = dict(public_data(json.loads(data)), session=published["session"])
    if not matches_board_intent(published, intent):
        raise ValueError("ULR_SNAPSHOT_INTENT_HASH_MISMATCH")
    return intent


def _legacy_evidence_hash(evidence):
    # Reconstruct the deployed v3 format from the same checked holdings and
    # rosters. The new supply proof is still required for an old pending intent.
    legacy = json.loads(evidence)
    for key in ("threshold_wei", "attributed_supply_wei", "observed_wei", "unaccounted_wei",
                "unattributed_staking_balance_wei", "token_balance_wei"):
        del legacy[key]
    legacy.update(version="3", eligibility_policy="HOLDERS_200K_PLUS_ACTIVE_RESIDUAL_V1")
    return Web3.to_hex(hashlib.sha256(canonical(legacy).encode()).digest())


def intent_for(w3, target, kind, helper, args, authority, role, at, **extra):
    authorization = contract(w3, "DKGAuthorization", authority)
    op = read(target, at, helper, *args)
    return dict(chain_id=str(w3.eth.chain_id), coordinator=read(authorization, at, "coordinator"),
                role=role, target=target.address, artifact=kind, hash_method=helper,
                hash_args=wire(args), operation_hash=Web3.to_hex(op), **extra)


def review(w3, intent, bundle, at, pin, *, require_finalized=True):
    """Independent participant review; called again before private work and packet export."""
    from .dkg_protocol import Manifest
    from .ethereum_intents import validate_signing_binding

    at = w3.eth.get_block(at).number
    if "ulr_bootstrap" in intent:
        from .bootstrap_ulr import enrolled_pin
        if pin != enrolled_pin(w3, intent, bundle, at):
            raise ValueError("ULR_BOOTSTRAP_PIN_MISMATCH")
    if require_finalized and at > read_block(w3, int(pin["confirmations"])).number:
        raise ValueError("ULR_UNFINALIZED_REVIEW")
    registry, _ = configuration(w3, pin, at)
    kind, helper = intent.get("artifact"), intent.get("hash_method")
    allowed = {
        ("ULRCustodianSnapshots", "getPublishDayOperationHash"),
        ("ULRCustodianSnapshots", "getEnrollTreasuryOperationHash"),
        ("ULRCustodianSnapshots", ACTIVATE_TREASURY_METHOD),
        ("ULRConfederationTreasury", "getSpendOperationHash"),
        ("CustodianDAOFeeRouter", "getSetFeeSharesOperationHash"),
        ("CustodianDAOFeeRouter", "getSetTreasuryOperationHash"),
        ("CustodianDAOFeeRouter", "getSetPayerOperationHash"),
        ("CustodianValidator", "getBindCandidateStakingOperationHash"),
        ("CustodianValidator", "getAddCustodianOperationHash"),
        ("CustodianValidator", "getRemoveCustodianOperationHash"),
        ("CustodianDAODecisionBoard", "getCreateOperationHash"),
    }
    if (kind, helper) not in allowed or intent.get("chain_id") != pin["chain_id"]:
        raise ValueError("ULR_INTENT_POLICY")
    target = contract(w3, kind, intent["target"])
    abi = next(f for f in target.abi if f.get("name") == helper)
    args = encode_args(abi["inputs"], intent["hash_args"])
    if kind == "ULRCustodianSnapshots":
        if target.address != registry.address:
            raise ValueError("ULR_SNAPSHOT_BINDING")
        if helper in (SNAPSHOT_METHOD, ACTIVATE_TREASURY_METHOD):
            authority = read(registry, at, "authorization")
            if helper == ACTIVATE_TREASURY_METHOD:
                _, _, treasury, _, _, _ = treasury_context(w3, pin, args[0], at)
                scope = read(treasury, at, "scope")
                if read(registry, at, "treasuries", scope) != treasury.address:
                    raise ValueError("ULR_TREASURY_NOT_ENROLLED")
        else:
            _, _, treasury, factory, conf_id, conf = treasury_context(w3, pin, args[2], at)
            if args[:2] != [factory.address, conf_id] or bundle.get("role") != "conf_ceremony":
                raise ValueError("ULR_ENROLLMENT_POLICY")
            enrolled = read(registry, at, "treasuries", read(treasury, at, "scope"))
            if enrolled not in ("0x" + "00" * 20, treasury.address):
                raise ValueError("ULR_ALREADY_ENROLLED")
            authority = conf[7]
    elif kind == "ULRConfederationTreasury":
        _, _, treasury, _, _, conf = treasury_context(w3, pin, target.address, at)
        if bundle.get("role") != "conf_ceremony":
            raise ValueError("ULR_SPEND_POLICY")
        if read(registry, at, "treasuries", read(treasury, at, "scope")) != treasury.address:
            raise ValueError("ULR_TREASURY_NOT_ENROLLED")
        authority = conf[7]
    elif kind in ("CustodianValidator", "CustodianDAODecisionBoard"):
        _, staking, treasury, _, _, conf = treasury_context(w3, pin, intent["ulr_treasury"], at)
        validator = contract(w3, "CustodianValidator", conf[8])
        if bundle.get("role") != "conf_ceremony":
            raise ValueError("ULR_CUSTODIAN_ROLE_REQUIRED")
        if kind == "CustodianValidator":
            if target.address != validator.address:
                raise ValueError("ULR_VALIDATOR_BINDING")
            bound = read(validator, at, "candidateStaking")
            if helper == "getBindCandidateStakingOperationHash":
                if args != [staking.address] or bound not in ("0x" + "00" * 20, staking.address):
                    raise ValueError("ULR_CANDIDATE_SOURCE_BINDING")
            elif bound != staking.address:
                raise ValueError("ULR_CANDIDATE_SOURCE_REQUIRED")
        else:
            voting = voting_context(w3, pin, target.address, treasury.address, at)
            if read(voting, at, "validator") != validator.address:
                raise ValueError("ULR_VOTING_BINDING")
        authority = conf[7]
    else:
        _, staking, treasury, factory, conf_id, conf = treasury_context(w3, pin, intent["ulr_treasury"], at)
        if read(registry, at, "treasuries", read(treasury, at, "scope")) != treasury.address:
            raise ValueError("ULR_TREASURY_NOT_ENROLLED")
        proven(w3, "CustodianDAOFeeRouter", target.address, at)
        vault = read(target, at, "incomeVault")
        if (bundle.get("role") != "conf_ceremony" or read(target, at, "authorization") != conf[7]
                or read(target, at, "rewardToken") != read(staking, at, "rewardToken")):
            raise ValueError("ULR_FEE_ROUTER_BINDING")
        legacy_vault = read(factory, at, "getCustodianDAO", conf_id)[4]
        if legacy_vault != vault:
            if legacy_vault != "0x" + "00" * 20:
                raise ValueError("ULR_FEE_ROUTER_BINDING")
            proven(w3, "CustodianDAOIncomeVault", vault, at, [read(staking, at, "rewardToken"), conf[7]])
            proven(w3, "CustodianDAOFeeRouter", target.address, at, [vault, treasury.address, 0, 0])
        if helper == "getSetPayerOperationHash":
            if ("ulr_bootstrap" not in intent or len(args) != 2 or args[1] is not True
                    or read(target, at, "treasury") != treasury.address
                    or read(target, at, "wrapStakerBps") != 0 or read(target, at, "deployStakerBps") != 0):
                raise ValueError("ULR_BOOTSTRAP_PAYER_POLICY")
            from .bootstrap_ulr import review_pipeline
            review_pipeline(w3, args[0], target.address, factory, conf_id, bundle, at)
        if helper == "getSetFeeSharesOperationHash" and args != [0, 0]:
            raise ValueError("ULR_GROSS_FEES_REQUIRED")
        if helper == "getSetTreasuryOperationHash" and (args != [treasury.address]
                or read(target, at, "wrapStakerBps") != 0 or read(target, at, "deployStakerBps") != 0):
            raise ValueError("ULR_ZERO_OLD_SPLIT_REQUIRED")
        authority = conf[7]
    auth = authority_context(w3, authority, at)
    manifest = Manifest.from_dict(bundle)
    coordinator = read(auth, at, "coordinator")
    validate_signing_binding(intent, bundle, coordinator)
    if (manifest.chain_id != w3.eth.chain_id or manifest.purpose != "EVM_AUTH"
            or bundle.get("role") not in ("evm_ceremony", "conf_ceremony") or intent.get("role") != bundle["role"]
            or read(auth, at, "registry") != manifest.registry
            or bytes(read(auth, at, "dkgId")) != manifest.session_id
            or list(read(auth, at, "getParticipants")) != list(manifest.participants)
            or read(auth, at, "threshold") != manifest.threshold):
        raise ValueError("ULR_SIGNING_AUTHORITY")
    op = digest(intent["operation_hash"])
    complete = read(auth, at, "executedOperations", op)
    if complete and read(auth, at, "operationTargets", op) != target.address:
        raise ValueError("ULR_OPERATION_TARGET")
    if (kind == "CustodianValidator" and helper == "getAddCustodianOperationHash" and not complete
            and not read(staking, at, "isCustodianCandidate", args[0])):
        raise ValueError("ULR_NOT_ELIGIBLE_CANDIDATE")
    if helper == SNAPSHOT_METHOD:
        if complete:
            from eth_utils.abi import collapse_if_tuple
            commitment = Web3.keccak(w3.codec.encode([collapse_if_tuple(x) for x in abi["inputs"]], args))
            if read(registry, at, "daySnapshots", args[0][0])[-1] != commitment:
                raise ValueError("ULR_SNAPSHOT_RECEIPT")
        else:
            snapshot_calldata_limit(w3, registry, args, op, at)
            expected, evidence = snapshot_arguments(w3, pin, intent["ulr_snapshot_inputs"], at)
            computed = wire(expected)
            if computed != intent["hash_args"]:
                computed[0][3] = _legacy_evidence_hash(evidence)
                if computed != intent["hash_args"]:
                    raise ValueError("ULR_SNAPSHOT_WEIGHT_MISMATCH")
    elif helper == ACTIVATE_TREASURY_METHOD and complete:
        if not read(registry, at, "activeTreasuries", scope):
            raise ValueError("ULR_TREASURY_NOT_ACTIVE")
    elif helper == "getEnrollTreasuryOperationHash" and complete:
        scope = read(treasury, at, "scope")
        if read(registry, at, "treasuries", scope) != treasury.address:
            raise ValueError("ULR_ENROLLMENT_RECEIPT")
    elif helper == "getSpendOperationHash" and not complete:
        if args[2] < w3.eth.get_block(at).timestamp:
            raise ValueError("ULR_SPEND_EXPIRED")
    elif kind == "CustodianDAOFeeRouter" and complete:
        if helper == "getSetPayerOperationHash" and not read(target, at, "authorizedPayers", args[0]):
            raise ValueError("ULR_ROUTER_RECEIPT")
        if (helper == "getSetFeeSharesOperationHash" and
                (read(target, at, "wrapStakerBps") != 0 or read(target, at, "deployStakerBps") != 0)):
            raise ValueError("ULR_ROUTER_RECEIPT")
        if helper == "getSetTreasuryOperationHash" and read(target, at, "treasury") != treasury.address:
            raise ValueError("ULR_ROUTER_RECEIPT")
    elif kind == "CustodianValidator" and complete:
        from eth_utils.abi import function_abi_to_4byte_selector
        method = {"getBindCandidateStakingOperationHash": "bindCandidateStaking",
            "getAddCustodianOperationHash": "addCustodian", "getRemoveCustodianOperationHash": "removeCustodian"}[helper]
        method_abi = next(f for f in target.abi if f.get("name") == method)
        receipt = Web3.keccak(w3.codec.encode(["bytes4", "address"], [function_abi_to_4byte_selector(method_abi), args[0]]))
        if read(target, at, "operationReceipts", op) != receipt:
            raise ValueError("ULR_CUSTODIAN_OPERATION_RECEIPT")
        if helper == "getBindCandidateStakingOperationHash" and read(target, at, "candidateStaking") != args[0]:
            raise ValueError("ULR_CANDIDATE_BINDING_RECEIPT")
    elif kind == "CustodianDAODecisionBoard" and complete:
        receipt = Web3.keccak(w3.codec.encode(["bytes32", "bytes32", "uint256", "uint16"],
            [Web3.keccak(text=args[0]), args[1], args[2], args[3]]))
        if read(target, at, "creationReceipts", op) != receipt:
            raise ValueError("ULR_VOTING_CREATION_RECEIPT")
    if not complete and bytes(read(target, at, helper, *args)) != op:
        raise ValueError("ULR_OPERATION_HASH")
    return dict(complete=bool(complete), authority=authority, arguments=args, target=target)


def voting_context(w3, pin, board_address, treasury_address, at):
    _, staking, _, _, _, conf = treasury_context(w3, pin, treasury_address, at)
    board = contract(w3, "CustodianDAODecisionBoard", board_address)
    adapter = read(board, at, "staking")
    voting = proven(w3, "ULRConfederationVoting", adapter, at, [staking.address, conf[8]])
    proven(w3, "CustodianDAODecisionBoard", board.address, at, [adapter, conf[7]])
    validator = contract(w3, "CustodianValidator", conf[8])
    if (read(board, at, "authorization") != conf[7] or read(voting, at, "staking") != staking.address
            or read(voting, at, "validator") != validator.address
            or read(validator, at, "candidateStaking") != staking.address):
        raise ValueError("ULR_VOTING_BINDING")
    return voting


def guard_signing_session(client, session):
    payload = getattr(client, "ulr_intent_context", None)
    if session.origin_contract == "0x" + "00" * 20:
        if payload is not None:raise ValueError("ULR_SESSION_HASH")
        return
    anchor = getattr(client, "_read_snapshot", None) or client.w3.eth.get_block("latest")
    # A previously reviewed ULR intent must never become an unrelated/unknown
    # target just because its runtime no longer matches during revalidation.
    kind = payload[0]["artifact"] if payload else None
    if kind is not None and kind not in (*KINDS, "CustodianDAOFeeRouter", "CustodianValidator", "CustodianDAODecisionBoard"):
        raise ValueError("ULR_INTENT_POLICY")
    for candidate in (() if kind else KINDS):
        try:
            checked_runtime(client.w3, session.origin_contract, candidate, anchor.number)
            kind = candidate
            break
        except ValueError as error:
            if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
                raise
    if kind is None:
        return
    if payload is None:
        raise ValueError("ULR_TYPED_INTENT_REQUIRED")
    intent, bundle, pin = payload
    if (intent["operation_hash"].removeprefix("0x") != session.message_hash.hex()
            or session.origin_contract.lower() != intent["target"].lower()
            or intent["coordinator"].lower() != client.address.lower()):
        raise ValueError("ULR_SESSION_HASH")
    from .onchain_transport import board_at
    for number_ in (anchor.number, "latest"):
        board = board_at(client.w3, bundle["onchain_board"], client.w3.eth.get_block(number_))
        published = json.loads(bytes(read(board, number_, "intents", session.session_id)))
        if (published.get("session", str(session.session_id)) != str(session.session_id)
                or not matches_board_intent(published, intent)):
            raise ValueError("ULR_PUBLIC_INTENT_REQUIRED")
        if review(client.w3, intent, bundle, number_, pin, require_finalized=number_ != "latest")["complete"]:
            raise ValueError("ULR_OPERATION_COMPLETE")
    if client.w3.eth.get_block(anchor.number).hash != anchor.hash:
        raise ValueError("ULR_SNAPSHOT_REORG")
    client.check_read_snapshot()
