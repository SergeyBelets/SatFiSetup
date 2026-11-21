import json
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3.exceptions import TransactionNotFound

from signer_agent.bootstrap_parallel import operation_binding, recover_identity, restore_signed
from signer_agent.bootstrap_create import prepare_transactions
from signer_agent.bootstrap_recovery import identity_lane, recover_create_group, require_resume_lane
from signer_agent.operations import Journal
from signer_agent.safe_errors import debug_diagnostics


def test_create_preflight_keeps_amsterdam_state_gas_and_enforces_local_cap(monkeypatch):
    block = SimpleNamespace(number=90, hash=HexBytes("11" * 32), timestamp=1791294816, gasLimit=200_000_000)
    used = [25_900_000, 22_800_000, 38_000_000]
    simulated = []

    def rpc(method, params):
        assert method == "eth_simulateV1"
        payload, tag = params
        assert payload["validation"] is True and tag == hex(block.number)
        calls = [entry["calls"][0] for entry in payload["blockStateCalls"]]
        assert all("to" not in call for call in calls)
        assert [int(call["nonce"], 16) for call in calls] == [212, 213, 214]
        simulated.append([int(call["gas"], 16) for call in calls])
        return {"result": [{"calls": [{"gasUsed": hex(gas), "status": "0x1"}]} for gas in used]}

    eth = SimpleNamespace(chain_id=11155111, get_block=lambda _: block, get_transaction_count=lambda *_: 212)
    w3 = SimpleNamespace(eth=eth, provider=SimpleNamespace(make_request=rpc))
    account = SimpleNamespace(address="0x" + "11" * 20)
    steps = [{"create_group": "withdrawal-fee-constructors"}] * 3
    def build(step, account, nonce, *, simulation_gas):
        return dict(nonce=nonce, gas=simulation_gas, to=b"")
    bootstrap = SimpleNamespace(w3=w3, build_transaction=build)
    monkeypatch.delenv("SATOSHIFI_CREATE_SIMULATION_RPC", raising=False)
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "60000000")
    result = prepare_transactions(bootstrap, steps, account, 212)
    assert simulated == [[60_000_000] * 3, [gas * 13 // 10 for gas in used]]
    assert [tx["gas"] for tx in result] == simulated[-1]
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "40000000")
    with pytest.raises(ValueError, match="CREATE_SIMULATION_GAS_BUDGET"):
        prepare_transactions(bootstrap, steps, account, 212)


@pytest.fixture
def failed_group(tmp_path, monkeypatch):
    account = Account.create()
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": account.key.hex()}))
    identity.chmod(0o600)
    _, _, lane = identity_lane(identity)
    journal = Journal(tmp_path / "bootstrap.sqlite")
    block = SimpleNamespace(number=100, hash=HexBytes("11" * 32), baseFeePerGas=1)
    latest = SimpleNamespace(number=101, hash=HexBytes("22" * 32), baseFeePerGas=1, timestamp=1020)
    state = SimpleNamespace(finalized=211, latest=211, pending=211, receipts={}, known={}, sent=[], code=b"")

    def observe(records, tx_hash):
        if tx_hash not in records:
            raise TransactionNotFound(tx_hash)
        return records[tx_hash]

    def count(_, tag):
        if tag == block.number:
            return state.finalized
        return state.pending if tag == "pending" else state.latest

    eth = SimpleNamespace(chain_id=11155111,
                          get_block=lambda tag: block if tag in ("finalized", block.number) else latest,
                          get_transaction_count=count, get_transaction_receipt=lambda h: observe(state.receipts, h),
                          get_transaction=lambda h: observe(state.known, h),
                          get_code=lambda address, **_: state.code if address == account.address else b"",
                          get_balance=lambda *_, **__: 10**18)
    steps = [dict(id="withdrawal_" + name, kind="deploy", create_group="withdrawal-fee-constructors",
                  artifact="WithdrawalFee" + name.title(), gas="8000000") for name in ("payments", "funding", "credits")]
    prior_step = dict(id="dkg_completed", kind="read")
    later_step = dict(id="bind_withdrawal_fees", kind="call")
    bootstrap = SimpleNamespace(prefix="fixture", journal=journal, w3=SimpleNamespace(eth=eth),
                                broadcast=state.sent.append,
                                profile=dict(operator=account.address, steps=[prior_step, *steps, later_step]))
    journal.put(bootstrap.prefix, dict(profile=bootstrap.profile))
    journal.put("fixture:dkg_completed", prior_step)
    journal.update("fixture:dkg_completed", "CONFIRMED", dict(read_anchor=dict(number="100", hash=block.hash.hex())))
    keys = []
    for nonce, step in enumerate(steps, 209):
        key = "fixture:" + step["id"]
        journal.put(key, step)
        tx = account.sign_transaction(dict(nonce=nonce, value=0, data="0x6000", gas=10_400_000,
                                           gasPrice=10, chainId=11155111))
        tx_hash = tx.hash.hex()
        keys.append(tx_hash)
        if nonce < 211:
            state.receipts[tx_hash] = SimpleNamespace(status=0, transactionHash=tx.hash,
                                                     blockNumber=block.number, blockHash=block.hash)
        saved = dict(tx_hash=tx_hash, raw_transaction=tx.raw_transaction.hex(), nonce=str(nonce))
        lane.put(tx_hash, dict(saved, chain_id="11155111", **operation_binding(bootstrap, key, step)))
        journal.update(key, "REVERTED" if nonce < 211 else "SIGNED", saved)

    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "20")
    monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "11")
    return SimpleNamespace(bootstrap=bootstrap, journal=journal, lane=lane, account=account, identity=identity,
                           keys=keys, steps=steps, later=later_step, state=state, block=block)


