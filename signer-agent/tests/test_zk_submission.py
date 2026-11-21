from types import SimpleNamespace

import pytest

from signer_agent import zk_submission
from signer_agent.operations import Journal


@pytest.fixture
def flow(tmp_path, monkeypatch):
    from signer_agent import bootstrap, bootstrap_recovery

    profile = dict(deployment_id="btc-board-proof", evm_rpc="http://127.0.0.1:18545",
                   steps=[dict(id="send", args=["public-proof"])])
    block = SimpleNamespace(number=25, hash=b"a" * 32)
    registry = SimpleNamespace(transaction_journal=tmp_path / "ops.sqlite",
                               identity_file=tmp_path / "identity.json", account=SimpleNamespace(address="operator"),
                               w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: block)))
    adapter = SimpleNamespace(address="pinned-adapter")
    header = b"h" * 32
    status = SimpleNamespace(root="0x11", final=False, simulation=True, sent=[], verified=[])
    state = Journal(registry.transaction_journal)
    monkeypatch.setattr(zk_submission, "_materialized_root", lambda *_: (status.root, block))

    def simulate(*_, block_identifier):
        assert block_identifier == block.number
        if not status.simulation:
            raise ValueError("SIMULATION_REJECTED")

    monkeypatch.setattr(bootstrap, "Bootstrap", lambda *_, **__: SimpleNamespace(
        fn=lambda _: SimpleNamespace(call=simulate)))

    def ack(identity, tx_hash, rpc):
        assert identity == registry.identity_file and rpc == profile["evm_rpc"]
        if not status.final:
            raise ValueError("RECOVERY_REVERT_NOT_FINALIZED")
        status.verified.append(tx_hash)

    monkeypatch.setattr(bootstrap_recovery, "acknowledge_revert", ack)

    def send(_, attempted):
        assert attempted["steps"] == profile["steps"]
        status.sent.append(attempted["deployment_id"])
        key = attempted["deployment_id"] + ":send"
        return state.put(key, attempted["steps"][0])

    def invoke():
        return zk_submission.submit(registry, profile, adapter, header, send)

    return SimpleNamespace(profile=profile, state=state, status=status, invoke=invoke)


def test_old_attempt_is_replayed_until_root_changes_and_revert_is_final(flow):
    base = flow.profile["deployment_id"]
    assert flow.invoke()["status"] == "PREPARED"
    flow.state.update(base + ":send", "CONFIRMING", {"tx_hash": "original"})
    flow.status.root = "0x22"
    assert flow.invoke()["status"] == "CONFIRMING"
    assert flow.status.sent == [base, base]
    flow.state.update(base + ":send", "REVERTED", {"tx_hash": "original"})
    with pytest.raises(ValueError, match="RECOVERY_REVERT_NOT_FINALIZED"):
        flow.invoke()
    assert flow.status.sent == [base, base]
    flow.status.final = True
    result = flow.invoke()
    assert result["id"] == base + "-zk-attempt-1:send"
    assert flow.invoke()["id"] == result["id"]
    assert flow.status.verified == ["original"]
    assert flow.state.get(base + ":send")["status"] == "REVERTED"


def test_retry_requires_new_root_and_an_unchanged_successful_simulation(flow):
    base = flow.profile["deployment_id"]
    flow.invoke()
    flow.state.update(base + ":send", "REVERTED", {"tx_hash": "original"})
    flow.status.final = True
    with pytest.raises(ValueError, match="ZK_PROOF_ROOT_UNCHANGED"):
        flow.invoke()
    flow.status.root = "0x33"
    flow.status.simulation = False
    with pytest.raises(ValueError, match="SIMULATION_REJECTED"):
        flow.invoke()
    assert flow.status.sent == [base]
    assert not flow.status.verified


def test_pre_upgrade_revert_is_adopted_without_losing_its_evidence(flow):
    base = flow.profile["deployment_id"]
    flow.state.put(base + ":send", flow.profile["steps"][0])
    flow.state.update(base + ":send", "REVERTED", {"tx_hash": "legacy"})
    flow.status.final = True
    flow.invoke()
    assert flow.status.sent == [base + "-zk-attempt-1"]
    assert flow.status.verified == ["legacy"]
    prior = flow.state.get("zk-proof-reverted-" + base)["request"]
    assert prior["tx_hash"] == "legacy" and prior["previous_root"] is None
