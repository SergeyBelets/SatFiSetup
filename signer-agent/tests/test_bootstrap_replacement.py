import json
from types import SimpleNamespace

from eth_account import Account
from hexbytes import HexBytes
import pytest
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TransactionNotFound

from signer_agent import bootstrap_replacement as replacement
from signer_agent import bootstrap_create, bootstrap_parallel
from signer_agent.bootstrap_parallel import recover_identity, restore_signed, save_signed
from signer_agent.bootstrap_recovery import identity_lane, protected_nonce_floor, signed_fields
from signer_agent.operations import Journal


@pytest.fixture
def fixture(tmp_path, monkeypatch, request):
    chain = getattr(request, "param", 1)
    account = Account.create()
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": account.key.hex()}))
    identity.chmod(0o600)
    _, _, lane = identity_lane(identity)
    journal = Journal(tmp_path / "sender.sqlite")
    step = dict(id="send", kind="call", contract="target", method="test", args=[])
    profile = dict(deployment_id="fixture", chain_id=str(chain), steps=[step], evm_confirmations="12")
    journal.put("fixture", dict(profile=profile))
    key = "fixture:send"
    journal.put(key, step)
    sent, receipts, known, estimates = [], {}, {}, []
    state = SimpleNamespace(finalized=100, latest=120, pending=211, nonce=211, base_fee=20,
                            estimate=50000, response_lost=False, block_hashes={})

    def block(tag):
        number = state.latest if tag == "latest" else state.finalized if tag == "finalized" else tag
        return AttributeDict(dict(number=number, hash=HexBytes(state.block_hashes.get(number, number.to_bytes(32, "big"))),
                                  baseFeePerGas=state.base_fee, gasLimit=10_000_000))

    def observed(store, digest):
        if digest not in store:
            raise TransactionNotFound(digest)
        return store[digest]

    def estimate(call, *, block_identifier):
        assert block_identifier == state.latest
        estimates.append(call)
        return state.estimate

    def send(raw):
        digest = Web3.keccak(raw)
        assert lane.get(digest.hex())["status"] == replacement.ALTERNATIVE
        sent.append(digest.hex())
        if state.response_lost:
            raise TimeoutError("RPC offline")
        known[digest.hex()] = dict(hash=digest, blockNumber=None)
        state.pending = 212
        return digest

    def count(_sender, tag):
        if tag == "pending":
            return state.pending
        if tag == "latest":
            return state.nonce
        return 212 if any(row.blockNumber <= tag for row in receipts.values()) else 211

    eth = SimpleNamespace(chain_id=chain, get_block=block, get_transaction_count=count,
                          get_transaction_receipt=lambda digest: observed(receipts, digest),
                          get_transaction=lambda digest: observed(known, digest), send_raw_transaction=send,
                          get_balance=lambda *args, **kwargs: 10**18,
                          get_code=lambda *args, **kwargs: b"", estimate_gas=estimate)
    w3 = SimpleNamespace(eth=eth)
    class FixtureWeb3:
        HTTPProvider = staticmethod(lambda *args, **kwargs: None)
        keccak = staticmethod(Web3.keccak)
        to_hex = staticmethod(Web3.to_hex)
        to_checksum_address = staticmethod(Web3.to_checksum_address)

        def __new__(cls, *args, **kwargs):
            return w3

    monkeypatch.setattr(replacement, "Web3", FixtureWeb3)
    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "100")
    monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "10")
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "100000")
    target = Web3.to_checksum_address("0x" + "42" * 20)
    values = dict(chainId=chain, nonce=211, to=target, gas=55000, type=2, value=4, data="0x1234",
                  maxFeePerGas=20, maxPriorityFeePerGas=2, accessList=[dict(address=target,
                  storageKeys=["0x" + "01" * 32])])
    signed = account.sign_transaction(values)
    original = dict(tx_hash=signed.hash.hex(), raw_transaction=signed.raw_transaction.hex(), nonce="211")

    def forbidden(_):
        raise AssertionError("retry rebroadcast an alternative without explicit confirmation")

    bootstrap = SimpleNamespace(w3=w3, journal=journal, broadcast=forbidden, prefix="fixture")
    save_signed(lane, bootstrap, key, step, original, allow_fee_replacement=True)
    journal.update(key, "BROADCAST", original)

    def command(tx_hash=None, **overrides):
        params = dict(fee="45", priority="3", max_cost="4000000", gas="60000")
        return replacement.replace_pending(identity, "rpc", tx_hash or original["tx_hash"], **dict(params, **overrides))

    def mine(tx_hash, where, status=1):
        receipts[tx_hash] = AttributeDict(dict(transactionHash=HexBytes(tx_hash), status=status,
            blockNumber=where, blockHash=block(where).hash, gasUsed=43000, effectiveGasPrice=16))
        state.nonce = 212
        state.pending = 212

    return SimpleNamespace(chain=chain, identity=identity, account=account, lane=lane, bootstrap=bootstrap,
        journal=journal, step=step,
        key=key, state=state, original=original, sent=sent, known=known, estimates=estimates,
        command=command, mine=mine)


