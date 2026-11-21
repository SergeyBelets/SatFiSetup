"""Bounded read-only assertions on original SDKs, linked to a pinned spec row.

No shell, injected Python, JSON-success fixture, signing or public submission.
These observations augment original effect jobs; they never qualify an E2E run.
"""
import argparse
import json
import os
from pathlib import Path
from .contour import ROOT,digest,private_write,sha
from signer_agent.onboarding import read_private


CATALOG=ROOT/'ops/zkspv-required-scenarios.json'
SPEC='6677a3e8ae27472fb7a3bfdf0e24ca2a5d3327dae2c5c123c4431262a83ec922'
KINDS={'EVM_CALL','ADDRESS_STATUS','OWNER_STATUS','FEE_STATUS','WORKER_STATE','OBSERVATION_FENCE'}


def catalog():
    p=json.loads(CATALOG.read_text())
    if p['version']!='1' or p['specification_sha256']!=SPEC or len(p['scenarios'])!=195:raise ValueError('CONTOUR_SCENARIO_CATALOG')
    out={}
    for row in p['scenarios']:
        if set(row)!={'id','clause','title','row_sha256'} or row['row_sha256']!=digest({k:row[k] for k in ('id','clause','title')}):raise ValueError('CONTOUR_SCENARIO_CATALOG')
        if row['id'] in out:raise ValueError('CONTOUR_SCENARIO_CATALOG')
        out[row['id']]=row
    return out


def load_config(path):
    c=json.loads(read_private(path,max_bytes=512*1024));rows=catalog()
    if (not isinstance(c,dict) or set(c)!={'version','scenario_id','row_sha256','checks'} or c.get('version')!='1'
            or c['scenario_id'] not in rows or c['row_sha256']!=rows[c['scenario_id']]['row_sha256']
            or not isinstance(c['checks'],list) or not 1<=len(c['checks'])<=64):raise ValueError('CONTOUR_SCENARIO_CONFIG')
    names=set()
    for check in c['checks']:
        base={'id','kind','config_file','args','expect'}
        if not isinstance(check,dict) or set(check)!=base or check['kind'] not in KINDS:raise ValueError('CONTOUR_SCENARIO_CHECK')
        name=check['id']
        if not isinstance(name,str) or not name.isascii() or not 1<=len(name)<=64 or not name.replace('_','').isalnum() or name in names:raise ValueError('CONTOUR_SCENARIO_CHECK')
        names.add(name)
        if not isinstance(check['config_file'],str) or not Path(check['config_file']).is_absolute() or not isinstance(check['args'],dict):raise ValueError('CONTOUR_SCENARIO_CHECK')
        expected=check['expect']
        if not isinstance(expected,dict) or set(expected) not in ({'fields'},{'error'}):raise ValueError('CONTOUR_SCENARIO_CHECK')
        if 'fields' in expected:
            if not isinstance(expected['fields'],list) or not 1<=len(expected['fields'])<=64:raise ValueError('CONTOUR_SCENARIO_CHECK')
            for row in expected['fields']:
                if not isinstance(row,dict) or set(row)!={'path','equals'} or not isinstance(row['path'],list) or not 1<=len(row['path'])<=16 or any(type(x) not in (int,str) for x in row['path']):raise ValueError('CONTOUR_SCENARIO_CHECK')
                if row['path'][0] in ('ready','production_ready'):raise ValueError('CONTOUR_SCENARIO_CHECK')
        else:
            from signer_agent.participant_errors import PARTICIPANT_ERROR_CODES
            if expected['error'] not in PARTICIPANT_ERROR_CODES:raise ValueError('CONTOUR_SCENARIO_CHECK')
    return c


