"""Historical balance/roster and signer-policy checks with explicit RPC/provenance fixtures.

Actual treasury/staking transfers and authorization consumption are tested in ULRTreasury.t.sol.
"""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from eth_utils.abi import collapse_if_tuple
from web3 import Web3

from signer_agent import ulr_treasury as policy
from signer_agent.bootstrap import artifact
from signer_agent.dkg_protocol import Manifest
from signer_agent.operations import canonical

ORIGINAL_AUTHORITY_CONTEXT = policy.authority_context


def addr(n):
    return Web3.to_checksum_address(f"0x{n:040x}")


@pytest.fixture
def env(monkeypatch):
    day = 101
    blocks = {
        3: SimpleNamespace(number=3, timestamp=(day + 1) * 86400 - 1, hash=b"a" * 32),
        4: SimpleNamespace(number=4, timestamp=(day + 1) * 86400, hash=b"b" * 32, parentHash=b"a" * 32),
        5: SimpleNamespace(number=5, timestamp=(day + 1) * 86400 + 10, hash=b"c" * 32, gasLimit=2**24),
    }
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda n: blocks[5 if n == "latest" else n]),
                         codec=Web3().codec)
    staking = SimpleNamespace(address=addr(1))
    registry = Web3().eth.contract(address=addr(2), abi=artifact("ULRCustodianSnapshots")["abi"])
    treasury = SimpleNamespace(address=addr(3))
    factory = SimpleNamespace(address=addr(4))
    token = SimpleNamespace(address=addr(5))
    validator = SimpleNamespace(address=addr(6))
    auth = SimpleNamespace(address=addr(7))
    owners = [addr(10), addr(20), addr(30)]
    manifest = Manifest(31337, addr(90), b"s" * 32, 1, "EVM_AUTH", (owners[0], owners[1]),
                        (b"a" * 32, b"b" * 32), 2)
    bundle = dict(manifest.to_dict(), role="conf_ceremony", onchain_board=addr(99))
    pin = dict(version="1", chain_id="31337", staking=staking.address, snapshots=registry.address,
               confirmations="1", treasuries=[treasury.address])
    view = SimpleNamespace(wallet={owners[0]: 100_000 * 10**18, owners[1]: 700_000 * 10**18, owners[2]: 100},
                           staked={owners[0]: 200_000 * 10**18, owners[1]: 0, owners[2]: 0},
                           roster=[owners[0]], scope=b"q" * 32, nonce=0, complete=False, calls=[], public=None)
    conf = [None] * 19
    conf[7], conf[8], conf[-1] = auth.address, validator.address, True
    inputs = dict(day=str(day), snapshot_block="3", inventory_evidence_hash="0x" + "44" * 32,
                  owners=list(reversed(owners)), treasuries=[treasury.address])
    objects = {x.address: x for x in (staking, registry, treasury, factory, token, validator, auth)}

    def contract(_, kind, target):
        return objects[target]

    def read(target, at, method, *args):
        view.calls.append((target.address, at, method, args))
        if method == "getPublishDayOperationHash":
            abi = next(f for f in registry.abi if f.get("name") == method)
            types = [collapse_if_tuple(x) for x in abi["inputs"]]
            return Web3.keccak(w3.codec.encode(types + ["uint256"], [*args, view.nonce]))
        if method == "balanceOf":
            if args[0] == staking.address:
                return sum(view.staked.values())
            if args[0] == token.address:
                return policy.SUPPLY - sum(view.wallet.values()) - sum(view.staked.values())
            return view.wallet[args[0]]
        if method == "stakedBalance":
            return view.staked[args[0]]
        if method == "daySnapshots":
            return view.receipt
        values = dict(CUSTODIAN_THRESHOLD=200000*10**18,minimumStakeAmount=1,firstEligibleDay=day, stakeToken=token.address, treasuries=treasury.address,
            totalSupply=policy.SUPPLY, totalStaked=sum(view.staked.values()),
            scope=view.scope, getConfederation=conf, validator=validator.address, getCustodians=view.roster,
            authorization=auth.address, coordinator=addr(80), registry=manifest.registry, dkgId=manifest.session_id,
            getParticipants=list(manifest.participants), threshold=2, executedOperations=view.complete,
            operationTargets=registry.address, intents=view.public)
        return values[method]

    monkeypatch.setattr(policy, "configuration", lambda *_: (registry, staking))
    monkeypatch.setattr(policy, "authority_context", lambda *_: auth)
    monkeypatch.setattr(policy, "treasury_context", lambda *_, **__: (registry, staking, treasury, factory, "A", conf))
    monkeypatch.setattr(policy, "contract", contract)
    monkeypatch.setattr(policy, "proven", lambda _, kind, target, *args: objects[target])
    monkeypatch.setattr(policy, "read", read)
    return SimpleNamespace(w3=w3, pin=pin, inputs=inputs, view=view, owners=owners, blocks=blocks,
                           registry=registry, staking=staking, treasury=treasury, bundle=bundle, auth=auth,
                           objects=objects)


