"""Constructor provenance regressions use synthetic public RPC history only."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.contract_provenance import (
    check_provenance_anchors,
    creation_input,
    prove_creation,
)


def address(index):
    return Web3.to_checksum_address("0x" + index.to_bytes(20, "big").hex())


class History:
    """The code history is monotonic, as for the supported deployed contracts."""

    def __init__(self):
        self.blocks = {}
        self.receipts = {}
        self.contracts = {}
        self.code_queries = []
        self.w3 = SimpleNamespace(
            codec=Web3().codec,
            eth=SimpleNamespace(
                get_block=self.get_block,
                get_code=self.get_code,
                get_transaction_receipt=lambda tx: self.receipts[bytes(tx)],
            ),
        )

    def get_block(self, number, full_transactions=False):
        return self.blocks[number]

    def get_code(self, contract, block_identifier):
        self.code_queries.append((contract, block_identifier))
        birth, code = self.contracts.get(contract, (0, b""))
        return code if block_identifier >= birth else b""

    def transaction(self, number, *, to, input_data, deployed=None, logs=None):
        block = self.blocks.setdefault(
            number,
            dict(number=number, hash=Web3.keccak(text=f"block {number}"), transactions=[]),
        )
        tx_hash = Web3.keccak(text=f"transaction {number}/{len(block['transactions'])}")
        transaction = dict(hash=tx_hash, to=to, input=input_data)
        transaction["from"] = address(9)
        receipt = dict(
            blockNumber=number,
            blockHash=block["hash"],
            transactionHash=tx_hash,
            transactionIndex=len(block["transactions"]),
            status=1,
            contractAddress=deployed,
            logs=logs or [],
        )
        block["transactions"].append(transaction)
        self.receipts[bytes(tx_hash)] = receipt
        return transaction, receipt

    def deploy(self, contract, kind, args, number=8):
        code = bytes.fromhex(artifact(kind)["deployedBytecode"]["object"].removeprefix("0x"))
        self.contracts[contract] = (number, code)
        return self.transaction(
            number, to=None, input_data=creation_input(kind, args), deployed=contract
        )


@pytest.fixture
def direct():
    history = History()
    contract = address(100)
    tx, receipt = history.deploy(contract, "FROSTVerifier", [])
    return SimpleNamespace(history=history, address=contract, tx=tx, receipt=receipt)


def direct_proof(direct):
    return prove_creation(direct.history.w3, direct.address, "FROSTVerifier", [], 32)


def test_creation_input_uses_artifact_and_constructor_abi():
    args = [address(1), b"g" * 32, [address(2), address(3)], 2]
    bytecode = bytes.fromhex(artifact("FROSTAuthorization")["bytecode"]["object"].removeprefix("0x"))
    expected = bytecode + encode(["address", "bytes", "address[]", "uint256"], args)
    assert creation_input("FROSTAuthorization", args) == expected


@pytest.mark.parametrize("tamper", [None, "runtime", "initcode"])
@pytest.mark.parametrize("version", ["Legacy", "V1", "V2"])
def test_old_treasury_requires_exact_reviewed_runtime_and_constructor(monkeypatch, tamper, version):
    from signer_agent import bitcoin_trust

    kind = "ULRConfederationTreasury"
    reference = Path(__file__).resolve().parents[1] / f"contracts/ULRConfederationTreasury{version}.json"
    legacy = json.loads(reference.read_text())
    current = dict(deepcopy(legacy), abi=artifact(kind)["abi"])
    current["deployedBytecode"]["object"] += "00"
    current["bytecode"]["object"] += "00"
    monkeypatch.setattr(bitcoin_trust, "artifact", lambda _: current)
    history = History()
    target = address(100)
    args = [address(1), "conf", address(2), 1000, 9000]
    runtime = bytes.fromhex(legacy["deployedBytecode"]["object"].removeprefix("0x"))
    initcode = bytes.fromhex(legacy["bytecode"]["object"].removeprefix("0x"))
    if tamper == "runtime":
        runtime = bytes([runtime[0] ^ 1]) + runtime[1:]
    if tamper == "initcode":
        initcode += b"\0"
    initcode += encode(["address", "string", "address", "uint256", "uint256"], args)
    history.contracts[target] = (8, runtime)
    history.transaction(8, to=None, input_data=initcode, deployed=target)
    if tamper is None:
        proof = prove_creation(history.w3, target, kind, args, 8, birth_hint=8)
        assert proof["runtime_hash"] == Web3.keccak(runtime).hex()
        assert proof["initcode_hash"] == Web3.keccak(initcode).hex()
    else:
        expected = "BTC_CONTRACT_BUILD_MISMATCH" if tamper == "runtime" else "BTC_CONSTRUCTOR_INPUT_MISMATCH"
        with pytest.raises(ValueError, match=expected):
            prove_creation(history.w3, target, kind, args, 8, birth_hint=8)


@pytest.mark.parametrize("kind,args", [
    ("ConfederationBitcoinReserve", ["pool", address(1), address(2), address(3)]),
    ("ConfederationPoolV2", ["conf", address(1), address(2), address(3)]),
    ("ConfederationPoolV2Deployer", [address(1)]),
])
def test_legacy_custody_origin_requires_explicit_recovery_and_exact_initcode(kind, args):
    from signer_agent.bitcoin_trust import checked_runtime

    path = Path(__file__).resolve().parents[1] / "contracts" / f"{kind}CustodyLegacy2630366.json"
    legacy = json.loads(path.read_text())
    history = History()
    target = address(100)
    runtime = bytes.fromhex(legacy["deployedBytecode"]["object"].removeprefix("0x"))
    initcode = bytes.fromhex(legacy["bytecode"]["object"].removeprefix("0x"))
    constructor = next(row for row in artifact(kind)["abi"] if row["type"] == "constructor")
    initcode += encode([item["type"] for item in constructor["inputs"]], args)
    history.contracts[target] = (8, runtime)
    history.transaction(8, to=None, input_data=initcode, deployed=target)
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        checked_runtime(history.w3, target, kind, 8)
    proof = prove_creation(history.w3, target, kind, args, 8, birth_hint=8, legacy_custody=True)
    assert proof["runtime_hash"] == Web3.keccak(runtime).hex()
    assert proof["initcode_hash"] == Web3.keccak(initcode).hex()
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        prove_creation(history.w3, target, kind, [*args[:-1], address(4)], 8,
                       birth_hint=8, legacy_custody=True)


def test_legacy_ulr_factory_can_only_be_proven_with_explicit_treasury_compatibility():
    kind = "ConfederationFactory"
    args = [address(i) for i in range(1, 5)]
    path = Path(__file__).resolve().parents[1] / "contracts/ConfederationFactoryULRLegacy2630366.json"
    legacy = json.loads(path.read_text())
    history = History()
    target = address(100)
    runtime = bytes.fromhex(legacy["deployedBytecode"]["object"].removeprefix("0x"))
    initcode = bytes.fromhex(legacy["bytecode"]["object"].removeprefix("0x")) + encode(["address"] * 4, args)
    history.contracts[target] = (8, runtime)
    history.transaction(8, to=None, input_data=initcode, deployed=target)
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        prove_creation(history.w3, target, kind, args, 8, birth_hint=8)
    proof = prove_creation(history.w3, target, kind, args, 8, birth_hint=8, legacy_ulr_factory=True)
    assert proof["method"] == "direct"
    assert proof["initcode_hash"] == Web3.keccak(initcode).hex()


@pytest.mark.parametrize("kind,types,args,pin", [
    ("ULRStaking", ["address", "address", "uint256", "uint256"],
     [address(1), address(2), 200_000 * 10**18, 10**18],
     "evm-v1:84e2e90b100a4a644f588cd309d252a71106f74efdab005fb5a5878adcc286c2"),
    ("BitcoinMintAdmission", ["address", "address"], [address(1), address(2)],
     "evm-v1:159ad4cb74e152d67e4bd4e499b82b98c2abb655f4664db8934dac532bac04a8"),
])
def test_previous_build_requires_opt_in_and_exact_runtime_and_initcode(monkeypatch, kind, types, args, pin):
    from signer_agent import bitcoin_trust
    from signer_agent.bootstrap import artifact_fingerprint

    previous = bitcoin_trust.previous_artifact(kind)
    assert artifact_fingerprint(previous) == pin
    assert bitcoin_trust.previous_artifact("FROSTVerifier") is None
    current = deepcopy(previous)
    current["deployedBytecode"]["object"] += "00"
    current["bytecode"]["object"] += "00"
    monkeypatch.setattr(bitcoin_trust, "artifact", lambda _: current)

    history = History()
    target = address(100)
    runtime = bytes.fromhex(previous["deployedBytecode"]["object"].removeprefix("0x"))
    initcode = bytes.fromhex(previous["bytecode"]["object"].removeprefix("0x"))
    tx, _ = history.transaction(8, to=None, input_data=initcode + encode(types, args), deployed=target)
    history.contracts[target] = (8, runtime)
    with pytest.raises(ValueError, match="^BTC_CONTRACT_BUILD_MISMATCH$"):
        bitcoin_trust.checked_runtime(history.w3, target, kind, 8)
    assert bitcoin_trust.checked_runtime(history.w3, target, kind, 8, previous_build=True) == Web3.keccak(runtime).hex()

    correct_input = tx["input"]
    tx["input"] = initcode + b"\0" + encode(types, args)
    with pytest.raises(ValueError, match="^BTC_CONSTRUCTOR_INPUT_MISMATCH$"):
        prove_creation(history.w3, target, kind, args, 8, previous_build=True)
    tx["input"] = correct_input
    proof = prove_creation(history.w3, target, kind, args, 8, previous_build=True)
    assert proof["initcode_hash"] == Web3.keccak(correct_input).hex()

    history.contracts[target] = (8, bytes([runtime[0] ^ 1]) + runtime[1:])
    with pytest.raises(ValueError, match="^BTC_CONTRACT_BUILD_MISMATCH$"):
        prove_creation(history.w3, target, kind, args, 8, previous_build=True)


@pytest.mark.parametrize("args", [[], None])
def test_direct_creation_has_exact_artifact_and_historical_anchor(direct, args):
    proof = prove_creation(direct.history.w3, direct.address, "FROSTVerifier", args, 32)
    assert proof == dict(
        version="1",
        address=direct.address,
        artifact="FROSTVerifier",
        method="direct",
        block_number="8",
        block_hash=direct.receipt["blockHash"].hex(),
        transaction_hash=direct.tx["hash"].hex(),
        initcode_hash=Web3.keccak(direct.tx["input"]).hex(),
        runtime_hash=Web3.keccak(direct.history.contracts[direct.address][1]).hex(),
        sender=address(9),
    )
    assert (direct.address, 7) in direct.history.code_queries
    assert (direct.address, 8) in direct.history.code_queries


def test_correct_birth_hint_proves_creation_without_searching_old_state(direct):
    code = direct.history.get_code

    def recent_only(contract, *, block_identifier):
        assert block_identifier == 32, "direct receipt replay must not query archive state"
        return code(contract, block_identifier)

    direct.history.w3.eth.get_code = recent_only
    proof = prove_creation(
        direct.history.w3, direct.address, "FROSTVerifier", [], 32, birth_hint="8"
    )
    assert proof["block_number"] == "8"
    assert {number for _, number in direct.history.code_queries} == {32}


def test_nested_constructor_checks_hint_before_adding_proof_to_cache():
    import rlp
    history = History()
    parent = address(100)
    kind = "BitcoinFeeReserveUser"
    child = Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(parent[2:]), 1]))[-20:])
    history.deploy(parent, "BitcoinFeeReserve", [address(i) for i in range(1, 6)] + [1000, 1000])
    runtime = bytes.fromhex(artifact(kind)["deployedBytecode"]["object"].removeprefix("0x"))
    history.contracts[child] = (8, runtime)
    history.w3.eth.contract = lambda **_: SimpleNamespace(
        functions=SimpleNamespace(reserve=lambda: SimpleNamespace(call=lambda **_: parent)))
    with pytest.raises(ValueError, match="BTC_CREATION_BLOCK_MISMATCH"):
        prove_creation(history.w3, child, kind, [parent], 32, birth_hint=9)
    assert prove_creation(history.w3, child, kind, [parent], 32, birth_hint=8)["block_number"] == "8"
    with pytest.raises(ValueError, match="BTC_CREATION_BLOCK_MISMATCH"):
        prove_creation(history.w3, child, kind, [parent], 32, birth_hint=9)


@pytest.mark.parametrize("hint,error", [
    (0, "BTC_CREATION_BLOCK_MISMATCH"),
    (7, "BTC_CANONICAL_CREATION_NOT_FOUND"),
    (9, "BTC_CANONICAL_CREATION_NOT_FOUND"),
    (33, "BTC_CREATION_BLOCK_MISMATCH"),
])
def test_wrong_birth_hint_cannot_replace_creation_evidence(direct, hint, error):
    direct.history.blocks.setdefault(hint, dict(
        number=hint, hash=Web3.keccak(text=f"block {hint}"), transactions=[]))
    with pytest.raises(ValueError, match=error):
        prove_creation(
            direct.history.w3, direct.address, "FROSTVerifier", [], 32, birth_hint=hint
        )


@pytest.mark.parametrize("change,error", [
    ("bytecode", "BTC_CONSTRUCTOR_INPUT_MISMATCH"),
    ("extra_data", "BTC_CONSTRUCTOR_INPUT_MISMATCH"),
    ("non_creation", "BTC_CANONICAL_CREATION_NOT_FOUND"),
    ("failed_receipt", "BTC_CANONICAL_CREATION_NOT_FOUND"),
    ("different_contract", "BTC_CANONICAL_CREATION_NOT_FOUND"),
    ("receipt_block", "BTC_CREATION_RECEIPT_MISMATCH"),
    ("receipt_transaction", "BTC_CREATION_RECEIPT_MISMATCH"),
])
def test_untrusted_birth_hint_cannot_bypass_direct_creation_proof(direct, change, error):
    code = direct.history.get_code

    def recent_only(contract, *, block_identifier):
        assert block_identifier == 32, "reject bad direct evidence without archive access"
        return code(contract, block_identifier)

    direct.history.w3.eth.get_code = recent_only
    if change == "bytecode":
        direct.tx["input"] = bytes([direct.tx["input"][0] ^ 1]) + direct.tx["input"][1:]
    elif change == "extra_data":
        direct.tx["input"] += bytes(32)
    elif change == "non_creation":
        direct.tx["to"] = address(9)
    elif change == "failed_receipt":
        direct.receipt["status"] = 0
    elif change == "different_contract":
        direct.receipt["contractAddress"] = address(101)
    elif change == "receipt_block":
        direct.receipt["blockHash"] = Web3.keccak(text="other block")
    elif change == "receipt_transaction":
        direct.receipt["transactionHash"] = Web3.keccak(text="other transaction")
    with pytest.raises(ValueError, match=error):
        prove_creation(direct.history.w3, direct.address, "FROSTVerifier", [], 32, birth_hint=8)


def test_hinted_creation_rechecks_canonical_block_after_receipt(direct):
    calls = []
    original = direct.history.get_block

    def reorg_after_receipt(number, full_transactions=False):
        result = deepcopy(original(number, full_transactions))
        calls.append(number)
        if len(calls) > 1:
            result["hash"] = Web3.keccak(text="replacement block")
        return result

    direct.history.w3.eth.get_block = reorg_after_receipt
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        prove_creation(direct.history.w3, direct.address, "FROSTVerifier", [], 32, birth_hint=8)


@pytest.mark.parametrize("change", ["bytecode", "extra_data", "non_creation"])
def test_direct_creation_rejects_input_or_transaction_mismatch(direct, change):
    if change == "bytecode":
        direct.tx["input"] = bytes([direct.tx["input"][0] ^ 1]) + direct.tx["input"][1:]
    elif change == "extra_data":
        direct.tx["input"] += bytes(32)
    else:
        direct.tx["to"] = address(9)
    error = "BTC_CANONICAL_CREATION_NOT_FOUND" if change == "non_creation" else "BTC_CONSTRUCTOR_INPUT_MISMATCH"
    with pytest.raises(ValueError, match=error):
        direct_proof(direct)


def test_direct_creation_rejects_different_expected_constructor_arguments():
    history = History()
    contract = address(100)
    args = [address(1), b"g" * 32, [address(2), address(3)], 2]
    history.deploy(contract, "FROSTAuthorization", args)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        prove_creation(history.w3, contract, "FROSTAuthorization", [*args[:-1], 1], 32)


def test_unspecified_constructor_arguments_accept_canonical_dynamic_encoding():
    history = History()
    contract = address(100)
    args = [address(1), b"g" * 32, [address(2), address(3)], 2]
    transaction, _ = history.deploy(contract, "FROSTAuthorization", args)
    proof = prove_creation(history.w3, contract, "FROSTAuthorization", None, 32)
    assert proof["method"] == "direct"
    assert proof["initcode_hash"] == Web3.keccak(transaction["input"]).hex()


@pytest.mark.parametrize("kind,args", [
    ("FROSTVerifier", []),
    ("FROSTAuthorization", [address(1), b"g" * 32, [address(2), address(3)], 2]),
])
def test_unspecified_constructor_arguments_reject_trailing_abi_data(kind, args):
    history = History()
    contract = address(100)
    transaction, _ = history.deploy(contract, kind, args)
    transaction["input"] += bytes(32)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        prove_creation(history.w3, contract, kind, None, 32)


@pytest.mark.parametrize("field,value", [
    ("blockNumber", 7),
    ("blockHash", Web3.keccak(text="different block")),
    ("transactionHash", Web3.keccak(text="different transaction")),
])
def test_creation_receipt_must_match_block_and_transaction(direct, field, value):
    direct.receipt[field] = value
    with pytest.raises(ValueError, match="BTC_CREATION_RECEIPT_MISMATCH"):
        direct_proof(direct)


def test_creation_block_number_must_match_requested_birth_block(direct):
    direct.history.blocks[8]["number"] = 9
    direct.receipt["blockNumber"] = 9
    with pytest.raises(ValueError, match="BTC_CREATION_BLOCK_MISMATCH"):
        direct_proof(direct)


@pytest.mark.parametrize("change", ["failed_receipt", "different_contract", "no_transaction"])
def test_direct_creation_requires_successful_receipt_for_target(direct, change):
    if change == "failed_receipt":
        direct.receipt["status"] = 0
    elif change == "different_contract":
        direct.receipt["contractAddress"] = address(101)
    else:
        direct.history.blocks[8]["transactions"] = []
    with pytest.raises(ValueError, match="BTC_CANONICAL_CREATION_NOT_FOUND"):
        direct_proof(direct)


def test_missing_latest_code_cannot_prove_creation(direct):
    direct.history.contracts.clear()
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        direct_proof(direct)


def test_genesis_code_cannot_prove_reviewed_constructor(direct):
    _, code = direct.history.contracts[direct.address]
    direct.history.contracts[direct.address] = (0, code)
    with pytest.raises(ValueError, match="BTC_GENESIS_CONTRACT_UNSUPPORTED"):
        direct_proof(direct)


def anchored_graph(direct):
    proof = direct_proof(direct)
    graph = dict(contracts=dict(verifier=dict(address=direct.address, artifact="FROSTVerifier")))
    return graph, dict(verifier=proof)


def test_cached_creation_proof_still_checks_explicit_creation_height(direct):
    w3 = direct.history.w3
    proof = prove_creation(w3, direct.address, "FROSTVerifier", [], 32, birth_hint=8)
    assert prove_creation(w3, direct.address, "FROSTVerifier", [], 32, birth_hint=8) == proof
    with pytest.raises(ValueError, match="^BTC_CREATION_BLOCK_MISMATCH$"):
        prove_creation(w3, direct.address, "FROSTVerifier", [], 32, birth_hint=9)


def test_cached_creation_anchor_is_checked_against_canonical_block(direct):
    graph, proofs = anchored_graph(direct)
    check_provenance_anchors(direct.history.w3, graph, proofs)
    direct.history.blocks[8]["hash"] = Web3.keccak(text="replacement block")
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        check_provenance_anchors(direct.history.w3, graph, proofs)


def test_unavailable_cached_creation_anchor_fails_closed(direct):
    graph, proofs = anchored_graph(direct)
    direct.history.blocks.clear()
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_UNAVAILABLE"):
        check_provenance_anchors(direct.history.w3, graph, proofs)


def test_repeated_provenance_lookup_rechecks_canonical_block(direct):
    direct_proof(direct)
    direct.history.blocks[8]["hash"] = Web3.keccak(text="replacement cached block")
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        direct_proof(direct)


def test_repeated_provenance_lookup_rechecks_runtime(direct):
    direct_proof(direct)
    birth, code = direct.history.contracts[direct.address]
    direct.history.contracts[direct.address] = (birth, code[:-1])
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        direct_proof(direct)


def test_authenticated_persistent_proof_survives_restart_without_historical_state(direct, tmp_path):
    from signer_agent.secure_state import SecureState
    from signer_agent.verified_cache import cache_state
    direct.history.w3.eth.chain_id = 31337
    token = cache_state.set(SecureState(str(tmp_path / "public-cache"), "test-password"))
    try:
        expected = direct_proof(direct)
        del direct.history.w3._satoshify_creation_proofs
        code = direct.history.get_code
        def recent_only(address, *, block_identifier):
            assert block_identifier == 32, "warm proof must not query archive state"
            return code(address, block_identifier)
        direct.history.w3.eth.get_code = recent_only
        assert direct_proof(direct) == expected
        direct.history.blocks[8]["hash"] = Web3.keccak(text="reorg")
        with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
            direct_proof(direct)
    finally:
        cache_state.reset(token)


def test_returned_proof_cannot_mutate_cached_provenance(direct):
    first = direct_proof(direct)
    expected = dict(first)
    first["address"] = address(999)
    second = direct_proof(direct)
    assert second == expected
    second["block_hash"] = "caller mutation"
    assert direct_proof(direct) == expected


@pytest.mark.parametrize("change", ["missing", "extra", "address", "artifact", "method", "version", "transaction"])
def test_cached_proof_must_match_pinned_contract_graph(direct, change):
    graph, proofs = anchored_graph(direct)
    if change == "missing":
        proofs.clear()
    elif change == "extra":
        proofs["extra"] = dict(proofs["verifier"])
    else:
        field, value = {
            "address": ("address", address(999)),
            "artifact": ("artifact", "RegtestHeaderRelay"),
            "method": ("method", "unverified"),
            "version": ("version", "0"),
            "transaction": ("transaction_hash", ""),
        }[change]
        proofs["verifier"][field] = value
    with pytest.raises(ValueError, match="BTC_REGISTRY_PROVENANCE_REQUIRED"):
        check_provenance_anchors(direct.history.w3, graph, proofs)


def nested_history(kind):
    history = History()
    child, leaf = address(100), address(200)
    custodian = kind == "CustodianValidator"
    leaf_kind = "FROSTCustodianDeployer" if custodian else "FROSTAuthorizationDeployer"
    history.deploy(leaf, leaf_kind, [], number=3)
    code = bytes.fromhex(artifact(kind)["deployedBytecode"]["object"].removeprefix("0x"))
    history.contracts[child] = (8, code)
    transaction, receipt = history.transaction(8, to=leaf, input_data=b"")
    if custodian:
        topics = [Web3.keccak(text="FROSTCustodianDeployed(address,address,address)"), encode(["address"], [address(9)])]
        data = encode(["address", "address"], [address(101), child])
    else:
        topics = [Web3.keccak(text="FROSTAuthorizationDeployed(address,address)"), encode(["address"], [child]), encode(["address"], [address(1)])]
        data = b""
    receipt["logs"].append(dict(
        address=leaf,
        topics=topics,
        data=data,
        logIndex=0,
        transactionIndex=0,
        transactionHash=transaction["hash"],
        blockHash=receipt["blockHash"],
        blockNumber=8,
        removed=False,
    ))
    return history, child, leaf, leaf_kind


@pytest.mark.parametrize("kind", ["DKGAuthorization", "FROSTAuthorization", "CustodianValidator"])
@pytest.mark.parametrize("birth_hint", [None, 8])
def test_nested_creation_requires_canonical_leaf_with_own_direct_creation(kind, birth_hint):
    history, child, leaf, leaf_kind = nested_history(kind)
    proof = prove_creation(history.w3, child, kind, None, 32, birth_hint=birth_hint)
    assert proof["method"] == "canonical_leaf"
    assert proof["block_number"] == "8"
    assert proof["log_index"] == "0"
    assert proof["leaf"]["method"] == "direct"
    assert proof["leaf"]["address"] == leaf
    assert proof["leaf"]["artifact"] == leaf_kind
    assert proof["leaf"]["block_number"] == "3"
    assert proof["leaf"]["runtime_hash"] == Web3.keccak(history.contracts[leaf][1]).hex()
    assert (child, 7) in history.code_queries
    assert (child, 8) in history.code_queries


def test_internal_leaf_hint_retains_historical_creation_boundary():
    history, child, _, _ = nested_history("DKGAuthorization")
    history.contracts[child] = (7, history.contracts[child][1])
    with pytest.raises(ValueError, match="BTC_CREATION_BLOCK_MISMATCH"):
        prove_creation(history.w3, child, "DKGAuthorization", None, 32, birth_hint=8)


def test_internal_leaf_hint_cannot_silently_fallback_from_missing_archive():
    history, child, _, _ = nested_history("DKGAuthorization")
    code = history.get_code

    def recent_only(contract, *, block_identifier):
        if block_identifier != 32:
            raise RuntimeError("historical state unavailable")
        return code(contract, block_identifier)

    history.w3.eth.get_code = recent_only
    with pytest.raises(RuntimeError, match="historical state unavailable"):
        prove_creation(history.w3, child, "DKGAuthorization", None, 32, birth_hint=8)


def test_leaf_event_cannot_replace_proof_of_leaf_constructor():
    history, child, _, _ = nested_history("DKGAuthorization")
    history.blocks[3]["transactions"][0]["input"] += bytes(32)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        prove_creation(history.w3, child, "DKGAuthorization", None, 32)


def add_unverified_leaf_event(history, leaf_kind, failure):
    receipt = history.receipts[bytes(history.blocks[8]["transactions"][0]["hash"])]
    unverified = deepcopy(receipt["logs"][0])
    unverified["address"] = address(300)
    receipt["logs"][0]["logIndex"] = 1
    if failure == "topics":
        unverified["topics"] = unverified["topics"][:1]
    elif failure == "data":
        unverified["data"] = b""
    elif failure == "constructor":
        transaction, _ = history.deploy(address(300), leaf_kind, [], number=2)
        transaction["input"] += bytes(32)
    receipt["logs"].insert(0, unverified)
    return receipt


@pytest.mark.parametrize("failure", ["runtime", "constructor", "topics", "data"])
def test_unrelated_leaf_events_do_not_hide_later_verified_creation(failure):
    kind = "CustodianValidator" if failure == "data" else "DKGAuthorization"
    history, child, leaf, leaf_kind = nested_history(kind)
    add_unverified_leaf_event(history, leaf_kind, failure)
    proof = prove_creation(history.w3, child, kind, None, 32)
    assert proof["method"] == "canonical_leaf"
    assert proof["log_index"] == "1"
    assert proof["leaf"]["address"] == leaf


@pytest.mark.parametrize("failure,error", [
    ("runtime", "BTC_CONTRACT_BUILD_MISMATCH"),
    ("constructor", "BTC_CONSTRUCTOR_INPUT_MISMATCH"),
    ("topics", "BTC_CANONICAL_CREATION_NOT_FOUND"),
    ("data", "BTC_CANONICAL_CREATION_NOT_FOUND"),
])
def test_unverified_leaf_events_without_canonical_evidence_fail_closed(failure, error):
    kind = "CustodianValidator" if failure == "data" else "DKGAuthorization"
    history, child, _, leaf_kind = nested_history(kind)
    receipt = add_unverified_leaf_event(history, leaf_kind, failure)
    receipt["logs"] = receipt["logs"][:1]
    with pytest.raises(ValueError, match=error):
        prove_creation(history.w3, child, kind, None, 32)


@pytest.mark.parametrize("birth_hint", [None, 8])
def test_verified_leaf_does_not_suppress_child_constructor_mismatch(birth_hint):
    history, child, _, _ = nested_history("DKGAuthorization")
    observed = dict(
        coordinator=address(1),
        dkgId=b"d" * 32,
        groupPubkey=b"g" * 32,
        getParticipants=[address(2), address(3)],
        threshold=2,
    )
    functions = SimpleNamespace(**{
        name: lambda value=value: SimpleNamespace(call=lambda **kwargs: value)
        for name, value in observed.items()
    })
    history.w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=functions)
    expected_args = [*list(observed.values())[:-1], 1]
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        prove_creation(history.w3, child, "DKGAuthorization", expected_args, 32, birth_hint=birth_hint)


def test_cached_nested_proof_rechecks_leaf_creation_anchor():
    history, child, _, _ = nested_history("DKGAuthorization")
    proof = prove_creation(history.w3, child, "DKGAuthorization", None, 32)
    graph = dict(contracts=dict(authorization=dict(address=child, artifact="DKGAuthorization")))
    proofs = dict(authorization=proof)
    check_provenance_anchors(history.w3, graph, proofs)
    history.blocks[3]["hash"] = Web3.keccak(text="replacement leaf block")
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        check_provenance_anchors(history.w3, graph, proofs)
