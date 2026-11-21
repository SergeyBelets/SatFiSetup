"""Explicit BTC fee observation and threshold request operations. Never auto-approve FROST."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .btc_fee_reserve import Inspector,load_config,read,number
from .btc_fee_client import Driver,bootstrap_plan
from .bootstrap import Bootstrap
from .onboarding import read_private


def main(argv=None):
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin fee-reserve')
    p.add_argument('action',choices=['bootstrap-plan','status','reconcile','prepare','donation-challenge','propose','execute','recover'])
    p.add_argument('--config');p.add_argument('--bundle');p.add_argument('--file');p.add_argument('--deadline')
    p.add_argument('--method',choices=['allocate','raiseCap','release','preparePlan','admitDonation'])
    p.add_argument('--bitcoin-plan');p.add_argument('--registry');p.add_argument('--board')
    p.add_argument('--fee-cap');p.add_argument('--max-vbytes');p.add_argument('--full',action='store_true')
    p.add_argument('--txid');p.add_argument('--vout');p.add_argument('--manifest');p.add_argument('--state');p.add_argument('--state-key-file')
    a=p.parse_args(argv)
    if a.action=='bootstrap-plan':
        result=bootstrap_plan(a.registry,a.board,a.fee_cap,a.max_vbytes)
    else:
        if not a.config:raise ValueError('BTC_FEE_CONFIG_REQUIRED')
        i=Inspector(load_config(a.config))
        driver=Driver(i,json.loads(Path(a.bundle).read_text())) if a.bundle else None
        if a.action=='status':result=i.status(full=a.full)
        elif a.action=='reconcile':
            if a.deadline:
                if driver is None:raise ValueError('BTC_FEE_ENROLLMENT_REQUIRED')
                result=driver.prepare_reconciliation(a.deadline)
            else:result=i.reconcile(full=True)
        elif a.action=='donation-challenge':
            from .funding_envelope import inspect_funding
            if not a.txid or a.vout is None:raise ValueError('BTC_FEE_DONOR_PROFILE')
            tx=i.bitcoin.call('getrawtransaction',a.txid,True);parsed=inspect_funding(tx['hex'],a.txid);vout=number(a.vout,0,2**32-1)
            if vout>=len(parsed['outputs']):raise ValueError('BTC_FEE_DONOR_PROFILE')
            out=parsed['outputs'][vout];ctx=i.context()
            if out['script']!=ctx['wallet'].hex():raise ValueError('BTC_FEE_DONOR_PROFILE')
            message=bytes(read(ctx['fee'],ctx['block'].number,'donationHash',bytes.fromhex(a.txid)[::-1],vout,int(out['amount'])))
            result=dict(status='DONOR_CONSENT_REQUIRED',scope=ctx['scope'],txid=a.txid,vout=a.vout,satoshis=out['amount'],message=message.hex(),ready=False)
        elif a.action=='recover':
            from .dkg_protocol import Manifest
            from .secure_state import SecureState
            from .btc_fee_reserve import recover
            if not all((a.manifest,a.state,a.state_key_file)):raise ValueError('BTC_FEE_RECOVERY_SCOPE')
            result=recover(i,Manifest.from_dict(json.loads(Path(a.manifest).read_text())),SecureState(a.state,read_private(a.state_key_file).strip()))
        else:
            if driver is None:raise ValueError('BTC_FEE_ENROLLMENT_REQUIRED')
            if not a.file or Path(a.file).stat().st_size>32768:raise ValueError('BTC_FEE_INTENT_LIMIT')
            body=json.loads(Path(a.file).read_text())
            if a.action=='prepare':
                if not a.method or not a.deadline or not isinstance(body,list):raise ValueError('BTC_FEE_INTENT_REQUIRED')
                ctx=i.context();b=ctx['block'];r=ctx['registry'];f=ctx['fee']
                control=Bootstrap.wire([read(f,b.number,'nonce'),read(r,b.number,'custodyGeneration'),read(r,b.number,'derivationGeneration'),number(a.deadline,1)])
                wire=[control,*body] if a.method=='release' else [[control,*body]]
                plan=json.loads(Path(a.bitcoin_plan).read_text()) if a.bitcoin_plan else None
                result=driver.prepare(a.method,wire,plan)
            elif a.action=='propose':result=driver.propose(body.get('request',body))
            else:result=driver.execute(body.get('intent',body))
    print(json.dumps(result,sort_keys=True));return 0
