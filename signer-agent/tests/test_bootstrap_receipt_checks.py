"""A consumed deployment allowance is an historical effect, not a live binding."""
import json
from types import SimpleNamespace

import pytest
from eth_account import Account
from hexbytes import HexBytes
from web3.datastructures import AttributeDict

from signer_agent.bootstrap import Bootstrap, artifact_fingerprint
from signer_agent.bootstrap_plan import build, spv_payment_deployment
from signer_agent.bitcoin_trust import previous_artifact
from signer_agent.contract_provenance import _creation_input
from signer_agent.operations import Journal
from test_ethereum_mainnet import ethereum_profile


def _bootstrap(tmp_path, *, historical=123, current=0):
    instance = Bootstrap.__new__(Bootstrap)
    instance.prefix = "receipt-bound-checks"
    instance.journal = Journal(tmp_path / "bootstrap.sqlite")
    instance.profile = dict(evm_confirmations="12", steps=[])
    receipt = AttributeDict(dict(status=1, blockNumber=80, blockHash=b"r" * 32))
    calls = []
    instance.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1,
        get_block=lambda number: SimpleNamespace(hash=receipt.blockHash),
        get_transaction_receipt=lambda tx: AttributeDict(dict(receipt, transactionHash=HexBytes(tx)))))
    def fn(check):
        def call(*, block_identifier):
            calls.append(block_identifier)
            return historical if block_identifier == 80 else current
        return SimpleNamespace(call=call)
    instance.fn = fn
    instance.resolve = lambda value: value
    instance.read = lambda fn: fn.call(block_identifier="current-finalized")
    instance.check_external_components = lambda: None
    instance.outputs = lambda step, result: None
    instance.manifest = lambda: {}
    instance.verify_receipt = lambda result, **kwargs: receipt
    instance.bitcoin_ready = lambda *args, **kw: dict(status="IN_PROGRESS")
    return instance, receipt, calls


def _check(*, at_receipt=True, equals="123"):
    return dict(contract="usd", method="allowance", args=[], equals=equals, at_receipt=at_receipt)


@pytest.mark.parametrize("initial_status", ["CONFIRMED", "CONFIRMING", "BROADCAST"])
def test_resume_uses_approval_receipt_after_deployment_consumes_allowance(tmp_path, initial_status):
    instance, receipt, calls = _bootstrap(tmp_path)
    approval = dict(id="approve_fee", kind="call", checks=[_check()])
    relay = dict(id="bitcoin_ready", kind="bitcoin_ready")
    instance.profile["steps"] = [approval, relay]
    instance.journal.put(instance.prefix, dict(profile=instance.profile))
    instance.journal.put(instance.prefix + ":approve_fee", approval)
    account = Account.from_key("0x" + "00" * 31 + "01")
    signed = account.sign_transaction(dict(chainId=1, nonce=0, to=account.address, value=0, gas=21000, gasPrice=1))
    result = dict(tx_hash=signed.hash.hex(), raw_transaction=signed.raw_transaction.hex(), nonce="0")
    instance.journal.update(instance.prefix + ":approve_fee", initial_status, result)
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(dict(private_key="0x" + "00" * 31 + "01")))
    identity.chmod(0o600)
    # Even a resume from a pending journal reads the already confirmed approval
    # receipt and must not try to approve/sign/send again or demand live allowance.
    assert instance.apply(str(identity))["status"] == "AWAITING_BITCOIN_RELAY"
    assert instance.apply(str(identity))["status"] == "AWAITING_BITCOIN_RELAY"
    assert calls == [80, 80]
    assert instance.journal.get(instance.prefix + ":approve_fee")["status"] == "CONFIRMED"


def test_receipt_checks_do_not_weaken_current_persistent_binding_checks(tmp_path):
    instance, receipt, calls = _bootstrap(tmp_path)
    instance.verify_checks(dict(checks=[_check(), _check(at_receipt=False, equals="0")]), receipt)
    assert calls == [80, "current-finalized"]
    with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
        instance.verify_checks(dict(checks=[_check(at_receipt=False)]), receipt)