@pytest.mark.parametrize("fixture", [1, 11155111], indirect=True, ids=["mainnet", "sepolia"])
def test_review_before_signing_and_wait_for_replacement_finality(fixture):
    f = fixture
    original = f.original["tx_hash"]
    review = f.command()
    assert review["status"] == "EVM_REPLACEMENT_REVIEW" and f.sent == []
    assert review["replacement"]["worst_case_wei"] == "2700004"
    assert f.estimates[0]["accessList"][0]["storageKeys"] == ["0x" + "01" * 32]
    with pytest.raises(ValueError, match="EVM_REPLACEMENT_FINGERPRINT"):
        f.command(confirmed="wrong")
    submitted = f.command(confirmed=review["fingerprint"])
    assert submitted == f.command(confirmed=review["fingerprint"])
    winner = submitted["tx_hash"]
    assert f.command(winner, confirmed=review["fingerprint"]) == submitted
    assert f.sent == [winner]
    old, new = [signed_fields(f.lane.get(tx)["request"], f.account, f.chain) for tx in (original, winner)]
    assert {key: old[key] for key in replacement.CALL_KEYS} == {key: new[key] for key in replacement.CALL_KEYS}
    f.mine(winner, 105)
    replacements = {}
    assert recover_identity(f.lane, f.bootstrap, f.account, replacements=replacements) == 212
    assert f.lane.get(original)["status"] == replacement.TRACKING
    assert f.journal.get(f.key)["result"]["tx_hash"] == original
    assert list(replacements.values())[0] == dict(status="BROADCAST", tx_hash=winner, original_tx_hash=original,
                                                reason="EVM_REPLACEMENT_PENDING")
    f.state.finalized = 110
    recover_identity(f.lane, f.bootstrap, f.account, replacements=replacements)
    assert f.lane.get(original)["status"] == replacement.SUPERSEDED
    assert f.journal.get(f.key)["result"]["tx_hash"] == winner
    assert protected_nonce_floor(f.bootstrap.w3, f.lane, f.account, f.chain) == 212
    assert f.command()["status"] == "EVM_REPLACEMENT_ALREADY_MINED"


def test_original_can_win_and_response_loss_replays_exact_saved_alternative(fixture):
    f = fixture
    review = f.command()
    f.state.response_lost = True
    lost = f.command(confirmed=review["fingerprint"])
    assert lost["status"] == "EVM_REPLACEMENT_SEND_UNCERTAIN"
    recover_identity(f.lane, f.bootstrap, f.account)
    assert len(f.sent) == 1
    f.state.response_lost = False
    assert f.command(confirmed=review["fingerprint"])["tx_hash"] == lost["tx_hash"]
    assert f.sent == [lost["tx_hash"]] * 2
    f.mine(f.original["tx_hash"], 105)
    f.state.finalized = 110
    recover_identity(f.lane, f.bootstrap, f.account)
    assert restore_signed(f.lane, f.bootstrap, f.key, f.step) == f.original
    assert f.command()["tx_hash"] == f.original["tx_hash"]


@pytest.mark.parametrize("overrides,error", [
    ({"fee": "21"}, "FEE_INCREASE"), ({"priority": "2"}, "FEE_INCREASE"),
    ({"max_cost": "2700003"}, "BUDGET_EXCEEDED"), ({"fee": "101"}, "BUDGET_EXCEEDED"),
    ({"gas": "100001", "max_cost": "5000000"}, "BUDGET_EXCEEDED"),
    ({"fee": "22", "priority": "3"}, "BASE_FEE"),
])
def test_fee_and_gas_limits_stop_the_send(fixture, overrides, error):
    with pytest.raises(ValueError, match="EVM_REPLACEMENT_" + error):
        fixture.command(**overrides)
    assert fixture.sent == []


@pytest.mark.parametrize("fixture", [1, 11155111], indirect=True, ids=["mainnet", "sepolia"])
def test_base_fee_above_original_cap_leaves_nonce_reserved_without_send(fixture):
    f = fixture
    f.state.base_fee = 25
    outcomes = {}
    assert recover_identity(f.lane, f.bootstrap, f.account, replacements=outcomes) == 212
    assert list(outcomes.values()) == [dict(status="BROADCAST", tx_hash=f.original["tx_hash"],
                                           reason="EVM_PENDING_FEE_CAP_BELOW_BASE")]
    assert f.sent == []


