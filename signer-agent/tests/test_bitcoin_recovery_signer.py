"""The cold-recovery pause fences private signing at the pinned EVM anchors."""

from types import SimpleNamespace as NS

from eth_utils import keccak
import pytest
from web3.exceptions import ContractLogicError

from signer_agent import bitcoin_trust, bitcoin_worker


BATCHES, ESCROW, MODULE, OTHER, ZERO = ("0x" + byte * 40 for byte in "12340")
SCRIPT = "5120" + "91" * 32
PLAN = {"wallet_script": SCRIPT, "batch_registry": BATCHES}


@pytest.fixture
def graph(monkeypatch):
    blocks = {i: NS(number=i, hash=bytes([i]) * 32) for i in (12, 13)}
    values = {
        BATCHES: {"recoveryController": MODULE, "withdrawalEscrow": ESCROW},
        ESCROW: {"recovery": MODULE},
        MODULE: {"escrow": ESCROW, "recoveryHeight": 100,
                 "sourceScriptHash": keccak(bytes.fromhex(SCRIPT)), "paused": False},
    }
    overrides = {}
    checked = []
    migration_checks = []
    failed_proofs = set()

    def contract(address, abi):
        def method(name):
            def configure(*args):
                def call(*, block_identifier):
                    if name == "checkMigration":
                        migration_checks.append((args[0], block_identifier))
                        if (args[0], block_identifier) in failed_proofs:
                            raise ContractLogicError("synthetic noncanonical SPV")
                        return None
                    return overrides.get((address, name, block_identifier), values[address][name])
                return NS(call=call)
            return configure
        functions = type("Functions", (), {"__getattr__": lambda self, name: method(name)})()
        return NS(address=address, functions=functions)

    eth = NS(contract=contract, get_block=lambda value: blocks[13 if value == "latest" else value])
    registry = NS(w3=NS(eth=eth))
    monkeypatch.setattr(bitcoin_trust, "confirmed_block", lambda *args: blocks[12])
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", lambda w3, address, kind, number:
        checked.append((address, kind, number)))
    monkeypatch.setattr(bitcoin_worker, "artifact", lambda kind: {"abi": []})
    records = {"contracts": {"batches": {"address": BATCHES},
                             "escrow": {"address": ESCROW, "artifact": "BitcoinWithdrawalEscrowV2"}}}
    key = {"bitcoin_recovery": {"height": "100"},
           "result": {"recovery_descriptor": {"script_pubkey": SCRIPT}}}
    return NS(registry=registry, records=records, key=key, overrides=overrides, checked=checked, values=values,
              migration_checks=migration_checks, failed_proofs=failed_proofs)


def test_legacy_signer_does_not_read_recovery_contract():
    bitcoin_worker.require_open_recovery_epoch(None, {"result": {}}, PLAN, {})


def test_enrollment_cannot_be_used_without_its_local_recovery_key(graph):
    records = dict(graph.records, bitcoin_recovery={"height": "100"})
    with pytest.raises(ValueError, match="^BTC_RECOVERY_BINDING$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, {"result": {}}, PLAN, records)
    with pytest.raises(ValueError, match="^BTC_RECOVERY_BINDING$"):
        bitcoin_worker.require_open_recovery_epoch(
            graph.registry, graph.key, PLAN, dict(records, bitcoin_recovery={"height": "200"})
        )


def test_open_epoch_requires_both_pinned_anchors(graph):
    bitcoin_worker.require_open_recovery_epoch(graph.registry, graph.key, PLAN, graph.records)
    assert graph.checked == [(MODULE, "BitcoinWithdrawalRecovery", 12),
                             (MODULE, "BitcoinWithdrawalRecovery", 13)]


@pytest.mark.parametrize("height", [12, 13])
def test_pause_at_either_anchor_stops_private_work(graph, height):
    graph.overrides[(MODULE, "paused", height)] = True
    with pytest.raises(ValueError, match="^PAYMENTS_PAUSED_FOR_RECOVERY$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, graph.key, PLAN, graph.records)


@pytest.mark.parametrize("address,method,height,value", [
    (BATCHES, "recoveryController", 12, ZERO),
    (BATCHES, "recoveryController", 13, OTHER),
    (ESCROW, "recovery", 13, OTHER),
    (MODULE, "recoveryHeight", 12, 101),
    (MODULE, "sourceScriptHash", 13, bytes(32)),
])
def test_enrolled_wallet_fails_closed_if_recovery_binding_differs(graph, address, method, height, value):
    graph.overrides[(address, method, height)] = value
    with pytest.raises(ValueError, match="^BTC_RECOVERY_BINDING$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, graph.key, PLAN, graph.records)


@pytest.fixture
def successor(graph):
    wallet_id, wallet_result, root = bytes([20]) * 32, bytes([21]) * 32, bytes([22]) * 32
    script = "5120" + "a4" * 32
    graph.records.update(bitcoin_recovery={"height": "100", "successor_script": script},
                         bitcoin_recovery_rotation={"wallet_id": wallet_id.hex(), "root": root.hex()})
    graph.values[BATCHES].update(walletId=wallet_id, walletResult=wallet_result)
    graph.values[MODULE].update(paused=True, withdrawalsPaused=False, successorWalletId=wallet_id,
                                successorWalletResult=wallet_result, rotationRoot=root,
                                activeScriptHash=keccak(bytes.fromhex(script)))
    txid = bytes(range(32))
    inputs = [{"outpoint": txid.hex() + ":0"}, {"outpoint": txid.hex() + ":1"}]
    return NS(graph=graph, plan=dict(PLAN, wallet_script=script, inputs=inputs), txid=txid,
              key={"result_hash": wallet_result.hex()})


def test_rotated_signer_can_start_but_old_key_stays_closed(successor):
    graph = successor.graph
    bitcoin_worker.require_open_recovery_epoch(graph.registry, successor.key, successor.plan, graph.records)
    assert graph.migration_checks == [(successor.txid[::-1], 12), (successor.txid[::-1], 13)]
    historical = dict(graph.records)
    del historical["bitcoin_recovery_rotation"]
    with pytest.raises(ValueError, match="^PAYMENTS_PAUSED_FOR_RECOVERY$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, graph.key, PLAN, historical)


@pytest.mark.parametrize("address,getter,height,value,error", [
    (MODULE, "rotationRoot", 12, bytes(32), "BTC_RECOVERY_BINDING"),
    (BATCHES, "walletResult", 13, bytes(32), "BTC_RECOVERY_BINDING"),
    (MODULE, "activeScriptHash", 12, bytes(32), "BTC_RECOVERY_BINDING"),
    (MODULE, "withdrawalsPaused", 13, True, "PAYMENTS_PAUSED_FOR_RECOVERY"),
])
def test_successor_blocks_private_work_on_changed_rotation(successor, address, getter, height, value, error):
    graph = successor.graph
    graph.overrides[address, getter, height] = value
    with pytest.raises(ValueError, match=f"^{error}$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, successor.key, successor.plan, graph.records)


def test_displaced_migration_proof_stops_private_work_with_rotation_still_active(successor):
    graph = successor.graph
    graph.failed_proofs.add((successor.txid[::-1], 13))
    with pytest.raises(ValueError, match="^BTC_RECOVERY_ROTATION_PROOF$"):
        bitcoin_worker.require_open_recovery_epoch(graph.registry, successor.key, successor.plan, graph.records)
