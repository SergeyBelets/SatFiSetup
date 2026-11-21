"""Executed status is authenticated, session-bound, and never requires a new signature."""
from types import SimpleNamespace
import pytest
from signer_agent.operation_status import executed_operation


@pytest.mark.parametrize("receipt,complete",[(False,False),(True,False),(False,True)])
def test_registry_consumed_authorization_alone_is_not_exact_registration(operation,monkeypatch,receipt,complete):
    from signer_agent import address_registration
    monkeypatch.setattr(address_registration,"review",lambda *_args,**_kwargs:dict(complete=complete))
    operation.values["registrationReceipts"]=receipt
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_POSTCONDITION"):
        executed_operation(operation.w3,operation.target,"BitcoinAddressRegistry",operation.intent,7)


def test_incident_receipt_uses_status_record_and_never_registration_receipt(operation,monkeypatch):
    from signer_agent import address_registration
    monkeypatch.setattr(address_registration,'review',lambda *_a,**_k:dict(complete=True))
    operation.intent['hash_method']='getStatusOperationHash';operation.values['statusReceipts']=2
    assert executed_operation(operation.w3,operation.target,'BitcoinAddressRegistry',operation.intent,7)
    assert ('statusReceipts',7) in operation.reads
    assert all(name!='registrationReceipts' for name,_ in operation.reads)


@pytest.fixture
def operation(monkeypatch):
    op, dkg, result = b"o" * 32, b"d" * 32, b"r" * 32
    values = dict(authorization="auth", executedOperations=True, operationTargets="target",
        coordinator="coordinator", consumedSessions=True, registry="registry", dkgRegistry="registry",
        dkgId=dkg, sessionDKGId=dkg, resultHash=result, sessionDKGResult=result,
        getAuthorizationResult=(0, op, True, 0, "target", 3, 3), defaultVerifier="verifier")
    reads, proofs = [], []
    class Functions:
        def __getattr__(self, name):
            def fn(*args):
                def call(*, block_identifier):
                    reads.append((name, block_identifier))
                    return values[name]
                return SimpleNamespace(call=call)
            return fn
    def contract(address, abi=None):
        return SimpleNamespace(address=address, functions=Functions())
    w3 = SimpleNamespace(eth=SimpleNamespace(contract=contract))
    # The in-memory contract double above ignores its ABI.
    monkeypatch.setattr("signer_agent.operation_status.artifact", lambda _kind: {"abi": []})
    monkeypatch.setattr("signer_agent.operation_status.checked_runtime", lambda *a: None)
    monkeypatch.setattr("signer_agent.contract_provenance.prove_creation", lambda *a: proofs.append(a[1:3]))
    return SimpleNamespace(w3=w3, target=contract("target"), intent=dict(operation_hash=op.hex(), coordinator="coordinator"),
                           values=values, proofs=proofs, reads=reads)


def status(f):
    return executed_operation(f.w3, f.target, "BitcoinBatchRegistry", f.intent, 42, session=7)


def test_consumed_operation_rechecks_origin_and_one_confirmed_session(operation):
    assert status(operation)
    assert operation.proofs == [("auth", "DKGAuthorization"), ("target", "BitcoinBatchRegistry"),
        ("verifier", "FROSTVerifier"), ("coordinator", "FROSTCoordinator"), ("registry", "DKGCoordinator")]
    assert all(block == 42 for _, block in operation.reads)


@pytest.mark.parametrize("field,value", [("operationTargets", "other"), ("coordinator", "other"),
    ("consumedSessions", False), ("dkgId", b"x" * 32), ("resultHash", b"x" * 32),
    ("registry", "other"), ("getAuthorizationResult", (0, b"x" * 32, True, 0, "target", 3, 3)),
    ("getAuthorizationResult", (0, b"o" * 32, False, 0, "target", 3, 3))])
def test_wrong_operation_session_or_wallet_cannot_be_reported_executed(operation, field, value):
    operation.values[field] = value
    with pytest.raises(ValueError, match="POLICY_MISMATCH"):
        status(operation)


def test_forged_constructor_storage_cannot_report_execution(operation, monkeypatch):
    def rejected(*args):
        raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")
    monkeypatch.setattr("signer_agent.contract_provenance.prove_creation", rejected)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        status(operation)


def test_unexecuted_operation_continues_normal_parameter_review(operation):
    operation.values["executedOperations"] = False
    assert status(operation) is False
    assert operation.proofs == []


def test_executed_dao_tariff_binds_confirmed_session_and_reviewed_authority(operation, monkeypatch):
    f = operation
    op = bytes.fromhex(f.intent["operation_hash"])
    helper = "getApproveTariffOperationHash"
    f.values.update(income="income", getApproveTariffOperationHash=op)
    f.target.abi = [dict(type="function", name=helper, inputs=[dict(type="tuple", components=[
        dict(type="uint64"), dict(type="uint16"), dict(type="uint256")])])]
    f.intent.update(hash_method=helper, hash_args=[["60", "100", "1000"]])
    monkeypatch.setattr("signer_agent.contract_provenance.prove_creation",
                        lambda *_: {"block_number": "1"})
    def check():
        return executed_operation(f.w3, f.target, "ConfederationNFTPass", f.intent, 42,
                                  session=7, dao_authorization="auth")

    assert check() is True
    f.values["consumedSessions"] = False
    with pytest.raises(ValueError, match="POLICY_MISMATCH"):
        check()
