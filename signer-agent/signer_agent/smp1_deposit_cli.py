"""Prepare/review a wallet PSBT; sign/export and broadcast only the exact quote."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .onboarding import write_public,read_private
from .smp1_deposit import Deposit,load_config


def main(argv=None):
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin deposit')
    sub=p.add_subparsers(dest='action',required=True)
    for action in ('recipient-preview','prepare','sign','export','import-signed','broadcast','cancel','status'):
        c=sub.add_parser(action);c.add_argument('--config',required=True)
        if action=='recipient-preview':
            c.add_argument('--recipient',required=True);c.add_argument('--recipient-mode',choices=('ordinary','wallet-v1','advanced-contract'),required=True)
            c.add_argument('--out',required=True)
        elif action=='prepare':
            c.add_argument('--recipient',required=True);c.add_argument('--amount-sats',required=True)
            c.add_argument('--fee-rate-sat-vb',required=True);c.add_argument('--max-fee-sats',required=True)
            c.add_argument('--ttl',default='300');c.add_argument('--out',required=True)
            c.add_argument('--recipient-file');c.add_argument('--recipient-accept')
        else:
            c.add_argument('--operation-id',required=True)
            if action!='status':c.add_argument('--accept',required=True,help='exact quote_hash displayed by prepare')
            if action=='export':c.add_argument('--out',required=True)
            if action=='import-signed':c.add_argument('--psbt-file',required=True,help='private file containing the signed PSBT as base64')
    a=p.parse_args(argv)
    d=Deposit(load_config(a.config))
    if a.action in ('recipient-preview','prepare','export') and (Path(a.out).exists() or Path(a.out).is_symlink()):
        raise ValueError('SMP1_OUTPUT_EXISTS')
    if a.action=='recipient-preview':
        result=d.recipient_preview(a.recipient,a.recipient_mode);write_public(a.out,result)
    elif a.action=='prepare':
        choice=json.loads(read_private(a.recipient_file))['selection'] if a.recipient_file else None
        result=d.prepare(a.recipient,a.amount_sats,a.fee_rate_sat_vb,max_fee=a.max_fee_sats,ttl=a.ttl,
            **(dict(recipient_choice=choice,recipient_accepted=a.recipient_accept) if choice else {}))
        write_public(a.out,result)
        q=result['quote']
        # PSBT derivation metadata stays in the private quote file/journal.
        result={k:v for k,v in result.items() if k!='quote'}|{k:result['quote'][k] for k in
            ('operation_id','chain_id','bitcoin_network','recipient','recipient_profile','amount_sats','mp_units','fee_sats','fee_cap_sats','fee_rate_sat_vb','change_address','change_sats','vout','allocation_hash','expires_at')}
        result['quote_file']=a.out
        result.update(mining_pool=q['graph']['pool'],mp_token=q['graph']['token'])
    elif a.action=='status':result=d.status(a.operation_id)
    elif a.action=='export':result=d.export(a.operation_id,a.accept,a.out)
    elif a.action=='import-signed':
        # read_private's general 64KiB identity limit does not apply to PSBTs.
        import os,stat
        fd=os.open(a.psbt_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            info=os.fstat(f.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
                    or info.st_size>1400000):raise ValueError('SMP1_PSBT_ENVELOPE')
            encoded=f.read(1400001).strip()
        result=d.import_signed(a.operation_id,a.accept,encoded)
    else:result=getattr(d,a.action)(a.operation_id,a.accept)
    print(json.dumps(result,sort_keys=True));return 0
