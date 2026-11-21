"""Production gating and pre-signing consent; accounting is tested in EVM."""
from types import SimpleNamespace

import pytest

from signer_agent.gas_treasury import GasTreasuryClient, SCHEMA


def client(chain_id):
    item = GasTreasuryClient.__new__(GasTreasuryClient)
    item.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=chain_id))
    item.manifest = dict(chain_id=str(chain_id), domain="0x" + "11" * 32)
    item.registry = SimpleNamespace(address="0x" + "22" * 20)
    return item


@pytest.mark.parametrize("chain_id", [1, 11155111, 31338])
def test_payment_profile_missing_cannot_be_overridden_by_json(chain_id):
    item = client(chain_id)
    item.manifest.update(qualified=True, status="PASS", production_ready=True)
    with pytest.raises(ValueError, match="^GAS_SPV_PROFILE_NOT_QUALIFIED$"):
        item.prepare_fund("0x" + "33" * 32)


@pytest.mark.parametrize("field,value", [
    ("schema", "old"), ("action", "transfer"), ("chain_id", "1"),
    ("registry", "0x" + "44" * 20), ("domain", "0x" + "55" * 32),
])
def test_cross_domain_or_arbitrary_action_rejected_before_identity_access(field, value):
    item = client(31337)
    request = item._base("claim")
    assert request["schema"] == SCHEMA
    request[field] = value
    with pytest.raises(ValueError, match="^GAS_REQUEST_BINDING$"):
        item.submit(request, "/must/not/read/identity")


def test_claim_cannot_send_native_value():
    item = client(31337)
    item._binding = lambda: object()
    item._treasury = lambda address, block: SimpleNamespace(address=address)
    request = dict(item._base("claim"), treasury="0x" + "66" * 20,
                   payee="0x" + "77" * 20, value="1")
    with pytest.raises(ValueError, match="^GAS_CLAIM_VALUE$"):
        item.submit(request, "/must/not/read/identity")


def test_submit_payment_has_independent_production_gate():
    item = client(1)
    item._binding = lambda: object()
    with pytest.raises(ValueError, match="^GAS_SPV_PROFILE_NOT_QUALIFIED$"):
        item.submit(item._base("fund"), "/must/not/read/identity")


def test_admission_prepares_exact_abi_without_payment(monkeypatch):
    item = client(31337)
    item.manifest["state_view"] = "0x"+"66"*20
    item._binding = lambda: SimpleNamespace(number=1)
    item.service_profile = lambda b: None
    item._anchor = lambda b: None
    captured = {}
    class Functions:
        def admit(self, *args):
            captured["args"] = args
            return SimpleNamespace(call=lambda tx, **kw: captured.update(transaction=tx))
    item.registry.functions = Functions()
    item.w3.eth.contract = lambda **kw: object()
    item._call = lambda target, name, block, *args: {
        "snapshot": [bytes.fromhex("11"*32), 7, True, False],
        "permissions": [9, 1, bytes(32), 0, 0, False],
        "configuration": ["treasury", "0x"+"77"*20],
    }[name]
    terms = dict(jobId="0x"+"22"*32, semanticKey="0x"+"33"*32,
        federationIdHash="0x"+"44"*32, payer="0x"+"55"*20, budgetScope="0x"+"66"*32, serviceDeadline="1000")
    result = item.prepare_admit(terms, "900")
    assert result["action"] == "admit" and result["value"] == "0"
    assert captured["args"][2:] == (7, 900, 9)
    assert captured["transaction"] == {"from": "0x"+"77"*20}