def test_formation_postcondition_compares_only_persistent_fields(tmp_path):
    instance, receipt, calls = _bootstrap(tmp_path,
        current=("conf-id", ["fed-a", "fed-b"], 2, 3, 5, 4, b"\x02"))
    check = dict(contract="confederations", method="getConfederation", args=["conf-id"],
                 indices=[0, 1, 4, 5], equals=["conf-id", ["fed-a", "fed-b"], "5", "4"])
    instance.verify_checks(dict(checks=[check]), receipt)
    assert calls == ["current-finalized"]
    for field, value in ((1, ["fed-b", "fed-a"]), (2, "4"), (3, "3")):
        expected = list(check["equals"])
        expected[field] = value
        with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
            instance.verify_checks(dict(checks=[dict(check, equals=expected)]), receipt)


def test_wrong_historical_allowance_rejected_even_if_current_value_matches(tmp_path):
    instance, receipt, _ = _bootstrap(tmp_path, historical=0, current=123)
    with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
        instance.verify_checks(dict(checks=[_check()]), receipt)


@pytest.mark.parametrize("historical", [False, True])
def test_spv_payment_registry_activation_preserves_original_receipt(tmp_path, historical):
    instance, receipt, _ = _bootstrap(tmp_path)
    instance.profile["confederation_id"] = "conf-a"
    step = spv_payment_deployment("conf-a", historical=historical)
    original = json.loads(json.dumps(step))
    values = {c["method"]: c["equals"] for c in step["checks"]}
    live_enabled = {"value": True}
    calls = []

    def fn(check):
        def call(*, block_identifier):
            method = check["method"]
            calls.append((method, block_identifier))
            if method == "enabled" and block_identifier != receipt.blockNumber:
                return live_enabled["value"]
            return values[method]
        return SimpleNamespace(call=call)

    instance.fn = fn
    instance.verify_checks(step, receipt)
    assert step == original
    assert calls[-1] == ("enabled", receipt.blockNumber)
    assert all(block == "current-finalized" for _, block in calls[:-1])
    with pytest.raises(ValueError, match="^BINDING_CHECK_RECEIPT_REQUIRED$"):
        instance.verify_checks(step)
    values["enabled"] = True
    live_enabled["value"] = False
    with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
        instance.verify_checks(step, receipt)
    if not historical:
        # A near-match custom configuration does not gain receipt-only semantics.
        values["enabled"] = False
        live_enabled["value"] = True
        with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
            instance.verify_checks(dict(step, id="another-registry"), receipt)


