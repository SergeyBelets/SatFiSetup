"""Constructor provenance using ordinary historical code, blocks and receipts.

Matching runtime cannot establish initial storage: custom initcode may return the
same runtime after arbitrary SSTOREs. Direct CREATE input, or an authenticated
canonical leaf deployment event, establishes that the reviewed constructor ran.
"""

from copy import deepcopy
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils.abi import collapse_if_tuple
from web3 import Web3
from web3._utils.events import get_event_data
from web3.exceptions import LogTopicError, MismatchedABI

from .bootstrap import artifact


def creation_input(kind, args):
    return _creation_input(artifact(kind), args)


def _creation_input(compiled, args):
    constructor = next(
        (x for x in compiled["abi"] if x["type"] == "constructor"), {"inputs": []}
    )
    return bytes.fromhex(compiled["bytecode"]["object"].removeprefix("0x")) + encode(
        [collapse_if_tuple(x) for x in constructor["inputs"]], args
    )


def birth_block(w3, address, latest):
    if not w3.eth.get_code(address, block_identifier=latest):
        raise ValueError("BTC_CREATION_CODE_MISSING")
    # Fresh deployments should not require an unrelated, ancient archive state.
    high, step = latest, 1
    while True:
        low = max(0, latest - step)
        if not w3.eth.get_code(address, block_identifier=low):
            low += 1
            break
        if low == 0:
            raise ValueError("BTC_GENESIS_CONTRACT_UNSUPPORTED")
        high, step = low, step * 2
    while low < high:
        middle = (low + high) // 2
        if w3.eth.get_code(address, block_identifier=middle):
            high = middle
        else:
            low = middle + 1
    if low == 0:
        raise ValueError("BTC_GENESIS_CONTRACT_UNSUPPORTED")
    return low


def _valid_input(compiled, args, supplied):
    if args is not None:
        return supplied == _creation_input(compiled, args)
    prefix = bytes.fromhex(compiled["bytecode"]["object"].removeprefix("0x"))
    if not supplied.startswith(prefix):
        return False
    constructor = next(
        (x for x in compiled["abi"] if x["type"] == "constructor"), {"inputs": []}
    )
    types = [collapse_if_tuple(x) for x in constructor["inputs"]]
    suffix = supplied[len(prefix) :]
    try:
        return encode(types, decode(types, suffix)) == suffix
    except Exception:
        return False


def _receipt(w3, transaction, block):
    receipt = w3.eth.get_transaction_receipt(transaction["hash"])
    if (
        receipt["blockNumber"] != block["number"]
        or bytes(receipt["blockHash"]) != bytes(block["hash"])
        or bytes(receipt["transactionHash"]) != bytes(transaction["hash"])
    ):
        raise ValueError("BTC_CREATION_RECEIPT_MISMATCH")
    return receipt


