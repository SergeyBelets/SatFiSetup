import hashlib
import json
from types import SimpleNamespace

from eth_account import Account
from hexbytes import HexBytes
import pytest
from web3 import Web3
from web3.exceptions import TransactionNotFound

from signer_agent import bitcoin_trust, bootstrap_nonce_legacy as legacy, bootstrap_replacement, finalization_recovery
from signer_agent.bootstrap import Bootstrap, main
from signer_agent.bootstrap_parallel import recover_identity
from signer_agent.bootstrap_recovery import identity_lane
from signer_agent.operations import Journal, canonical


@pytest.fixture
def prepare(tmp_path, monkeypatch):
    def make(*, value=0, source_nonce="211", source_runtime=None, chain=11155111,
             method="finalizeSession", lane_status="PREPARED", wrong_signer=False, trailing=False,
             pending=False):
        account = Account.create()
        identity = tmp_path / "signer.json"
        identity.write_text(json.dumps({"private_key": account.key.hex()}))
        identity.chmod(0o600)
        _, lock, lane = identity_lane(identity)
        journal = Journal(tmp_path / "signer.transactions.sqlite")
        address = Web3.to_checksum_address("0x" + "11" * 20)
        signature, message_hash = b"s" * 64, b"m" * 32
        inputs = [
            {"type": "uint256", "name": "sessionId"},
            {"type": "bytes", "name": "aggregatedSignature"},
            {"type": "bytes32", "name": "messageHash"},
        ] if method == "finalizeSession" else []
        abi = [dict(type="function", name=method, inputs=inputs, outputs=[], stateMutability="nonpayable")]
        decoder = Web3().eth.contract(address=address, abi=abi)
        args = [41, signature, message_hash] if method == "finalizeSession" else []
        final = SimpleNamespace(number=92, hash=HexBytes(b"b" * 32))
        runtime = b"\x60\x00"
        compiled = dict(abi=abi, deployedBytecode=dict(object=runtime.hex()))
        deployment = "signer-" + Web3.keccak(text=canonical([address, method, Bootstrap.wire(args)])).hex()
        key = deployment + ":send"
        step = dict(id="send", kind="call", contract="target", method=method, args=Bootstrap.wire(args))
        journal.put(deployment, dict(version="1", profile=dict(deployment_id=deployment, chain_id=str(chain),
            evm_confirmations="12" if chain == 1 else "1",
            steps=[step], contracts=dict(target=dict(artifact="FROSTCoordinator", address=address,
            runtime_hash=source_runtime or Web3.keccak(runtime).hex())))))
        journal.put(key, step)

        function = getattr(decoder.functions, method)
        data = function(*args)._encode_transaction_data()
        if trailing:
            data += "00"
        signer = Account.create() if wrong_signer else account
        signed = signer.sign_transaction(dict(chainId=chain, nonce=211, to=address, value=value,
            gas=100_000, maxFeePerGas=20, maxPriorityFeePerGas=2, data=data))
        tx_hash = signed.hash.hex()
        result = dict(tx_hash=tx_hash, raw_transaction=signed.raw_transaction.hex(), nonce=source_nonce)
        if not pending:
            result.update(block_number="92", block_hash=final.hash.hex())
        journal.update(key, "BROADCAST" if pending else "REVERTED", result)
        receipt = SimpleNamespace(blockNumber=92, blockHash=final.hash, status=0, transactionHash=signed.hash)
        lane.put(tx_hash, dict(chain_id=str(chain), tx_hash=tx_hash, raw_transaction=result["raw_transaction"]))
        if lane_status != "PREPARED":
            lane.update(tx_hash, lane_status, {"block_number": "92", "block_hash": final.hash.hex()})

        def read(value):
            def call(*, block_identifier):
                assert block_identifier == final.number
                return value
            return SimpleNamespace(call=call)

        def read_code(target, *, block_identifier):
            if target == address:
                assert block_identifier == final.number
                return runtime
            assert target == account.address
            return b""

        def get_receipt(digest):
            if pending:
                raise TransactionNotFound(digest)
            return receipt

        coordinator = SimpleNamespace(decode_function_input=decoder.decode_function_input,
            functions=SimpleNamespace(**{method: function}, isFinalized=lambda _: read(True),
                getResult=lambda _: read((signature, message_hash))))
        w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=chain, contract=lambda **_: coordinator,
            get_block=lambda block: SimpleNamespace(number=104, hash=HexBytes(b"a" * 32),
                baseFeePerGas=10, gasLimit=10_000_000) if block in ("latest", 104) else final,
            get_code=read_code, get_transaction_receipt=get_receipt,
            estimate_gas=lambda *_args, **_kwargs: 100_000,
            get_balance=lambda *_args, **_kwargs: 10**18,
            get_transaction_count=lambda *_: 211 if pending else 212))

        class FixtureWeb3:
            HTTPProvider = staticmethod(lambda *args, **kwargs: None)
            keccak = staticmethod(Web3.keccak)
            to_checksum_address = staticmethod(Web3.to_checksum_address)
            to_hex = staticmethod(Web3.to_hex)

            def __new__(cls, *args, **kwargs):
                return w3

        monkeypatch.setattr(legacy, "Web3", FixtureWeb3)
        monkeypatch.setattr(bootstrap_replacement, "Web3", FixtureWeb3)
        monkeypatch.setattr(legacy, "artifact", lambda _: compiled)
        monkeypatch.setattr(bitcoin_trust, "artifact", lambda _: compiled)
        monkeypatch.setattr(finalization_recovery, "artifact", lambda _: compiled)

        def command(source=None):
            return legacy.bind_legacy_nonce(identity, source or journal.path, "rpc", tx_hash)

        return SimpleNamespace(account=account, identity=identity, lane=lane, lock=lock, journal=journal,
            result=result, key=key, tx_hash=tx_hash, w3=w3, command=command)

    return make