@pytest.mark.parametrize("kind,archive,arguments", [
    ("ULRStaking", "ULRStaking", ["0x" + "11" * 20, "0x" + "22" * 20, 200_000, 1]),
    ("BitcoinMintAdmission", "BitcoinMintAdmission", ["0x" + "11" * 20, "0x" + "22" * 20]),
    ("BitcoinMintAdmission", "BitcoinMintAdmissionRetirementV1", ["0x" + "11" * 20, "0x" + "22" * 20]),
    ("MiningPoolV1", "MiningPoolV1", ["old-mp", "0x" + "11" * 20, "0x" + "22" * 20, "MP", "MP"]),
    ("ConfederationBitcoinReserve", "ConfederationBitcoinReserve",
     ["old-s", "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20]),
])
@pytest.mark.parametrize("legacy_hash", [False, True])
def test_bootstrap_previous_build_pin_requires_exact_canonical_creation(tmp_path, monkeypatch, kind, archive, arguments,
                                                                   legacy_hash):
    from signer_agent import bootstrap as module
    from web3 import Web3

    instance, receipt, _ = _bootstrap(tmp_path)
    archived = previous_artifact(archive)
    current = dict(archived, deployedBytecode={"object": "0x6000"})
    monkeypatch.setattr(module, "artifact", lambda name: current)
    addr = "0x" + "55" * 20
    receipt = AttributeDict(dict(receipt, contractAddress=addr))
    runtime = HexBytes(archived["deployedBytecode"]["object"])
    tx_hash = HexBytes(b"t" * 32)
    transaction = dict(to=None, hash=tx_hash, blockHash=receipt.blockHash,
                       input=HexBytes(_creation_input(archived, arguments)))
    instance.w3.eth.get_code = lambda *args, **kwargs: runtime
    instance.w3.eth.get_transaction = lambda saved: transaction
    pin = artifact_fingerprint(archived, legacy=legacy_hash)
    item = dict(address=addr, artifact=kind, artifact_hash=pin, runtime_hash=Web3.keccak(runtime).hex())
    step = dict(kind="deploy", artifact=kind, args=instance.wire(arguments))
    result = dict(tx_hash=tx_hash.hex(), contract=item)
    instance.contracts, instance.values = {"legacy": item}, {}
    del instance.manifest
    instance.profile.update(mode="integration", bitcoin_network="regtest")

    instance.verify_deployment_pin(step, result, receipt)
    assert instance.manifest()["contracts"] == {"legacy": item}
    with pytest.raises(ValueError, match="^ARTIFACT_HASH_MISMATCH$"):
        instance.verify_deployment_pin(step, dict(result, contract=dict(item, artifact_hash="bad-pin")), receipt)
    with pytest.raises(ValueError, match="^BTC_CREATION_RECEIPT_MISMATCH$"):
        instance.verify_deployment_pin(step, result, AttributeDict(dict(receipt, contractAddress="0x" + "44" * 20)))
    transaction["input"] += b"\x00"
    with pytest.raises(ValueError, match="^BTC_CANONICAL_CREATION_NOT_FOUND$"):
        instance.verify_deployment_pin(step, result, receipt)


@pytest.mark.parametrize("changed_at", [1, 2])
def test_receipt_anchor_reorg_before_or_after_historical_read_is_rejected(tmp_path, changed_at):
    instance, receipt, _ = _bootstrap(tmp_path)
    reads = []
    def get_block(number):
        reads.append(number)
        return SimpleNamespace(hash=b"x" * 32 if len(reads) >= changed_at else receipt.blockHash)
    instance.w3.eth.get_block = get_block
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        instance.verify_checks(dict(checks=[_check()]), receipt)


def test_historical_check_cannot_fall_back_without_receipt(tmp_path):
    instance, _, calls = _bootstrap(tmp_path)
    with pytest.raises(ValueError, match="^BINDING_CHECK_RECEIPT_REQUIRED$"):
        instance.verify_checks(dict(checks=[_check()]))
    assert calls == []


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_receipt_snapshot_policy_requires_boolean(tmp_path, flag):
    instance, receipt, calls = _bootstrap(tmp_path)
    with pytest.raises(ValueError, match="^BINDING_CHECK_SNAPSHOT_POLICY$"):
        instance.verify_checks(dict(checks=[_check(at_receipt=flag)]), receipt)
    assert calls == []


def test_mainnet_factory_allowances_are_exact_then_consumed_at_receipt():
    profile = ethereum_profile()
    steps = {step["id"]: step for step in build(profile)["steps"]}
    for role in ("federation", "confederation"):
        approved = steps["approve_" + role + "_fee"]["checks"]
        spent = [c for c in steps["deploy_" + role]["checks"] if c.get("at_receipt")]
        assert len(approved) == len(spent) == 1
        assert approved[0]["equals"] == profile["deployment_fee"]["amount"]
        assert spent[0]["equals"] == "0"
        assert approved[0]["at_receipt"] is spent[0]["at_receipt"] is True
        assert approved[0]["args"] == spent[0]["args"]


def test_admission_operator_transfer_preserves_initial_receipt_and_live_pool_binding(tmp_path):
    profile = ethereum_profile()
    step = next(row for row in build(profile)["steps"] if row["id"] == "mint_admission")
    checks = {check["method"]: check for check in step["checks"]}
    assert checks["operator"]["at_receipt"] is True
    assert not checks["pool"].get("at_receipt", False)
    instance, receipt, _ = _bootstrap(tmp_path)
    old_operator = profile["operator"]
    new_operator = "0x0000000000000000000000000000000000000042"
    pool = "0x0000000000000000000000000000000000000043"
    current = {"operator": new_operator, "pool": pool}
    calls = []
    instance.resolve = lambda value: pool if value == "$mining" else value
    def fn(check):
        def call(*, block_identifier):
            method = check["method"]
            calls.append((method, block_identifier))
            return old_operator if method == "operator" and block_identifier == receipt.blockNumber else current[method]
        return SimpleNamespace(call=call)
    instance.fn = fn
    instance.verify_checks(step, receipt)
    assert calls == [("pool", "current-finalized"), ("operator", receipt.blockNumber)]
    current["pool"] = new_operator
    with pytest.raises(ValueError, match="^BINDING_CHECK_FAILED$"):
        instance.verify_checks(step, receipt)