def test_retire_create_group_only_after_all_original_nonces_are_consumed(failed_group):
    f = failed_group
    result = recover_create_group(f.bootstrap, f.identity)
    assert result["status"] == "CREATE_GROUP_NONCES_NOT_FINALIZED"
    assert result["nonces"] == ["211"]
    assert result["minimum_max_fee_per_gas_wei"] == result["minimum_max_priority_fee_per_gas_wei"] == "11"
    f.state.finalized = 212
    result = recover_create_group(f.bootstrap, f.identity)
    assert result["status"] == "CREATE_GROUP_RECOVERY_REVIEW"
    with pytest.raises(ValueError, match="CREATE_GROUP_RECOVERY_FINGERPRINT"):
        recover_create_group(f.bootstrap, f.identity, "wrong")
    confirmed = recover_create_group(f.bootstrap, f.identity, result["fingerprint"])
    assert confirmed["status"] == "CREATE_GROUP_RETRY_READY"
    assert recover_create_group(f.bootstrap, f.identity, result["fingerprint"]) == confirmed
    assert [f.lane.get(key)["status"] for key in f.keys] == ["RETIRED"] * 3
    for step in f.steps:
        key = "fixture:" + step["id"]
        assert f.journal.get(key)["status"] == "PREPARED"
        assert restore_signed(f.lane, f.bootstrap, key, step) is None


def test_nonce_close_requires_review_retries_exact_bytes_and_waits_for_finality(failed_group, capsys):
    f = failed_group
    prior = f.journal.get("fixture:dkg_completed")
    review = recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    assert review["status"] == "CREATE_GROUP_NONCE_CLOSE_REVIEW"
    assert review["nonce_close"]["to"] == f.account.address
    assert review["nonce_close"]["max_cost_wei"] == "420000"
    assert f.state.sent == []
    with pytest.raises(ValueError, match="CREATE_GROUP_NONCE_CLOSE_FINGERPRINT"):
        recover_create_group(f.bootstrap, f.identity, confirm_close="wrong")
    assert f.state.sent == []
    fingerprint = review["fingerprint"]
    key = "withdrawal-nonce-close:" + f.keys[-1]

    def uncertain(raw):
        assert f.lane.get(key)["request"]["raw_transaction"] == raw
        f.state.sent.append(raw)
        raise TimeoutError(raw)

    f.bootstrap.broadcast = uncertain
    with debug_diagnostics(True):
        result = recover_create_group(f.bootstrap, f.identity, confirm_close=fingerprint)
    assert result["status"] == "CREATE_GROUP_NONCE_CLOSE_SEND_UNCERTAIN"
    diagnostic = capsys.readouterr().err
    assert f.state.sent[0] not in diagnostic
    record = json.loads(diagnostic)
    assert record["status"] == "DEBUG_ERROR"
    assert record["code"] == result["status"]
    assert any(frame["function"] == "close_nonce" for frame in record["frames"])
    assert recover_create_group(f.bootstrap, f.identity)["status"] == "CREATE_GROUP_NONCE_CLOSE_SAVED"
    assert len(f.state.sent) == 1
    with pytest.raises(ValueError, match="CREATE_GROUP_RECOVERY_REQUIRED"):
        recover_identity(f.lane, f.bootstrap, f.account)
    assert len(f.state.sent) == 1
    f.bootstrap.broadcast = f.state.sent.append
    result = recover_create_group(f.bootstrap, f.identity, confirm_close=fingerprint)
    assert result["status"] == "CREATE_GROUP_NONCE_CLOSE_BROADCAST"
    assert f.state.sent == [f.state.sent[0]] * 2
    raw = HexBytes(f.state.sent[0])
    fields = TypedTransaction.from_bytes(raw).as_dict()
    assert fields["nonce"] == 211 and fields["value"] == 0 and not fields["data"]
    assert fields["to"] == HexBytes(f.account.address) and fields["gas"] == 21_000
    assert fields["maxFeePerGas"] == 20 and fields["maxPriorityFeePerGas"] == 11
    assert Account.recover_transaction(raw) == f.account.address
    f.state.latest = f.state.pending = 212
    assert recover_create_group(f.bootstrap, f.identity)["status"] == "CREATE_GROUP_NONCE_CLOSE_CONFIRMING"
    assert f.journal.get("fixture:withdrawal_credits")["status"] == "SIGNED"
    f.state.finalized = 212
    f.state.receipts[result["tx_hash"]] = SimpleNamespace(status=1, transactionHash=HexBytes(result["tx_hash"]),
                                                          blockNumber=f.block.number, blockHash=f.block.hash)
    final = recover_create_group(f.bootstrap, f.identity)
    assert final["status"] == "CREATE_GROUP_RECOVERY_REVIEW"
    assert final["nonce_closures"][0]["tx_hash"] == result["tx_hash"]
    assert final["fingerprint"] != fingerprint
    assert recover_create_group(f.bootstrap, f.identity, final["fingerprint"])["status"] == "CREATE_GROUP_RETRY_READY"
    assert f.lane.get(key)["status"] == "CONFIRMED"
    assert f.journal.get("fixture:dkg_completed") == prior