@pytest.mark.parametrize("fixture", [11155111], indirect=True, ids=["sepolia"])
def test_sepolia_finalization_does_not_skip_configured_depth(fixture):
    f = fixture
    winner = f.command(confirmed=f.command()["fingerprint"])["tx_hash"]
    f.mine(winner, 115)
    f.state.finalized = 116
    outcomes = {}
    recover_identity(f.lane, f.bootstrap, f.account, replacements=outcomes)
    assert list(outcomes.values())[0]["reason"] == "EVM_REPLACEMENT_PENDING"
    assert f.lane.get(f.original["tx_hash"])["status"] == replacement.TRACKING
    f.state.latest = 126
    recover_identity(f.lane, f.bootstrap, f.account)
    assert f.lane.get(f.original["tx_hash"])["status"] == replacement.SUPERSEDED


@pytest.mark.parametrize("fixture", [11155111], indirect=True, ids=["sepolia"])
@pytest.mark.parametrize("changed_block", [False, True], ids=["canonical", "reorg"])
def test_older_sepolia_promotion_waits_only_while_its_recorded_block_is_canonical(fixture, changed_block):
    from signer_agent.bootstrap_recovery import ReplacementAwaitingFinality

    f = fixture
    original = f.original["tx_hash"]
    winner = f.command(confirmed=f.command()["fingerprint"])["tx_hash"]
    f.mine(winner, 105)
    f.state.finalized = 110
    recover_identity(f.lane, f.bootstrap, f.account)
    # Recreate the persisted shape of a pre-fix promotion at depth alone.
    f.state.finalized = 100
    if changed_block:
        f.state.block_hashes[105] = b"r" * 32
        with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
            protected_nonce_floor(f.bootstrap.w3, f.lane, f.account, f.chain)
    else:
        with pytest.raises(ReplacementAwaitingFinality) as caught:
            protected_nonce_floor(f.bootstrap.w3, f.lane, f.account, f.chain)
        assert caught.value.outcome == dict(status="CONFIRMING", reason="EVM_REPLACEMENT_PENDING",
            original_tx_hash=original, tx_hash=winner, nonce="211")
        f.state.finalized = 105
        assert protected_nonce_floor(f.bootstrap.w3, f.lane, f.account, f.chain) == 212
    assert f.sent == [winner]


@pytest.mark.parametrize("fixture", [11155111], indirect=True, ids=["sepolia"])
@pytest.mark.parametrize("module,group_field", [(bootstrap_create, "create_group"), (bootstrap_parallel, "parallel_group")])
def test_group_waits_for_same_lane_replacement_missing_from_mempool(fixture, module, group_field, monkeypatch):
    f = fixture
    f.state.response_lost = True
    winner = f.command(confirmed=f.command()["fingerprint"])["tx_hash"]
    step = dict(id="later", kind="deploy", gas="200000", **{group_field: "later-group"})
    if module is bootstrap_parallel:
        monkeypatch.setattr(bootstrap_parallel, "validate_targets", lambda *_: None)
    outcome = module.apply_group(f.bootstrap, [step], f.account, f.identity)
    assert outcome == dict(status="BROADCAST", reason="EVM_REPLACEMENT_PENDING",
        tx_hash=winner, original_tx_hash=f.original["tx_hash"], step="later", **{group_field: "later-group"})
    assert f.sent == [winner]
    assert f.journal.get("fixture:later")["result"] == {}


def test_resume_recovers_source_write_after_winner_is_saved(fixture, monkeypatch):
    f = fixture
    review = f.command()
    winner = f.command(confirmed=review["fingerprint"])["tx_hash"]
    f.mine(winner, 105)
    f.state.finalized = 110
    original = Journal.update

    def interrupted(self, key, status, result):
        if self.path == f.journal.path and result.get("tx_hash") == winner:
            raise RuntimeError("source journal offline")
        return original(self, key, status, result)

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "update", interrupted)
        with pytest.raises(RuntimeError, match="source journal offline"):
            recover_identity(f.lane, f.bootstrap, f.account)
    assert f.lane.get(f.original["tx_hash"])["status"] == replacement.SUPERSEDED
    assert f.journal.get(f.key)["result"]["tx_hash"] == f.original["tx_hash"]
    assert restore_signed(f.lane, f.bootstrap, f.key, f.step, f.original)["tx_hash"] == winner
    assert recover_identity(f.lane, f.bootstrap, f.account) == 212
    assert f.journal.get(f.key)["result"]["tx_hash"] == winner
