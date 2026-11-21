"""Offline adversarial tests of the public staged-deployment runner.

The fake RPC controls receipts, forks, code and nonces independently of Foundry's
journal. These tests never spawn a node or sign/broadcast a real transaction.
"""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import ContractLogicError, TransactionNotFound

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("deployment_stages_under_test", ROOT / "ops/deployment_stages.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

# Artificial test identity; never funded or connected to a node in this module.
KEY = "0x" + "00" * 31 + "01"
SENDER = Account.from_key(KEY).address
CHAIN = 31337
CODE = bytes.fromhex("600160005260206000f3")
RPC = "https://local-user:local-pass@example.invalid/private-token"


def block_hash(number):
    return HexBytes(Web3.keccak(text="canonical block " + str(number)))


class FakeEth:
    chain_id = CHAIN

    def __init__(self):
        self.block_number = 100
        self.latest_nonce = self.pending_nonce = 0
        self.codes, self.transactions, self.receipts, self.blocks = {}, {}, {}, {}
        self.historical_codes = {}
        self.birth_blocks = {}

    def get_transaction_count(self, sender, tag="latest"):
        assert sender == SENDER
        return self.latest_nonce if tag == "latest" else self.pending_nonce

    def get_code(self, deployed_address, block_identifier=None):
        target = Web3.to_checksum_address(deployed_address)
        if block_identifier is not None and block_identifier < self.birth_blocks.get(target, 0):
            return HexBytes(b"")
        return HexBytes(self.historical_codes.get((target, block_identifier), self.codes.get(target, b"")))

    def get_transaction_receipt(self, tx_hash):
        if not isinstance(tx_hash, str):
            tx_hash = Web3.to_hex(tx_hash)
        if tx_hash not in self.receipts:
            raise TransactionNotFound("intentionally unmined")
        return self.receipts[tx_hash]

    def get_transaction(self, tx_hash):
        return self.transactions[tx_hash]

    def get_block(self, number, full_transactions=False):
        block = {"number": number, "hash": self.blocks.get(number, block_hash(number))}
        if full_transactions:
            block["transactions"] = [dict(tx, hash=HexBytes(tx_hash), input=HexBytes(tx["input"]))
                for tx_hash, tx in self.transactions.items()
                if self.receipts[tx_hash]["blockNumber"] == number]
        return block

    def mine(self, entry, *, status=1, number=100):
        tx_hash = entry["hash"]
        transaction = entry["transaction"]
        self.transactions[tx_hash] = deepcopy(transaction)
        deployed_address = entry["contractAddress"] if entry["transactionType"] == "CREATE" else None
        if deployed_address and status:
            self.codes[deployed_address] = CODE
            self.birth_blocks[deployed_address] = number
        self.receipts[tx_hash] = dict(transactionHash=HexBytes(tx_hash), blockNumber=number,
            blockHash=block_hash(number), status=status, contractAddress=deployed_address, logs=[])
        self.latest_nonce = self.pending_nonce = max(self.latest_nonce, int(transaction["nonce"], 16) + 1)


@pytest.fixture
def w3(monkeypatch):
    rpc = SimpleNamespace(eth=FakeEth())
    monkeypatch.setattr(runner, "connect_rpc", lambda url: rpc)
    monkeypatch.setattr(runner, "artifact", lambda name: {
        "metadata": {}, "abi": [], "deployedBytecode": {"object": CODE.hex(), "immutableReferences": {}}
    })
    # Existing journal/schema tests isolate runner behavior. The origin tests
    # below restore the actual constructor prover against synthetic RPC history.
    monkeypatch.setattr(runner, "prove_creation", fake_creation_proof)
    monkeypatch.setenv("PRIVATE_KEY", KEY)
    return rpc


def fake_creation_proof(w3, target, name, args, latest, *, birth_hint=None):
    return dict(version="1", method="direct", address=target, artifact=name,
        block_number="1", block_hash=block_hash(1).hex(),
        runtime_hash=Web3.keccak(w3.eth.get_code(target)).hex(),
        transaction_hash=Web3.keccak(text="creation " + target).hex(),
        initcode_hash=Web3.keccak(text="constructor " + name).hex(), sender=SENDER)


def run_state(*, stage="base", success=True, run_number=1, start_nonce=0):
    state = runner.new_state(CHAIN, 1)
    state["sender"] = SENDER
    run_id = f"{run_number:032x}"
    append_run(state, run_id, stage, success=success, start_nonce=start_nonce)
    return state, run_id


def append_run(state, run_id, stage, *, success=True, start_nonce=0):
    state["runs"][run_id] = dict(stage=stage, parameters_hash=runner.digest(state["parameters"]),
        previous_parameters=dict(state["parameters"]), start_nonce=start_nonce,
        transaction_hashes=[], plan_hash=None, planned_count=0,
        execution_success=success, no_transactions=False, confirmed=False)
    state["stages"][stage] = dict(confirmed=False, transaction_hashes=[], run_id=run_id)
    state["inflight_run"] = run_id


def entries(names=("FROSTVerifier",), *, sent=None, start_nonce=0):
    result = []
    for index, name in enumerate(names):
        nonce = start_nonce + index
        deployed_address = runner.create_address(SENDER, nonce)
        transaction = dict(chainId=hex(CHAIN), nonce=hex(nonce), **{"from": SENDER},
                           to=None, value="0x0", input="0x600160005360016000f3")
        tx_hash = Web3.to_hex(Web3.keccak(text=json.dumps(transaction, sort_keys=True) + name))
        result.append(dict(hash=tx_hash if sent is None or index < sent else None,
            transactionType="CREATE", contractName=name, contractAddress=deployed_address,
            transaction=transaction))
    return result


def write_journal(path, transactions, *, chain=CHAIN):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(chain=chain, transactions=transactions, receipts=[], pending=[])))


def import_entries(tmp_path, state, run_id, transactions):
    path = runner.journal_path(tmp_path / "manifest.json", run_id, CHAIN)
    write_journal(path, transactions)
    runner.import_broadcast(path, state, run_id)
    return path


def cli(tmp_path, *, stage="base", broadcast=True, resume=False, parameters=None, confirmations=1):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(chain_id=str(CHAIN), confirmations=str(confirmations),
                                    parameters=parameters or {})))
    args = ["--config", str(config), "--manifest", str(tmp_path / "manifest.json"),
            "--stage", stage, "--rpc-url", RPC]
    if broadcast:
        args.append("--broadcast")
    if resume:
        args.append("--resume-broadcast")
    return args


def load(tmp_path):
    return json.loads((tmp_path / "manifest.json").read_text())


