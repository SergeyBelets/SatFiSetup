from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent import confederation_dao_report as report
from signer_agent.confederation_dao_report import _receipt
from signer_agent.operations import canonical


@pytest.mark.parametrize("attack", [None, "unconfirmed", "different_hash", "wrong_index",
                                    "unlisted_method", "empty_sweep"])
def test_expense_requires_canonical_successful_receipt_with_actual_income(attack):
    tx_hash = b"t" * 32
    block_hash = b"b" * 32
    payer = Web3.to_checksum_address(f"0x{1:040x}")
    target_address = Web3.to_checksum_address(f"0x{2:040x}")
    tx = dict(hash=tx_hash, blockNumber=6, blockHash=block_hash, transactionIndex=0,
              input=b"abcd", to=target_address)
    tx["from"] = payer
    receipt = dict(tx, status=1, transactionHash=tx_hash, gasUsed=21000, effectiveGasPrice=7)
    events = [] if attack == "empty_sweep" else [
        dict(address=target_address, args=dict(stream=3, amount=1))]
    target = SimpleNamespace(address=target_address, events=SimpleNamespace(
        Income=lambda: SimpleNamespace(process_receipt=lambda *args, **kwargs: events)))
    method = "unused" if attack == "unlisted_method" else "sweep"
    target.decode_function_input = lambda _: (SimpleNamespace(fn_name=method), {})
    included = b"x" * 32 if attack == "wrong_index" else tx_hash
    canonical = SimpleNamespace(hash=block_hash, timestamp=86400, transactions=[included])
    reads = []

    def block_at(number):
        reads.append(number)
        return canonical

    rpc = SimpleNamespace(eth=SimpleNamespace(get_block=block_at, get_transaction=lambda _: tx,
                           get_transaction_receipt=lambda _: receipt))
    head = SimpleNamespace(number=5 if attack == "unconfirmed" else 7)
    digest = Web3.to_hex(b"x" * 32 if attack == "different_hash" else tx_hash)
    targets = {target_address: (target, {"sweep"})}
    coordinator = SimpleNamespace(address=payer)
    anchors = {}
    if attack:
        with pytest.raises(ValueError, match="DAO_EXPENSE_"):
            _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors)
        return

    row = _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors)
    assert row["payer"] == payer and row["wei"] == str(21000 * 7)
    assert _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors) == row
    assert reads == [6]


def test_native_gas_report_accepts_reserve_operations_and_excludes_prevout_cache(monkeypatch):
    addresses = {name: Web3.to_checksum_address(f"0x{i:040x}") for i, name in enumerate(
        ("income", "pool", "escrow", "authorization", "validator", "coordinator", "deposits",
         "reserve", "gateway", "spv"), start=1)}
    nodes = {name: SimpleNamespace(address=where) for name, where in addresses.items()}
    by_address = {node.address: node for node in nodes.values()}
    epoch = b"e" * 32
    bindings = {
        "income": dict(pool=addresses["pool"], authorization=addresses["authorization"],
                       validator=addresses["validator"]),
        "pool": dict(withdrawalEscrow=addresses["escrow"], daoIncome=addresses["income"],
                     frostMultisig=addresses["authorization"], custodianValidator=addresses["validator"],
                     bitcoinReserve=addresses["reserve"]),
        "escrow": dict(depositRegistry=addresses["deposits"], spv=addresses["spv"]),
        "authorization": dict(coordinator=addresses["coordinator"], dkgId=epoch),
        "deposits": dict(authorization=addresses["authorization"]),
        "reserve": dict(confederationPool=addresses["pool"], deposits=addresses["deposits"],
                        escrow=addresses["escrow"], custodyTransfer=addresses["gateway"]),
        "gateway": dict(mining=addresses["reserve"]),
    }
    read = {addresses[name]: values for name, values in bindings.items()}
    monkeypatch.setattr(report, "_contract", lambda _w3, where, _kind, _at: by_address[where])
    monkeypatch.setattr(report, "_read", lambda node, _at, method: read[node.address][method])

    targets, coordinator, dkg_id = report._targets(None, nodes["income"], 7)

    assert "finalize" in targets[addresses["gateway"]][1]
    assert "mintFromProof" in targets[addresses["reserve"]][1]
    assert "settle" in targets[addresses["escrow"]][1]
    assert addresses["deposits"] not in targets
    assert coordinator == nodes["coordinator"] and dkg_id == epoch


