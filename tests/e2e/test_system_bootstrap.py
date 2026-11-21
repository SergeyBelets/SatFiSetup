"""Real independent FROST processes, full Bitcoin/ULR create and shared-root reuse.

Runs only on an isolated Anvil; production qualification remains pending.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
from eth_account import Account

from web3 import Web3
from test_full_bootstrap import exercise_bootstrap

ROOT = Path(__file__).resolve().parents[2]


def test_system_create_reuse_and_owner_staking(tmp_path, record_property):
    system = json.loads(Path(os.environ.get("SATOHSI_SYSTEM_POLICY", str(ROOT / "config/system-bootstrap-testnet.example.json"))).read_text())
    assert system["token_metadata"]["governance"]["symbol"] != "ULR"
    one = tmp_path / "first"; one.mkdir()
    first = exercise_bootstrap(one, record_property, system=system)
    b = first["bootstrap"]; w3 = b.w3
    assert w3.eth.chain_id == 31337
    assert first["manifest"]["system"]["graph_configured"] is True
    assert first["manifest"]["system"]["ready"] is False
    assert b.contract("address_derivation").functions.QUALIFIED().call() is False
    root_policy = one / 'root-policy.json'
    root_policy.write_text(json.dumps(first['manifest']['system']['family_root_policy']))
    review = first['command']('family-root','inspect','--rpc',w3.provider.endpoint_uri,
        '--board',first['board'],'--policy',root_policy)
    for repeat in range(2):
        assert first['command']('family-root','pin','--rpc',w3.provider.endpoint_uri,
            '--board',first['board'],'--policy',root_policy,'--identity-file',first['identities'][1],
            '--accept-fingerprint',review['fingerprint'])['status']=='FAMILY_ROOT_PINNED'
    assert first["manifest"]["system"]["token_metadata"] == system["token_metadata"]
    assert first["manifest"]["ulr"]["reuse_policy"]["token"] == system["token_metadata"]["governance"]
    token, staking = b.contract("dao_token"), b.contract("staking")
    supply = token.functions.totalSupply().call()
    assert supply == 10_000_000 * 10**18
    assert b.contract("fee_router").functions.wrapStakerBps().call() == 0
    pin_path = one / "ulr.json"
    pin_path.write_text(json.dumps(first["manifest"]["ulr"]["pin"]))
    owner = first["addresses"][1]
    amount = 200_000 * 10**18
    for action in ("approve", "stake", "unstake"):
        args = [action, "--rpc", w3.provider.endpoint_uri, "--config", pin_path, "--amount", str(amount),
            "--identity-file", first["identities"][1], "--state", one / "wallet.sqlite", "--id", action, "--send"]
        result = first["command"]("ulr-treasury", *args, member=1)
        assert result["status"] == "VERIFIED", result
        assert first["command"]("ulr-treasury", *args, member=1)["tx_hash"] == result["tx_hash"]
        assert staking.functions.stakedBalance(owner).call() == (amount if action == "stake" else 0)
    wallet = first["command"]("ulr-treasury", "wallet", "--rpc", w3.provider.endpoint_uri,
        "--config", pin_path, "--owner", owner)
    assert int(wallet["balance"]) == supply and int(wallet["staked"]) == 0

    reused = deepcopy(system)
    reused["ulr"] = first["manifest"]["ulr"]["reuse_policy"]
    reused["withdrawal_fee"].update(family_root=first["manifest"]["system"]["family_root_policy"],
        family_enrollment="PRE_ENROLLED")
    two = tmp_path / "second"; two.mkdir()
    # A completely different roster/operator imports the existing economic graph.
    # Funding is explicit fixture testUSD from an independent Anvil account;
    # the reuse bootstrap itself transfers rather than mints.
    new_operator=Account.from_key((4).to_bytes(32,'big')).address
    receipt=w3.eth.wait_for_transaction_receipt(b.contract('usd').functions.mint(new_operator,7654321).transact({'from':w3.eth.accounts[0]}))
    assert receipt.status==1
    second = exercise_bootstrap(two, record_property, system=reused, shared=first,participant_keys=(4,5,6))
    c = second["bootstrap"]
    for root in ("dao_token", "staking", "snapshots", "usd", "family_root"):
        assert c.contract(root).address == b.contract(root).address
    for independent in ("conf_auth", "ulr_treasury", "address_registry", "address_message_board"):
        assert c.contract(independent).address != b.contract(independent).address
    assert token.functions.totalSupply().call() == supply
    assert not set(first['addresses']) & set(second['addresses'])
    root = b.contract("family_root")
    assert root.functions.governance().call() == b.contract("conf_auth").address
    for graph in (b, c):
        for role, name in (("governance", "dao_token"), ("mining", "mp"),
                           ("federation_share", "fed_shares"), ("confederation_share", "conf_shares")):
            actual = {key: getattr(graph.contract(name).functions, key)().call() for key in ("name", "symbol")}
            assert actual == system["token_metadata"][role]
        assert bytes(root.functions.requireGraph(graph.contract("address_registry").address).call()) != bytes(32)
        assert graph.contract("validator").functions.candidateStaking().call() == staking.address
    assert second["manifest"]["system"]["graph_configured"] is True
    assert second["manifest"]["system"]["ready"] is False

    if 'startup_rewards' in b.contracts:
        program=b.contract('startup_rewards')
        assert c.contract('startup_rewards').address==program.address
        assert not any(step['id']=='fund_startup_rewards' for step in c.profile['steps'])
        assert staking.functions.CUSTODIAN_THRESHOLD().call()==500000*10**18
        assert staking.functions.minimumStakeAmount().call()==100*10**18
        assert b.contract('ulr_treasury').functions.reserveFloor().call()==500*10**6
        assert b.contract('ulr_treasury').functions.payoutBps().call()==7500
        day=program.functions.startDay().call()
        assert w3.eth.get_block('latest').timestamp < day*86400
        w3.provider.make_request('evm_setNextBlockTimestamp',[day*86400-3600]);w3.provider.make_request('evm_mine',[])
        args=['stake','--rpc',w3.provider.endpoint_uri,'--config',pin_path,'--amount',str(amount),
            '--identity-file',first['identities'][1],'--state',one/'wallet.sqlite','--id','startup-stake','--send']
        # Approval from the first stake is exact and was spent; approve again.
        args[0]='approve';args[args.index('--id')+1]='startup-approve'
        assert first['command']('ulr-treasury',*args,member=1)['status']=='VERIFIED'
        args[0]='stake';args[args.index('--id')+1]='startup-stake'
        assert first['command']('ulr-treasury',*args,member=1)['status']=='VERIFIED'
        w3.provider.make_request('evm_setNextBlockTimestamp',[(day+1)*86400]);w3.provider.make_request('evm_mine',[])
        common=['--rpc',w3.provider.endpoint_uri,'--chain-id','31337','--evm-confirmations','1','--program',program.address]
        state=first['command']('startup-rewards','status',*common)
        assert state['unfunded_debt'] is False and int(state['available_budget_raw'])==1000*10**6
        paid=first['command']('startup-rewards','pay',*common,'--day',str(day),'--identity-file',first['identities'][0],
            '--state',one/'startup.sqlite','--id','first-day','--send')
        assert paid['status']=='VERIFIED'
        funded=program.functions.paid(day).call();assert 0<funded<=1000*10**6
        before=b.contract('usd').functions.balanceOf(owner).call()
        claimed=first['command']('ulr-treasury','claim','--rpc',w3.provider.endpoint_uri,'--config',pin_path,
            '--days',str(day),'--identity-file',first['identities'][1],'--state',one/'wallet.sqlite','--id','startup-claim','--send',member=1)
        assert claimed['status']=='VERIFIED'
        assert b.contract('usd').functions.balanceOf(owner).call()-before==funded
