"""A competing finalizer may consume an EOA nonce, never a second FROST share."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

from signer_agent import bootstrap_recovery, finalize_recovery
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import recover_identity, save_signed
from signer_agent.coordinator import CoordinatorClient
from signer_agent.operations import Journal
from signer_agent.transaction_wait import TransactionPending, TransactionRecoveryRequired


@pytest.fixture
def race(tmp_path):
    identity = tmp_path / "custodian.json"
    owner = Account.create()
    identity.write_text(json.dumps({"private_key": owner.key.hex()}))
    identity.chmod(0o600)
    source = Journal(str(identity) + ".transactions.sqlite")
    _, _, lane = bootstrap_recovery.identity_lane(identity)
    coordinator = Web3.to_checksum_address("0x" + "aa" * 20)
    signature, message = b"s" * 64, b"m" * 32
    args = Bootstrap.wire([42, signature, message])
    prefix = finalize_recovery._operation(coordinator, args)
    step = dict(id="send", kind="call", contract="target", method="finalizeSession", args=args)
    code = b"coordinator runtime fixture"
    pin = dict(address=coordinator, artifact="FROSTCoordinator", runtime_hash=Web3.keccak(code).hex())
    profile = dict(deployment_id=prefix, chain_id="1", evm_confirmations="12", steps=[step], contracts={"target": pin})
    source.put(prefix, dict(version="1", profile=profile))
    key = prefix + ":send"
    source.put(key, step)

    abi = json.loads((Path(__file__).resolve().parents[1] / "contracts/FROSTCoordinator.json").read_text())
    writer = Web3().eth.contract(address=coordinator, abi=abi)
    raw = owner.sign_transaction(dict(chainId=1, type=2, nonce=8, gas=180000,
        maxFeePerGas=20, maxPriorityFeePerGas=1, to=coordinator, value=0,
        data=writer.encode_abi("finalizeSession", args=[42, signature, message]))).raw_transaction
    tx = Web3.keccak(raw).hex()
    envelope = dict(tx_hash=tx, raw_transaction=raw.hex(), nonce="8")
    block = lambda n: NS(number=n, hash=HexBytes(int(n).to_bytes(32, "big")))
    receipt = NS(status=0, gasUsed=33000, transactionHash=HexBytes(tx), blockNumber=50,
                 blockHash=block(50).hash, transactionIndex=5)
    state = NS(finalized=52, latest=70, signature=signature, message=message, log_index=3,
               previous_final=False, current_final=True, sent=0)
    def session(n):
        finalized = state.current_final if n >= 50 else state.previous_final
        return (42, coordinator, b"g" * 32, state.message, True, 2, 2, 0,
                True, coordinator, 2 if finalized else 1, 2, 2, 0, 1, coordinator, 0, 0, b"p" * 32, 2)
    def logs(**kwargs):
        assert kwargs == dict(from_block=50, to_block=50, argument_filters={"sessionId": 42})
        return [] if state.log_index is None else [dict(address=coordinator, blockHash=receipt.blockHash,
            transactionIndex=state.log_index, args=dict(sessionId=42, aggregatedSignature=state.signature, messageHash=state.message))]
    contract = NS(address=coordinator, encode_abi=writer.encode_abi, functions=NS(
        getSession=lambda i:NS(call=lambda block_identifier=70:session(block_identifier)),
        getResult=lambda i:NS(call=lambda block_identifier:(state.signature, state.message)),
        finalizeSession=lambda *a:NS(call=lambda *a:None)),
        events=NS(SessionFinalized=lambda:NS(get_logs=logs)))
    def get_block(n):
        if n == "latest": n=state.latest
        if n == "finalized": n=state.finalized
        return block(n)
    w3 = NS(eth=NS(chain_id=1, get_block=get_block, contract=lambda **kw:contract,
        get_code=lambda address, n:code, get_transaction_receipt=lambda h:receipt,
        get_transaction_count=lambda address,n:9))
    bootstrap = NS(w3=w3, journal=source, prefix=prefix, evm_confirmations=12)
    save_signed(lane, bootstrap, key, step, envelope)
    def failed_send(_):
        state.sent += 1
        state.current_final = True
        source.update(key, "REVERTED", envelope)
        raise TransactionRecoveryRequired(dict(reason="TRANSACTION_REVERTED", tx_hash=tx))
    client = NS(address=coordinator, contract=contract, identity_file=identity,
                transaction_journal=str(source.path), sender=owner.address, chain_id=1,
                _confirmation_graph=lambda:dict(evm_confirmations="12"), w3=w3, _send=failed_send)
    return NS(**locals())


def test_loser_after_preflight_proves_receipt_and_releases_only_its_nonce(race):
    f = race
    f.state.current_final = False
    assert CoordinatorClient.finalize(f.client, 42, f.signature, f.message) is None
    assert f.state.sent == 1
    assert f.source.get(f.key)["status"] == "REVERTED"
    assert f.lane.get(f.tx)["result"]["receipt_status"] == "0"
    assert f.lane.get(f.tx)["result"]["reason"] == "SESSION_ALREADY_FINALIZED"
    assert finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert f.state.sent == 1


@pytest.mark.parametrize("change", ["different_result", "winner_after", "out_of_gas", "session_open", "wrong_target"])
def test_unrelated_revert_cannot_be_acknowledged(race, change):
    f = race
    if change == "different_result": f.state.message = b"x" * 32
    if change == "winner_after": f.state.log_index = f.receipt.transactionIndex + 1
    if change == "out_of_gas": f.receipt.gasUsed = 180000
    if change == "session_open": f.state.current_final = False
    if change == "wrong_target":
        other = Web3.to_checksum_address("0x" + "15" * 20)
        f.client.address = other
    assert not finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert f.lane.get(f.tx)["status"] != "CONFIRMED"


@pytest.mark.parametrize("status", ["SIGNED", "REVERTED"])
def test_restart_on_next_send_recovers_old_nonce_and_keeps_source_failed(race, status):
    f = race
    f.state.log_index = None
    f.state.previous_final = True
    f.source.update(f.key, status, f.envelope)
    assert recover_identity(f.lane, f.bootstrap, f.owner) == 9
    assert f.lane.get(f.tx)["status"] == "CONFIRMED"
    assert f.source.get(f.key)["status"] == "REVERTED"
    assert f.state.sent == 0


def test_restart_waits_for_finality_before_changing_journal(race):
    f = race
    f.state.finalized = 49
    with pytest.raises(TransactionPending):
        finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert f.lane.get(f.tx)["status"] != "CONFIRMED"
    f.state.finalized = 52
    assert finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)


def test_acknowledgement_preserves_a_different_pending_lane_entry(race):
    f = race
    f.lane.put("unrelated", {"chain_id": "1", "operation": "withdrawal-1:send"})
    before = f.lane.get("unrelated")
    assert finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert f.lane.get("unrelated") == before


def test_restart_finds_matching_reservation_after_partial_source_write(race):
    f = race
    f.source.update(f.key, "PREPARED", {})
    assert finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert f.source.get(f.key)["result"]["raw_transaction"] == f.envelope["raw_transaction"]
    assert f.lane.get(f.tx)["result"]["reason"] == "SESSION_ALREADY_FINALIZED"


def test_terminal_restart_finds_the_attempt_and_closes_only_that_lane_entry(race):
    f = race
    finalize_recovery.reconcile_terminal(f.client, NS(session_id=42, message_hash=f.message))
    assert f.lane.get(f.tx)["result"]["reason"] == "SESSION_ALREADY_FINALIZED"
    assert f.state.sent == 0


def test_terminal_session_without_reservation_needs_no_rpc(race):
    f = race
    f.client.w3 = None
    finalize_recovery.reconcile_terminal(f.client, NS(session_id=41, message_hash=f.message))
    assert f.lane.get(f.tx)["status"] != "CONFIRMED"
    f.client.transaction_journal = None
    finalize_recovery.reconcile_terminal(f.client, NS(session_id=42, message_hash=f.message))


@pytest.mark.parametrize("source", ["configuration", "client", "absent", "wrong_schema"])
def test_recovery_abi_with_wheel_installation(race, tmp_path, monkeypatch, source):
    f = race
    abi = json.loads(finalize_recovery.PACKAGED_ABI.read_text())
    monkeypatch.setattr(finalize_recovery, "PACKAGED_ABI", tmp_path / "not-in-wheel.json")
    monkeypatch.delenv("FROST_ABI_PATH", raising=False)
    if source in ("configuration", "wrong_schema"):
        if source == "wrong_schema":
            next(v for v in abi if v.get("name") == "SessionFinalized")["inputs"][0]["indexed"] = False
        filename = tmp_path / "public-abi.json"
        filename.write_text(json.dumps({"abi": abi}))
        monkeypatch.setenv("FROST_ABI_PATH", str(filename))
    if source == "client":
        f.contract.abi = abi
    ok = finalize_recovery.reconcile_session(f.client, 42, f.signature, f.message)
    assert ok == (source in ("configuration", "client"))
    assert (f.lane.get(f.tx)["status"] == "CONFIRMED") == ok


@pytest.mark.parametrize("journal_status", ["SIGNED", "REVERTED"])
def test_terminal_command_reports_own_conflicting_revert(race, journal_status):
    f = race
    f.state.signature = b"x" * 64
    f.source.update(f.key, journal_status, f.envelope)
    session = NS(session_id=42, message_hash=f.message)
    with pytest.raises(TransactionRecoveryRequired) as failure:
        finalize_recovery.reconcile_terminal(f.client, session)
    assert failure.value.result == dict(status="RECOVERY_REQUIRED", reason="FINALIZATION_REVERT_REQUIRES_REVIEW",
                                        tx_hash=f.tx, step="send")
    assert f.lane.get(f.tx)["status"] != "CONFIRMED"


def test_terminal_read_after_explicit_acknowledgement_is_not_blocked(race):
    f = race
    f.state.signature = b"x" * 64
    f.source.update(f.key, "REVERTED", f.envelope)
    f.lane.update(f.tx, "CONFIRMED", dict(nonce="8", receipt_status="0", tx_hash=f.tx,
                  block_number="50", block_hash=f.receipt.blockHash.hex()))
    finalize_recovery.reconcile_terminal(f.client, NS(session_id=42, message_hash=f.message))
    assert f.lane.get(f.tx)["result"].get("reason") is None


def test_service_terminal_entrypoint_surfaces_local_revert(race):
    from signer_agent.service import SigningService
    f = race
    f.state.signature = b"x" * 64
    f.source.update(f.key, "REVERTED", f.envelope)
    # Use the real coordinator client type with the fixture's provider and journals.
    client = object.__new__(CoordinatorClient)
    client.__dict__.update({k: v for k, v in vars(f.client).items() if k != "chain_id"})
    service = object.__new__(SigningService)
    service.client = client
    session = NS(state=2, session_id=42, message_hash=f.message)
    with pytest.raises(TransactionRecoveryRequired) as failure:
        service.try_finalize(session)
    assert failure.value.result["tx_hash"] == f.tx
    assert f.lane.get(f.tx)["status"] != "CONFIRMED"