def test_staged_report_binds_canonical_order_and_latest_seal(monkeypatch):
    address = Web3.to_checksum_address(f"0x{1:040x}")
    usd = Web3.to_checksum_address(f"0x{2:040x}")
    block = SimpleNamespace(number=20, timestamp=3 * 86400, hash=b"b" * 32)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda _: block))
    node = SimpleNamespace(address=address, abi=[dict(name=helper, inputs=[]) for helper in
                            (report.HELPER, report.STAGE_HELPER)])
    observations = dict(
        nextSettlementDay=2, processing=False, MAX_EXPENSES=128, stagedExpenseCount=0, stagedExpensePosition=0,
        stagedExpenseCommitment=bytes(32), rewardTokens=[usd], dayIncome=0, expenseRecorded=False,
        settlementDayLimit=2**256 - 1,
    )
    first, last = ["0x" + bytes([byte]).hex() * 32 for byte in (1, 2)]

    def receipt(_w3, digest, *_):
        number = 7 if digest == first else 8
        return dict(payer=address, transaction=digest, wei="100", block_number=str(number), transaction_index="1")

    def read(_node, _height, method, *args):
        if method in (report.STAGE_HELPER, report.HELPER):
            return Web3.keccak(text=canonical([method, args]))
        return observations[method]

    monkeypatch.setattr(report, "_contract", lambda *_: node)
    monkeypatch.setattr(report, "_targets", lambda *_: ({}, None, None))
    monkeypatch.setattr(report, "_receipt", receipt)
    monkeypatch.setattr(report, "_read", read)
    monkeypatch.setattr(report, "encode_args", lambda _, args: args)
    monkeypatch.setattr(report, "read_block", lambda *_: block)
    normal = dict(day="2", transactions=[], units_per_eth={usd: "1"})
    _, old_seal = report.prepare(w3, address, normal, block)
    _, stage = report.prepare(w3, address, dict(day="2", transactions=[last, first]), block, stage=True)
    batch = stage["hash_args"][0]
    assert batch[:3] == ["2", "0", "0x" + "00" * 32]
    assert [row[1] for row in batch[-1]] == [first, last]
    assert batch[4:6] == [str((7 << 32) | 1), str((8 << 32) | 1)]
    report.review(w3, stage, 1, block.number)

    observations.update(stagedExpenseCount=2, stagedExpensePosition=int(batch[5]),
                        stagedExpenseCommitment=b"s" * 32)
    with pytest.raises(ValueError, match="DAO_EXPENSE_BATCH_ORDER"):
        report.review(w3, stage, 1, block.number)
    with pytest.raises(ValueError, match="DAO_EXPENSES_ALREADY_STAGED"):
        report.prepare(w3, address, dict(normal, transactions=[last]), block)
    with pytest.raises(ValueError, match="DAO_REPORT_MISMATCH"):
        report.review(w3, old_seal, 1, block.number)
    _, seal = report.prepare(w3, address, normal, block)
    report.review(w3, seal, 1, block.number)
    assert seal["operation_hash"] != old_seal["operation_hash"]
    observations["settlementDayLimit"] = 3
    report.review(w3, seal, 1, block.number)
    observations["settlementDayLimit"] = 2
    with pytest.raises(ValueError, match="DAO_REPORT_DAY"):
        report.review(w3, seal, 1, block.number)
    with pytest.raises(ValueError, match="DAO_REPORT_DAY"):
        report.review(w3, stage, 1, block.number)


def test_closing_rate_review_requires_all_assets_current_utc_day_and_outstanding_debt(monkeypatch):
    share, usd = [Web3.to_checksum_address(f"0x{i:040x}") for i in (1, 2)]
    block = SimpleNamespace(number=19, timestamp=10 * 86400)
    node = object()
    state = dict(settlementDayLimit=8, nextSettlementDay=8, processing=False, debtHead=1, debtTail=3,
                 closingRatesDay=9, rewardTokens=[share, usd])

    def read(target, number, method):
        assert target is node and number == block.number
        return state[method]

    monkeypatch.setattr(report, "_read", read)
    exact = [10, [share, usd], [10**18, 2 * 10**18]]
    report.validate_closing_rates(node, block, exact)
    for supplied in ([9, *exact[1:]], [10, [usd, share], exact[2]], [10, exact[1], [1]],
                     [10, exact[1], [0, 2]], [10, exact[1], [2**128, 2]]):
        with pytest.raises(ValueError, match="CONFEDERATION_DAO_CLOSING_RATES"):
            report.validate_closing_rates(node, block, supplied)
    for key, value in (("closingRatesDay", 10), ("nextSettlementDay", 7), ("processing", True), ("debtHead", 3)):
        original = state[key]
        state[key] = value
        with pytest.raises(ValueError, match="CONFEDERATION_DAO_CLOSING_RATES"):
            report.validate_closing_rates(node, block, exact)
        state[key] = original
