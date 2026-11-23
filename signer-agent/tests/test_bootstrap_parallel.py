"""Independent sends never open the dependency barrier before finality."""
import json
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction
from hexbytes import HexBytes
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TransactionNotFound

from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import observe, parallel_groups, validate_targets
from signer_agent.operations import Journal


def steps(count=2):
    return [dict(id=f"leaf{i}", kind="deploy", artifact="Fixture", parallel_group="leaves") for i in range(count)]


@pytest.mark.parametrize("bad,error", [
    (steps(9), "BOOTSTRAP_PARALLEL_STEP"),
    ([steps()[0], dict(id="gap", kind="read"), steps()[1]], "BOOTSTRAP_PARALLEL_GROUP"),
    ([dict(steps()[0], kind="wait")], "BOOTSTRAP_PARALLEL_STEP"),
    ([dict(steps()[0], outputs={"key": "value"})], "BOOTSTRAP_PARALLEL_STEP"),
    ([dict(steps()[0], verify_token_transfer={"amount": "1"})], "BOOTSTRAP_PARALLEL_STEP"),
    ([steps()[0], dict(steps()[1], args=[{"$json_hex": ["$leaf0"]}])], "BOOTSTRAP_PARALLEL_DEPENDENCY"),
    ([steps()[0], dict(steps()[1], kind="call", contract="leaf0")], "BOOTSTRAP_PARALLEL_DEPENDENCY"),
    ([steps()[0], steps()[0]], "BOOTSTRAP_STEP_ID"),
    ([dict(steps()[0], parallel_group="../bad")], "BOOTSTRAP_PARALLEL_GROUP"),
])
def test_group_metadata_fails_closed(bad, error):
    with pytest.raises(ValueError, match="^" + error + "$"):
        parallel_groups(bad)


def test_eight_independent_steps_are_allowed():
    assert len(parallel_groups(steps(8))["leaves"]) == 8


class FakeEth:
    chain_id = 31337
    block_number = 4
    def __init__(self):
        self.transactions, self.receipts, self.sent = {}, {}, []
        self.automine, self.fail_nonce = True, None
    def get_transaction_count(self, address, block):
        return len(self.transactions)
    def get_transaction(self, tx):
        if tx not in self.transactions:
            raise TransactionNotFound(tx)
        return self.transactions[tx]
    def get_transaction_receipt(self, tx):
        if tx not in self.receipts:
            raise TransactionNotFound(tx)
        return self.receipts[tx]
    def wait_for_transaction_receipt(self, tx, **kw):
        return self.get_transaction_receipt(tx)
    def get_block(self, number):
        number = self.block_number if number == "latest" else number
        return AttributeDict(dict(number=number, hash=HexBytes(int(number).to_bytes(32, "big"))))
    def send_raw_transaction(self, raw):
        tx = Web3.keccak(raw).hex()
        decoded = Transaction.from_bytes(raw).as_dict()
        assert decoded["nonce"] == len(self.transactions), "nonce allocation must be serialized"
        self.sent.append(raw)
        self.transactions[tx] = decoded
        if self.automine:
            self.mine(tx)
        return HexBytes(tx)
    def mine(self, tx):
        nonce = self.transactions[tx]["nonce"]
        self.receipts[tx] = AttributeDict(dict(status=0 if nonce == self.fail_nonce else 1,
            transactionHash=HexBytes(tx), blockNumber=nonce + 1, blockHash=self.get_block(nonce + 1).hash))


def fixture(tmp_path, count=2):
    b = Bootstrap.__new__(Bootstrap)
    b.prefix = "parallel-test"
    b.profile = dict(steps=steps(count), evm_confirmations="12")
    b.journal = Journal(tmp_path / "steps.sqlite")
    b.journal.put(b.prefix, dict(profile=b.profile))
    b.w3 = SimpleNamespace(eth=FakeEth())
    b.contracts, b.values, b.prepared, b.finished = {}, {}, [], []
    b.check_external_components = lambda: None
    b.manifest = lambda: dict(finished=list(b.finished))
    def record_deployment(step, result, receipt):
        result["contract"] = {"artifact": step["artifact"]}
    b.record_deployment = record_deployment
    b.outputs = lambda step, result: b.finished.append(step["id"])
    b.verify_checks = lambda step, receipt=None: None
    def build(step, account, nonce):
        b.prepared.append(step["id"])
        return dict(chainId=31337, nonce=nonce, gasPrice=1, gas=21000, value=0,
                    to="0x" + "11" * 20)
    b.build_transaction = build
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(dict(private_key="0x" + "00" * 31 + "01")))
    identity.chmod(0o600)
    return b, str(identity)


