"""Executed status is authenticated, session-bound, and never requires a new signature."""
from types import SimpleNamespace
import pytest
from web3 import Web3
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
    ("dkgId", b"x" * 32), ("resultHash", b"x" * 32),
    ("registry", "other"), ("getAuthorizationResult", (0, b"x" * 32, True, 0, "target", 3, 3)),
    ("getAuthorizationResult", (0, b"o" * 32, False, 0, "target", 3, 3))])
def test_wrong_operation_session_or_wallet_cannot_be_reported_executed(operation, field, value):
    operation.values[field] = value
    with pytest.raises(ValueError, match="POLICY_MISMATCH"):
        status(operation)


@pytest.mark.parametrize("different", [None, "hash", "target", "session", "unbound", "verifier"])
def test_concurrent_session_reports_only_the_same_executed_operation(operation, different):
    zero, target, creator = [Web3.to_checksum_address(f"0x{n:040x}") for n in range(3)]
    operation.target.address = target
    group = b"g" * 32
    session = [7, creator, group, b"o" * 32, True, 2, 2, 100, True, zero,
               1, 0, 0, 0, 0, target, 0, 0, bytes(32), 0]
    if different == "hash": session[3] = b"x" * 32
    if different == "target": session[15] = creator
    if different == "session": session[0] = 9
    if different == "unbound": session[4] = False
    if different == "verifier": session[9] = creator
    operation.values.update(operationTargets=target, consumedSessions=False, getSession=session,
                            threshold=2, groupPubkey=group)
    if different is None:
        assert status(operation)
        assert ("getAuthorizationResult", 42) not in operation.reads
    else:
        with pytest.raises(ValueError, match="POLICY_MISMATCH"):
            status(operation)


def test_pass_tariff_repeat_uses_its_income_authority(operation):
    operation.values["income"] = "income"
    assert executed_operation(operation.w3, operation.target, "ConfederationNFTPass", operation.intent, 42, session=7)
    assert ("income", 42) in operation.reads


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
