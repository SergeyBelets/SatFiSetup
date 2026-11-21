"""External DKG/approval observations obey enrolled depth and durable anchors."""
from types import SimpleNamespace

from hexbytes import HexBytes
from eth_account import Account
import pytest
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TransactionNotFound

from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import Journal


class ReadEth:
    def __init__(self, chain_id):
        self.chain_id, self.block_number, self.finalized = chain_id, 100, 85
        self.changed = {}
    def get_block(self, number):
        if number == "latest":
            number = self.block_number
        elif number == "finalized":
            number = self.finalized
        return AttributeDict(dict(number=number, timestamp=1000 + number,
            hash=HexBytes(self.changed.get(number, int(number).to_bytes(32, "big")))))


def fixture(tmp_path, *, chain=11155111, depth="12"):
    b = Bootstrap.__new__(Bootstrap)
    b.prefix = "read-finality"
    b.profile = dict(steps=[], evm_confirmations=depth)
    b.w3 = SimpleNamespace(eth=ReadEth(chain))
    b.journal = Journal(tmp_path / "bootstrap.sqlite")
    b.contracts, b.values, b.calls = {}, {}, []
    b.check_external_components = lambda: None
    b.manifest = lambda: dict(values=dict(b.values))
    b.observe = lambda method, number: number >= 95 if method == "ready" else number
    def fn(spec):
        def call(*, block_identifier):
            b.calls.append((spec["method"], block_identifier))
            number = b.w3.eth.block_number if block_identifier == "latest" else block_identifier
            return b.observe(spec["method"], number)
        return SimpleNamespace(call=call)
    b.fn = fn
    return b


def wait_step(**kwargs):
    return dict(id="gate", kind="wait", contract="external", method="ready", args=[], equals=True, **kwargs)


def read_step():
    return dict(id="read", kind="read", outputs=[dict(name="observed", contract="external", method="value", args=[])])


def install(b, *steps):
    b.profile["steps"] = list(steps)
    b.journal.put(b.prefix, dict(profile=b.profile))


@pytest.mark.parametrize("chain,safe", [(1, 85), (11155111, 89), (31337, 89)])
def test_direct_reads_use_enrolled_depth_and_mainnet_finality(tmp_path, chain, safe):
    b = fixture(tmp_path, chain=chain)
    assert b.read_number() == safe
    assert b.read(b.fn(dict(method="value"))) == safe
    assert b.calls == [("value", safe)]


@pytest.mark.parametrize("chain", [11155111, 31337])
@pytest.mark.parametrize("depth", [None, "1"])
def test_legacy_depth_one_retains_latest_call_shape(tmp_path, chain, depth):
    b = fixture(tmp_path, chain=chain)
    if depth is None:
        del b.profile["evm_confirmations"]
    else:
        b.profile["evm_confirmations"] = depth
    assert b.read_number() == "latest"
    assert b.read(b.fn(dict(method="value"))) == 100
    assert b.calls == [("value", "latest")]


@pytest.mark.parametrize("chain", [1, 11155111, 31337])
def test_latest_only_external_completion_cannot_open_wait_barrier(tmp_path, chain):
    b = fixture(tmp_path, chain=chain)
    install(b, wait_step(), dict(id="dependent", kind="call"))
    assert b.apply(dry_run=True)["status"] == "AWAITING_PARTICIPANTS"
    with pytest.raises(KeyError):
        b.journal.get(b.prefix + ":dependent")
    b.w3.eth.block_number, b.w3.eth.finalized = 111, 100
    assert b.apply(dry_run=True)["status"] == "DRY_RUN"
    saved = b.journal.get(b.prefix + ":gate")
    assert saved["status"] == "CONFIRMED"
    assert saved["result"]["read_anchor"]["number"] == "100"


def test_wait_and_outputs_share_one_snapshot_even_when_head_moves(tmp_path):
    b = fixture(tmp_path)
    original = b.observe
    def observe(method, number):
        b.w3.eth.block_number = 200
        return True if method == "ready" else original(method, number)
    b.observe = observe
    install(b, wait_step(outputs=read_step()["outputs"]))
    assert b.apply()["status"] == "VERIFIED"
    assert b.calls == [("ready", 89), ("value", 89)]
    assert b.values == dict(observed="89")