def prepare(env):
    args, evidence = policy.snapshot_arguments(env.w3, env.pin, env.inputs, 5)
    intent = policy.intent_for(env.w3, env.registry, "ULRCustodianSnapshots", "getPublishDayOperationHash",
        args, env.auth.address, "conf_ceremony", 5, ulr_snapshot_inputs=deepcopy(env.inputs))
    return args, evidence, intent


@pytest.mark.parametrize("count", [51, 65, 300])
def test_snapshot_supports_active_residuals_beyond_old_masks(env, count):
    owners = [addr(1000 + i) for i in range(count)]
    env.inputs["owners"] = owners
    env.view.roster = owners[49:]
    env.view.wallet = {owner: (200_000 * 10**18 if i < 49 else 200_000 * 10**18 // (count - 49))
                       for i, owner in enumerate(owners)}
    env.view.staked = {owner: 0 for owner in owners}
    args, evidence = policy.snapshot_arguments(env.w3, env.pin, env.inputs, 5)
    assert args[1] == owners
    assert len(args[2]) == count
    assert sum(args[2]) <= 10_000_000 * 10**18
    assert args[3][0][1] == b"".join(i.to_bytes(2, "big") for i in range(49, count))
    assert json.loads(evidence)["version"] == "4"


def test_large_snapshot_board_commitment_binds_exact_intent(env, cli_env, tmp_path, monkeypatch, capsys):
    from signer_agent import onchain_transport

    owners = [addr(1000 + i) for i in range(300)]
    env.inputs["owners"] = owners
    env.view.roster = owners
    env.view.wallet = dict.fromkeys(owners, 100 * 10**18)
    env.view.staked = dict.fromkeys(owners, 0)
    _, _, intent = prepare(env)
    published = policy.board_intent(dict(intent, session="19"))
    assert "ulr_snapshot_intent_sha256" in published
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(intent))
    assert policy.resolve_board_intent(published, path) == dict(intent, session="19")
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(env.bundle))
    board = SimpleNamespace(address=addr(99))
    monkeypatch.setattr(onchain_transport, "board_at", lambda *_: board)
    read = policy.read
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args:
        env.owners[0] if target is board and method == "operator" else read(target, at, method, *args))
    event = SimpleNamespace(process_receipt=lambda *a, **kw: [{"args": {"sessionId": 19}}])
    env.objects[addr(80)] = SimpleNamespace(address=addr(80), events=SimpleNamespace(SessionCreated=lambda: event))
    env.w3.eth.get_code = lambda *_: b"fixture code"
    env.w3.eth.get_transaction_receipt = lambda _: {}
    transact = cli_env.executor.transact
    cli_env.executor.transact = lambda *args: dict(transact(*args), tx_hash="fixture")
    assert cli_env.cli.main(["propose", "--rpc", "unused", "--config", "unused", "--bundle", str(bundle),
        "--intent", str(path), "--sender", env.owners[0], "--deadline", str(103 * 86400), "--send"]) == 0
    assert json.loads(cli_env.calls[-1][2][1]) == published
    assert json.loads(capsys.readouterr().out)["snapshot_intent_sha256"] == published["ulr_snapshot_intent_sha256"]
    intent["hash_args"][2][0] = "101"
    path.write_text(json.dumps(intent))
    with pytest.raises(ValueError, match="ULR_SNAPSHOT_INTENT_HASH_MISMATCH"):
        policy.resolve_board_intent(published, path)


def test_snapshot_uses_wallet_plus_own_stake_at_same_historical_block(env):
    args, evidence, _ = prepare(env)
    assert args[1] == env.owners[:2]
    assert args[2] == [300_000 * 10**18, 700_000 * 10**18]
    assert args[3][0][1] == b"\x00\x00"
    assert args[0][3] == hashlib.sha256(evidence).digest()
    observations = json.loads(evidence)["holders"]
    assert observations[0]["staked"] == str(200_000 * 10**18)
    assert all(at == 3 for _, at, method, _ in env.view.calls if method in ("balanceOf", "stakedBalance", "getCustodians"))