def test_all_sends_before_common_finality_then_dependent_step(tmp_path):
    b, identity = fixture(tmp_path, 3)
    b.profile["steps"].append(dict(id="dependent", kind="deploy", artifact="Fixture", args=["$leaf0"]))
    result = b.apply(identity)
    assert result == dict(status="CONFIRMING", step="leaf0", parallel_group="leaves", pending=["leaf0", "leaf1", "leaf2"])
    assert len(b.w3.eth.sent) == 3
    assert b.prepared == ["leaf0", "leaf1", "leaf2"]
    assert b.finished == []
    assert all(b.journal.get(b.prefix + ":" + s["id"])["status"] == "CONFIRMING" for s in b.profile["steps"][:3])
    assert b.apply(identity)["status"] == "CONFIRMING"
    assert len(b.w3.eth.sent) == 3
    b.w3.eth.block_number = 100
    assert b.apply(identity)["status"] == "VERIFIED"
    assert len(b.w3.eth.sent) == 4
    assert b.prepared == ["leaf0", "leaf1", "leaf2", "dependent"]
    assert b.apply(identity)["status"] == "VERIFIED"
    assert len(b.w3.eth.sent) == 4


def test_all_unsigned_preflight_precedes_first_signature_or_send(tmp_path):
    b, identity = fixture(tmp_path, 3)
    original = b.build_transaction
    def build(step, *args):
        if step["id"] == "leaf2":
            raise ValueError("INVALID_CONSTRUCTOR")
        return original(step, *args)
    b.build_transaction = build
    with pytest.raises(ValueError, match="INVALID_CONSTRUCTOR"):
        b.apply(identity)
    assert b.w3.eth.sent == []
    assert all(not row["result"] for row in b.journal.list())


def test_global_nonce_reservation_recovers_crash_before_step_journal(tmp_path, monkeypatch):
    b, identity = fixture(tmp_path)
    original = Journal.update
    crashed = False
    def update(journal, key, status, result):
        nonlocal crashed
        if journal.path == b.journal.path and status == "SIGNED" and not crashed:
            crashed = True
            raise SystemExit("fixture crash after global fsync")
        return original(journal, key, status, result)
    monkeypatch.setattr(Journal, "update", update)
    with pytest.raises(SystemExit):
        b.apply(identity)
    assert b.w3.eth.sent == []
    assert b.journal.get(b.prefix + ":leaf0")["result"] == {}
    global_journal = Journal(str(__import__("pathlib").Path(identity).with_suffix(".evm-nonce.lock")) + ".sqlite")
    saved = global_journal.list()[0]["request"]
    assert b.apply(identity)["status"] == "CONFIRMING"
    assert len(b.w3.eth.sent) == 2
    assert b.w3.eth.sent[0].hex() == saved["raw_transaction"]
    assert b.journal.get(b.prefix + ":leaf0")["result"]["tx_hash"] == saved["tx_hash"]
    assert b.prepared.count("leaf0") == 1  # No re-estimation or signing on recovery.


def test_mempool_eviction_rebroadcasts_exact_signed_bytes(tmp_path):
    b, identity = fixture(tmp_path)
    b.w3.eth.automine = False
    assert b.apply(identity)["status"] == "BROADCAST"
    signed = list(b.w3.eth.sent)
    b.w3.eth.transactions.clear()
    assert b.apply(identity)["status"] == "BROADCAST"
    assert b.w3.eth.sent == signed + signed
    assert b.prepared == ["leaf0", "leaf1"]


def test_observation_uses_one_receipt_read_and_only_records_a_changed_state(tmp_path):
    b, identity = fixture(tmp_path)
    assert b.apply(identity)["status"] == "CONFIRMING"
    key = b.prefix + ":leaf0"
    entry = b.journal.get(key)
    calls = []
    original = b.w3.eth.get_transaction_receipt

    def read(tx_hash):
        calls.append(tx_hash)
        return original(tx_hash)

    b.w3.eth.get_transaction_receipt = read
    for _ in range(2):
        assert observe(b, b.profile["steps"][0], entry) is None
    assert calls == [entry["result"]["tx_hash"]] * 2
    with b.journal.db() as db:
        count = db.execute("SELECT count(*) FROM events WHERE operation=? AND status='CONFIRMING'", (key,)).fetchone()[0]
    assert count == 1