def fake_forge(monkeypatch, callback):
    calls = []

    def execute(command, *, cwd, env, text, capture_output):
        calls.append(dict(command=command, environment=dict(env)))
        path = Path(env["FOUNDRY_BROADCAST"]) / "DeployFederation.s.sol" / str(CHAIN) / "run-latest.json"
        return callback(path, command, env)

    monkeypatch.setattr(runner.subprocess, "run", execute)
    return calls


def result(code=0, output=""):
    return SimpleNamespace(returncode=code, stdout=output, stderr="")


@pytest.mark.parametrize("field,value", [("chain_id", True), ("chain_id", 1.5),
    ("confirmations", 0), ("confirmations", False)])
def test_config_rejects_lossy_integer_coercions(tmp_path, w3, field, value):
    args = cli(tmp_path)
    path = tmp_path / "config.json"
    config = json.loads(path.read_text())
    config[field] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="DEPLOYMENT_INTEGER"):
        runner.main(args)
    assert not (tmp_path / "manifest.json").exists()


def test_config_and_manifest_reject_secret_fields_before_persisting(tmp_path, w3):
    args = cli(tmp_path, parameters={"PRIVATE_KEY": KEY})
    with pytest.raises(ValueError, match="PUBLIC_PARAMETERS_ONLY"):
        runner.main(args)
    assert not (tmp_path / "manifest.json").exists()
    state = runner.new_state(CHAIN, 1)
    state["rpc_url"] = RPC
    with pytest.raises(ValueError, match="DEPLOYMENT_SCHEMA_FIELDS"):
        runner.save(tmp_path / "manifest.json", state)
    state.pop("rpc_url")
    state["parameters"]["FROST_SECRET_SHARE"] = "should never be public"
    with pytest.raises(ValueError, match="PUBLIC_PARAMETERS_ONLY"):
        runner.save(tmp_path / "manifest.json", state)
    assert not (tmp_path / "manifest.json").exists()


def test_duplicate_json_keys_and_unknown_nested_manifest_fields_fail(tmp_path):
    path = tmp_path / "ambiguous.json"
    path.write_text('{"chain_id":31337,"chain_id":1}')
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        runner.read_json(path)
    state, run_id = run_state()
    state["runs"][run_id]["PRIVATE_KEY"] = KEY
    with pytest.raises(ValueError, match="DEPLOYMENT_SCHEMA_FIELDS"):
        runner.save(tmp_path / "manifest.json", state)


def test_atomic_public_manifest_permissions(tmp_path):
    path = tmp_path / "manifest.json"
    state = runner.new_state(CHAIN, 1)
    runner.save(path, state)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == state
    assert not list(tmp_path.glob("*.tmp"))


def immutable_build(monkeypatch, refs):
    expected = b"\x7f" + bytes(32) + b"\x50\x7f" + bytes(32) + b"\x00"
    monkeypatch.setattr(runner, "artifact", lambda name: {
        "metadata": {}, "deployedBytecode": {"object": expected.hex(), "immutableReferences": refs}
    })
    return expected


def test_runtime_masks_only_compiler_immutable_words_and_requires_same_value(w3, monkeypatch):
    expected = immutable_build(monkeypatch, {"42": [{"start": 1, "length": 32}, {"start": 35, "length": 32}]})
    actual = bytearray(expected)
    actual[1:33] = actual[35:67] = b"\x11" * 32
    w3.eth.codes[SENDER] = bytes(actual)
    assert runner.verify_runtime(w3, "FROSTVerifier", SENDER) == bytes(actual)
    actual[35] = 0x22
    w3.eth.codes[SENDER] = bytes(actual)
    with pytest.raises(ValueError, match="INCONSISTENT_IMMUTABLE"):
        runner.verify_runtime(w3, "FROSTVerifier", SENDER)
    actual[35] = 0x11
    actual[-1] = 0x01
    w3.eth.codes[SENDER] = bytes(actual)
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH"):
        runner.verify_runtime(w3, "FROSTVerifier", SENDER)


@pytest.mark.parametrize("refs", [
    {"1": [{"start": -1, "length": 32}]}, {"1": [{"start": 999, "length": 32}]},
    {"1": [{"start": 1, "length": 33}]}, {"1": [{"start": True, "length": 32}]},
    {"1": [{"start": 1, "length": 32}], "2": [{"start": 2, "length": 32}]},
    {"1": []},
])
def test_malformed_compiler_immutable_masks_fail_closed(w3, monkeypatch, refs):
    w3.eth.codes[SENDER] = immutable_build(monkeypatch, refs)
    with pytest.raises(ValueError, match="INVALID_IMMUTABLE_REFERENCES"):
        runner.verify_runtime(w3, "FROSTVerifier", SENDER)


def test_duplicate_address_does_not_rebaseline_changed_immutable_code(w3, monkeypatch):
    expected = immutable_build(monkeypatch, {"42": [{"start": 1, "length": 32}]})
    w3.eth.codes[SENDER] = expected
    state = runner.new_state(CHAIN, 1)
    first_hash = "0x" + "11" * 32
    runner.add_address(w3, state, "FROSTVerifier", SENDER, first_hash)
    runner.add_address(w3, state, "FROSTVerifier", SENDER, "0x" + "22" * 32)
    assert state["addresses"]["FROSTVerifier"]["tx_hash"] == first_hash
    changed = bytearray(expected)
    changed[1] = 1
    w3.eth.codes[SENDER] = bytes(changed)
    with pytest.raises(ValueError, match="DEPLOYMENT_CODE_CHANGED"):
        runner.add_address(w3, state, "FROSTVerifier", SENDER, first_hash)


def test_supplied_actual_erc20_runtime_cannot_be_used_as_verifier_before_forge(tmp_path, w3, monkeypatch):
    def real_artifact(name):
        paths = list((ROOT / "web3/contracts/out").glob(f"*/{name}.json"))
        assert len(paths) == 1
        return json.loads(paths[0].read_text())

    monkeypatch.setattr(runner, "artifact", real_artifact)
    w3.eth.codes[SENDER] = bytes.fromhex(real_artifact("DAOTestUSD")["deployedBytecode"]["object"].removeprefix("0x"))
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH: FROSTVerifier"):
        runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": SENDER}))
    assert not calls
    assert w3.eth.latest_nonce == w3.eth.pending_nonce == 0
    assert not load(tmp_path)["runs"]
    assert not load(tmp_path)["addresses"]


def test_existing_address_has_observation_provenance_and_remains_pinned(tmp_path, w3, monkeypatch):
    w3.eth.codes[SENDER] = CODE
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": SENDER})) == 0
    state = load(tmp_path)
    item = state["addresses"]["FROSTVerifier"]
    assert item == dict(address=SENDER, runtime_hash=Web3.to_hex(Web3.keccak(CODE)),
        provenance="observed_existing", block_hash=Web3.to_hex(block_hash(100)), block_number=100,
        creation_proof=fake_creation_proof(w3, SENDER, "FROSTVerifier", None, 100))
    assert not state["transactions"]
    w3.eth.block_number += 1
    assert runner.main(cli(tmp_path)) == 0
    assert load(tmp_path)["addresses"]["FROSTVerifier"] == item
    w3.eth.blocks[100] = block_hash(999)
    with pytest.raises(ValueError, match="DEPLOYMENT_EXISTING_ANCHOR_REORG"):
        runner.main(cli(tmp_path))