def test_large_holders_remain_in_denominator_even_if_not_active_in_confederation(env):
    args, _, _ = prepare(env)
    assert sum(args[2]) == 1_000_000 * 10**18
    assert args[3][0][1] == b"\x00\x00"


def test_active_custodian_below_threshold_keeps_actual_weight(env):
    env.view.wallet[env.owners[0]] = 50_000 * 10**18
    env.view.staked[env.owners[0]] = 0
    args, _, _ = prepare(env)
    assert args[1] == env.owners[:2]
    assert args[2] == [50_000 * 10**18, 700_000 * 10**18]
    assert args[3][0][1] == b"\x00\x00"


def test_active_roster_adds_missing_low_balance_member_to_inventory(env):
    env.inputs["owners"] = [env.owners[1]]
    env.view.wallet[env.owners[0]] = 50_000 * 10**18
    env.view.staked[env.owners[0]] = 0
    env.view.wallet[env.owners[2]] = 0
    args, evidence, _ = prepare(env)
    assert args[1] == env.owners[:2]
    assert json.loads(evidence)["holders"][0]["active_custodian"] is True


def test_omitting_a_holder_breaks_fixed_supply_reconciliation(env):
    env.inputs["owners"].remove(env.owners[1])
    with pytest.raises(ValueError, match="ULR_HOLDER_INVENTORY_INCOMPLETE"):
        prepare(env)


def test_calldata_floor_over_network_limit_is_rejected(env):
    owners = [addr(1000 + i) for i in range(policy.MAX_HOLDERS)]
    indices = b"".join(i.to_bytes(2, "big") for i in range(len(owners)))
    allocations = [(i.to_bytes(32, "big"), indices, b"r" * 32) for i in range(1, 65)]
    args = [(101, 3, b"a" * 32, b"e" * 32), owners, [100] * len(owners), allocations]
    with pytest.raises(ValueError, match="ULR_SNAPSHOT_TRANSACTION_GAS"):
        policy.snapshot_calldata_limit(env.w3, env.registry, args, b"o" * 32, 5)


def test_known_treasury_cannot_be_omitted_from_global_active_inventory(env):
    env.pin["treasuries"].append(addr(777))
    with pytest.raises(ValueError, match="COMPLETE_TREASURY_INVENTORY_REQUIRED"):
        prepare(env)


@pytest.mark.parametrize("timestamp", [102 * 86400, 103 * 86400])
def test_snapshot_must_be_exact_last_block_of_day(env, timestamp):
    env.blocks[3].timestamp = timestamp
    with pytest.raises(ValueError, match="LAST_BLOCK_OF_DAY"):
        prepare(env)


def test_following_block_must_cross_midnight(env):
    env.blocks[4].timestamp -= 1
    with pytest.raises(ValueError, match="LAST_BLOCK_OF_DAY"):
        prepare(env)


def test_midnight_headers_must_be_linked(env):
    env.blocks[4].parentHash = b"z" * 32
    with pytest.raises(ValueError, match="LAST_BLOCK_OF_DAY"):
        prepare(env)


def test_duplicate_candidate_cannot_double_weight(env):
    env.inputs["owners"].append(env.owners[0])
    with pytest.raises(ValueError, match="HOLDER_INVENTORY"):
        prepare(env)


def test_staking_contract_is_not_a_custodian_with_all_users_tokens(env):
    env.inputs["owners"] = [env.staking.address]
    with pytest.raises(ValueError, match="SYSTEM_HOLDER_EXCLUDED"):
        prepare(env)


def test_snapshot_totals_cannot_exceed_fixed_supply(env):
    env.view.wallet[env.owners[0]] = 10_000_000 * 10**18
    with pytest.raises(ValueError, match="HOLDER_INVENTORY"):
        prepare(env)


def test_missing_inventory_evidence_is_rejected(env):
    env.inputs["inventory_evidence_hash"] = "0x" + "00" * 32
    with pytest.raises(ValueError, match="NONZERO_HASH"):
        prepare(env)


def test_review_recomputes_report_and_refuses_tampered_weight(env):
    _, _, intent = prepare(env)
    assert policy.review(env.w3, intent, env.bundle, 5, env.pin)["complete"] is False
    intent["hash_args"][2][0] = str(300_001 * 10**18)
    with pytest.raises(ValueError, match="WEIGHT_MISMATCH"):
        policy.review(env.w3, intent, env.bundle, 5, env.pin)