def inputs(c,wc):
    result=[]
    for check in c['checks']:
        if check['kind'] in ('WORKER_STATE','OBSERVATION_FENCE'):
            from signer_agent.zk_worker import load_config
            cfg=load_config(check['config_file'])
        elif check['kind']=='ADDRESS_STATUS':
            from signer_agent.address_transport import load_config
            cfg=load_config(check['config_file'])
        else:
            from signer_agent.btc_fee_reserve import load_config
            cfg=load_config(check['config_file'])
        if any(cfg[k]!=wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations')):raise ValueError('CONTOUR_SCENARIO_SCOPE')
        if check['kind'] in ('WORKER_STATE','OBSERVATION_FENCE') and cfg['zk_state']!=wc['zk_state']:raise ValueError('CONTOUR_SCENARIO_SCOPE')
        if check['kind']=='ADDRESS_STATUS' and wc['allowed_consumers'].get(cfg['pool'])!='MiningPoolV1':raise ValueError('CONTOUR_SCENARIO_SCOPE')
        result.append(cfg)
    return result


def probe(check,cfg,wc=None):
    """Dispatch only original public review APIs; no method selected via eval."""
    kind=check['kind'];args=check['args']
    if kind in ('WORKER_STATE','OBSERVATION_FENCE'):
        from signer_agent.zk_worker import Worker,state_wire
        from signer_agent.zk_observations import observer
        if args:raise ValueError('CONTOUR_SCENARIO_CHECK')
        w=Worker(cfg)
        try:
            with w.exclusive():
                b,state,_,_,_=w.context(allow_halt=True)
                log=observer(w)
                if kind=='OBSERVATION_FENCE':
                    if log is None:raise ValueError('ZK_OBSERVATION_POLICY')
                    log.fence()
                result=dict(state=state_wire(state.functions.getState().call(block_identifier=b.number)),
                    halted=state.functions.halted().call(block_identifier=b.number),fresh=state.functions.isFresh().call(block_identifier=b.number),
                    observation_checkpoint=log.rows[-1][1] if log and log.rows else None)
                w.stable(b);return result,dict(number=str(b.number),hash=b.hash.hex())
        finally:w.close()
    if kind=='ADDRESS_STATUS':
        from signer_agent.address_transport import Client
        from signer_agent.bitcoin_rpc import BitcoinRPC
        if set(args)!={'intent_id'}:raise ValueError('CONTOUR_SCENARIO_CHECK')
        client=Client(cfg);b,_,_,_,_=client.context()
        core=BitcoinRPC(cfg['bitcoin_rpc'],cfg['bitcoin_cookie'],cfg['bitcoin_network'],cfg['bitcoin_policy'])
        result=client.status(args['intent_id'],core);client.stable(b)
        return result,dict(number=str(b.number),hash=b.hash.hex())
    from signer_agent.btc_fee_reserve import Inspector
    i=Inspector(cfg);ctx=i.context();b=ctx['block']
    if wc is not None:
        from signer_agent.zk_spv import inspect_binding
        from signer_agent.btc_fee_reserve import read
        if wc['allowed_consumers'].get(ctx['pool'].address)!='MiningPoolV1':raise ValueError('CONTOUR_SCENARIO_SCOPE')
        inspect_binding(i.w3,wc['zk_state'],wc['bitcoin_policy'],b.number,adapter_address=read(ctx['common'],b.number,'spv'))
    if kind=='FEE_STATUS':
        if set(args)!={'full'} or type(args['full']) is not bool:raise ValueError('CONTOUR_SCENARIO_CHECK')
        result=i.status(full=args['full'])
    elif kind=='OWNER_STATUS':
        from signer_agent.withdrawal_user import Withdrawal
        if set(args)!={'operation_id'}:raise ValueError('CONTOUR_SCENARIO_CHECK')
        result=Withdrawal(i).status(args['operation_id'])
    else:
        # A real eth_call at the authenticated original graph. Reverts are
        # measured separately from transport failures and cannot spend funds.
        from signer_agent.bootstrap import artifact,encode_args,Bootstrap
        from web3.exceptions import ContractLogicError
        base={'component','method','arguments','sender','revert_data_sha256'}
        if set(args) not in (base,base|{'treasury'}) or 'treasury' in args and args['component']!='gas_treasury':raise ValueError('CONTOUR_SCENARIO_CHECK')
        targets={k:ctx[k] for k in ('registry','common','batch','escrow','fee','board','pool')}
        targets.update({k:ctx['withdrawal_fees'][k] for k in ('credits','payments','funding')})
        kinds=dict(registry='BitcoinAddressRegistry',common='BitcoinDepositRegistry',batch='BitcoinBatchRegistry',escrow='BitcoinWithdrawalEscrow',
            fee='BitcoinFeeReserve',board='BitcoinSigningBoard',pool='MiningPoolV1',credits='WithdrawalFeeCredits',payments='WithdrawalFeePayments',funding='WithdrawalFeeFunding')
        extra={'authorization':'DKGAuthorization','coordinator':'FROSTCoordinator','dkg':'DKGCoordinator','recipient_policy':'RecipientPolicy','family_root':'BitcoinKeyFamilyRegistry'}
        if args['component'] in extra:
            from signer_agent.btc_fee_reserve import read
            from signer_agent.bitcoin_trust import checked_runtime
            from signer_agent.contract_provenance import prove_creation
            component=args['component'];kind=extra[component]
            if component=='coordinator':
                auth=i.w3.eth.contract(address=read(ctx['registry'],b.number,'authorization'),abi=artifact('DKGAuthorization')['abi'])
                address=read(auth,b.number,'coordinator')
            else:address=read(ctx['registry'],b.number,dict(authorization='authorization',dkg='dkg',recipient_policy='recipientPolicy',family_root='familyRegistry')[component])
            checked_runtime(i.w3,address,kind,b.number);prove_creation(i.w3,address,kind,None,b.number)
            targets[component]=i.w3.eth.contract(address=address,abi=artifact(kind)['abi']);kinds[component]=kind
        if args['component'] in ('service_payments','gas_treasury'):
            from signer_agent.gas_treasury import GasTreasuryClient
            if wc is None or not wc.get('service',{}).get('binding'):raise ValueError('CONTOUR_SCENARIO_SCOPE')
            client=GasTreasuryClient(i.w3,wc['service']['binding']);anchor=client._binding();view=client.service_profile(anchor)
            if client._call(view,'state',anchor)!=wc['zk_state']:raise ValueError('CONTOUR_SCENARIO_SCOPE')
            if args['component']=='service_payments':target=client.registry;kind='SPVServicePaymentRegistry'
            else:
                current=client._call(client.registry,'configuration',anchor)
                target=client._treasury(args.get('treasury',current[0]),anchor);kind='ConfederationGasTreasury'
            client._anchor(anchor);targets[args['component']]=target;kinds[args['component']]=kind
        if args['component'] not in targets:raise ValueError('CONTOUR_SCENARIO_CHECK')
        target=targets[args['component']];specs=[x for x in artifact(kinds[args['component']])['abi'] if x.get('type')=='function' and x.get('name')==args['method']]
        if len(specs)!=1:raise ValueError('CONTOUR_SCENARIO_CHECK')
        typed=encode_args(specs[0]['inputs'],args['arguments'])
        from web3 import Web3
        if not Web3.is_checksum_address(args['sender']):raise ValueError('CONTOUR_SCENARIO_CHECK')
        try:
            value=getattr(target.functions,args['method'])(*typed).call({'from':args['sender'],'value':0},block_identifier=b.number)
            if args['revert_data_sha256'] is not None:raise ValueError('CONTOUR_SCENARIO_EXPECTED_REVERT')
            result=dict(outcome='RETURN',value=Bootstrap.wire(value))
        except ContractLogicError as exc:
            data=getattr(exc,'data',None)
            if not isinstance(data,str) or not data.startswith('0x') or digest(data)!=args['revert_data_sha256']:raise ValueError('CONTOUR_SCENARIO_REVERT') from None
            result=dict(outcome='REVERT',data_sha256=digest(data))
    i.stable(b);return result,dict(number=str(b.number),hash=b.hash.hex())


def execute(c,data,wc=None):
    observed=[]
    for check,cfg in zip(c['checks'],data):
        expected=check['expect'];error=None
        try:result,anchor=probe(check,cfg,wc)
        except ValueError as exc:
            from signer_agent.participant_errors import PARTICIPANT_ERROR_CODES
            code=str(exc)
            if code not in PARTICIPANT_ERROR_CODES or expected.get('error')!=code:raise
            result=None;anchor=None;error=code
        if 'error' in expected and error is None:raise ValueError('CONTOUR_SCENARIO_EXPECTED_DENIAL')
        for row in expected.get('fields',[]):
            value=result
            try:
                for part in row['path']:value=value[part]
            except (TypeError,KeyError,IndexError):raise ValueError('CONTOUR_SCENARIO_ASSERTION') from None
            if type(value) is not type(row['equals']) or value!=row['equals']:raise ValueError('CONTOUR_SCENARIO_ASSERTION')
        observed.append(dict(id=check['id'],kind=check['kind'],result=result,error=error,anchor=anchor))
    return dict(status='SATISFIED',observations=dict(scenario_id=c['scenario_id'],row_sha256=c['row_sha256'],
        assertions=observed,observation_only=True,e2e_qualified=False,ready=False,production_ready=False))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('scenario-config','scenario-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):p.add_argument('--'+k,required=True)
    a=p.parse_args(argv)
    from signer_agent.zk_worker import load_config as wc_load
    wc=wc_load(a.worker_config);c=load_config(a.scenario_config);data=inputs(c,wc)
    if digest(wc)!=a.worker_config_sha256 or digest(c)!=a.scenario_config_sha256 or digest(data)!=a.inputs_sha256:raise ValueError('CONTOUR_SCENARIO_SCOPE')
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    if ctx['capability']!='scenario_check':raise ValueError('CONTOUR_CAPABILITY')
    artifacts=ctx['prior'].get('artifacts',{})
    if artifacts:
        from .contour import relative_file
        from web3 import Web3
        from signer_agent.evm_network import read_block
        if len(artifacts)!=1:raise ValueError('CONTOUR_SCENARIO_CHECK')
        name,h=next(iter(artifacts.items()));out=relative_file(Path(ctx['directory']),name)
        if sha(out)!=h:raise ValueError('CONTOUR_SCENARIO_CHECK')
        observed=json.loads(read_private(out,max_bytes=1024**2))
        if observed.get('config_sha256')!=digest(c) or observed.get('inputs_sha256')!=digest(data):raise ValueError('CONTOUR_SCENARIO_SCOPE')
        w3=Web3(Web3.HTTPProvider(wc['evm_rpc'],request_kwargs={'timeout':30}));b=read_block(w3,int(wc['evm_confirmations']))
        if str(w3.eth.chain_id)!=wc['chain_id']:raise ValueError('CONTOUR_SCENARIO_SCOPE')
        for row in observed['assertions']:
            a=row['anchor']
            if a and (int(a['number'])>b.number or w3.eth.get_block(int(a['number'])).hash.hex()!=a['hash']):raise ValueError('CONTOUR_SCENARIO_ANCHOR')
        result=dict(status='SATISFIED',observations=observed)
    else:
        result=execute(c,data,wc);result['observations'].update(config_sha256=digest(c),inputs_sha256=digest(data))
        out=path.parent/'scenario-observations.json';private_write(out,result['observations'])
        artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
