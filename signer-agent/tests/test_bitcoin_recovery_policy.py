from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest
from eth_keys import keys
from web3 import Web3

from signer_agent.bitcoin_recovery_policy import validate
from signer_agent.bootstrap_plan import build
from signer_agent.launch_configurator import build as launch
from signer_agent.onboarding import assemble, verify_profile

from test_launch_configurator import complete, relay_policy
from test_onboarding_mainnet import signed_enrollments


def policy():
    def key(number):
        return keys.PrivateKey(number.to_bytes(32, "big")).public_key.to_bytes()[:32].hex()

    return dict(height="490000", threshold="2", keys=sorted(key(i) for i in (7, 8, 9)),
                successor_script="5120" + key(10), sponsor_script="5120" + key(11),
                max_fee_sats="2200", confirmations="12",
                pause_guardian="0x" + "00" * 19 + "01")


@pytest.mark.parametrize("backend", ["relay", "zk-state-v2"])
def test_recovery_terms_are_enrolled_and_bound_before_native_reserve_activation(tmp_path, backend):
    terms = policy()
    path = tmp_path / "checkpoint.json"
    policy_value = relay_policy()
    path.write_text(json.dumps(policy_value))
    terms["height"] = str(int(policy_value["checkpoint_height"]) + 1000)
    value = complete()
    value.update(ethereum_network="sepolia", bitcoin_network="testnet4", bitcoin_backend=backend,
                 bitcoin_policy_file=str(path), income_model="in-kind", default_miner_fee_sats="1000",
                 reward_token=dict(address=value["treasury"], runtime_hash="0x" + "42" * 32, decimals="6"),
                 bitcoin_recovery=terms)
    launch(value, tmp_path / "out")
    invite = json.loads((tmp_path / "out" / "invitation.json").read_text())
    assert invite["bitcoin_recovery"] == terms
    profile = assemble(invite, signed_enrollments(invite), "http://local.invalid")
    steps = {s["id"]: s for s in build(profile)["steps"]}
    order = list(steps)
    assert order.index("register_mining") < order.index("bind_withdrawal_recovery") < order.index("bind_bitcoin_reserve")
    assert steps["deploy_withdrawal_recovery"]["contract"] == "withdrawal_recovery_factory"
    assert steps["deploy_withdrawal_recovery"]["args"][-4:] == [
        terms["height"], terms["confirmations"], terms["max_fee_sats"], terms["pause_guardian"]]
    assert steps["bind_withdrawal_recovery"]["checks"][0]["equals"] == "$withdrawal_recovery"
    tampered = deepcopy(profile)
    tampered["bitcoin_recovery"] = dict(terms, height=str(int(terms["height"]) + 1))
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(tampered)
    del tampered["bitcoin_recovery"]
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(tampered)


@pytest.mark.parametrize("field,value,error", [
    ("confirmations", "5", "BTC_RECOVERY_DEPTH"),
    ("threshold", "1", "BTC_RECOVERY_KEYS"),
    ("keys", ["00" * 32, "00" * 32], "BTC_RECOVERY_KEYS"),
    ("successor_script", "5120" + "ff" * 32, "BTC_RECOVERY_SCRIPT"),
    ("pause_guardian", "0x" + "00" * 20, "BTC_RECOVERY_GUARDIAN"),
    ("successor_dkg", {"registry": "0x" + "00" * 20, "wallet_id": "01" * 32}, "BTC_RECOVERY_SUCCESSOR_DKG"),
])
def test_recovery_fails_closed_on_invalid_terms(field, value, error):
    with pytest.raises(ValueError, match=error):
        validate(dict(policy(), **{field: value}))