def prove_creation(w3, address, kind, args, latest, *, birth_hint=None):
    """Prove the constructor ran; args=None accepts any canonical constructor arguments.

    Callers still validate the deployment's business configuration and bindings.
    The per-provider cache is rechecked against canonical blocks and current code.
    A birth hint is an untrusted block locator, never evidence of provenance.
    Direct CREATE is established from its canonical receipt and exact initcode;
    replaying that evidence does not require historical Ethereum state.
    """
    from .bitcoin_trust import checked_runtime, runtime_artifact

    address = Web3.to_checksum_address(address)
    hint = None if birth_hint is None else int(birth_hint)
    if hint is not None and not 1 <= hint <= latest:
        raise ValueError("BTC_CREATION_BLOCK_MISMATCH")
    if kind == "ULRConfederationTreasury":
        runtime_hash, compiled = runtime_artifact(w3, address, kind, latest)
    else:
        runtime_hash = checked_runtime(w3, address, kind, latest)
        compiled = artifact(kind)
    cache_key = (
        address,
        kind,
        None if args is None else Web3.keccak(_creation_input(compiled, args)).hex(),
        Web3.keccak(hexstr=compiled["bytecode"]["object"]).hex(),
    )
    from . import verified_cache
    # Artifact bytecode is included even when args=None. A different build must
    # never inherit approval of an older constructor with matching runtime.
    persistent_key = list(cache_key)
    cache = getattr(w3, "_satoshify_creation_proofs", None)
    if cache is None:
        cache = {}
        w3._satoshify_creation_proofs = cache
    stored = verified_cache.get(w3, "creation", persistent_key)
    if stored is not None:
        cache.setdefault(cache_key, stored)

    def check_height(proof):
        number = int(proof["block_number"])
        if number > latest or hint is not None and number != hint:
            raise ValueError("BTC_CREATION_BLOCK_MISMATCH")

    if cache_key in cache:
        proof = cache[cache_key]
        check_height(proof)
        check_proof_anchor(w3, proof)
        if proof["runtime_hash"] != runtime_hash:
            raise ValueError("BTC_CREATION_RUNTIME_CHANGED")
        return deepcopy(proof)

    def finish(proof):
        check_height(proof)
        check_proof_anchor(w3, proof)
        cache[cache_key] = deepcopy(proof)
        verified_cache.put(w3, "creation", persistent_key, proof)
        # An exact-arguments proof also proves a canonical constructor with any
        # arguments. The reverse is deliberately forbidden for internal leaves.
        if args is not None:
            any_args = (cache_key[0], cache_key[1], None, cache_key[3])
            cache[any_args] = deepcopy(proof)
            verified_cache.put(w3, "creation", list(any_args), proof)
        return proof

    if kind == 'BitcoinFeeReserveUser':
        import rlp
        child=w3.eth.contract(address=address,abi=artifact(kind)['abi'])
        parent_address=child.functions.reserve().call(block_identifier=latest)
        if args is not None and args != [parent_address]:raise ValueError('BTC_CONSTRUCTOR_INPUT_MISMATCH')
        expected=Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(parent_address[2:]),1]))[-20:])
        if expected != address:raise ValueError('BTC_CONSTRUCTOR_INPUT_MISMATCH')
        parent=prove_creation(w3,parent_address,'BitcoinFeeReserve',None,latest)
        return finish(dict(version='1',address=address,artifact=kind,method='canonical_constructor',parent=parent,
            block_number=parent['block_number'],block_hash=parent['block_hash'],runtime_hash=runtime_hash,
            transaction_hash=parent['transaction_hash']))

    number = birth_block(w3, address, latest) if hint is None else hint
    block = w3.eth.get_block(number, full_transactions=True)
    if block["number"] != number:
        raise ValueError("BTC_CREATION_BLOCK_MISMATCH")
    result = dict(
        version="1",
        address=address,
        artifact=kind,
        block_number=str(number),
        block_hash=block["hash"].hex(),
        runtime_hash=runtime_hash,
    )
    receipts = {}
    for transaction in block["transactions"]:
        # A direct CREATE can only be a to=null transaction. Do not fetch every
        # unrelated receipt in a busy mainnet block for each contract proof.
        if transaction.get("to") is not None:
            continue
        if "nonce" in transaction:
            import rlp
            created = Web3.to_checksum_address(Web3.keccak(rlp.encode([
                bytes.fromhex(transaction["from"][2:]), (int(transaction["nonce"], 16)
                    if isinstance(transaction["nonce"], str) and transaction["nonce"].startswith("0x")
                    else int(transaction["nonce"]))]))[-20:])
            if created != address:
                continue
        receipt = _receipt(w3, transaction, block)
        receipts[bytes(transaction["hash"])] = receipt
        if receipt["status"] != 1 or receipt.get("contractAddress") != address:
            continue
        initcode = bytes(transaction["input"])
        if transaction.get("to") is not None or not _valid_input(compiled, args, initcode):
            raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")
        return finish(
            dict(
                result,
                method="direct",
                transaction_hash=transaction["hash"].hex(),
                initcode_hash=Web3.keccak(initcode).hex(),
                sender=transaction["from"],
            )
        )

    if kind == 'RecipientPolicy':
        child=w3.eth.contract(address=address,abi=artifact(kind)['abi'])
        creator=child.functions.creator().call(block_identifier=latest)
        if w3.eth.get_code(creator,block_identifier=latest):
            import rlp
            expected=Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(creator[2:]),1]))[-20:])
            if expected!=address or args not in (None,[]):raise ValueError('BTC_CONSTRUCTOR_INPUT_MISMATCH')
            parent=prove_creation(w3,creator,'BitcoinAddressRegistry',None,latest)
            registry=w3.eth.contract(address=creator,abi=artifact('BitcoinAddressRegistry')['abi'])
            if registry.functions.recipientPolicy().call(block_identifier=latest)!=address:raise ValueError('RECIPIENT_POLICY_ORIGIN')
            return finish(dict(version='1',address=address,artifact=kind,method='canonical_constructor',parent=parent,
                block_number=parent['block_number'],block_hash=parent['block_hash'],runtime_hash=runtime_hash,
                transaction_hash=parent['transaction_hash']))

    if kind not in ("DKGAuthorization", "FROSTAuthorization", "CustodianValidator"):
        raise ValueError("BTC_CANONICAL_CREATION_NOT_FOUND")

    # Internal deployment logs alone do not establish a child's constructor
    # arguments. Keep their historical creation boundary and getter checks;
    # only an authenticated direct CREATE above can avoid historical state.
    if birth_hint is not None and (
        w3.eth.get_code(address, block_identifier=number - 1)
        or not w3.eth.get_code(address, block_identifier=number)
    ):
        raise ValueError("BTC_CREATION_BLOCK_MISMATCH")

    # The only supported internal constructors are these two reviewed leaves.
    # Their exact direct CREATE input is verified too, excluding forged logs from
    # custom leaf initcode that later returns the same runtime.
    leaves = (
        (
            (
                "FROSTAuthorizationDeployer",
                "FROSTAuthorizationDeployed",
                "authorization",
            ),
            ("FROSTCustodianDeployer", "FROSTCustodianDeployed", "frostMultisig"),
        )
        if kind != "CustodianValidator"
        else (
            ("FROSTCustodianDeployer", "FROSTCustodianDeployed", "custodianValidator"),
        )
    )
    # Internal constructors emit canonical leaf events. One single-block log
    # query is enough to locate candidate receipts; the receipts, leaf origin,
    # constructor and canonical block are still independently authenticated.
    if hasattr(w3.eth, "get_logs"):
        topics = []
        for leaf_kind, event_name, _ in leaves:
            event = next(x for x in artifact(leaf_kind)["abi"] if x.get("type") == "event" and x["name"] == event_name)
            topics.append(Web3.to_hex(Web3.keccak(text=event_name + "(" + ",".join(
                collapse_if_tuple(x) for x in event["inputs"]) + ")")))
        candidate_hashes = {bytes(log["transactionHash"]) for log in w3.eth.get_logs(
            {"fromBlock": number, "toBlock": number, "topics": [topics]})}
    else:
        candidate_hashes = {bytes(tx["hash"]) for tx in block["transactions"]}
    for transaction in block["transactions"]:
        tx_hash = bytes(transaction["hash"])
        if tx_hash in candidate_hashes and tx_hash not in receipts:
            receipts[tx_hash] = _receipt(w3, transaction, block)
    candidate_error = None
    for receipt in receipts.values():
        if receipt["status"] != 1:
            continue
        for log in receipt["logs"]:
            if not log.get("topics"):
                continue
            for leaf_kind, event_name, child_field in leaves:
                event = next(
                    x
                    for x in artifact(leaf_kind)["abi"]
                    if x.get("type") == "event" and x["name"] == event_name
                )
                signature = (
                    event_name
                    + "("
                    + ",".join(collapse_if_tuple(x) for x in event["inputs"])
                    + ")"
                )
                try:
                    if bytes(log["topics"][0]) != Web3.keccak(text=signature):
                        continue
                    decoded = get_event_data(w3.codec, event, log)
                    if decoded["args"][child_field] != address:
                        continue
                    leaf_address = Web3.to_checksum_address(log["address"])
                except (
                    DecodingError,
                    LogTopicError,
                    MismatchedABI,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    # A signature alone does not identify a canonical deployer.
                    # Unrelated contracts may emit incompatible event payloads.
                    continue
                try:
                    runtime_hash = checked_runtime(w3, leaf_address, leaf_kind, number)
                    leaf = prove_creation(w3, leaf_address, leaf_kind, [], number)
                except ValueError as error:
                    # Keep looking for verified evidence, but retain a concrete
                    # verification failure if no canonical candidate succeeds.
                    candidate_error = error
                    continue
                if args is not None:
                    child = w3.eth.contract(address=address, abi=artifact(kind)["abi"])
                    getters = {
                        "DKGAuthorization": (
                            "coordinator",
                            "dkgId",
                            "groupPubkey",
                            "getParticipants",
                            "threshold",
                        ),
                        "FROSTAuthorization": (
                            "coordinator",
                            "groupPubkey",
                            "getParticipants",
                            "threshold",
                        ),
                        "CustodianValidator": (
                            "getCustodians",
                            "getMinCustodiansRequired",
                            "frostMultisig",
                        ),
                    }[kind]
                    observed = [
                        getattr(child.functions, name)().call(block_identifier=number)
                        for name in getters
                    ]
                    if creation_input(kind, observed) != creation_input(kind, args):
                        raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")
                return finish(
                    dict(
                        result,
                        method="canonical_leaf",
                        transaction_hash=receipt["transactionHash"].hex(),
                        log_index=str(log["logIndex"]),
                        leaf=dict(leaf, runtime_hash=runtime_hash),
                    )
                )
    if candidate_error is not None:
        raise candidate_error
    raise ValueError("BTC_CANONICAL_CREATION_NOT_FOUND")


def check_proof_anchor(w3, proof):
    if (
        proof.get("version") != "1"
        or proof.get("method") not in ("direct", "canonical_leaf", "canonical_constructor")
        or not proof.get("transaction_hash")
    ):
        raise ValueError("BTC_REGISTRY_PROVENANCE_REQUIRED")
    try:
        block = w3.eth.get_block(int(proof["block_number"]))
    except Exception:
        raise ValueError("BTC_CREATION_ANCHOR_UNAVAILABLE") from None
    if block["hash"].hex() != proof["block_hash"]:
        raise ValueError("BTC_CREATION_ANCHOR_REORG")
    if proof["method"] == "canonical_leaf":
        check_proof_anchor(w3, proof["leaf"])
    if proof["method"] == "canonical_constructor":
        check_proof_anchor(w3, proof["parent"])


def check_provenance_anchors(w3, graph, proofs):
    """Pinned immutable proof data needs only canonical block checks on each sign."""
    if not isinstance(proofs, dict) or set(proofs) != set(graph["contracts"]):
        raise ValueError("BTC_REGISTRY_PROVENANCE_REQUIRED")

    for name, item in graph["contracts"].items():
        proof = proofs[name]
        if (
            proof.get("address") != item["address"]
            or proof.get("artifact") != item["artifact"]
        ):
            raise ValueError("BTC_REGISTRY_PROVENANCE_REQUIRED")
        check_proof_anchor(w3, proof)