def test_existing_address_requires_code_at_confirmation_depth_before_forge(tmp_path, w3, monkeypatch):
    w3.eth.codes[SENDER] = CODE
    w3.eth.historical_codes[(SENDER, 99)] = b""
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH: FROSTVerifier"):
        runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": SENDER}, confirmations=2))
    assert not calls and not load(tmp_path)["runs"]


def test_existing_anchor_rechecks_historical_code_and_rejects_a_head_behind_it(w3):
    w3.eth.codes[SENDER] = CODE
    addresses = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": SENDER}, 1)
    w3.eth.block_number = 99
    with pytest.raises(ValueError, match="DEPLOYMENT_EXISTING_ANCHOR_AHEAD"):
        runner.checked_reusable_addresses(w3, addresses, {}, 1)
    w3.eth.block_number = 102
    w3.eth.historical_codes[(SENDER, 100)] = b""
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH"):
        runner.checked_reusable_addresses(w3, addresses, {}, 1)


def test_raising_confirmations_does_not_treat_an_observation_as_creation(w3):
    w3.eth.codes[SENDER] = CODE
    addresses = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": SENDER}, 1)
    assert runner.checked_reusable_addresses(w3, addresses, {}, 10) == addresses
    w3.eth.historical_codes[(SENDER, 91)] = b""
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH"):
        runner.checked_reusable_addresses(w3, addresses, {}, 10)


def test_unsent_failed_preflight_and_dry_run_do_not_pin_reused_candidates(tmp_path, w3, monkeypatch):
    w3.eth.codes[SENDER] = CODE
    fake_forge(monkeypatch, lambda *args: result(1))
    assert runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": SENDER})) == 1
    state = load(tmp_path)
    assert state["addresses"] == state["parameters"] == {}
    assert "inflight_run" not in state
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, broadcast=False, parameters={"FROST_VERIFIER": SENDER})) == 0
    assert load(tmp_path)["addresses"] == load(tmp_path)["parameters"] == {}


def test_legacy_v2_unsent_resume_restores_addresses_before_allowing_repair(tmp_path, w3, monkeypatch):
    first, replacement = [Web3.to_checksum_address("0x" + byte * 20) for byte in ("11", "22")]
    w3.eth.codes[first] = w3.eth.codes[replacement] = CODE
    state, run_id = run_state(success=None)
    state["parameters"] = {"FROST_VERIFIER": first}
    state["runs"][run_id]["parameters_hash"] = runner.digest(state["parameters"])
    assert "previous_addresses" not in state["runs"][run_id]
    runner.save(tmp_path / "manifest.json", state)

    @contextmanager
    def wallet(environment, key):
        yield

    monkeypatch.setattr(runner, "resume_wallet", wallet)
    fake_forge(monkeypatch, lambda *args: result(1))
    assert runner.main(cli(tmp_path, resume=True, parameters={"FROST_VERIFIER": first})) == 1
    recovered = load(tmp_path)
    assert recovered["parameters"] == recovered["addresses"] == {}
    assert "inflight_run" not in recovered
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": replacement})) == 0
    assert load(tmp_path)["addresses"]["FROSTVerifier"]["address"] == replacement


def test_legacy_invalid_runtime_unsent_run_reports_failure_then_allows_corrected_retry(tmp_path, w3, monkeypatch):
    invalid, replacement = [Web3.to_checksum_address("0x" + byte * 20) for byte in ("11", "22")]
    w3.eth.codes[invalid], w3.eth.codes[replacement] = b"\x00", CODE
    state, run_id = run_state(success=None)
    state["parameters"] = {"FROST_VERIFIER": invalid}
    state["runs"][run_id]["parameters_hash"] = runner.digest(state["parameters"])
    runner.save(tmp_path / "manifest.json", state)
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH"):
        runner.main(cli(tmp_path, resume=True, parameters={"FROST_VERIFIER": invalid}))
    recovered = load(tmp_path)
    assert not calls and w3.eth.latest_nonce == w3.eth.pending_nonce == 0
    assert recovered["parameters"] == recovered["addresses"] == {}
    assert "inflight_run" not in recovered
    assert recovered["runs"][run_id]["execution_success"] is False
    assert not recovered["runs"][run_id]["confirmed"]
    assert runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": replacement})) == 0
    assert load(tmp_path)["addresses"]["FROSTVerifier"]["address"] == replacement


def test_supplied_immutable_dependency_mismatch_fails_before_forge(tmp_path, w3, monkeypatch):
    coordinator, actual_verifier, selected_verifier = [Web3.to_checksum_address("0x" + byte * 20) for byte in ("11", "22", "33")]
    for target in (coordinator, actual_verifier, selected_verifier):
        w3.eth.codes[target] = CODE
    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=SimpleNamespace(
        defaultVerifier=lambda: SimpleNamespace(call=lambda: actual_verifier),
        dkgRegistry=lambda: SimpleNamespace(call=lambda: "0x" + "00" * 20)))
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="DEPLOYMENT_DEPENDENCY_MISMATCH: FROSTCoordinator.defaultVerifier"):
        runner.main(cli(tmp_path, parameters={"FROST_COORDINATOR": coordinator, "FROST_VERIFIER": selected_verifier}))
    assert not calls and not load(tmp_path)["addresses"]


def test_omitted_dependency_is_discovered_and_its_runtime_is_checked(w3):
    coordinator, verifier = [Web3.to_checksum_address("0x" + byte * 20) for byte in ("11", "22")]
    w3.eth.codes[coordinator] = w3.eth.codes[verifier] = CODE
    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=SimpleNamespace(
        defaultVerifier=lambda: SimpleNamespace(call=lambda: verifier),
        dkgRegistry=lambda: SimpleNamespace(call=lambda: "0x" + "00" * 20)))
    addresses = runner.checked_reusable_addresses(w3, {}, {"FROST_COORDINATOR": coordinator}, 1)
    assert addresses["FROSTVerifier"]["address"] == verifier
    assert set(addresses) == {"FROSTCoordinator", "FROSTVerifier"}
    w3.eth.codes[verifier] = b"\x00"
    with pytest.raises(ValueError, match="DEPLOYED_CODE_MISMATCH: FROSTVerifier"):
        runner.checked_reusable_addresses(w3, {}, {"FROST_COORDINATOR": coordinator}, 1)