@pytest.mark.parametrize("case,error", [
    ("later_source", "CREATE_GROUP_RECOVERY_DEPENDENT_OPERATION"),
    ("later_lane", "CREATE_GROUP_RECOVERY_DEPENDENT_OPERATION"),
    ("other_identity_operation", "CREATE_GROUP_RECOVERY_IDENTITY_PENDING"),
    ("prior_reorg", "EVM_REORG_RECOVERY_REQUIRED"),
    ("old_success", "CREATE_GROUP_RECOVERY_SUCCESSFUL_DEPLOYMENT"),
    ("unknown_pending", "CREATE_GROUP_NONCE_CLOSE_CONFLICT"),
    ("delegated_account", "CREATE_GROUP_NONCE_CLOSE_ACCOUNT_CODE"),
    ("approval_expired", "CREATE_GROUP_RECOVERY_APPROVAL_EXPIRED"),
    ("weak_fee", "CREATE_GROUP_NONCE_CLOSE_FEE"),
])
def test_nonce_close_refuses_unreconciled_state(failed_group, case, error, monkeypatch):
    f = failed_group
    if case == "later_source":
        key = "fixture:" + f.later["id"]
        f.journal.put(key, f.later)
        f.journal.update(key, "SIGNED", dict(tx_hash="aa"))
    elif case == "later_lane":
        f.lane.put("later", dict(chain_id="11155111",
            **operation_binding(f.bootstrap, "fixture:" + f.later["id"], f.later)))
    elif case == "other_identity_operation":
        f.lane.put("other", dict(chain_id="11155111"))
    elif case == "prior_reorg":
        f.journal.update("fixture:dkg_completed", "CONFIRMED", dict(read_anchor=dict(number="100", hash="ab"*32)))
    elif case == "old_success":
        f.state.receipts[f.keys[0]].status = 1
    elif case == "unknown_pending":
        f.state.pending = 212
    elif case == "delegated_account":
        f.state.code = bytes.fromhex("ef0100")
    elif case == "approval_expired":
        f.bootstrap.profile["approval_deadline"] = "1020"
    else:
        monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "10")
    with pytest.raises(ValueError, match=error):
        recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    assert f.state.sent == []


def test_nonce_close_can_replace_known_pending_create(failed_group):
    f = failed_group
    f.state.pending = 212
    f.state.known[f.keys[-1]] = dict(hash=HexBytes(f.keys[-1]), blockNumber=None)
    review = recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    result = recover_create_group(f.bootstrap, f.identity, confirm_close=review["fingerprint"])
    f.state.known[result["tx_hash"]] = dict(hash=HexBytes(result["tx_hash"]), blockNumber=None)
    assert recover_create_group(f.bootstrap, f.identity, confirm_close=review["fingerprint"]) == result
    assert len(f.state.sent) == 1