def test_changed_historical_balance_does_not_silently_pass_review(env):
    _, _, intent = prepare(env)
    env.view.wallet[env.owners[0]] += 1
    with pytest.raises(ValueError, match="WEIGHT_MISMATCH"):
        policy.review(env.w3, intent, env.bundle, 5, env.pin)


def test_nonce_race_invalidates_unexecuted_approval(env):
    _, _, intent = prepare(env)
    env.view.nonce = 1
    with pytest.raises(ValueError, match="OPERATION_HASH"):
        policy.review(env.w3, intent, env.bundle, 5, env.pin)


def test_executed_report_requires_exact_immutable_receipt(env):
    args, _, intent = prepare(env)
    abi = next(f for f in env.registry.abi if f.get("name") == "getPublishDayOperationHash")
    commitment = Web3.keccak(env.w3.codec.encode([collapse_if_tuple(x) for x in abi["inputs"]], args))
    env.view.complete, env.view.nonce = True, 1
    env.view.receipt = [True, sum(args[2]), 3, env.blocks[3].hash, args[0][3], commitment]
    assert policy.review(env.w3, intent, env.bundle, 5, env.pin)["complete"]
    env.view.receipt[-1] = b"z" * 32
    with pytest.raises(ValueError, match="SNAPSHOT_RECEIPT"):
        policy.review(env.w3, intent, env.bundle, 5, env.pin)


def test_reorg_during_observation_rejected(env):
    original = env.w3.eth.get_block
    calls = []
    def block(n):
        calls.append(n)
        if n == 3 and calls.count(3) > 1:
            return SimpleNamespace(number=3, timestamp=env.blocks[3].timestamp, hash=b"x" * 32)
        return original(n)
    env.w3.eth.get_block = block
    with pytest.raises(ValueError, match="SNAPSHOT_REORG"):
        prepare(env)


@pytest.mark.parametrize("value", ["-1", "1.0", "01", "1e3", 1, str(2**256)])
def test_integer_money_and_days_are_exact(value):
    with pytest.raises(ValueError, match="ULR_"):
        policy.number(value)


def test_pin_requires_explicit_mainnet_finality_and_unique_treasuries(tmp_path, env):
    output = tmp_path / "pin.json"
    pin = dict(env.pin, chain_id="1", confirmations="11")
    output.write_text(json.dumps(pin))
    with pytest.raises(ValueError, match="FINALITY_POLICY"):
        policy.load_pin(output)
    pin.update(confirmations="12", treasuries=[env.treasury.address] * 2)
    output.write_text(json.dumps(pin))
    with pytest.raises(ValueError, match="TREASURY_INVENTORY"):
        policy.load_pin(output)
    pin["treasuries"] = [env.treasury.address]
    output.write_text(json.dumps(pin))
    assert policy.load_pin(output) == pin


def test_signing_guard_rechecks_published_intent_and_both_chain_snapshots(env, monkeypatch):
    from signer_agent import onchain_transport
    _, _, intent = prepare(env)
    session = SimpleNamespace(origin_contract=env.registry.address, session_id=12,
                              message_hash=policy.digest(intent["operation_hash"]))
    env.view.public = canonical(intent).encode()
    monkeypatch.setattr(policy, "checked_runtime", lambda *_: None)
    monkeypatch.setattr(onchain_transport, "board_at", lambda *_: SimpleNamespace(address=addr(99)))
    checked = []
    monkeypatch.setattr(policy, "review", lambda w3, intent, bundle, at, pin, **kw: checked.append(at) or dict(complete=False))
    client = SimpleNamespace(w3=env.w3, _read_snapshot=env.blocks[5], address=addr(80),
        ulr_intent_context=(intent, env.bundle, env.pin), check_read_snapshot=lambda: checked.append("anchor"))
    policy.guard_signing_session(client, session)
    assert checked == [5, "latest", "anchor"]
    env.view.public = canonical(dict(intent, target=addr(123))).encode()
    with pytest.raises(ValueError, match="PUBLIC_INTENT_REQUIRED"):
        policy.guard_signing_session(client, session)


def test_new_treasury_session_without_explicit_local_pin_cannot_sign(env, monkeypatch):
    monkeypatch.setattr(policy, "checked_runtime", lambda *_: None)
    client = SimpleNamespace(w3=env.w3, _read_snapshot=env.blocks[5])
    with pytest.raises(ValueError, match="TYPED_INTENT_REQUIRED"):
        policy.guard_signing_session(client, SimpleNamespace(origin_contract=env.registry.address))