def test_receipts_require_depth_before_addresses_and_stage_confirmation(tmp_path, w3):
    state, run_id = run_state()
    transaction = entries()[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction)
    assert runner.reconcile(w3, state, 3) == [transaction["hash"]]
    assert not state["addresses"]
    assert not state["stages"]["base"]["confirmed"]
    w3.eth.block_number = 102
    assert runner.reconcile(w3, state, 3) == []
    assert state["stages"]["base"]["confirmed"]
    assert "inflight_run" not in state
    deployed = state["addresses"]["FROSTVerifier"]
    assert deployed["address"] == transaction["contractAddress"]
    assert deployed["tx_hash"] == transaction["hash"]
    assert state["transactions"][transaction["hash"]]["sizes"]["initcode_bytes"] == 10
    runner.validate_manifest(state)


@pytest.mark.parametrize("reorg", ["canonical", "disappeared", "replacement"])
def test_confirmed_receipt_reorg_is_not_reinterpreted_as_pending(tmp_path, w3, reorg):
    state, run_id = run_state()
    transaction = entries()[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction)
    runner.reconcile(w3, state, 1)
    if reorg == "canonical":
        w3.eth.blocks[100] = block_hash(999)
    elif reorg == "disappeared":
        del w3.eth.receipts[transaction["hash"]]
    else:
        w3.eth.receipts[transaction["hash"]]["blockHash"] = block_hash(999)
        w3.eth.blocks[100] = block_hash(999)
    with pytest.raises(ValueError, match="DEPLOYMENT_RECEIPT_REORG"):
        runner.reconcile(w3, state, 1)


def test_reverted_receipt_waits_for_depth_and_never_confirms_run(tmp_path, w3):
    state, run_id = run_state()
    transaction = entries()[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction, status=0)
    assert runner.reconcile(w3, state, 2) == [transaction["hash"]]
    w3.eth.block_number += 1
    assert runner.reconcile(w3, state, 2) == []
    assert state["transactions"][transaction["hash"]]["status"] == "REVERTED"
    assert not state["addresses"]
    assert not state["stages"]["base"]["confirmed"]
    assert state["inflight_run"] == run_id


def test_create_receipt_address_is_bound_to_sender_and_nonce(tmp_path, w3):
    state, run_id = run_state()
    transaction = entries()[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction)
    # Matching runtime elsewhere cannot substitute for this transaction's CREATE.
    w3.eth.codes[SENDER] = CODE
    w3.eth.receipts[transaction["hash"]]["contractAddress"] = SENDER
    with pytest.raises(ValueError, match="CREATE_RECEIPT_ADDRESS_MISMATCH"):
        runner.reconcile(w3, state, 1)
    assert not state["addresses"]


def test_dkg_result_never_survives_a_lost_result_or_hidden_rpc_failure(tmp_path, w3):
    state, run_id = run_state()
    state["parameters"]["PROJECT_DKG_ID"] = "0x" + "12" * 32
    transaction = entries(("DKGCoordinator",))[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction)
    response = ContractLogicError("execution reverted: incomplete DKG")

    def read():
        if isinstance(response, Exception):
            raise response
        return response

    contract = SimpleNamespace(functions=SimpleNamespace(getCompletedResult=lambda session:
                               SimpleNamespace(call=read)))
    w3.eth.contract = lambda **kwargs: contract
    runner.reconcile(w3, state, 1)
    assert "project_dkg_result" not in state
    response = (b"", bytes.fromhex("34" * 32), b"", Web3.keccak(text="EVM_AUTH"))
    runner.reconcile(w3, state, 1)
    assert state["project_dkg_result"] == "0x" + "34" * 32
    response = ConnectionError("offline RPC failure")
    with pytest.raises(ConnectionError):
        runner.reconcile(w3, state, 1)
    response = ContractLogicError("execution reverted: result lost")
    with pytest.raises(ValueError, match="PROJECT_DKG_RESULT_UNAVAILABLE"):
        runner.reconcile(w3, state, 1)


@pytest.mark.parametrize("change,error", [
    (lambda tx: tx["transaction"].update(chainId="0x1"), "BROADCAST_CHAIN_OR_SENDER"),
    (lambda tx: tx["transaction"].update({"from": "0x" + "22" * 20}), "BROADCAST_CHAIN_OR_SENDER"),
    (lambda tx: tx["transaction"].update(nonce="0x1"), "BROADCAST_NONCE_SEQUENCE"),
    (lambda tx: tx.update(transactionType="CREATE2"), "BROADCAST_TRANSACTION_TYPE"),
    (lambda tx: tx.update(contractName="UnreviewedContract"), "UNKNOWN_DEPLOYED_CONTRACT"),
])
def test_foreign_or_unsupported_journal_does_not_modify_manifest(tmp_path, w3, change, error):
    state, run_id = run_state()
    transaction = entries()[0]
    change(transaction)
    original = deepcopy(state)
    with pytest.raises(ValueError, match=error):
        import_entries(tmp_path, state, run_id, [transaction])
    assert state == original


def test_journal_chain_and_previous_stage_association_are_checked(tmp_path, w3):
    state, run_id = run_state()
    transaction = entries()[0]
    path = import_entries(tmp_path, state, run_id, [transaction])
    other_run = "22" * 16
    append_run(state, other_run, "factories")
    with pytest.raises(ValueError, match="BROADCAST_TRANSACTION_RUN_OR_INTENT"):
        runner.import_broadcast(path, state, other_run)
    write_journal(path, [transaction], chain=1)
    with pytest.raises(ValueError, match="BROADCAST_CHAIN_OR_SCHEMA"):
        runner.import_broadcast(path, state, run_id)


@pytest.mark.parametrize("field,value", [("input", "0xdeadbeef"), ("nonce", "0x4"), ("value", "0x1")])
def test_mined_transaction_must_match_journal_intent(tmp_path, w3, field, value):
    state, run_id = run_state()
    transaction = entries()[0]
    import_entries(tmp_path, state, run_id, [transaction])
    w3.eth.mine(transaction)
    w3.eth.transactions[transaction["hash"]][field] = value
    with pytest.raises(ValueError, match="DEPLOYMENT_TRANSACTION_INTENT"):
        runner.reconcile(w3, state, 1)
    assert not state["addresses"]


def test_resume_journal_cannot_change_unsent_calldata_or_discard_hashed_prefix(tmp_path, w3):
    state, run_id = run_state(success=False)
    transactions = entries(("FROSTVerifier", "FROSTAuthorizationDeployer"), sent=1)
    path = import_entries(tmp_path, state, run_id, transactions)
    changed = deepcopy(transactions)
    changed[1]["transaction"]["input"] = "0xdeadbeef"
    write_journal(path, changed)
    with pytest.raises(ValueError, match="BROADCAST_PLAN_CHANGED"):
        runner.import_broadcast(path, state, run_id)
    changed = deepcopy(transactions)
    changed[0]["hash"] = None
    write_journal(path, changed)
    with pytest.raises(ValueError, match="BROADCAST_SUBMITTED_PREFIX_CHANGED"):
        runner.import_broadcast(path, state, run_id)