def test_public_recovery_evm_action_builds_backed_transaction(monkeypatch, tmp_path):
    from signer_agent import bitcoin_onchain, bitcoin_recovery_cli

    operator = "0x" + "11" * 20
    module = "0x" + "22" * 20
    w3 = NS(eth=NS(chain_id=11155111, get_code=lambda address: b"recovery"),
            provider=NS(endpoint_uri="http://local.invalid"))
    invite = dict(chain_id="11155111", bitcoin_network="testnet4", evm_confirmations="12",
                  bitcoin_policy=relay_policy())
    ctx = NS(w3=w3, invite=invite, block=NS(number=33), module=NS(address=module))
    args = NS(identity_file="identity.json", state=tmp_path / "state.sqlite", send=True)
    monkeypatch.setattr(bitcoin_recovery_cli, "account_at", lambda path: NS(address=operator))
    monkeypatch.setattr(bitcoin_recovery_cli, "snapshot_check", lambda w3, block: None)

    def execute(registry, profile):
        assert profile["mode"] == "backed" and profile["bitcoin_network"] == "testnet4"
        assert profile["evm_confirmations"] == "12" and profile["chain_id"] == "11155111"
        assert profile["contracts"]["target"]["address"] == module
        assert profile["steps"][0]["method"] == "pause"
        return NS(transactionHash=bytes.fromhex("33" * 32))

    monkeypatch.setattr(bitcoin_onchain, "_send_profile", execute)
    assert bitcoin_recovery_cli._evm(ctx, args, "pause", [])["status"] == "CONFIRMED"


@pytest.mark.parametrize("paused", [True, False])
@pytest.mark.parametrize("idle", [False, True])
def test_planner_checks_exact_reservation_and_independent_sponsor(monkeypatch, paused, idle):
    from signer_agent import bitcoin_recovery_cli as cli

    terms = policy()
    source = terms["keys"][0]
    funding, sponsor = "01" * 32 + ":0", "02" * 32 + ":1"
    root = bytes.fromhex("03" * 32)
    sponsor_reserved = False
    funding_root = bytes(32) if idle else root

    def read(node, method, *values):
        if node == "batch" and method == "reservations":
            if values[0] == cli.outpoint_hash(funding):
                return funding_root
            return root if sponsor_reserved else bytes(32)
        return {("batch", "batchState"): 2, ("batch", "reservationRoot"): root,
                ("batch", "getInputs"): [cli.outpoint_hash(funding)], ("escrow", "isBound"): True,
                ("module", "recoveredTxid"): bytes(32), ("module", "sponsorSats"): 5000,
                ("deposits", "backingSats"): 1_000_000 if values and values[0] == cli._split(funding)[2] else 0,
                ("deposits", "consumed"): False, ("deposits", "walletOutput"): False}[(node, method)]

    ctx = NS(paused=paused, read=read, batches="batch", escrow="escrow", module="module",
             deposits="deposits", policy=terms, public={"source_script": "5120" + source}, w3=None, block=None)
    args = NS(root=root.hex(), idle=idle, input=[funding], sponsor=sponsor, internal_key=source, fee_sats="1000")
    calls = []
    monkeypatch.setattr(cli, "_unspent", lambda node, outpoint, script, sats: calls.append((outpoint, sats)))
    monkeypatch.setattr(cli, "snapshot_check", lambda w3, block: None)

    if idle and not paused:
        with pytest.raises(ValueError, match="BTC_RECOVERY_IDLE_REQUIRES_PAUSE"):
            cli._plan(ctx, None, args)
        return
    plan = cli._plan(ctx, None, args)
    assert plan["inputs"][0]["amount"] == "1000000" and plan["sponsor"]["amount"] == "5000"
    assert calls == [(funding, 1_000_000), (sponsor, 5000)]
    if idle:
        funding_root = root
        with pytest.raises(ValueError, match="BTC_RECOVERY_INPUT"):
            cli._plan(ctx, None, args)
        funding_root = bytes(32)
    sponsor_reserved = True
    with pytest.raises(ValueError, match="BTC_RECOVERY_SPONSOR"):
        cli._plan(ctx, None, args)
    args.input = [sponsor]
    with pytest.raises(ValueError, match="BTC_RECOVERY_INPUT"):
        cli._plan(ctx, None, args)


