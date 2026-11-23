"""Compile enrolled organization terms with the factory's Solidity encoding."""

from eth_abi import encode
from eth_utils import keccak

from .bootstrap_tokens import resolve
from .dkg_protocol import raw


def federation_formation_hash(members, threshold):
    return keccak(encode(["address[]", "uint256"], [members, int(threshold)]))


def confederation_formation_hash(mode, federation_ids, custodians, threshold, min_custodians):
    return keccak(encode(
        ["uint8", "string[]", "(string,address,bytes,bool)[]", "uint256", "uint256"],
        [mode, federation_ids, custodians, int(threshold), int(min_custodians)],
    ))


def share_token_hash(name, symbol):
    return keccak(encode(["string", "string"], [name, symbol]))


def expected_formation(config):
    """Return the exact factory terms for a bootstrap profile or signed invitation."""
    if "evm_ceremony" in config:
        fed, conf = config["evm_ceremony"], config["conf_ceremony"]
        members, fed_threshold, fed_session = fed["participants"], fed["threshold"], fed["id"]
        conf_members, conf_threshold, conf_session = conf["participants"], conf["threshold"], conf["id"]
    else:
        from .bootstrap_custody import role_inputs

        members, fed_threshold = role_inputs(config, "evm_ceremony")
        conf_members, conf_threshold = role_inputs(config, "conf_ceremony")
        fed_session = config["sessions"]["evm_ceremony"]
        conf_session = config["sessions"]["conf_ceremony"]

    fed_id = config["federation_id"]
    conf_id = config["confederation_id"]
    custody_only = "bitcoin_custody" in config
    mode = 2 if custody_only else 0
    addresses = conf_members if custody_only else conf_members[1:]
    custodians = [(f"custodian-{i}", address, b"\x02", True) for i, address in enumerate(addresses)]
    min_custodians = int(config["min_custodians"])
    tokens = resolve(config)
    fed_token, conf_token = tokens["federation_share"], tokens["confederation_share"]

    return {
        "federation": dict(
            id=fed_id, session_id=raw(fed_session, 32), members=list(members), threshold=int(fed_threshold),
            formation_hash=federation_formation_hash(members, fed_threshold),
            token_hash=share_token_hash(**fed_token), token_metadata=fed_token,
        ),
        "confederation": dict(
            id=conf_id, session_id=raw(conf_session, 32), mode=mode, federation_ids=[fed_id],
            custodians=custodians, threshold=int(conf_threshold), min_custodians=min_custodians,
            formation_hash=confederation_formation_hash(mode, [fed_id], custodians, conf_threshold, min_custodians),
            token_hash=share_token_hash(**conf_token), token_metadata=conf_token,
        ),
    }