@pytest.mark.parametrize("empty_journal", [False, True])
def test_missing_or_empty_journal_with_advanced_nonce_is_not_success(tmp_path, w3, monkeypatch, empty_journal):
    def execute(path, command, env):
        if empty_journal:
            write_journal(path, [])
        w3.eth.latest_nonce = w3.eth.pending_nonce = 1
        return result()
    fake_forge(monkeypatch, execute)
    assert runner.main(cli(tmp_path)) == 2
    state = load(tmp_path)
    assert not state["stages"]["base"]["confirmed"]
    assert state["inflight_run"] in state["runs"]
    with pytest.raises(ValueError, match="INCOMPLETE_BROADCAST_RUN"):
        runner.main(cli(tmp_path, stage="factories"))


def test_real_noop_is_confirmed_only_with_unchanged_mined_and_pending_nonces(tmp_path, w3, monkeypatch):
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path)) == 0
    state = load(tmp_path)
    run_id = state["stages"]["base"]["run_id"]
    assert state["stages"]["base"]["confirmed"]
    assert state["runs"][run_id]["no_transactions"]
    assert "inflight_run" not in state
    assert not state["transactions"]


def test_failed_preflight_rolls_back_new_parameters_and_allows_repair(tmp_path, w3, monkeypatch):
    calls = fake_forge(monkeypatch, lambda *args: result(1))
    assert runner.main(cli(tmp_path, parameters={"PROJECT_WALLET_1": "invalid wallet"})) == 1
    state = load(tmp_path)
    assert "inflight_run" not in state
    assert state["parameters"] == {}
    assert not state["stages"]["base"]["confirmed"]
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: result())
    assert runner.main(cli(tmp_path, parameters={"PROJECT_WALLET_1": SENDER})) == 0
    assert load(tmp_path)["parameters"]["PROJECT_WALLET_1"] == SENDER
    assert len(calls) == 1


def test_dry_run_does_not_pin_config_and_uses_an_isolated_journal(tmp_path, w3, monkeypatch):
    def execute(path, command, env):
        write_journal(path, [])
        return result()
    calls = fake_forge(monkeypatch, execute)
    assert runner.main(cli(tmp_path, broadcast=False, parameters={"PROJECT_WALLET_1": "dry candidate"})) == 0
    assert load(tmp_path)["parameters"] == {}
    assert runner.main(cli(tmp_path, parameters={"PROJECT_WALLET_1": SENDER})) == 0
    assert calls[0]["environment"]["FOUNDRY_BROADCAST"] != calls[1]["environment"]["FOUNDRY_BROADCAST"]
    assert len(load(tmp_path)["runs"]) == 1


def test_shared_run_latest_from_another_stage_is_never_imported(tmp_path, w3, monkeypatch):
    stale = tmp_path / "manifest-broadcast/DeployFederation.s.sol/31337/run-latest.json"
    write_journal(stale, entries(("FederationFactory",)))
    calls = fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path)) == 0
    assert not load(tmp_path)["transactions"]
    selected = Path(calls[0]["environment"]["FOUNDRY_BROADCAST"])
    assert runner.RUN_ID.fullmatch(selected.name)
    assert selected != stale.parents[2]


def test_partial_broadcast_keeps_confirmed_prefix_and_resumes_exact_run(tmp_path, w3, monkeypatch):
    names = ("FROSTVerifier", "FROSTAuthorizationDeployer")
    partial, complete = entries(names, sent=1), entries(names)

    def first(path, command, env):
        write_journal(path, partial)
        w3.eth.mine(partial[0])
        return result(1)

    calls = fake_forge(monkeypatch, first)
    assert runner.main(cli(tmp_path)) == 1
    state = load(tmp_path)
    run_id = state["inflight_run"]
    assert state["runs"][run_id]["planned_count"] == 2
    assert len(state["runs"][run_id]["transaction_hashes"]) == 1
    assert state["transactions"][partial[0]["hash"]]["status"] == "CONFIRMED"
    assert state["addresses"][names[0]]["address"] == partial[0]["contractAddress"]
    assert not state["stages"]["base"]["confirmed"]
    with pytest.raises(ValueError, match="INCOMPLETE_BROADCAST_RUN"):
        runner.main(cli(tmp_path, stage="factories"))
    with pytest.raises(ValueError, match="RESUME_PARAMETERS_CHANGED"):
        runner.main(cli(tmp_path, resume=True, parameters={"FROST_SESSION_ID": "2"}))
    assert len(calls) == 1

    @contextmanager
    def local_wallet(environment, key):
        assert key == bytes.fromhex(KEY[2:])
        yield

    monkeypatch.setattr(runner, "resume_wallet", local_wallet)

    def second(path, command, env):
        assert "--resume" in command
        assert env["FROST_VERIFIER"] == partial[0]["contractAddress"]
        assert Path(env["FOUNDRY_BROADCAST"]).name == run_id
        write_journal(path, complete)
        w3.eth.mine(complete[1])
        return result()

    second_calls = fake_forge(monkeypatch, second)
    assert runner.main(cli(tmp_path, resume=True)) == 0
    state = load(tmp_path)
    assert state["stages"]["base"]["confirmed"]
    assert state["stages"]["base"]["run_id"] == run_id
    assert "inflight_run" not in state
    assert len(state["addresses"]) == 2
    assert calls[0]["environment"]["FOUNDRY_CACHE_PATH"] == second_calls[0]["environment"]["FOUNDRY_CACHE_PATH"]


def test_interruption_before_receipt_import_recovers_journal_and_addresses(tmp_path, w3, monkeypatch):
    transaction = entries()[0]

    def interrupted(path, command, env):
        write_journal(path, [transaction])
        w3.eth.mine(transaction)
        raise KeyboardInterrupt()

    fake_forge(monkeypatch, interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.main(cli(tmp_path))
    original = load(tmp_path)
    assert original["inflight_run"]
    assert not original["transactions"]

    @contextmanager
    def wallet(environment, key):
        yield
    monkeypatch.setattr(runner, "resume_wallet", wallet)
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, resume=True)) == 0
    recovered = load(tmp_path)
    assert recovered["stages"]["base"]["confirmed"]
    assert recovered["addresses"]["FROSTVerifier"]["tx_hash"] == transaction["hash"]


