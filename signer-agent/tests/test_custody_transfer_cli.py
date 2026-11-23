from types import SimpleNamespace

import pytest

from signer_agent import custody_transfer_cli, zk_profiles, zk_spv, zk_submission
from signer_agent.operations import Journal


@pytest.fixture
def handover(tmp_path, monkeypatch):
    block = SimpleNamespace(number=42, hash=b"h" * 32)
    args = SimpleNamespace(identity_file=tmp_path / "identity.json", state=tmp_path / "ops.sqlite",
                           id="handover", rpc="http://127.0.0.1:18545", send=True)
    account = SimpleNamespace(address="0x" + "01" * 20)
    adapter, read_depth = SimpleNamespace(address="destination-adapter"), {"depth": 6}
    checks = []

    def read(value):
        def call(*, block_identifier):
            assert block_identifier == block.number
            return value() if callable(value) else value
        return lambda: SimpleNamespace(call=call)

    def contract(address, abi):
        if address == "destination-deposits":
            assert abi == "BitcoinDepositRegistry"
            return SimpleNamespace(functions=SimpleNamespace(depth=read(lambda: read_depth["depth"]),
                                                             spv=read(adapter.address)))
        assert address == adapter.address and abi == "ZkTestnet4SpvAdapter"
        return SimpleNamespace(functions=SimpleNamespace(state=read("destination-state")))

    w3 = SimpleNamespace(eth=SimpleNamespace(contract=contract,
                                             get_code=lambda *a, **kw: b"runtime",
                                             get_block=lambda _: block))
    context = {"block": block, "fingerprint": "reviewed-handover", "legacy_gateway": False,
               "invite": {"chain_id": "11155111", "bitcoin_network": "testnet4", "mode": "backed",
                          "evm_confirmations": "1", "bitcoin_confirmations": "6"},
               "nodes": {"mining": SimpleNamespace(functions=SimpleNamespace(deposits=read("destination-deposits")))}}
    values = [[51], b"p" * 32, b"raw", 1, b"b" * 32, [], 0]
    sent = []

    def bootstrap(profile, path):
        assert path == args.state
        sent.append(profile)
        return SimpleNamespace(apply_confirmed=lambda _: {"status": "VERIFIED"},
                               journal=SimpleNamespace(get=lambda key: {"result": {"tx_hash": key}}))

    def inspect_binding(web3, state, policy, at, *, adapter_address):
        assert (web3, state, policy, at, adapter_address) == (
            w3, "destination-state", context["invite"]["bitcoin_policy"], block.number, adapter.address)
        return None, adapter, {}, {}

    monkeypatch.setattr(custody_transfer_cli, "account_at", lambda _: account)
    monkeypatch.setattr(custody_transfer_cli, "artifact", lambda kind: {"abi": kind})
    monkeypatch.setattr(custody_transfer_cli, "Bootstrap", bootstrap)
    monkeypatch.setattr(custody_transfer_cli, "checked_runtime", lambda w, a, k, n, **_: checks.append((a, k, n)))
    monkeypatch.setattr(zk_profiles, "profile", lambda _: {"adapter": "ZkTestnet4SpvAdapter"})
    monkeypatch.setattr(zk_spv, "inspect_binding", inspect_binding)

    def finalize():
        return custody_transfer_cli.transact(w3, context, args, "BitcoinCustodyTransferV2", "gateway",
                                             "finalize", values)

    return SimpleNamespace(context=context, finalize=finalize, adapter=adapter, args=args, account=account,
                           values=values, sent=sent, checks=checks, depth=read_depth)


def test_zk_finalize_returns_receipt_from_retried_attempt(handover, monkeypatch):
    handover.context["invite"]["bitcoin_policy"] = {"version": "2"}

    def retry(registry, profile, adapter, header, send_attempt):
        assert registry.account is handover.account and registry.transaction_journal == handover.args.state
        assert adapter is handover.adapter and header == handover.values[4]
        base = profile["deployment_id"]
        original = Journal(handover.args.state).get(base + ":request")["request"]
        assert original["args"] == profile["steps"][0]["args"]
        return send_attempt(registry, dict(profile, deployment_id=base + "-zk-attempt-1"))

    monkeypatch.setattr(zk_submission, "submit", retry)
    result = handover.finalize()
    assert result["tx_hash"].endswith("-zk-attempt-1:send")
    assert handover.checks == [("destination-deposits", "BitcoinDepositRegistry", 42),
                               ("destination-adapter", "ZkTestnet4SpvAdapter", 42)]


def test_zk_finalize_rejects_mismatched_deposit_depth_before_sending(handover):
    handover.context["invite"]["bitcoin_policy"] = {"version": "2"}
    handover.depth["depth"] = 7
    with pytest.raises(ValueError, match="ZK_CONSUMER_BINDING"):
        handover.finalize()
    assert not handover.sent


def test_relay_finalize_preserves_original_transaction_lane(handover):
    handover.context["invite"]["bitcoin_policy"] = {"version": "1"}
    assert handover.finalize()["tx_hash"].endswith("-handover:send")
    assert len(handover.sent) == 1 and not handover.checks
