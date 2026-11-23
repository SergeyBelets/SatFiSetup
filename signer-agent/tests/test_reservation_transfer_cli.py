"""Operator recovery reviews and journal retries against local EVM fixtures."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

from eth_abi import encode
from eth_account import Account
from hexbytes import HexBytes
import pytest
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound

from signer_agent import reservation_transfer_cli as cli
from signer_agent.operations import Journal


def address(number):
    return Web3.to_checksum_address(f"0x{number:040x}")


@pytest.fixture(params=["federation", "confederation"])
def transfer(request, tmp_path, monkeypatch):
    title = request.param.capitalize()
    operator = Account.from_key(bytes.fromhex("01" * 32))
    block = lambda n, timestamp: NS(number=n, hash=bytes([n]) * 32, timestamp=timestamp)
    old, new = bytes.fromhex("aa" * 32), bytes.fromhex("bb" * 32)
    formation, token = bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)
    target = address(11)
    registry, coordinator = address(12), address(13)
    args = NS(kind=request.param, factory=target, entity_id="fixture-formation",
              from_session=Web3.to_hex(old), to_session=Web3.to_hex(new), rpc="fixture1", chain_id="31337",
              confirmations=2, send=False, state=str(tmp_path / "transfer.sqlite"), identity_file="fixture",
              accept_fingerprint=None)
    method = "transfer" + title + "Reservation"
    view = NS(blocks={n: block(n, 1000 + n) for n in range(20)}, head=11,
              source_phase=6, replacement_phase=2, replacement_deadline=1100,
              reserved=old, matching_roster=True, authority=operator.address, registry=registry,
              genesis=b"G" * 32, receipt=None, event_hash=None, rejected=False)
    roster = [address(1), address(2), address(3)]
    calls, sent = [], []
    def get_block(which):
        if which == "latest":
            which = view.head
        elif which == "finalized":
            which = view.finalized
        return view.blocks[which] if which else NS(hash=view.genesis)

    def get_receipt(tx_hash):
        if view.receipt is None or HexBytes(tx_hash) != view.receipt.transactionHash:
            raise TransactionNotFound("no fixture receipt")
        return view.receipt

    def read(name, values, at, sender):
        calls.append((name, at))
        if name in ("dkgCoordinator", "dkgRegistry"):
            return registry if name == "dkgCoordinator" else view.registry
        if name == "feeSetupAuthority":
            return view.authority
        if name == "frostCoordinator":
            return coordinator
        if name == "reserved" + title + "Session":
            return view.reserved
        if name == "reserved" + title + "Terms":
            return formation, token
        if name == "getSessionInfo":
            phase = view.source_phase if values[0] == old else view.replacement_phase
            return (values[0], args.entity_id, len(roster), 2, phase, view.replacement_deadline, 0, 0, 0, phase == 4)
        if name == "ceremonies":
            return (bytes([1 if values[0] == old else 2]) * 32, 1, cli.EVM_AUTH, bytes(32), False, 0, 0)
        if name == "getSessionParticipants":
            return roster if values[0] == old or view.matching_roster else list(reversed(roster))
        if name == "requireReservationTransfer":
            return None
        assert name == method and sender == {"from": operator.address}
        assert values == (args.entity_id, old, new)
        if view.rejected:
            raise ContractLogicError("synthetic fixture does not allow transfer")

    def calldata(function, args):
        assert function == method
        return Web3.to_hex(Web3.keccak(text=function + "(string,bytes32,bytes32)")[:4]
                          + encode(["string", "bytes32", "bytes32"], args))

    def event(receipt, errors):
        return [{"address": target, "args": {request.param + "Id": view.event_hash or Web3.keccak(text=args.entity_id),
                                              "previous": old, "replacement": new}}]

    def contract(address, abi):
        class Functions:
            def __getattr__(self, name):
                return lambda *values: NS(call=lambda sender=None, *, block_identifier:
                    read(name, values, block_identifier, sender))
        return NS(address=address, functions=Functions(), encode_abi=calldata,
                  events=NS(**{title + "ReservationTransferred": lambda: NS(process_receipt=event)}))

    w3 = NS(eth=NS(chain_id=31337, get_block=get_block, contract=contract, get_transaction_receipt=get_receipt))

    class Executor:
        def __init__(self, profile, state, *, evm_rpc_override, evm_confirmation_floor):
            self.w3 = w3
            self.journal = Journal(state)
            self.prefix = profile["deployment_id"]
            self.journal.put(self.prefix, dict(profile=profile, version="1"))
            self.step = profile["steps"][0]

        def apply(self, identity, *, before_submit):
            key = self.prefix + ":transfer"
            item = self.journal.put(key, self.step)
            if item["status"] != "CONFIRMED":
                assert not before_submit([self.step])
                args = self.step["args"]
                data = calldata(method, [args[0], HexBytes(args[1]), HexBytes(args[2])])
                tx = operator.sign_transaction(dict(chainId=31337, nonce=0, to=target, data=data, value=0,
                                                    maxFeePerGas=2, maxPriorityFeePerGas=1, gas=100000))
                sent.append(tx.hash)
                view.reserved = new
                included = get_block("latest")
                view.receipt = NS(transactionHash=tx.hash, status=1, blockNumber=included.number, blockHash=included.hash)
                self.journal.update(key, "CONFIRMED", dict(tx_hash=tx.hash.hex(), raw_transaction=tx.raw_transaction.hex(),
                    nonce="0", block_number=str(included.number), block_hash=included.hash.hex()))
            return {"status": "VERIFIED"}

        @staticmethod
        def verify_receipt(result):
            return get_receipt(result["tx_hash"])

    def runtime(w3, candidate, kind, block):
        registered = {target: title + "Factory", registry: "DKGCoordinator", coordinator: "FROSTCoordinator"}
        if registered.get(candidate) != kind:
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
        return Web3.keccak(text=kind).hex()

    monkeypatch.setattr(cli, "evm_web3", lambda *a, **k: w3)
    monkeypatch.setattr(cli, "artifact", lambda kind:
        {"abi": [], "bytecode": {"object": "0x"}, "deployedBytecode": {"object": "0x"}})
    monkeypatch.setattr(cli, "checked_runtime", runtime)
    monkeypatch.setattr(cli, "account_at", lambda _: operator)
    monkeypatch.setattr(cli, "ReservationExecutor", Executor)
    return NS(args=args, view=view, sent=sent, calls=calls, old=old, new=new, w3=w3, operator=operator)


def authorized(transfer):
    review = cli.execute(transfer.args)
    return NS(**{**vars(transfer.args), "send": True, "accept_fingerprint": review["fingerprint"]})


def test_review_is_read_only_and_fingerprint_survives_normal_block_advance(transfer):
    from pathlib import Path
    first = cli.execute(transfer.args)
    assert first["status"] == "RESERVATION_TRANSFER_REVIEW"
    assert first["review"]["source_status"] == "Aborted"
    assert first["replacement_status"] == "Shares"
    assert not Path(transfer.args.state).exists()
    assert {height for _, height in transfer.calls} == {10}
    transfer.view.head += 1
    transfer.view.replacement_phase = 4
    second = cli.execute(transfer.args)
    assert second["fingerprint"] == first["fingerprint"]
    assert second["block_hash"] != first["block_hash"] and second["replacement_status"] == "Completed"


@pytest.mark.parametrize("change,error", [
    ("still_active_old", "RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL"),
    ("expired_replacement", "RESERVATION_TRANSFER_REPLACEMENT_UNAVAILABLE"),
    ("reordered_roster", "RESERVATION_TRANSFER_DKG_MISMATCH"),
    ("different_registry", "RESERVATION_TRANSFER_BINDING"),
    ("moved_reservation", "RESERVATION_TRANSFER_CHANGED"),
    ("simulation_rejected", "RESERVATION_TRANSFER_SIMULATION_REJECTED"),
])
def test_review_refuses_ineligible_or_stale_transfer(transfer, change, error):
    s = transfer.view
    if change == "still_active_old":
        s.source_phase, s.replacement_deadline = 2, 1000
    elif change == "expired_replacement":
        s.replacement_deadline = 1000
    elif change == "reordered_roster":
        s.matching_roster = False
    elif change == "different_registry":
        s.registry = address(50)
    elif change == "moved_reservation":
        s.reserved = transfer.new
    else:
        s.rejected = True
    with pytest.raises(ValueError, match=f"^{error}$"):
        cli.execute(transfer.args)
    assert not transfer.sent


def test_send_rechecks_exact_fingerprint_identity_and_live_replacement(transfer, monkeypatch):
    args = authorized(transfer)
    wrong = NS(**{**vars(args), "accept_fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_FINGERPRINT"):
        cli.execute(wrong)
    transfer.view.genesis = b"G" * 31 + b"X"
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_FINGERPRINT"):
        cli.execute(args)
    transfer.view.genesis = b"G" * 32
    with monkeypatch.context() as patch:
        patch.setattr(cli, "account_at", lambda _: NS(address=address(100)))
        with pytest.raises(ValueError, match="SENDER_IDENTITY_MISMATCH"):
            cli.execute(args)
    transfer.view.replacement_deadline = 1010  # At the confirmed height it is still active.
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_REPLACEMENT_UNAVAILABLE"):
        cli.execute(args)
    assert not transfer.sent


def test_already_changed_reservation_requires_the_exact_authorized_receipt(transfer):
    args = authorized(transfer)
    transfer.view.replacement_deadline = 1010
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_REPLACEMENT_UNAVAILABLE"):
        cli.execute(args)  # The memo was saved, but no authorized transaction was sent.
    transfer.view.reserved = transfer.new
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_CHANGED"):
        cli.execute(args)
    assert not transfer.sent


def test_send_and_rpc_retries_use_same_receipt_and_committed_anchor(transfer):
    args = authorized(transfer)
    transfer.view.head = 12
    sent = cli.execute(args)
    assert sent["status"] == "RESERVATION_TRANSFERRED"
    duplicate = NS(**{**vars(args), "rpc": "fixture2", "confirmations": 1})
    transfer.view.head = 11  # Provider has not reached the saved review anchor's depth.
    assert cli.execute(duplicate)["status"] == "AWAITING_EVM_RPC"
    transfer.view.head = 13
    assert cli.execute(duplicate) == sent and len(transfer.sent) == 1
    original = deepcopy(transfer.view.blocks[11])
    transfer.view.blocks[11].hash = b"F" * 32
    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        cli.execute(duplicate)
    transfer.view.blocks[11] = original
    transfer.view.event_hash = b"X" * 32
    with pytest.raises(ValueError, match="RESERVATION_TRANSFER_RECEIPT"):
        cli.execute(duplicate)
    assert len(transfer.sent) == 1


def test_mainnet_review_pins_finalized_snapshot(transfer):
    transfer.w3.eth.chain_id = 1
    transfer.args.chain_id = "1"
    transfer.args.confirmations = 12
    # Keep finalized below the height that confirmations alone would select.
    transfer.view.blocks[55] = NS(number=55, hash=b"Y" * 32, timestamp=1055)
    transfer.view.head, transfer.view.finalized = 55, 15
    result = cli.execute(transfer.args)
    assert result["status"] == "RESERVATION_TRANSFER_REVIEW" and result["block_number"] == "15"
    assert {height for _, height in transfer.calls} == {15}


def test_operator_journal_does_not_resolve_literal_organization_id():
    executor = cli.ReservationExecutor.__new__(cli.ReservationExecutor)
    assert executor.resolve(["$factory"]) == ["$factory"]


def pending_transfer(fixture):
    from signer_agent.bootstrap_parallel import save_signed
    from signer_agent.bootstrap_recovery import identity_lane

    args, w3 = fixture.args, fixture.w3
    review = cli.execute(args)["review"]
    source = Journal(args.state)
    inputs = {key: review[key] for key in
              ("chain_id", "kind", "entity_id", "factory", "from_session", "to_session")}
    prefix = cli.PREFIX + cli._digest(inputs)
    safe = cli.read_block(w3, args.confirmations)
    source.put(prefix + ":review", dict(review=review, number=str(safe.number), hash=Web3.to_hex(safe.hash),
                                        confirmations=str(args.confirmations)))
    profile = cli._profile(args, prefix, review, args.confirmations)
    step = profile["steps"][0]
    key = prefix + ":transfer"
    source.put(prefix, dict(profile=profile))
    source.put(key, step)
    identity = Path(args.state).with_name("operator.json")
    identity.write_text(json.dumps(dict(private_key=fixture.operator.key.hex())))
    identity.chmod(0o600)
    _, _, lane = identity_lane(identity)
    signed = fixture.operator.sign_transaction(dict(chainId=31337, nonce=0, to=review["factory"],
        data=review["calldata"], value=0, maxFeePerGas=20, maxPriorityFeePerGas=2, gas=55000))
    sends = []
    different_plan = NS(w3=w3, prefix="unrelated-operation", journal=source, broadcast=sends.append)
    save_signed(lane, different_plan, key, step,
                dict(tx_hash=signed.hash.hex(), raw_transaction=signed.raw_transaction.hex(), nonce="0"))
    def get_transaction(_tx_hash):
        raise TransactionNotFound("no fixture transaction")

    w3.eth.get_transaction = get_transaction
    return NS(lane=lane, runner=different_plan, source=source, key=key, tx_hash=signed.hash.hex(), sends=sends,
              identity=identity, review=review)


@pytest.mark.parametrize(("change", "error"), [
    ("height", "EVM_RPC_BEHIND"),
    ("hash", "EVM_REORG_RECOVERY_REQUIRED"),
    ("deadline", "RESERVATION_TRANSFER_REPLACEMENT_UNAVAILABLE"),
])
def test_common_nonce_recovery_refuses_rebroadcast_without_current_review(transfer, change, error):
    from signer_agent.bootstrap_parallel import recover_identity

    saved = pending_transfer(transfer)
    view = transfer.view
    if change == "height":
        view.head -= 1
    elif change == "hash":
        view.blocks[10].hash = b"F" * 32
    else:
        view.replacement_deadline = 1010
    with pytest.raises(ValueError, match=f"^{error}$"):
        recover_identity(saved.lane, saved.runner, transfer.operator)
    assert saved.sends == []


def test_common_nonce_recovery_relays_exact_bytes_for_live_reservation(transfer):
    from signer_agent.bootstrap_parallel import recover_identity

    saved = pending_transfer(transfer)
    recover_identity(saved.lane, saved.runner, transfer.operator)
    assert list(map(lambda raw: Web3.keccak(HexBytes(raw)).hex(), saved.sends)) == [saved.tx_hash]


def test_cancel_stale_reservation_preserves_original_binding_and_finalizes_nonce(transfer, monkeypatch):
    from signer_agent import bootstrap_replacement as replacement
    from signer_agent.bootstrap_parallel import recover_identity
    from signer_agent.bootstrap_recovery import signed_fields

    saved = pending_transfer(transfer)
    view, w3 = transfer.view, transfer.w3
    view.blocks[10].hash = b"F" * 32
    for block in view.blocks.values():
        block.baseFeePerGas, block.gasLimit = 15, 1000000
    observed = {}
    broadcasts = []

    def get_transaction(tx_hash):
        if tx_hash not in observed:
            raise TransactionNotFound("no fixture transaction")
        return observed[tx_hash]

    def send_transaction(raw):
        txid = Web3.keccak(raw)
        broadcasts.append(txid.hex())
        observed[txid.hex()] = dict(hash=txid, blockNumber=None)
        return txid

    w3.eth.get_transaction = get_transaction
    w3.eth.send_raw_transaction = send_transaction
    w3.eth.get_transaction_count = lambda _, tag: int(view.receipt is not None or bool(observed) and tag == "pending")
    w3.eth.get_balance = lambda *_args, **_kwargs: 10**18
    w3.eth.get_code = lambda *_args, **_kwargs: b""
    w3.eth.estimate_gas = lambda *_args, **_kwargs: 21000
    monkeypatch.setattr(replacement, "evm_web3", lambda *_args, **_kwargs: w3)

    def command(**options):
        return replacement.replace_pending(saved.identity, "fixture", saved.tx_hash, fee="40", priority="3",
                                           max_cost="3000000", **options)

    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        command()
    reviewed = command(cancel=True)
    assert reviewed["status"] == "EVM_REPLACEMENT_REVIEW" and broadcasts == []
    assert reviewed["replacement"]["cancel_reservation"]
    with pytest.raises(ValueError, match="EVM_REPLACEMENT_FINGERPRINT"):
        command(cancel=True, confirmed="wrong")
    result = command(cancel=True, confirmed=reviewed["fingerprint"])
    assert command(cancel=True, confirmed=reviewed["fingerprint"]) == result
    winner = result["tx_hash"]
    assert broadcasts == [winner]
    fields = signed_fields(saved.lane.get(winner)["request"], transfer.operator, 31337)
    assert fields["gas"] == 21000 and fields["value"] == 0 and fields["nonce"] == 0
    assert fields["to"] == HexBytes(transfer.operator.address) and not fields["data"]
    with pytest.raises(ValueError, match="EVM_REPLACEMENT_CANCEL_PENDING"):
        command()

    block = view.blocks[view.head]
    view.receipt = NS(transactionHash=HexBytes(winner), status=1, blockNumber=block.number, blockHash=block.hash,
                      gasUsed=21000, effectiveGasPrice=20)
    assert command(cancel=True)["status"] == "RESERVATION_TRANSFER_CANCEL_CONFIRMING"
    view.head += 1
    assert recover_identity(saved.lane, saved.runner, transfer.operator) == 1
    assert command(cancel=True)["status"] == "RESERVATION_TRANSFER_CANCELLED"
    assert saved.source.get(saved.key)["status"] == "CANCELLED"
    assert saved.sends == [] and broadcasts == [winner]
    assert view.reserved == transfer.old