def test_delayed_confirmations_refresh_old_stage_before_starting_next(tmp_path, w3, monkeypatch):
    transaction = entries()[0]

    def first(path, command, env):
        write_journal(path, [transaction])
        w3.eth.mine(transaction)
        return result()

    fake_forge(monkeypatch, first)
    assert runner.main(cli(tmp_path, confirmations=2)) == 2
    with pytest.raises(ValueError, match="PENDING_DEPLOYMENT_TRANSACTIONS"):
        runner.main(cli(tmp_path, stage="factories", confirmations=2))
    w3.eth.block_number += 1
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, stage="factories", confirmations=2)) == 0
    state = load(tmp_path)
    assert state["stages"]["base"]["confirmed"]
    assert state["stages"]["factories"]["confirmed"]
    assert state["stages"]["base"]["run_id"] != state["stages"]["factories"]["run_id"]


def test_sufficiently_final_reverted_run_allows_same_stage_fresh_retry_only(tmp_path, w3, monkeypatch):
    transaction = entries()[0]

    def first(path, command, env):
        write_journal(path, [transaction])
        w3.eth.mine(transaction, status=0)
        return result(1)

    fake_forge(monkeypatch, first)
    assert runner.main(cli(tmp_path)) == 1
    prior_run = load(tmp_path)["inflight_run"]
    with pytest.raises(ValueError, match="INCOMPLETE_BROADCAST_RUN"):
        runner.main(cli(tmp_path, stage="factories"))

    def retry(path, command, env):
        assert "--resume" not in command
        replacement = entries(start_nonce=1)[0]
        write_journal(path, [replacement])
        w3.eth.mine(replacement)
        return result()

    fake_forge(monkeypatch, retry)
    assert runner.main(cli(tmp_path)) == 0
    state = load(tmp_path)
    assert state["stages"]["base"]["run_id"] != prior_run
    assert state["stages"]["base"]["confirmed"]
    assert state["transactions"][transaction["hash"]]["status"] == "REVERTED"


def test_prepare_payer_is_read_only_without_signing_key(tmp_path, w3, monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY")
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, stage="prepare_payer", broadcast=False)) == 0
    assert not load(tmp_path)["runs"]
    with pytest.raises(ValueError, match="PREPARE_PAYER_IS_READ_ONLY"):
        runner.main(cli(tmp_path, stage="prepare_payer"))
    with pytest.raises(ValueError, match="EXPLICIT_PRIVATE_KEY_ENV_REQUIRED"):
        runner.main(cli(tmp_path))


def test_manifest_sender_and_confirmation_policy_cannot_silently_change(tmp_path, w3, monkeypatch):
    fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(cli(tmp_path, confirmations=2)) == 0
    with pytest.raises(ValueError, match="CONFIRMATIONS_CANNOT_DECREASE"):
        runner.main(cli(tmp_path, confirmations=1))
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "00" * 31 + "02")
    with pytest.raises(ValueError, match="MANIFEST_SENDER_MISMATCH"):
        runner.main(cli(tmp_path, confirmations=2))


def test_shell_graph_parameters_are_scrubbed_and_credentials_are_redacted(tmp_path, w3, monkeypatch, capsys):
    monkeypatch.setenv("FROST_SESSION_ID", "999")
    monkeypatch.setenv("DEPLOYMENT_FEE_AMOUNT", "999")
    calls = fake_forge(monkeypatch, lambda *args: result(output=KEY + " " + RPC + "\n"))
    assert runner.main(cli(tmp_path)) == 0
    output = capsys.readouterr().out
    assert KEY not in output and RPC not in output
    assert output.count("[REDACTED]") == 2
    env = calls[0]["environment"]
    assert "FROST_SESSION_ID" not in env and "DEPLOYMENT_FEE_AMOUNT" not in env
    manifest = (tmp_path / "manifest.json").read_text()
    assert KEY not in manifest and RPC not in manifest and "PRIVATE_KEY" not in manifest
    assert env["FOUNDRY_CACHE_PATH"] != env["FOUNDRY_BROADCAST"]
    assert (tmp_path / "manifest-private").stat().st_mode & 0o777 == 0o700


def test_resume_wallet_is_encrypted_local_ephemeral_and_has_no_key_argument():
    environment = {}
    key = bytes.fromhex(KEY[2:])
    with runner.resume_wallet(environment, key):
        path = Path(environment["ETH_KEYSTORE"])
        encoded = path.read_text()
        assert KEY not in encoded
        assert path.stat().st_mode & 0o777 == 0o600
        password_path = Path(environment["ETH_PASSWORD"])
        assert password_path.stat().st_mode & 0o777 == 0o600
        assert Account.decrypt(json.loads(encoded), password_path.read_text()) == key
    assert not path.exists()
    assert not password_path.exists()


@pytest.fixture
def origin_rpc(w3, monkeypatch):
    """Exercise the real origin verifier with local synthetic creation history."""
    from signer_agent import bitcoin_trust, contract_provenance

    transaction = entries()[0]
    compiled = dict(metadata={}, abi=[],
        bytecode=dict(object=transaction["transaction"]["input"]),
        deployedBytecode=dict(object=CODE.hex(), immutableReferences={}))
    for module in (runner, bitcoin_trust, contract_provenance):
        monkeypatch.setattr(module, "artifact", lambda name: deepcopy(compiled))
    monkeypatch.setattr(runner, "prove_creation", contract_provenance.prove_creation)
    w3.eth.mine(transaction, number=80)
    return w3, transaction


def test_existing_component_requires_and_records_real_constructor_proof(origin_rpc):
    w3, transaction = origin_rpc
    target = transaction["contractAddress"]
    selected = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": target}, 1)
    proof = selected["FROSTVerifier"]["creation_proof"]
    assert proof["method"] == "direct"
    assert proof["block_number"] == "80"
    assert proof["transaction_hash"] == HexBytes(transaction["hash"]).hex()
    assert proof["sender"] == SENDER
    runner.validate_addresses(selected, {})
    w3._satoshify_creation_proofs.clear()
    assert runner.checked_reusable_addresses(w3, selected, {}, 1) == selected


def test_matching_runtime_with_unreviewed_constructor_stops_before_forge(tmp_path, origin_rpc, monkeypatch):
    w3, transaction = origin_rpc
    w3.eth.transactions[transaction["hash"]]["input"] += "00"
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        runner.main(cli(tmp_path, parameters={"FROST_VERIFIER": transaction["contractAddress"]}))
    assert not calls
    assert not load(tmp_path)["addresses"]
    assert not load(tmp_path)["runs"]


def test_journaled_create_cannot_skip_constructor_verification(origin_rpc):
    w3, transaction = origin_rpc
    w3.eth.transactions[transaction["hash"]]["input"] += "00"
    state = runner.new_state(CHAIN, 1)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        runner.add_address(w3, state, "FROSTVerifier", transaction["contractAddress"], transaction["hash"])
    assert not state["addresses"]