@pytest.mark.parametrize("kind", ["wait", "read"])
def test_confirmed_observation_survives_legitimate_later_value_change(tmp_path, kind):
    b = fixture(tmp_path)
    b.observe = lambda method, number: True if method == "ready" else 7
    step = wait_step(outputs=read_step()["outputs"]) if kind == "wait" else read_step()
    install(b, step)
    assert b.apply()["status"] == "VERIFIED"
    calls = list(b.calls)
    b.w3.eth.block_number = 150
    b.observe = lambda method, number: False if method == "ready" else 99
    assert b.apply(verify=True)["status"] == "VERIFIED"
    assert b.values == dict(observed="7")
    assert b.calls == calls  # Historical approvals need not remain true forever.


@pytest.mark.parametrize("kind", ["wait", "read"])
@pytest.mark.parametrize("mutation", ["hash", "depth"])
def test_saved_read_anchor_reorg_or_depth_regression_blocks_resume(tmp_path, kind, mutation):
    b = fixture(tmp_path)
    b.observe = lambda method, number: True if method == "ready" else 7
    step = wait_step() if kind == "wait" else read_step()
    install(b, step)
    b.apply()
    if mutation == "hash":
        b.w3.eth.changed[89] = b"x" * 32
    else:
        b.w3.eth.block_number = 95
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        b.apply(verify=True)


@pytest.mark.parametrize("kind", ["wait", "read"])
def test_legacy_unanchored_observation_must_match_current_confirmed_view(tmp_path, kind):
    b = fixture(tmp_path)
    step = wait_step() if kind == "wait" else read_step()
    install(b, step)
    b.journal.put(b.prefix + ":" + step["id"], step)
    old = dict(outputs={} if kind == "wait" else dict(observed="100"))
    b.journal.update(b.prefix + ":" + step["id"], "CONFIRMED", old)
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        b.apply(verify=True)
    saved = b.journal.get(b.prefix + ":" + step["id"])
    assert saved["status"] == "RECOVERY_REQUIRED"
    assert saved["result"] == old
    assert b.values == {}
    assert b.apply() == dict(status="RECOVERY_REQUIRED", step=step["id"],
                            reason="POSTCONDITION_REVIEW_REQUIRED")


@pytest.mark.parametrize("kind", ["wait", "read"])
def test_matching_legacy_observation_is_anchored_without_a_transaction(tmp_path, kind):
    b = fixture(tmp_path)
    b.observe = lambda method, number: True if method == "ready" else 7
    step = wait_step() if kind == "wait" else read_step()
    install(b, step)
    b.journal.put(b.prefix + ":" + step["id"], step)
    b.journal.update(b.prefix + ":" + step["id"], "CONFIRMED", dict(outputs={} if kind == "wait" else dict(observed="7")))
    assert b.apply(verify=True)["status"] == "VERIFIED"
    assert b.journal.get(b.prefix + ":" + step["id"])["result"]["read_anchor"]["number"] == "89"


def test_reorg_during_wait_outputs_is_not_saved_as_success(tmp_path):
    b = fixture(tmp_path)
    def observe(method, number):
        if method == "value":
            b.w3.eth.changed[number] = b"x" * 32
        return True if method == "ready" else 7
    b.observe = observe
    install(b, wait_step(outputs=read_step()["outputs"]))
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        b.apply()
    assert b.journal.get(b.prefix + ":gate")["status"] != "CONFIRMED"