@pytest.fixture
def cli_env(env, monkeypatch):
    from signer_agent import ulr_treasury_cli as cli
    class Web3Fixture(Web3):
        def __new__(cls, *args, **kwargs):
            return env.w3
        HTTPProvider = staticmethod(lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "Web3", Web3Fixture)
    monkeypatch.setattr(cli, "read_block", lambda *_: env.blocks[5])
    monkeypatch.setattr(policy, "load_pin", lambda *_: env.pin)
    calls = []
    executor = SimpleNamespace(contracts={"authorization": env.auth}, config={"contracts": {}})
    executor.transact = lambda name, method, values, sender, identity, send, state, operation_id: (
        calls.append((name, method, values, send, operation_id)) or dict(status="VERIFIED" if send else "SIMULATED"))
    monkeypatch.setattr(cli, "_executor", lambda *_, **kw: executor)
    original = policy.read
    def read(target, at, method, *args):
        if method == "settlements":
            return [False, 0, 0, 0, 0, 0, bytes(32)]
        if method == "previewDay":
            return [False, 0, 11_000e6, 300_000 * 10**18, 1_000_000 * 10**18, 2700e6, b"q" * 32]
        if method == "isOperationApproved":
            return False
        return original(target, at, method, *args)
    monkeypatch.setattr(policy, "read", read)
    return SimpleNamespace(cli=cli, calls=calls, executor=executor, env=env)


def test_daily_cli_defaults_to_simulation_and_uses_chain_utc_day(cli_env, capsys):
    c = cli_env
    assert c.cli.main(["pay", "--rpc", "unused", "--config", "unused", "--treasury", c.env.treasury.address,
                       "--sender", c.env.owners[0]]) == 0
    assert c.calls[0][:4] == ("target", "payDay", [101], False)
    assert json.loads(capsys.readouterr().out)["status"] == "SIMULATED"


def test_daily_cli_waits_when_finalized_and_latest_cross_different_utc_days(cli_env):
    c = cli_env
    current = c.env.w3.eth.get_block
    c.env.w3.eth.get_block = lambda n: SimpleNamespace(timestamp=103 * 86400) if n == "latest" else current(n)
    with pytest.raises(ValueError, match="UTC_BOUNDARY_WAIT_FOR_FINALITY"):
        c.cli.main(["pay", "--rpc", "unused", "--config", "unused", "--treasury", c.env.treasury.address,
                    "--sender", c.env.owners[0]])
    assert c.calls == []


def test_daily_cli_reports_retryable_zero_without_sending(cli_env, monkeypatch, capsys):
    c = cli_env
    original = policy.read
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args:
        [False, 1, 0, 0, 0, 0, bytes(32)] if method == "previewDay" else original(target, at, method, *args))
    assert c.cli.main(["pay", "--rpc", "unused", "--config", "unused", "--treasury", c.env.treasury.address,
                       "--sender", c.env.owners[0]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "AWAITING_TREASURY_BALANCE"
    assert c.calls == []


def test_daily_payment_uses_new_id_only_after_canonical_completed_noop(tmp_path):
    from signer_agent.operations import Journal
    from signer_agent.ulr_treasury_cli import pay_attempt

    state = tmp_path / "pay.sqlite"
    key = "ulr-pay-fix-101"
    journal = Journal(state)
    block = SimpleNamespace(number=5, hash=b"current")
    receipt = SimpleNamespace(status=1, blockNumber=4, blockHash=b"c" * 32, transactionHash=b"p" * 32)
    hashes = {4: receipt.blockHash, 5: block.hash}
    w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda _: receipt,
        get_block=lambda number: SimpleNamespace(hash=hashes[number])))
    assert pay_attempt(w3, block, state, key, True) is None
    assert pay_attempt(w3, block, state, key, False) == key
    original = "dao-request-" + key
    journal.put(original, {"kind": "fixture"})
    assert pay_attempt(w3, block, state, key, True) == key
    journal.update(original, "VERIFIED", {"tx_hash": receipt.transactionHash.hex(), "block_number": "4"})
    step = "dao-tx-" + key + ":send"
    journal.put(step, {"kind": "call"})
    journal.update(step, "CONFIRMED", {"tx_hash": receipt.transactionHash.hex(), "block_number": "4",
                                        "block_hash": receipt.blockHash.hex()})
    assert pay_attempt(w3, SimpleNamespace(number=3), state, key, False) == key
    assert pay_attempt(w3, block, state, key, True) is None
    assert pay_attempt(w3, block, state, key, False) == key + "-1"
    hashes[4] = b"orphaned"
    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        pay_attempt(w3, block, state, key, False)


