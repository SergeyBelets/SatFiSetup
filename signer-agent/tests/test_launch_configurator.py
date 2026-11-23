"""Offline launch choices bind runtime economics and participant enrollment."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from eth_account import Account
from signer_agent import launch_configurator as c
from signer_agent.launch_economics import economics,encoded,units
from signer_agent.onboarding import validate_invite,assemble
from signer_agent.bootstrap_plan import build as plan
from signer_agent.bitcoin_network import header_hash
from test_onboarding_mainnet import signed_enrollments
from test_zk_testnet4 import relay_policy


def complete():
    value=c.defaults();value.update(ethereum_network='local',bitcoin_network='regtest',
        participants=[Account.from_key(i.to_bytes(32,'big')).address for i in (1,2,3)],
        approval_deadline=str(int(time.time())+7*86400))
    value.update(treasury=value['participants'][0],token_recipient=value['participants'][1])
    value['economics'].update(candidate_threshold='500000',minimum_stake='100',treasury_reserve_usd='500',treasury_payout_bps='7500')
    return value


@pytest.mark.parametrize('evm',list(c.CHAINS))
@pytest.mark.parametrize('btc,backend',[('mainnet','relay'),('testnet4','relay'),('regtest','relay'),('mainnet','zk-state-v2'),('testnet4','zk-state-v2')])
def test_all_supported_pairs_are_draft_choices_without_rpc(evm,btc,backend):
    v=c.defaults();v.update(ethereum_network=evm,bitcoin_network=btc,bitcoin_backend=backend,evm_confirmations='12' if evm=='mainnet' else '1')
    assert c.validate(v)['bitcoin_backend']==backend


def test_regtest_zk_and_lower_consensus_bounds_have_no_fallback():
    v=c.defaults();v.update(bitcoin_network='regtest',bitcoin_backend='zk-state-v2')
    with pytest.raises(ValueError,match='REGTEST_ZK'):c.validate(v)
    for key,value in [('bitcoin_confirmations','5'),('wrap_fee_bps','10001')]:
        v=c.defaults();v[key]=value
        with pytest.raises(ValueError):c.validate(v)


@pytest.mark.parametrize('field,value',[('candidate_threshold','0'),('minimum_stake','-1'),('treasury_payout_bps','10001'),
    ('treasury_reserve_usd','1e3'),('minimum_stake',1.0)])
def test_invalid_economics_are_rejected(field,value):
    v=c.defaults();v['economics'][field]=value
    with pytest.raises(ValueError):c.validate(v)


def test_exact_asset_decimals_do_not_silently_round():
    assert units('1.234567',6)==1234567
    with pytest.raises(ValueError,match='PRECISION'):units('1.2345678',6)


def test_init_refuses_overwrite_and_incomplete_draft_cannot_build(tmp_path):
    path=tmp_path/'launch.json';c.main(['init','--output',str(path)])
    with pytest.raises(FileExistsError):c.main(['init','--output',str(path)])
    with pytest.raises(ValueError,match='INPUTS_REQUIRED'):c.build(json.loads(path.read_text()),tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_ambiguous_deployment_id_is_rejected_before_creating_output(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="^LAUNCH_DEPLOYMENT_ID$"):
        c.build(dict(complete(), deployment_id="$factory"), output)
    assert not output.exists()


@pytest.mark.parametrize('startup',[False,True])
def test_build_custom_invitation_and_constructor_plan(tmp_path,startup):
    v=complete()
    v['token_metadata']['governance']=dict(name='New Launch Governance',symbol='NLG')
    if startup:
        v['economics']['startup']=dict(enabled=True,apy_bps='1000',unit_price_usd='2',duration_days='30',
            start_day=str(int(time.time())//86400+7),budget_usd='1000')
    result=c.build(v,tmp_path/'launch');assert not result['sends_transactions']
    invite=json.loads((tmp_path/'launch/invitation.json').read_text());validate_invite(invite)
    assert invite['ulr']['economics']==v['economics']
    profile=assemble(invite,signed_enrollments(invite),'http://local.invalid');profile['onchain_board']=Account.from_key((90).to_bytes(32,'big')).address
    steps={s['id']:s for s in plan(profile)['steps']}
    assert steps['staking']['args'][2:]==['500000000000000000000000','100000000000000000000']
    assert steps['ulr_treasury']['args'][3:]==['500000000','7500']
    assert steps['dao_token']['args'][:2]==['New Launch Governance','NLG']
    assert ('startup_rewards' in steps)==startup
    if startup:
        assert steps['startup_rewards']['args'][1:3]==['1000','2000000']
        assert steps['fund_startup_rewards']['args']==['$startup_rewards','1000000000']
    altered=deepcopy(invite);altered['ulr']['economics']['minimum_stake']='101'
    with pytest.raises(ValueError):assemble(altered,signed_enrollments(invite),'http://local.invalid')
    with pytest.raises(FileExistsError):c.build(v,tmp_path/'launch')


def test_failed_build_leaves_no_partial_output(tmp_path):
    v=complete();v['participants']=[]
    with pytest.raises(ValueError):c.build(v,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_review_distinguishes_mint_input_selection_and_fee_ingress(tmp_path):
    v=complete();v.update(bitcoin_confirmations='144', evm_discovery_from_block='100')
    c.build(v,tmp_path/'launch')
    review=json.loads((tmp_path/'launch/review.json').read_text())
    assert review['bitcoin_confirmations']=='144'
    assert review['coinbase_mint_minimum']==100
    assert review['coinbase_spend_minimum']==101
    assert review['fee_ingress_coinbase_minimum']==144
    assert review['fee_ingress_regular_minimum']==144
    assert review['evm_discovery_from_block']=='100'
    assert review['bitcoin_checkpoint_height'] is None
    assert 'evm_discovery_from_block' not in json.loads((tmp_path/'launch/invitation.json').read_text())


@pytest.mark.parametrize("value", [-1, "-1", True, "010", str(2**64)])
def test_invalid_discovery_height_is_rejected(value):
    v = complete()
    v["evm_discovery_from_block"] = value
    with pytest.raises(ValueError, match="^LAUNCH_DISCOVERY_START_BLOCK$"):
        c.validate(v)


def test_existing_launch_config_keeps_default_scope():
    v = complete()
    del v["evm_discovery_from_block"]
    del v["bitcoin_watch_start_height"]
    checked = c.validate(v)
    assert checked["evm_discovery_from_block"] is None
    assert checked["bitcoin_watch_start_height"] == "0"


@pytest.mark.parametrize("invalid", [True, 40, "-1", "0001", str(2**32)])
def test_invalid_bitcoin_watch_height(invalid):
    v = complete()
    v["bitcoin_watch_start_height"] = invalid
    with pytest.raises(ValueError, match="^ADDRESS_SCAN_START_HEIGHT$"):
        c.validate(v)


def test_watch_launch_config_must_match_service_networks(tmp_path):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps(complete()))
    with pytest.raises(ValueError, match="^CHAIN_MISMATCH$"):
        c.watch_start_height(path, dict(chain_id="11155111", bitcoin_network="regtest"))
    with pytest.raises(ValueError, match="^BITCOIN_NETWORK_MISMATCH$"):
        c.watch_start_height(path, dict(chain_id="31337", bitcoin_network="testnet4"))


@pytest.mark.parametrize("remote_trust", [False, True])
def test_both_watch_clis_use_the_height_from_built_launch(tmp_path, monkeypatch, capsys, remote_trust):
    from signer_agent import address_transport, bitcoin_rpc, withdrawal_fee_cli

    v = complete()
    v["bitcoin_watch_start_height"] = "555"
    output = tmp_path / "out"
    c.build(v, output)
    assert json.loads((output/"review.json").read_text())["bitcoin_watch_start_height"] == "555"
    assert "bitcoin_watch_start_height" not in json.loads((output/"invitation.json").read_text())
    options = ["--launch-config", str(output/"launch-config.json"), "--config", "service.json"]
    core = SimpleNamespace(url="https://core.example.invalid")
    store = SimpleNamespace(scan=Mock(return_value={"status": "RESCANNING"}))
    config = dict(version="1", chain_id="31337", bitcoin_network="regtest", bitcoin_policy=None,
                  bitcoin_rpc=core.url, bitcoin_cookie="private.cookie", trust_remote_watch_filters=remote_trust,
                  max_blocks_per_poll="7", max_reorg="5")
    monkeypatch.setattr(bitcoin_rpc, "BitcoinRPC", Mock(return_value=core))
    monkeypatch.setattr(address_transport, "load_config", Mock(return_value=config))
    monkeypatch.setattr(address_transport, "Client", Mock(return_value=SimpleNamespace(
        c=config, store=store, publish_status=Mock()
    )))
    address_transport.main(["scan", *options])
    store.scan.assert_called_once_with(core, max_blocks=7, max_reorg=5, start_height=555, allow_remote_filters=remote_trust)
    assert json.loads(capsys.readouterr().out)["status"] == "RESCANNING"

    store.scan.reset_mock()
    monkeypatch.setattr(withdrawal_fee_cli, "load_config", Mock(return_value=config))
    monkeypatch.setattr(withdrawal_fee_cli, "Inspector", Mock(return_value=SimpleNamespace(
        bitcoin=core, context=lambda: dict(withdrawal_fees={})
    )))
    monkeypatch.setattr(withdrawal_fee_cli, "Funding", Mock(return_value=SimpleNamespace(store=lambda _: store)))
    withdrawal_fee_cli.main(["scan", "--watch", "fee.sqlite", *options])
    store.scan.assert_called_once_with(core, max_blocks=16, start_height=555, allow_remote_filters=remote_trust)
    assert json.loads(capsys.readouterr().out)["status"] == "RESCANNING"


@pytest.mark.parametrize("backend", ["relay", "zk-state-v2"])
def test_validate_and_build_report_checkpoint_bound_to_invitation(tmp_path, capsys, backend):
    policy = relay_policy()
    policy["max_tip_age_seconds"] = "3600"
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(policy))
    v = complete()
    v.update(ethereum_network="sepolia", bitcoin_network="testnet4", bitcoin_backend=backend,
             bitcoin_policy_file=str(path), evm_discovery_from_block="1234567",
             reward_token=dict(address=v["treasury"], runtime_hash="0x" + "42" * 32, decimals="6"))
    config = tmp_path / "launch.json"
    config.write_text(json.dumps(v))
    assert c.main(["validate", "--config", str(config)]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["evm_discovery_from_block"] == "1234567"
    assert review["bitcoin_checkpoint_height"] == policy["checkpoint_height"]
    assert review["bitcoin_checkpoint_hash"] == header_hash(bytes.fromhex(policy["checkpoint_header"]))[::-1].hex()
    c.build(v, tmp_path / "out")
    invite = json.loads((tmp_path / "out/invitation.json").read_text())
    assert invite["bitcoin_policy"]["checkpoint_height"] == review["bitcoin_checkpoint_height"]
    assert invite["bitcoin_policy"]["version"] == ("2" if backend == "zk-state-v2" else "1")
    if backend == "zk-state-v2":
        assert invite["bitcoin_policy"]["zk_spv"]["max_tip_lag_seconds"] == "3600"


@pytest.mark.parametrize("backend,verifier", [
    ("relay", "Testnet4HeaderRelay"),
    ("zk-state-v2", "ZkBitcoinTestnet4StateV2"),
])
def test_in_kind_config_builds_signed_sepolia_testnet4_graph(tmp_path, backend, verifier):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(relay_policy()))
    value = complete()
    value.update(ethereum_network="sepolia", bitcoin_network="testnet4", bitcoin_backend=backend,
                 bitcoin_policy_file=str(path), income_model="in-kind", default_miner_fee_sats="125",
                 reward_token=dict(address=value["treasury"], runtime_hash="0x" + "42" * 32, decimals="6"))
    c.build(value, tmp_path / "out")
    invite = json.loads((tmp_path / "out/invitation.json").read_text())
    profile = assemble(invite, signed_enrollments(invite), "http://local.invalid")
    steps = {step["id"]: step for step in plan(profile)["steps"]}
    assert invite["ulr"]["version"] == "3"
    assert "withdrawal_fee" not in invite and "system_bootstrap" not in invite
    assert steps["relay"]["artifact"] == verifier
    assert steps["escrow"]["artifact"] == "BitcoinWithdrawalEscrowV2"
    assert steps["escrow"]["args"][3:] == [invite["min_p2wpkh_sats"], invite["min_p2tr_sats"], "125"]
    assert steps["staking"]["artifact"] == "ConfederationDAOStake"
    assert steps["mining"]["artifact"] == "ConfederationBitcoinReserve"
    assert steps["custody_transfer"]["artifact"] == "BitcoinCustodyTransferV2"
    assert steps["bind_bitcoin_reserve"]["method"] == "bindBitcoinReserve"
    assert not any(output.get("name") == "mp" for step in steps.values() for output in step.get("outputs", []))
    assert "$decisions" in steps["dao_income"]["args"]
    assert "converter" not in steps and "startup_rewards" not in steps and "ulr_treasury" not in steps


def test_validate_refuses_checkpoint_from_another_bitcoin_network(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(relay_policy()))
    v = complete()
    v.update(bitcoin_network="mainnet", bitcoin_policy_file=str(path))
    with pytest.raises(ValueError, match="^MAINNET_POLICY_NETWORK$"):
        c.bitcoin_launch_policy(v)


def test_unfunded_or_past_startup_is_rejected():
    v=complete();v['economics']['startup'].update(enabled=True)
    with pytest.raises(ValueError):c.validate(v,complete=True)
    v['economics']['startup']=dict(enabled=True,apy_bps='1000',unit_price_usd='1',duration_days='30',budget_usd='1000',start_day='1')
    with pytest.raises(ValueError,match='FUTURE'):c.validate(v,complete=True)


def test_reuse_does_not_reject_an_already_started_shared_campaign():
    v=complete();v.update(deployment_mode='reuse',reuse_policy_file='reviewed.json',family_root_policy_file='root.json')
    v['economics']['startup']=dict(enabled=True,apy_bps='1000',unit_price_usd='1',duration_days='30',budget_usd='1000',start_day='1')
    assert c.validate(v,complete=True)['deployment_mode']=='reuse'


def test_cli_build_emits_one_machine_readable_result(tmp_path,capsys):
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(complete()))
    assert c.main(['build','--config',str(cfg),'--output',str(tmp_path/'out')])==0
    assert json.loads(capsys.readouterr().out)['status']=='LAUNCH_CONFIGURED'


def test_cli_reports_safe_missing_field_names(tmp_path,capsys):
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(c.defaults()))
    assert c.main(['validate','--config',str(cfg)])==2
    error=json.loads(capsys.readouterr().out)
    assert error['code']=='LAUNCH_INPUTS_REQUIRED' and 'participants' in error['missing']
    assert not any(k in error for k in ('exception','traceback'))


def test_wizard_preserves_existing_campaign_and_choices_on_enter(monkeypatch):
    v=complete();v['threshold']='2'
    v['economics']['startup']=dict(enabled=True,apy_bps='1234',unit_price_usd='2.5',duration_days='45',budget_usd='2000',start_day=str(int(time.time())//86400+10))
    old=deepcopy(v)
    monkeypatch.setattr('builtins.input',lambda prompt:'')
    assert c.wizard(v)==old