def recovery_fixture(tmp_path):
    from signer_agent.bootstrap_parallel import save_signed
    source = fixture(tmp_path)
    source.prefix = "source%_scope"
    gate = wait_step()
    tx_steps = [dict(id=f"dependent{i}", kind="call") for i in range(2)]
    install(source, gate, *tx_steps)
    source.journal.put(source.prefix + ":gate", gate)
    source.journal.update(source.prefix + ":gate", "CONFIRMED", dict(
        read_anchor=dict(number="89", hash=source.w3.eth.get_block(89).hash.hex())))
    identity_journal = Journal(tmp_path / "identity-nonces.sqlite")
    account = Account.from_key("0x" + "00" * 31 + "01")
    for nonce, step in enumerate(tx_steps):
        key = source.prefix + ":" + step["id"]
        source.journal.put(key, step)
        signed = account.sign_transaction(dict(chainId=11155111, nonce=nonce,
            gasPrice=1, gas=21000, to="0x" + "11" * 20, value=0))
        save_signed(identity_journal, source, key, step, dict(nonce=str(nonce),
            tx_hash=Web3.keccak(signed.raw_transaction).hex(), raw_transaction=signed.raw_transaction.hex()))
    receiver = fixture(tmp_path)
    receiver.prefix = "other-deployment"
    receiver.profile["evm_confirmations"] = "1"  # Source depth must win.
    receiver.sent = []
    receiver.broadcast = receiver.sent.append
    def absent(tx):
        raise TransactionNotFound(tx)
    receiver.w3.eth.get_transaction_receipt = absent
    receiver.w3.eth.get_transaction = absent
    return source, receiver, identity_journal, account


@pytest.mark.parametrize("mutation", ["hash", "depth"])
def test_cross_journal_recovery_checks_source_read_anchor_before_any_rebroadcast(tmp_path, mutation):
    from signer_agent.bootstrap_parallel import recover_identity
    source, receiver, identity_journal, account = recovery_fixture(tmp_path)
    if mutation == "hash":
        receiver.w3.eth.changed[89] = b"x" * 32
    else:
        receiver.w3.eth.block_number = 95
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        recover_identity(identity_journal, receiver, account)
    assert receiver.sent == []


def test_source_anchor_check_is_cached_per_pass_and_excludes_other_prefixes(tmp_path, monkeypatch):
    import signer_agent.bootstrap_parallel as parallel
    source, receiver, identity_journal, account = recovery_fixture(tmp_path)
    # SQL wildcard characters in a deployment ID must not match other plans.
    source.journal.put("sourceXXscope:orphan", dict(id="orphan", kind="wait"))
    source.journal.update("sourceXXscope:orphan", "CONFIRMED", dict(
        read_anchor=dict(number="89", hash="ff" * 32)))
    checks = []
    original = parallel.verify_source_anchors
    def counted(w3, journal, prefix):
        checks.append(prefix)
        return original(w3, journal, prefix)
    monkeypatch.setattr(parallel, "verify_source_anchors", counted)
    parallel.recover_identity(identity_journal, receiver, account)
    assert checks == [source.prefix]
    assert len(receiver.sent) == 2
    parallel.recover_identity(identity_journal, receiver, account)
    assert checks == [source.prefix] * 2  # Cache never outlives one recovery pass.


def test_recovery_does_not_invent_anchors_for_legacy_read_observations(tmp_path):
    from signer_agent.bootstrap_parallel import recover_identity
    source, receiver, identity_journal, account = recovery_fixture(tmp_path)
    source.journal.update(source.prefix + ":gate", "CONFIRMED", {})
    receiver.w3.eth.block_number = 0
    recover_identity(identity_journal, receiver, account)
    assert len(receiver.sent) == 2


def test_failed_legacy_wait_migration_blocks_cross_journal_pending_rebroadcast(tmp_path):
    from signer_agent.bootstrap_parallel import recover_identity
    source, receiver, identity_journal, account = recovery_fixture(tmp_path)
    source.journal.update(source.prefix + ":gate", "CONFIRMED", {})
    # Legacy latest-only approval is now false at the enrolled confirmed view.
    with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
        source.apply()
    assert source.journal.get(source.prefix + ":gate")["status"] == "RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match="^PRIOR_TRANSACTION_RECOVERY_REQUIRED$"):
        recover_identity(identity_journal, receiver, account)
    assert receiver.sent == []