def test_copied_manifest_creation_claim_is_recomputed(origin_rpc):
    w3, transaction = origin_rpc
    selected = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": transaction["contractAddress"]}, 1)
    selected["FROSTVerifier"]["creation_proof"]["sender"] = Web3.to_checksum_address("0x" + "12" * 20)
    w3._satoshify_creation_proofs.clear()
    with pytest.raises(ValueError, match="DEPLOYMENT_CREATION_PROOF_MISMATCH"):
        runner.checked_reusable_addresses(w3, selected, {}, 1)


def test_creation_anchor_reorg_is_rejected_even_when_observation_is_unchanged(origin_rpc):
    w3, transaction = origin_rpc
    selected = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": transaction["contractAddress"]}, 1)
    w3.eth.blocks[80] = block_hash(999)
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        runner.checked_reusable_addresses(w3, selected, {}, 1)


def test_older_observation_record_is_upgraded_only_after_constructor_verification(origin_rpc):
    w3, transaction = origin_rpc
    selected = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": transaction["contractAddress"]}, 1)
    old = deepcopy(selected)
    del old["FROSTVerifier"]["creation_proof"]
    assert runner.checked_reusable_addresses(w3, old, {}, 1) == selected
    assert "creation_proof" not in old["FROSTVerifier"]
    w3._satoshify_creation_proofs.clear()
    w3.eth.transactions[transaction["hash"]]["input"] += "00"
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        runner.checked_reusable_addresses(w3, old, {}, 1)


def test_automatically_discovered_dependency_also_requires_canonical_creation(origin_rpc):
    w3, verifier_tx = origin_rpc
    coordinator_tx = entries(("FROSTCoordinator",), start_nonce=1)[0]
    w3.eth.mine(coordinator_tx, number=81)
    w3.eth.transactions[verifier_tx["hash"]]["input"] += "00"
    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=SimpleNamespace(
        defaultVerifier=lambda: SimpleNamespace(call=lambda: verifier_tx["contractAddress"]),
        dkgRegistry=lambda: SimpleNamespace(call=lambda: "0x" + "00" * 20)))
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        runner.checked_reusable_addresses(w3, {}, {"FROST_COORDINATOR": coordinator_tx["contractAddress"]}, 1)


def test_creation_proof_cannot_persist_unknown_fields(origin_rpc):
    w3, transaction = origin_rpc
    selected = runner.checked_reusable_addresses(w3, {}, {"FROST_VERIFIER": transaction["contractAddress"]}, 1)
    selected["FROSTVerifier"]["creation_proof"]["unexpected"] = "not allowed"
    with pytest.raises(ValueError, match="DEPLOYMENT_SCHEMA_FIELDS"):
        runner.validate_addresses(selected, {})


@pytest.mark.parametrize("stage,creator,retained,completed,rejected", [
    ("base", SENDER, True, False, False),
    ("base", SENDER, True, True, True),
    ("base", Web3.to_checksum_address("0x" + "12" * 20), True, False, True),
    ("factories", SENDER, True, False, True),
    ("fee_router", SENDER, True, False, True),
    ("fees", SENDER, False, True, False),
])
def test_coordinator_admin_is_checked_for_original_constructor_sender(w3, stage, creator, retained, completed, rejected):
    checked = []

    def has_role(role, member):
        checked.append((role, member))
        return SimpleNamespace(call=lambda: retained)

    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=SimpleNamespace(hasRole=has_role))
    addresses = {"FROSTCoordinator": dict(address=SENDER, creation_proof=dict(sender=creator))}
    if rejected:
        with pytest.raises(ValueError, match="COORDINATOR_ORIGINAL_ADMIN_RETAINED"):
            runner.check_coordinator_admin(w3, addresses, stage, SENDER, completed=completed)
    else:
        runner.check_coordinator_admin(w3, addresses, stage, SENDER, completed=completed)
    assert checked == [(Web3.keccak(text="ADMIN_ROLE"), creator)]

# Ethereum mainnet policy tests use a synthetic chain-id-1 RPC; no public RPC or
# real account is contacted. The finalized tag advances independently of depth.
def mainnet_rpc(w3):
    w3.eth.chain_id = 1
    w3.eth.finalized_number = 80
    original = w3.eth.get_block

    def get_block(number, full_transactions=False):
        if number == "latest":
            number = w3.eth.block_number
        elif number == "finalized":
            number = w3.eth.finalized_number
        return AttributeDict(original(number, full_transactions=full_transactions))

    w3.eth.get_block = get_block
    return w3


def mainnet_parameters():
    return dict(INTEGRATION_FEE_EXEMPTION="false", DEPLOYMENT_FEE_AMOUNT="123",
        PROJECT_REWARD_TOKEN=SENDER, PROJECT_REWARD_TOKEN_CODE_HASH=Web3.to_hex(Web3.keccak(CODE)),
        PROJECT_REWARD_TOKEN_DECIMALS="6", WRAP_STAKER_BPS="0", DEPLOY_STAKER_BPS="0")


@pytest.mark.parametrize("change,error", [
    ({"INTEGRATION_FEE_EXEMPTION": "true"}, "EXEMPTION_FORBIDDEN"),
    ({"DEPLOYMENT_FEE_AMOUNT": "0"}, "PAID_DEPLOYMENT_REQUIRED"),
    ({"PROJECT_REWARD_TOKEN": "0x" + "00" * 20}, "REWARD_TOKEN"),
    ({"PROJECT_REWARD_TOKEN_CODE_HASH": "0x" + "00" * 32}, "REWARD_HASH"),
    ({"PROJECT_REWARD_TOKEN_DECIMALS": "19"}, "REWARD_DECIMALS"),
    ({"WRAP_STAKER_BPS": "1"}, "PROJECT_TREASURY_ONLY"),
    ({"DEPLOY_STAKER_BPS": "1"}, "PROJECT_TREASURY_ONLY"),
])
def test_ethereum_mainnet_financial_policy_rejects_unsafe_inputs(change, error):
    parameters = dict(mainnet_parameters(), **change)
    with pytest.raises(ValueError, match=error):
        runner.ethereum_mainnet_parameters(1, 12, "fees", parameters)


def test_ethereum_mainnet_policy_requires_depth_and_explicit_pins():
    runner.ethereum_mainnet_parameters(1, 12, "base", {})
    runner.ethereum_mainnet_parameters(1, 12, "fees", mainnet_parameters())
    with pytest.raises(ValueError, match="CONFIRMATIONS"):
        runner.ethereum_mainnet_parameters(1, 11, "base", {})
    with pytest.raises(ValueError, match="EXEMPTION_FORBIDDEN"):
        runner.ethereum_mainnet_parameters(1, 12, "exempt_all", {})
    for parameter in ("PROJECT_REWARD_TOKEN_CODE_HASH", "PROJECT_REWARD_TOKEN_DECIMALS", "INTEGRATION_FEE_EXEMPTION"):
        parameters = mainnet_parameters()
        parameters.pop(parameter)
        with pytest.raises(ValueError, match="ASSET_POLICY_REQUIRED"):
            runner.ethereum_mainnet_parameters(1, 12, "fees", parameters)