def test_confirmation_rechecks_pending_nonce_and_reviewed_fees(failed_group, monkeypatch):
    f = failed_group
    review = recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    f.state.pending = 212
    with pytest.raises(ValueError, match="CREATE_GROUP_NONCE_CLOSE_CONFLICT"):
        recover_create_group(f.bootstrap, f.identity, confirm_close=review["fingerprint"])
    f.state.pending = 211
    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "21")
    with pytest.raises(ValueError, match="CREATE_GROUP_NONCE_CLOSE_FINGERPRINT"):
        recover_create_group(f.bootstrap, f.identity, confirm_close=review["fingerprint"])
    assert f.state.sent == []


def test_nonce_close_does_not_unlock_a_queued_later_create(failed_group):
    f = failed_group
    f.state.finalized = f.state.latest = f.state.pending = 210
    f.state.receipts.pop(f.keys[1])
    key = "fixture:withdrawal_funding"
    f.journal.update(key, "SIGNED", f.journal.get(key)["result"])
    f.state.known[f.keys[-1]] = dict(hash=HexBytes(f.keys[-1]), blockNumber=None)
    with pytest.raises(ValueError, match="CREATE_GROUP_NONCE_CLOSE_CONFLICT"):
        recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    assert f.state.sent == []


def test_retirement_retries_after_partial_journal_write(failed_group, monkeypatch):
    f = failed_group
    review = recover_create_group(f.bootstrap, f.identity, prepare_close=True)
    close = recover_create_group(f.bootstrap, f.identity, confirm_close=review["fingerprint"])
    # An outside sender with the same key consumed the nonce first.
    f.state.finalized = 212
    final = recover_create_group(f.bootstrap, f.identity)
    assert final["nonce_closures"][0]["receipt_status"] == "REPLACED"
    original = Journal.update

    def interrupt(journal, key, status, result):
        if journal.path == f.lane.path and key == f.keys[1] and status == "RETIRED":
            raise OSError()
        return original(journal, key, status, result)

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "update", interrupt)
        with pytest.raises(OSError):
            recover_create_group(f.bootstrap, f.identity, final["fingerprint"])
    assert f.lane.get(f.keys[0])["status"] == "RETIRED"
    assert f.journal.get("fixture:withdrawal_payments")["status"] == "REVERTED"
    assert recover_create_group(f.bootstrap, f.identity, final["fingerprint"])["status"] == "CREATE_GROUP_RETRY_READY"
    saved = f.lane.get("withdrawal-nonce-close:" + f.keys[-1])
    assert saved["status"] == "RETIRED" and saved["result"]["tx_hash"] == close["tx_hash"]
    assert len(f.state.sent) == 1


@pytest.mark.parametrize("change", ["finalized_nonce", "finalized_block"])
def test_retired_nonce_evidence_is_rechecked_before_any_send(failed_group, change):
    f = failed_group
    f.state.finalized = 212
    review = recover_create_group(f.bootstrap, f.identity)
    recover_create_group(f.bootstrap, f.identity, review["fingerprint"])
    assert recover_identity(f.lane, f.bootstrap, f.account) == 212
    if change == "finalized_nonce":
        f.state.finalized = 211
    else:
        f.block.hash = HexBytes("ff" * 32)
    code = "EVM_RPC_BEHIND" if change == "finalized_nonce" else "EVM_REORG_RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match=code):
        recover_identity(f.lane, f.bootstrap, f.account)
    assert f.state.sent == []


def test_recovered_plan_requires_original_operator_and_lane_after_manifest_updates(failed_group, tmp_path):
    f = failed_group
    f.state.finalized = 212
    review = recover_create_group(f.bootstrap, f.identity)
    recover_create_group(f.bootstrap, f.identity, review["fingerprint"])
    f.journal.update(f.bootstrap.prefix, "BROADCAST", {})
    require_resume_lane(f.bootstrap, f.identity, f.account)
    with pytest.raises(ValueError, match="CREATE_GROUP_RECOVERY_IDENTITY_LANE"):
        require_resume_lane(f.bootstrap, f.identity, Account.create())
    other_path = tmp_path / "other.json"
    other_path.write_bytes(f.identity.read_bytes())
    other_path.chmod(0o600)
    identity_lane(other_path)
    with pytest.raises(ValueError, match="CREATE_GROUP_RECOVERY_IDENTITY_LANE"):
        require_resume_lane(f.bootstrap, other_path, f.account)