def test_execute_without_send_does_not_authorize_or_publish(cli_env, tmp_path, capsys):
    c = cli_env
    _, _, intent = prepare(c.env)
    intent_path, bundle_path = tmp_path / "intent.json", tmp_path / "bundle.json"
    intent_path.write_text(json.dumps(intent))
    bundle_path.write_text(json.dumps(c.env.bundle))
    c.cli.main(["execute", "--rpc", "unused", "--config", "unused", "--intent", str(intent_path),
                "--bundle", str(bundle_path), "--session", "12"])
    assert json.loads(capsys.readouterr().out)["status"] == "AUTHORIZATION_REQUIRED"
    assert c.calls == []


def test_overlarge_public_intent_is_rejected_before_opening_paid_session(cli_env, tmp_path, monkeypatch):
    from signer_agent import onchain_transport
    c = cli_env
    _, _, intent = prepare(c.env)
    intent["oversize_fixture"] = "x" * 32768
    intent_path, bundle_path = tmp_path / "intent.json", tmp_path / "bundle.json"
    intent_path.write_text(json.dumps(intent)); bundle_path.write_text(json.dumps(c.env.bundle))
    monkeypatch.setattr(onchain_transport, "board_at", lambda *_: SimpleNamespace(address=addr(99)))
    original = policy.read
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args: c.env.owners[0] if method == "operator"
                        else original(target, at, method, *args))
    with pytest.raises(ValueError, match="PUBLIC_INTENT_TOO_LARGE"):
        c.cli.main(["propose", "--rpc", "unused", "--config", "unused", "--intent", str(intent_path),
                    "--bundle", str(bundle_path), "--sender", c.env.owners[0], "--deadline", str(103 * 86400)])
    assert c.calls == []


def test_router_connection_requires_zero_split_canonical_vault_and_enrolled_treasury(env, monkeypatch):
    router = SimpleNamespace(address=addr(70), abi=artifact("CustodianDAOFeeRouter")["abi"])
    env.objects[router.address] = router
    view = SimpleNamespace(wrap=5000, vault=addr(71), canonical_vault=addr(71), enrolled=True)
    monkeypatch.setattr(policy, "proven", lambda *args: router)
    original = policy.read
    def read(target, at, method, *args):
        if method in ("getSetFeeSharesOperationHash", "getSetTreasuryOperationHash"):
            abi = next(f for f in router.abi if f.get("name") == method)
            return Web3.keccak(env.w3.codec.encode([collapse_if_tuple(x) for x in abi["inputs"]], args))
        if method == "incomeVault": return view.vault
        if method == "getCustodianDAO": return [addr(1)] * 4 + [view.canonical_vault, addr(1), addr(1)]
        if method == "rewardToken": return addr(72)
        if method == "wrapStakerBps": return view.wrap
        if method == "deployStakerBps": return 0
        if method == "treasuries": return env.treasury.address if view.enrolled else addr(0)
        return original(target, at, method, *args)
    monkeypatch.setattr(policy, "read", read)
    def intent(helper, values):
        return policy.intent_for(env.w3, router, "CustodianDAOFeeRouter", helper, values,
            env.auth.address, "conf_ceremony", 5, ulr_treasury=env.treasury.address)
    shares = intent("getSetFeeSharesOperationHash", [0, 0])
    assert not policy.review(env.w3, shares, env.bundle, 5, env.pin)["complete"]
    redirect = intent("getSetTreasuryOperationHash", [env.treasury.address])
    with pytest.raises(ValueError, match="ZERO_OLD_SPLIT_REQUIRED"):
        policy.review(env.w3, redirect, env.bundle, 5, env.pin)
    view.wrap = 0
    assert not policy.review(env.w3, redirect, env.bundle, 5, env.pin)["complete"]
    view.canonical_vault = addr(999)
    with pytest.raises(ValueError, match="FEE_ROUTER_BINDING"):
        policy.review(env.w3, redirect, env.bundle, 5, env.pin)
    view.canonical_vault = view.vault
    view.enrolled = False
    with pytest.raises(ValueError, match="TREASURY_NOT_ENROLLED"):
        policy.review(env.w3, redirect, env.bundle, 5, env.pin)