def test_ethereum_mainnet_asset_is_checked_at_finalized_view(w3, monkeypatch):
    mainnet_rpc(w3)
    w3.eth.codes[SENDER] = CODE
    monkeypatch.setattr(runner, "artifact", lambda name: {"deployedBytecode": {"object": "6000"}})
    read_blocks = []
    def decimals_call(*, block_identifier):
        read_blocks.append(block_identifier)
        return 6
    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=SimpleNamespace(
        decimals=lambda: SimpleNamespace(call=decimals_call)))
    runner.checked_reusable_addresses(w3, {}, mainnet_parameters(), 12)
    assert read_blocks == [80]
    w3.eth.historical_codes[(SENDER, 80)] = b"different at finalized"
    with pytest.raises(ValueError, match="REWARD_HASH"):
        runner.checked_reusable_addresses(w3, {}, mainnet_parameters(), 12)


def test_ethereum_mainnet_known_mock_rejected_even_with_matching_hash(w3):
    mainnet_rpc(w3)
    w3.eth.codes[SENDER] = CODE  # Fixture artifact intentionally has exactly this code.
    with pytest.raises(ValueError, match="TEST_TOKEN_FORBIDDEN"):
        runner.check_mainnet_asset(w3, mainnet_parameters(), 80)


def test_ethereum_mainnet_receipts_wait_for_consensus_finality(w3, tmp_path):
    mainnet_rpc(w3)
    state, run_id = run_state()
    state["chain_id"], state["confirmations"] = 1, 12
    transactions = entries()
    transactions[0]["transaction"]["chainId"] = "0x1"
    journal = tmp_path / "broadcast.json"
    write_journal(journal, transactions, chain=1)
    runner.import_broadcast(journal, state, run_id)
    w3.eth.mine(transactions[0], number=85)
    assert runner.reconcile(w3, state, 12) == [transactions[0]["hash"]]
    assert not state["addresses"]
    w3.eth.finalized_number = 89
    assert runner.reconcile(w3, state, 12) == []
    assert state["stages"]["base"]["confirmed"] is True
    assert "FROSTVerifier" in state["addresses"]


def test_ethereum_mainnet_missing_finalized_tag_fails_before_forge(w3, monkeypatch, tmp_path):
    mainnet_rpc(w3)
    original = w3.eth.get_block
    w3.eth.get_block = lambda number, **kwargs: (_ for _ in ()).throw(ValueError("provider private error")) if number == "finalized" else original(number, **kwargs)
    args = cli(tmp_path, confirmations=12)
    config = json.loads((tmp_path / "config.json").read_text())
    config["chain_id"] = "1"
    (tmp_path / "config.json").write_text(json.dumps(config))
    calls = fake_forge(monkeypatch, lambda *args: result())
    with pytest.raises(ValueError, match="ETHEREUM_FINALITY_UNAVAILABLE"):
        runner.main(args)
    assert calls == []


def test_ethereum_mainnet_runner_passes_actual_fee_and_osaka_limits(w3, monkeypatch, tmp_path):
    mainnet_rpc(w3)
    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "50000000000")
    monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "1000000000")
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "16000000")
    @contextmanager
    def proxy(rpc, sender, fees, gas):
        assert rpc is w3 and sender == SENDER and gas == 16000000
        assert fees == dict(maxFeePerGas=50000000000, maxPriorityFeePerGas=1000000000)
        yield "http://127.0.0.1:12345/private-local-route"
    monkeypatch.setattr(runner, "budget_rpc", proxy)
    args = cli(tmp_path, broadcast=False, confirmations=12)
    config = json.loads((tmp_path / "config.json").read_text())
    config["chain_id"] = "1"
    (tmp_path / "config.json").write_text(json.dumps(config))
    calls = fake_forge(monkeypatch, lambda *args: result())
    assert runner.main(args) == 0
    command = calls[0]["command"]
    assert command[command.index("--with-gas-price") + 1] == "50000000000"
    assert command[command.index("--priority-gas-price") + 1] == "1000000000"
    assert command[command.index("--rpc-url") + 1] == "http://127.0.0.1:12345/private-local-route"
    assert "--enable-tx-gas-limit" not in command and "--gas-limit" not in command
    assert command[command.index("--gas-estimate-multiplier") + 1] == "130"


@pytest.mark.parametrize("stage,payers,session_done,error", [
    ("fees", [True, False], True, "PAYERS_NOT_FINALIZED"),
    ("execute_payer", [False, False], False, "SESSION_NOT_FINALIZED"),
    ("execute_payer", [False, False], True, None),
    ("fees", [True, True], True, None),
])
def test_ethereum_mainnet_stage_prerequisites_use_only_finalized_state(w3, stage, payers, session_done, error):
    mainnet_rpc(w3)
    names = ["DKGCoordinator", "FederationFactory", "ConfederationFactory", "CustodianDAOFeeRouter", "DKGAuthorization", "FROSTCoordinator"]
    addresses = {name: dict(address=Web3.to_checksum_address("0x" + f"{index+1:040x}")) for index, name in enumerate(names)}
    epoch, result_hash, operation = (Web3.keccak(text=text) for text in ("epoch", "result", "operation"))
    parameters = dict(mainnet_parameters(), PROJECT_DKG_ID=Web3.to_hex(epoch),
        PAYER_OPERATION_HASH=Web3.to_hex(operation), PAYER_OPERATION_NONCE="0", FROST_SESSION_ID="1")
    blocks = []
    class Functions:
        def __getattr__(self, getter):
            def function(*arguments):
                def call(*, block_identifier):
                    blocks.append(block_identifier)
                    if getter == "getCompletedResult":
                        return (None, result_hash, None, Web3.keccak(text="EVM_AUTH"), None, None, None)
                    if getter == "authorizedPayers":
                        return payers[[addresses[name]["address"] for name in names[1:3]].index(arguments[0])]
                    return {"operationNonce": 0, "getSetPayerOperationHash": operation,
                        "isOperationApproved": False, "getAuthorizationResult":
                        (None, operation, session_done, None, addresses["CustodianDAOFeeRouter"]["address"]),
                        "sessionDKGId": epoch, "sessionDKGResult": result_hash}[getter]
                return SimpleNamespace(call=call)
            return function
    w3.eth.contract = lambda **kwargs: SimpleNamespace(functions=Functions())
    if error:
        with pytest.raises(ValueError, match=error):
            runner.check_mainnet_stage_ready(w3, stage, parameters, addresses, 12)
    else:
        runner.check_mainnet_stage_ready(w3, stage, parameters, addresses, 12)
    assert blocks and set(blocks) == {80}