def test_successor_review_binds_registry_wallet_group_roster_and_threshold():
    from signer_agent.bitcoin_recovery_policy import review_successor

    terms = policy()
    wallet_id = bytes.fromhex("a1" * 32)
    registry = "0x" + "12" * 20
    roster = ["0x" + "34" * 20, "0x" + "56" * 20]
    terms["successor_dkg"] = dict(registry=registry, wallet_id=wallet_id.hex())
    validate(terms)
    completed = [wallet_id, bytes.fromhex("7a" * 32), 2, Web3.keccak(text="BTC_SPEND"),
                 bytes.fromhex(terms["successor_script"][4:]), roster, 2]

    def lookup(value):
        assert value == wallet_id

        def read(*, block_identifier):
            assert block_identifier == 13
            return completed

        return NS(call=read)

    dkg = NS(address=registry, functions=NS(getCompletedResult=lookup))
    assert review_successor(dkg, 13, wallet_id, terms, roster, 2) == completed[1]
    for proposal, users, threshold in ((bytes(32), roster, 2), (wallet_id, roster[::-1], 2),
                                      (wallet_id, roster, 3)):
        with pytest.raises(ValueError, match="BTC_RECOVERY_SUCCESSOR_DKG"):
            review_successor(dkg, 13, proposal, terms, users, threshold)
    completed[4] = bytes.fromhex(terms["sponsor_script"][4:])
    with pytest.raises(ValueError, match="BTC_RECOVERY_SUCCESSOR_DKG"):
        review_successor(dkg, 13, wallet_id, terms, roster, 2)


def test_rotation_intent_checks_authority_canonical_proof_and_remaining_backing(monkeypatch):
    from signer_agent import bitcoin_recovery_cli as cli
    from signer_agent.dkg_protocol import Manifest

    def address(byte):
        return "0x" + byte * 20

    module, dkg, auth = (NS(address=address(byte)) for byte in ("11", "22", "33"))
    coordinator, board, batches, deposits = (address(byte) for byte in ("44", "55", "66", "77"))
    users = (address("12"), address("13"))
    old_wallet, next_wallet, root, op, source, successor = (bytes([i]) * 32 for i in range(1, 7))
    manifest = Manifest(31337, dkg.address, old_wallet, 1, "EVM_AUTH", users,
                        (bytes([7]) * 32, bytes([8]) * 32), 2)
    invite = {"evm_confirmations": "1"}
    bundle = dict(manifest.to_dict(), role=cli.ROTATE_ROLE, onchain_board=board, onboarding={"invitation": invite})
    intent = dict(artifact=cli.ARTIFACT, hash_method=cli.ROTATE_HELPER, target=module.address,
                  bitcoin_rotation="1", role=cli.ROTATE_ROLE, coordinator=coordinator, chain_id="31337",
                  hash_args=[next_wallet.hex(), root.hex()], operation_hash=op.hex())
    values = {
        (batches, "dkg"): dkg.address, (batches, "authorization"): auth.address,
        (batches, "initialWalletId"): old_wallet, (auth.address, "registry"): dkg.address,
        (auth.address, "dkgId"): old_wallet, (auth.address, "coordinator"): coordinator,
        (auth.address, "getParticipants"): users, (auth.address, "threshold"): 2,
        (module.address, "canRefund", root): True, (module.address, cli.ROTATE_HELPER, next_wallet, root): op,
        (module.address, "successorWalletId"): bytes(32), (module.address, "successorScriptHash"): successor,
        (deposits, "backingByScript", source): 0, (deposits, "backingByScript", successor): 100,
        (deposits, "totalBackingSats"): 100,
    }

    def read(node, method, *args):
        return values[(getattr(node, "address", node), method, *args)]

    def load(node, kind):
        return {dkg.address: dkg, auth.address: auth}[node]

    ctx = NS(invite=invite, block=NS(number=13), load=load, read=read, batches=batches, deposits=deposits,
             module=module, policy=policy(), source_hash=source, paused=True)
    w3 = NS(eth=NS(chain_id=31337))
    monkeypatch.setattr(cli, "_context", lambda w, b, *, at: ctx)
    monkeypatch.setattr(cli, "snapshot_check", lambda w, b: None)
    monkeypatch.setattr(cli, "review_successor", lambda *args: next_wallet)

    def review(candidate):
        return cli.review_rotation_intent(w3, candidate, bundle, 13, finalized=False)

    result = review(intent)
    assert (result.op, result.wallet_id, result.root, result.complete) == (op, next_wallet, root, False)
    with pytest.raises(ValueError, match="BTC_RECOVERY_ROTATION_INTENT"):
        review(dict(intent, coordinator=address("99")))
    values[(module.address, "canRefund", root)] = False
    with pytest.raises(ValueError, match="BTC_RECOVERY_ROTATION_PROOF"):
        review(intent)
    values[(module.address, "canRefund", root)] = True
    values[(deposits, "backingByScript", source)] = 1
    with pytest.raises(ValueError, match="BTC_RECOVERY_ROTATION_BACKING"):
        review(intent)