@pytest.mark.parametrize("field", ["purpose", "result", "group", "registry", "verifier"])
def test_authority_cannot_use_self_reported_dkg_with_wrong_origin_or_result(env, monkeypatch, field):
    # Invoke the real authority review; only the constructor-provenance probes are fixtures.
    original_read = policy.read
    coordinator, dkg, verifier = (SimpleNamespace(address=addr(n)) for n in (80, 90, 91))
    for item in (coordinator, dkg, verifier): env.objects[item.address] = item
    completed = [b"c" * 32, b"r" * 32, 1, Web3.keccak(text="EVM_AUTH"), b"g" * 32, env.owners[:2], 2]
    if field == "purpose": completed[3] = Web3.keccak(text="BTC_SPEND")
    if field == "result": completed[1] = b"x" * 32
    if field == "group": completed[4] = b"x" * 32
    def read(target, at, method, *args):
        if method == "defaultVerifier": return verifier.address
        if method == "verifier": return addr(999) if field == "verifier" else verifier.address
        if method == "getCompletedResult": return completed
        if method == "dkgRegistry": return addr(999) if field == "registry" else dkg.address
        if method == "resultHash": return b"r" * 32
        if method == "groupPubkey": return b"g" * 32
        return original_read(target, at, method, *args)
    monkeypatch.setattr(policy, "read", read)
    monkeypatch.setattr(policy, "proven", lambda w3, kind, target, at, args=None: env.objects[target])
    with pytest.raises(ValueError, match="AUTHORIZATION_DKG_BINDING"):
        ORIGINAL_AUTHORITY_CONTEXT(env.w3, env.auth.address, 5)


@pytest.fixture
def candidates_env(env, monkeypatch):
    from eth_utils.abi import function_abi_to_4byte_selector
    validator = env.objects[addr(6)]
    validator.abi = artifact("CustodianValidator")["abi"]
    view = SimpleNamespace(bound=env.staking.address, eligible=True, receipt=None)
    original = policy.read
    def read(target, at, method, *args):
        if method in ("getBindCandidateStakingOperationHash", "getAddCustodianOperationHash", "getRemoveCustodianOperationHash"):
            spec = next(f for f in validator.abi if f.get("name") == method)
            return Web3.keccak(env.w3.codec.encode(["bytes4", "address", "uint256"],
                [function_abi_to_4byte_selector(spec), args[0], env.view.nonce]))
        if method == "candidateStaking": return view.bound
        if method == "isCustodianCandidate": return view.eligible
        if method == "operationTargets": return validator.address
        if method == "operationReceipts": return view.receipt
        return original(target, at, method, *args)
    monkeypatch.setattr(policy, "read", read)
    def intent(helper, owner):
        return policy.intent_for(env.w3, validator, "CustodianValidator", helper, [owner],
            env.auth.address, "conf_ceremony", 5, ulr_treasury=env.treasury.address)
    return SimpleNamespace(env=env, validator=validator, view=view, intent=intent)


def test_candidate_binding_must_use_pinned_common_staking(candidates_env):
    c = candidates_env; e = c.env
    c.view.bound = addr(0)
    intent = c.intent("getBindCandidateStakingOperationHash", e.staking.address)
    assert not policy.review(e.w3, intent, e.bundle, 5, e.pin)["complete"]
    c.view.bound = addr(777)
    with pytest.raises(ValueError, match="CANDIDATE_SOURCE_BINDING"):
        policy.review(e.w3, intent, e.bundle, 5, e.pin)


def test_candidate_admission_rechecks_live_eligibility_before_signature(candidates_env):
    c = candidates_env; e = c.env
    intent = c.intent("getAddCustodianOperationHash", e.owners[0])
    assert not policy.review(e.w3, intent, e.bundle, 5, e.pin)["complete"]
    c.view.eligible = False
    with pytest.raises(ValueError, match="NOT_ELIGIBLE_CANDIDATE"):
        policy.review(e.w3, intent, e.bundle, 5, e.pin)


def test_completed_admission_requires_exact_receipt_not_current_holdings(candidates_env):
    from eth_utils.abi import function_abi_to_4byte_selector
    c = candidates_env; e = c.env
    intent = c.intent("getAddCustodianOperationHash", e.owners[0])
    spec = next(f for f in c.validator.abi if f.get("name") == "addCustodian")
    c.view.receipt = Web3.keccak(e.w3.codec.encode(["bytes4", "address"],
        [function_abi_to_4byte_selector(spec), e.owners[0]]))
    e.view.complete = True; e.view.nonce += 1; c.view.eligible = False
    assert policy.review(e.w3, intent, e.bundle, 5, e.pin)["complete"]
    intent["hash_args"][0] = e.owners[1]
    with pytest.raises(ValueError, match="CUSTODIAN_OPERATION_RECEIPT"):
        policy.review(e.w3, intent, e.bundle, 5, e.pin)