@pytest.mark.parametrize("lane_status", ["PREPARED", "CONFIRMED"])
def test_binding_preserves_original_transaction_and_lane_state(prepare, lane_status):
    f = prepare(lane_status=lane_status)
    original = f.lane.get(f.tx_hash)
    source = f.journal.get(f.key)
    bound = f.command()
    assert bound == f.command() == dict(status=legacy.BOUND, tx_hash=f.tx_hash, nonce="211", operation=f.key)
    updated = f.lane.get(f.tx_hash)
    assert updated["status"] == original["status"]
    assert updated["result"] == original["result"]
    assert updated["request"] == dict(original["request"], nonce="211", operation=f.key,
        source_journal=str(f.journal.path.resolve()),
        step_hash=hashlib.sha256(canonical(source["request"]).encode()).hexdigest())
    assert f.journal.get(f.key) == source
    with f.lane.db() as db:
        assert [row["status"] for row in db.execute(
            "SELECT status FROM events WHERE operation=? AND status=?", (f.tx_hash, legacy.BOUND)
        )] == [legacy.BOUND]


def test_bound_legacy_finalization_reconciles_nonce(prepare):
    f = prepare()
    f.command()
    bootstrap = SimpleNamespace(w3=f.w3, journal=f.journal,
        broadcast=lambda _: pytest.fail("binding and finalized receipt recovery are read-only on chain"))
    assert recover_identity(f.lane, bootstrap, f.account) == 212
    assert f.lane.get(f.tx_hash)["status"] == "CONFIRMED"
    assert f.lane.get(f.tx_hash)["result"]["receipt_status"] == "0"
    assert f.journal.get(f.key)["status"] == "REVERTED"


def test_legacy_pending_call_can_be_reviewed_after_binding(prepare, monkeypatch):
    f = prepare(chain=1, pending=True)
    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "50")
    monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "5")
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "100000")

    def review():
        return bootstrap_replacement.replace_pending(f.identity, "rpc", f.tx_hash,
            fee="25", priority="3", max_cost="2500000")

    with pytest.raises(ValueError, match="EVM_LEGACY_NONCE_SOURCE_REQUIRED"):
        review()
    f.command()
    evidence = review()
    assert evidence["status"] == "EVM_REPLACEMENT_REVIEW"
    assert evidence["replacement"]["nonce"] == "211"
    assert evidence["replacement"]["operation"] == f.key
    assert f.lane.get(f.tx_hash)["status"] == "PREPARED"


@pytest.mark.parametrize("overrides, code", [
    ({"value": 1}, "EVM_LEGACY_NONCE_UNSUPPORTED"),
    ({"method": "authorizeOperation"}, "EVM_LEGACY_NONCE_UNSUPPORTED"),
    ({"wrong_signer": True}, "EVM_SIGNED_TRANSACTION_BINDING"),
    ({"source_nonce": "210"}, "EVM_SIGNED_TRANSACTION_BINDING"),
    ({"source_runtime": "aa" * 32}, "EVM_SIGNED_TRANSACTION_BINDING"),
    ({"trailing": True}, "EVM_SIGNED_TRANSACTION_BINDING"),
])
def test_unproven_binding_keeps_legacy_row_unchanged(prepare, overrides, code):
    f = prepare(**overrides)
    before = f.lane.get(f.tx_hash)
    with pytest.raises(ValueError, match=code):
        f.command()
    assert f.lane.get(f.tx_hash) == before


def test_runtime_must_match_coordinator_build_at_canonical_block(prepare):
    f = prepare()
    before = f.lane.get(f.tx_hash)
    code = f.w3.eth.get_code
    f.w3.eth.get_code = lambda *_args, **_kwargs: b"\x60\xff"
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        f.command()

    def reorg(*args, **kwargs):
        f.w3.eth.get_block = lambda _: SimpleNamespace(number=92, hash=HexBytes(b"r" * 32))
        return code(*args, **kwargs)

    f.w3.eth.get_code = reorg
    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        f.command()
    assert f.lane.get(f.tx_hash) == before


def test_correct_journal_and_chain_are_required(prepare, tmp_path, capsys):
    f = prepare()
    before = f.lane.get(f.tx_hash)
    with pytest.raises(ValueError, match="EVM_LEGACY_NONCE_SOURCE_REQUIRED"):
        f.command(Journal(tmp_path / "other.sqlite").path)
    with pytest.raises(ValueError, match="EVM_LEGACY_NONCE_SOURCE_REQUIRED"):
        f.command(f.lane.path)
    f.w3.eth.chain_id = 1
    with pytest.raises(ValueError, match="CHAIN_MISMATCH"):
        f.command()
    assert f.lane.get(f.tx_hash) == before
    f.w3.eth.chain_id = 11155111
    assert main(["bind-legacy-nonce", "--identity-file", str(f.identity), "--source-journal",
                 str(f.journal.path), "--rpc", "rpc", "--tx-hash", f.tx_hash]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == legacy.BOUND
