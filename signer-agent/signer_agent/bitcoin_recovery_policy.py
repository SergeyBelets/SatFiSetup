"""Public bootstrap terms for one recoverable native-S Bitcoin wallet."""

import re

from web3 import Web3

from .bitcoin_onchain_plan import MAX_MONEY, decimal


ARTIFACT = "BitcoinWithdrawalRecovery"
FACTORY_ARTIFACT = "BitcoinWithdrawalRecoveryFactory"
GETTERS = ("escrow", "reserve", "sourceScriptHash", "successorScriptHash", "sponsorScriptHash",
           "recoveryHeight", "recoveryDepth", "maxMinerFeeSats", "pauseGuardian")
FIELDS = {"height", "threshold", "keys", "successor_script", "sponsor_script",
          "max_fee_sats", "confirmations", "pause_guardian"}
LOCKTIME_THRESHOLD = 500_000_000
SECP256K1_FIELD = 2**256 - 2**32 - 977
SUCCESSOR_DKG = "successor_dkg"


def xonly(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return False
    x = int(value, 16)
    p = SECP256K1_FIELD
    return x < p and pow((pow(x, 3, p) + 7) % p, (p - 1) // 2, p) == 1


def validate(policy, *, payment_depth=6):
    if not isinstance(policy, dict) or set(policy) - {SUCCESSOR_DKG} != FIELDS:
        raise ValueError("BTC_RECOVERY_POLICY")
    keys = policy["keys"]
    if (not isinstance(keys, list) or not 2 <= len(keys) <= 15
            or not all(xonly(key) for key in keys) or sorted(set(keys)) != keys):
        raise ValueError("BTC_RECOVERY_KEYS")
    if decimal(policy["threshold"], len(keys)) < 2:
        raise ValueError("BTC_RECOVERY_KEYS")
    decimal(policy["height"], LOCKTIME_THRESHOLD - 1, positive=True)
    decimal(policy["max_fee_sats"], MAX_MONEY, positive=True)
    if decimal(policy["confirmations"], 2**32 - 1) < payment_depth:
        raise ValueError("BTC_RECOVERY_DEPTH")
    successor, sponsor = policy["successor_script"], policy["sponsor_script"]
    if (not all(isinstance(script, str) and script.startswith("5120") and xonly(script[4:])
                for script in (successor, sponsor)) or successor == sponsor):
        raise ValueError("BTC_RECOVERY_SCRIPT")
    guardian = policy["pause_guardian"]
    if not Web3.is_checksum_address(guardian) or not int(guardian, 16):
        raise ValueError("BTC_RECOVERY_GUARDIAN")
    if SUCCESSOR_DKG in policy:
        node = policy[SUCCESSOR_DKG]
        if (not isinstance(node, dict) or set(node) != {"registry", "wallet_id"}
                or not Web3.is_checksum_address(node["registry"]) or not int(node["registry"], 16)
                or not isinstance(node["wallet_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", node["wallet_id"])
                or not int(node["wallet_id"], 16)):
            raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    return policy


def enrolled_policy(profile):
    if "bitcoin_recovery" not in profile:
        return None
    from .bootstrap_confederation_dao import enabled

    if (not enabled(profile) or "bitcoin_custody" not in profile or "withdrawal_fee" in profile
            or "onboarding" not in profile and profile.get("version") != "6"):
        raise ValueError("BTC_RECOVERY_NATIVE_S_ENROLLMENT")
    policy = validate(profile["bitcoin_recovery"], payment_depth=int(profile["bitcoin_confirmations"]))
    if "bitcoin_policy" in profile and int(policy["height"]) <= int(profile["bitcoin_policy"]["checkpoint_height"]):
        raise ValueError("BTC_RECOVERY_HEIGHT")
    if SUCCESSOR_DKG in policy and policy[SUCCESSOR_DKG]["wallet_id"] in profile.get("sessions", {}).values():
        raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    return policy


def review_successor(dkg, block, wallet_id, policy, roster, threshold):
    """Check the completed key-path DKG for the pinned P2TR output."""
    enrolled = policy.get(SUCCESSOR_DKG)
    if enrolled and (enrolled["registry"] != dkg.address or enrolled["wallet_id"] != wallet_id.hex()):
        raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    context, result, _, purpose, group, participants, required = dkg.functions.getCompletedResult(wallet_id).call(
        block_identifier=block)
    if (not any(context) or not any(result) or purpose != Web3.keccak(text="BTC_SPEND")
            or bytes(group).hex() != policy["successor_script"][4:]
            or list(participants) != list(roster) or required != int(threshold)):
        raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    return bytes(result)


def check_enrolled_successor(w3, profile, dkg, block):
    from .contract_provenance import prove_creation
    from .bitcoin_trust import checked_runtime

    policy = profile["bitcoin_recovery"]
    enrolled = policy[SUCCESSOR_DKG]
    if dkg.address != enrolled["registry"]:
        raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    checked_runtime(w3, dkg.address, "DKGCoordinator", block)
    proof = prove_creation(w3, dkg.address, "DKGCoordinator", [], block)
    if (proof.get("method") != "direct" or proof.get("sender") != profile["operator"]
            or dkg.functions.ceremonyRegistrar().call(block_identifier=block) != profile["operator"]
            or dkg.functions.PHASE_DURATION().call(block_identifier=block)
                != int(profile.get("phase_duration", "86400" if profile["chain_id"] == "1" else "3600"))):
        raise ValueError("BTC_RECOVERY_SUCCESSOR_DKG")
    btc = profile["btc_ceremony"]
    return review_successor(dkg, block, bytes.fromhex(enrolled["wallet_id"]), policy, btc["participants"], btc["threshold"])


def constructor_args(policy, escrow, reserve, source_hash):
    return [
        escrow, reserve, source_hash,
        "0x" + Web3.keccak(hexstr=policy["successor_script"]).hex().removeprefix("0x"),
        "0x" + Web3.keccak(hexstr=policy["sponsor_script"]).hex().removeprefix("0x"),
        policy["height"], policy["confirmations"], policy["max_fee_sats"], policy["pause_guardian"],
    ]


def review_module(w3, escrow, reserve, module, block, policy, *, source_hash=None):
    from .bitcoin_trust import checked_runtime
    from .bootstrap import Bootstrap, artifact, encode_args

    checked_runtime(w3, module.address, ARTIFACT, block)

    def read(node, method, *args):
        return getattr(node.functions, method)(*args).call(block_identifier=block)

    address = read(escrow, "recoveryFactory")
    checked_runtime(w3, address, FACTORY_ARTIFACT, block)
    factory = w3.eth.contract(address=address, abi=artifact(FACTORY_ARTIFACT)["abi"])
    if source_hash is None:
        source_hash = Web3.keccak(bytes(read(reserve, "payoutScript")))
    fields = next(row["inputs"] for row in artifact(ARTIFACT)["abi"] if row["type"] == "constructor")
    expected = encode_args(fields, constructor_args(policy, escrow.address, reserve.address, Web3.to_hex(source_hash)))
    if (read(factory, "escrow") != escrow.address or not read(factory, "deployed", module.address)
            or read(factory, "policyFor", *expected[1:]) != module.address
            or Bootstrap.wire([read(module, getter) for getter in GETTERS]) != Bootstrap.wire(expected)):
        raise ValueError("BTC_RECOVERY_BINDING")