def test_pending_group_polls_without_rechecking_prefixes_for_known_transactions(tmp_path):
    b, identity = fixture(tmp_path, 4)
    b.w3.eth.automine = False
    assert b.apply(identity)["status"] == "BROADCAST"
    calls = []
    original = b.w3.eth.get_transaction_receipt

    def read(tx_hash):
        calls.append(tx_hash)
        return original(tx_hash)

    b.w3.eth.get_transaction_receipt = read
    assert b.apply(identity)["status"] == "BROADCAST"
    assert len(calls) == 12  # Four at entry, four in identity recovery and four at the exit.
    assert len(b.w3.eth.sent) == 4


def test_manifest_omits_unpublished_signed_bytes_and_auxiliary_journals(tmp_path):
    b, _ = fixture(tmp_path)
    b.profile.update(mode="mock", bitcoin_network="regtest")
    step = b.profile["steps"][0]
    key = b.prefix + ":" + step["id"]
    b.journal.put(key, step)
    b.journal.update(key, "SIGNED", dict(tx_hash="ab"*32, nonce="1", raw_transaction="private-envelope"))
    b.journal.put(b.prefix + ":auxiliary", dict(profile=dict(evm_rpc="private-rpc")))
    manifest = Bootstrap.manifest(b)
    assert [row["id"] for row in manifest["steps"]] == [key]
    assert manifest["steps"][0]["result"] == dict(tx_hash="ab"*32, nonce="1")
    assert b.journal.get(key)["result"]["raw_transaction"] == "private-envelope"


def test_conflicting_step_journal_does_not_replay_reserved_transaction(tmp_path):
    b, identity = fixture(tmp_path)
    b.w3.eth.automine = False
    assert b.apply(identity)["status"] == "BROADCAST"
    count = len(b.w3.eth.sent)
    b.w3.eth.transactions.clear()
    key = b.prefix + ":leaf1"
    existing = b.journal.get(key)["result"]
    b.journal.update(key, "SIGNED", dict(existing, nonce="99"))
    with pytest.raises(ValueError, match="EVM_SIGNED_TRANSACTION_BINDING"):
        b.apply(identity)
    assert len(b.w3.eth.sent) == count


@pytest.mark.parametrize("change,error", [
    ("transactionHash", "EVM_SIGNED_TRANSACTION_BINDING"),
    ("blockHash", "EVM_REORG_RECOVERY_REQUIRED"),
])
def test_foreign_or_orphaned_revert_does_not_latch_recovery(tmp_path, change, error):
    b, identity = fixture(tmp_path)
    b.w3.eth.automine = False
    assert b.apply(identity)["status"] == "BROADCAST"
    key = b.prefix + ":leaf0"
    tx_hash = b.journal.get(key)["result"]["tx_hash"]
    b.w3.eth.mine(tx_hash)
    b.w3.eth.receipts[tx_hash] = AttributeDict(dict(b.w3.eth.receipts[tx_hash], status=0, **{change: HexBytes("99"*32)}))
    with pytest.raises(ValueError, match=error):
        b.apply(identity)
    assert b.journal.get(key)["status"] == "BROADCAST"
    assert len(b.w3.eth.sent) == 2


def test_failure_observed_during_group_stops_later_sends(tmp_path):
    b, identity = fixture(tmp_path, 3)
    b.w3.eth.fail_nonce = 0
    result = b.apply(identity)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["step"] == "leaf0" and result["reason"] == "TRANSACTION_REVERTED"
    assert len(b.w3.eth.sent) == 1
    assert b.finished == []
    assert b.apply(identity) == result
    assert len(b.w3.eth.sent) == 1


@pytest.mark.parametrize("change", ["missing", "different", "canonical"])
def test_reorg_preserves_recorded_anchor_and_blocks_resume(tmp_path, change):
    b, identity = fixture(tmp_path)
    b.apply(identity)
    tx = b.journal.get(b.prefix + ":leaf0")["result"]["tx_hash"]
    if change == "missing":
        del b.w3.eth.receipts[tx]
    else:
        r = dict(b.w3.eth.receipts[tx])
        if change == "different":
            r["blockHash"] = HexBytes(b"x" * 32)
        else:
            b.w3.eth.get_block = lambda number: AttributeDict(dict(hash=HexBytes(b"x" * 32)))
        b.w3.eth.receipts[tx] = AttributeDict(r)
    code = "EVM_RPC_BEHIND" if change == "missing" else "EVM_REORG_RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match=f"^{code}$"):
        b.apply(identity)
    assert len(b.w3.eth.sent) == 2
    assert b.finished == []


def test_postcondition_failure_never_opens_barrier(tmp_path):
    b, identity = fixture(tmp_path)
    b.apply(identity)
    b.w3.eth.block_number = 100
    def checks(step, receipt):
        if step["id"] == "leaf1":
            raise ValueError("BINDING_CHECK_FAILED")
    b.verify_checks = checks
    with pytest.raises(ValueError, match="BINDING_CHECK_FAILED"):
        b.apply(identity)
    assert b.journal.get(b.prefix + ":leaf1")["status"] == "RECOVERY_REQUIRED"
    assert b.journal.get(b.prefix + ":leaf0")["status"] != "CONFIRMED"


