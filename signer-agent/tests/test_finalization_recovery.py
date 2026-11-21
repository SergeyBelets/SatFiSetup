from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

from signer_agent import finalization_recovery
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import recover_identity, save_signed
from signer_agent.operations import Journal, canonical
from signer_agent.transaction_wait import TransactionPending


@pytest.mark.parametrize("scenario", ["already_marked", "first_observation", "not_final", "different_result", "other_step"])
def test_only_canonical_matching_finalization_revert_unblocks_nonce(tmp_path, monkeypatch, scenario):
    account = Account.create()
    address = Web3.to_checksum_address("0x" + "11" * 20)
    chain_id, nonce, session = 1, 211, 41
    signature, digest = b"s" * 64, b"d" * 32
    method = "finalizeSession"
    if scenario == "other_step":
        method = "authorizeOperation"
    values = Bootstrap.wire([session, signature, digest])
    operation = "signer-" + Web3.keccak(text=canonical([address, method, values])).hex()
    step = dict(kind="call", id="send", contract="target", method=method, args=values)
    source = Journal(tmp_path / "source.sqlite")
    lane = Journal(tmp_path / "lane.sqlite")
    source.put(operation, dict(profile=dict(chain_id=str(chain_id), steps=[step], contracts=dict(
        target=dict(artifact="FROSTCoordinator", address=address)))))
    key = operation + ":send"
    source.put(key, step)
    payload = (Web3.keccak(text="finalizeSession(uint256,bytes,bytes32)")[:4]
               + encode(["uint256", "bytes", "bytes32"], [session, signature, digest]))
    raw = account.sign_transaction(dict(chainId=chain_id, nonce=nonce, to=address, value=0,
        gas=100_000, maxFeePerGas=10, maxPriorityFeePerGas=1, data=payload)).raw_transaction
    result = dict(nonce=str(nonce), tx_hash=Web3.keccak(raw).hex(), raw_transaction=raw.hex())
    receipt = SimpleNamespace(blockNumber=92, blockHash=HexBytes("0x" + "ab" * 32), status=0,
                              transactionHash=Web3.keccak(raw))
    final = SimpleNamespace(number=91 if scenario == "not_final" else 92,
                            hash=HexBytes("0x" + "bc" * 32) if scenario == "not_final" else receipt.blockHash)

    def read(result):
        def call(*, block_identifier):
            assert block_identifier == receipt.blockNumber
            return result
        return SimpleNamespace(call=call)

    abi = [{"type": "function", "name": "finalizeSession", "inputs": [
        {"name": "sessionId", "type": "uint256"}, {"name": "aggregatedSignature", "type": "bytes"},
        {"name": "messageHash", "type": "bytes32"}], "outputs": [], "stateMutability": "nonpayable"}]
    decoder = Web3().eth.contract(abi=abi)
    coordinator = SimpleNamespace(decode_function_input=decoder.decode_function_input,
        functions=SimpleNamespace(isFinalized=lambda _: read(True), getResult=lambda _: read(
            (signature if scenario != "different_result" else b"x" * 64, digest))))

    def read_block(block):
        if block in ("finalized", final.number):
            return final
        assert block == receipt.blockNumber
        return SimpleNamespace(number=92, hash=receipt.blockHash)

    def receipt_at(tx_hash):
        assert tx_hash == result["tx_hash"]
        return receipt

    def transaction_count(sender, number):
        assert (sender, number) == (account.address, final.number)
        return nonce + 1

    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=chain_id, get_block=read_block,
        get_transaction_receipt=receipt_at, get_transaction_count=transaction_count,
        contract=lambda **_: coordinator))
    monkeypatch.setattr(finalization_recovery, "artifact", lambda _: dict(abi=abi))
    runtime_checks = []
    monkeypatch.setattr(finalization_recovery, "checked_runtime", lambda *args: runtime_checks.append(args[1:]))
    bootstrap = SimpleNamespace(journal=source, w3=w3,
        broadcast=lambda _: pytest.fail("recovery must only read the canonical receipt"))
    save_signed(lane, bootstrap, key, step, result)
    if scenario in ("already_marked", "other_step"):
        source.update(key, "REVERTED", dict(result, block_number="92", block_hash=receipt.blockHash.hex()))

    if scenario == "not_final":
        with pytest.raises(TransactionPending) as pending:
            recover_identity(lane, bootstrap, account)
        assert pending.value.result["tx_hash"] == result["tx_hash"]
        assert pending.value.result["reason"] == "FROST_FINALIZATION_REVERT_AWAITING_FINALITY"
    elif scenario in ("different_result", "other_step"):
        with pytest.raises(ValueError, match="PRIOR_TRANSACTION_RECOVERY_REQUIRED"):
            recover_identity(lane, bootstrap, account)
    else:
        assert recover_identity(lane, bootstrap, account) == nonce + 1
        evidence = lane.get(result["tx_hash"])
        assert evidence["status"] == "CONFIRMED"
        assert int(evidence["result"]["receipt_status"]) == 0
        assert recover_identity(lane, bootstrap, account) == nonce + 1
    assert source.get(key)["status"] == "REVERTED"
    assert runtime_checks == ([] if scenario == "other_step" else [(address, "FROSTCoordinator", 92)])
    if scenario in ("different_result", "not_final", "other_step"):
        assert lane.get(result["tx_hash"])["status"] == "PREPARED"