def test_delegate_cli_only_changes_vote_scope_and_defaults_to_simulation(cli_env, capsys):
    c = cli_env
    c.cli.main(["delegate", "--rpc", "unused", "--config", "unused", "--treasury", c.env.treasury.address,
        "--custodian", c.env.owners[1], "--sender", c.env.owners[0]])
    assert c.calls[0][:4] == ("target", "setVotingDelegate", [addr(6), c.env.owners[1]], False)
    assert json.loads(capsys.readouterr().out)["status"] == "SIMULATED"


def test_owner_writes_require_distinct_operation_id_for_new_actions(cli_env):
    c = cli_env
    with pytest.raises(ValueError, match="USER_OPERATION_ID_REQUIRED"):
        c.cli.main(["delegate", "--rpc", "unused", "--config", "unused", "--treasury", c.env.treasury.address,
            "--custodian", c.env.owners[1], "--sender", c.env.owners[0], "--send"])
    assert c.calls == []


def test_claim_does_not_depend_on_active_confederation_or_snapshot_authority(cli_env, monkeypatch):
    c = cli_env
    original = policy.read
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args: addr(5) if method == "rewardToken"
                        else original(target, at, method, *args))
    monkeypatch.setattr(policy, "proven", lambda *_: c.env.staking)
    def unavailable(*_): raise AssertionError("claim must not consult governance")
    monkeypatch.setattr(policy, "configuration", unavailable)
    monkeypatch.setattr(policy, "treasury_context", unavailable)
    c.cli.main(["claim", "--rpc", "unused", "--config", "unused", "--days", "101", "--sender", c.env.owners[0]])
    assert c.calls[0][:4] == ("target", "claim", [[101]], False)


@pytest.mark.parametrize("action,amount", [("approve", "0"), ("approve", "200000000000000000000000"),
    ("stake", "200000000000000000000000"), ("unstake", "1")])
def test_owner_staking_actions_need_no_snapshot_or_active_confederation(cli_env, monkeypatch, action, amount):
    c = cli_env
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args: addr(5) if method == "rewardToken"
                        else addr(4) if method == "stakeToken" else None)
    monkeypatch.setattr(policy, "proven", lambda *args: c.env.staking)
    def unavailable(*args): raise AssertionError("owner operation must not consult governance")
    monkeypatch.setattr(policy, "configuration", unavailable)
    monkeypatch.setattr(policy, "treasury_context", unavailable)
    c.cli.main([action, "--rpc", "unused", "--config", "unused", "--amount", amount, "--sender", c.env.owners[0]])
    assert c.calls[0][1] == action and c.calls[0][3] is False
    assert c.calls[0][2] == ([c.env.staking.address, int(amount)] if action == "approve" else [int(amount)])


@pytest.mark.parametrize("action", ["approve", "stake", "unstake"])
def test_owner_staking_sends_require_a_distinct_id(cli_env, action):
    with pytest.raises(ValueError, match="USER_OPERATION_ID_REQUIRED"):
        cli_env.cli.main([action, "--rpc", "unused", "--config", "unused", "--amount", "1", "--send"])


@pytest.mark.parametrize("action", ["stake", "unstake"])
def test_zero_stake_or_unstake_is_rejected_before_transaction(cli_env, monkeypatch, action):
    c = cli_env
    original = policy.read
    monkeypatch.setattr(policy, "read", lambda target, at, method, *args: addr(5) if method == "rewardToken"
                        else original(target, at, method, *args))
    monkeypatch.setattr(policy, "proven", lambda *args: c.env.staking)
    with pytest.raises(ValueError, match="POSITIVE_AMOUNT_REQUIRED"):
        c.cli.main([action, "--rpc", "unused", "--config", "unused", "--amount", "0", "--sender", c.env.owners[0]])
    assert not c.calls


def test_owner_wallet_can_read_by_owner_without_a_signing_identity(cli_env, monkeypatch, capsys):
    c = cli_env
    monkeypatch.setattr(policy, "proven", lambda *args: c.env.staking)
    def read(target, at, method, *args):
        return {"CUSTODIAN_THRESHOLD":200000*10**18,"minimumStakeAmount":1,"stakeToken": addr(4), "rewardToken": addr(5), "balanceOf": 100, "stakedBalance": 200, "allowance": 300}[method]
    monkeypatch.setattr(policy, "read", read)
    c.cli.main(["wallet", "--rpc", "unused", "--config", "unused", "--owner", c.env.owners[0]])
    result = json.loads(capsys.readouterr().out)
    assert result["owner"] == c.env.owners[0]
    assert [result[k] for k in ("balance", "staked", "allowance")] == ["100", "200", "300"]
    assert not c.calls