@pytest.mark.parametrize("mode,status", [("verify", "INCOMPLETE"), ("dry_run", "DRY_RUN")])
def test_readonly_modes_do_not_sign_or_broadcast(tmp_path, mode, status):
    b, _ = fixture(tmp_path)
    assert b.apply(**{mode: True})["status"] == status
    assert b.w3.eth.sent == b.prepared == []


def test_same_target_exception_requires_canonical_dkg_and_unique_sessions(monkeypatch):
    address = "0x" + "11" * 20
    block = AttributeDict(dict(number=42, hash=HexBytes(b"b" * 32)))
    b = SimpleNamespace(contracts={"dkg": dict(address=address, artifact="DKGCoordinator"),
        "alias": dict(address=address, artifact="DKGCoordinator")}, resolve=lambda x: x,
        read_number=lambda: 42, w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda n: block)))
    checked = []
    monkeypatch.setattr("signer_agent.bitcoin_trust.checked_runtime", lambda *args: checked.append(args))
    group = [dict(id=f"dkg{i}", kind="call", contract=name, method="createCeremony",
        args=["0x" + f"{i + 1:064x}", [], "2", [], "1", "0x" + "22" * 32, "0x" + "33" * 32])
        for i, name in enumerate(("dkg", "alias"))]
    validate_targets(b, group)
    assert checked == [(b.w3, Web3.to_checksum_address(address), "DKGCoordinator", 42)]
    group[1]["args"][0] = group[0]["args"][0]
    with pytest.raises(ValueError, match="BOOTSTRAP_PARALLEL_SESSION"):
        validate_targets(b, group)
    group[1]["method"] = "setPolicy"
    with pytest.raises(ValueError, match="BOOTSTRAP_PARALLEL_TARGET"):
        validate_targets(b, group)


def test_recovery_does_not_truncate_old_pending_nonce_at_journal_ui_limit(tmp_path):
    b, identity = fixture(tmp_path)
    b.w3.eth.automine = False
    b.apply(identity)
    signed = list(b.w3.eth.sent)
    global_journal = Journal(str(__import__("pathlib").Path(identity).with_suffix(".evm-nonce.lock")) + ".sqlite")
    with global_journal.db() as db:
        db.executemany("INSERT INTO operations(id,digest,request,status,result) VALUES (?, '', '{}', 'CONFIRMED', '{}')",
                       [(f"newer-complete-{i}",) for i in range(1001)])
    assert all(row["status"] == "CONFIRMED" for row in global_journal.list())
    b.w3.eth.transactions.clear()
    assert b.apply(identity)["status"] == "BROADCAST"
    assert b.w3.eth.sent == signed + signed


def test_reorg_during_postconditions_does_not_open_barrier(tmp_path):
    b, identity = fixture(tmp_path)
    b.apply(identity)
    b.w3.eth.block_number = 100
    tx = b.journal.get(b.prefix + ":leaf0")["result"]["tx_hash"]
    def checks(step, receipt):
        if step["id"] == "leaf1":
            r = dict(b.w3.eth.receipts[tx], blockHash=HexBytes(b"x" * 32))
            b.w3.eth.receipts[tx] = AttributeDict(r)
    b.verify_checks = checks
    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        b.apply(identity)
    assert all(b.journal.get(b.prefix + ":" + step["id"])["status"] != "CONFIRMED" for step in b.profile["steps"])


@pytest.mark.parametrize("known", ["same", "missing", "different"])
def test_rebroadcast_mining_race_accepts_only_the_same_signed_hash(known):
    b = Bootstrap.__new__(Bootstrap)
    encoded = bytes.fromhex("010203")
    expected = Web3.keccak(encoded)
    def send(raw):
        assert raw == encoded
        raise ValueError("nonce too low")
    def lookup(tx_hash):
        assert tx_hash == expected.hex()
        if known == "missing":
            raise TransactionNotFound(tx_hash)
        return dict(hash=expected if known == "same" else HexBytes(b"x" * 32))
    b.w3 = SimpleNamespace(eth=SimpleNamespace(send_raw_transaction=send, get_transaction=lookup))
    if known == "same":
        assert b.broadcast(encoded.hex()) == expected
    else:
        with pytest.raises(ValueError, match="nonce too low" if known == "missing" else "EVM_SIGNED_TRANSACTION_BINDING"):
            b.broadcast(encoded.hex())
