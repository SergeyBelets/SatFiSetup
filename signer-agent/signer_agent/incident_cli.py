"""Incident evidence and explicit EVM_AUTH decisions. No automatic compromise inference."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .incident_transport import Client,load_config,report_id,public_report,delivery_proof
from .address_status_client import Driver
from .dkg import atomic_private
from .onboarding import read_private
from .operations import canonical


def main(argv=None):
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin incident')
    p.add_argument('action',choices=('register-key','report-prepare','report-send','report-import','report-export','reports',
        'receive-once','flush','notify','backup','status-preview','status-prepare','status-propose','status-adopt','status-review','status-approve',
        'status-execute','status-retry','status-recover','status'))
    p.add_argument('--config',required=True);p.add_argument('--registration-board');p.add_argument('--operation-id');p.add_argument('--session')
    p.add_argument('--epoch');p.add_argument('--report-id');p.add_argument('--incident-id');p.add_argument('--recipient')
    p.add_argument('--delivery',default='0',help='Explicit bounded delivery retry 0..31; report/evidence stay unchanged')
    p.add_argument('--scope',choices=('custody','derivation'))
    reasons=p.add_mutually_exclusive_group()
    reasons.add_argument('--reason',help='32-byte reason commitment, lowercase hex')
    reasons.add_argument('--reason-code',choices=('OPERATOR_REVIEW','OBSERVATION_UNAVAILABLE','SUSPECTED_CUSTODY_COMPROMISE','DERIVATION_FAILURE','RECOVERY_REVIEW'))
    p.add_argument('--details-file');p.add_argument('--file');p.add_argument('--output');p.add_argument('--message-id')
    p.add_argument('--bitcoin-block');p.add_argument('--bitcoin-tx');p.add_argument('--evm-anchor');p.add_argument('--deadline')
    p.add_argument('--next-state',choices=('ACTIVE','DRAINING','SUSPENDED','COMPROMISED','QUALIFIED','REVOKED'))
    p.add_argument('--close-issuance',action='store_true');p.add_argument('--accept')
    a=p.parse_args(argv)
    if a.output and (Path(a.output).exists() or Path(a.output).is_symlink()):raise ValueError('OUTPUT_EXISTS')
    c=Client(load_config(a.config))
    if a.action=='register-key':
        from .spv_transport import uint
        epoch=uint(a.epoch,1)
        if epoch not in c.keys:raise ValueError('ADDRESS_LOCAL_KEY_REQUIRED')
        result=c.send('key-'+str(epoch)+'-'+c.keys[epoch]['public'],'registerKey',[str(epoch),'0x'+c.keys[epoch]['public']])
    elif a.action=='report-prepare':
        if not a.output or not a.details_file or a.scope is None:raise ValueError('INCIDENT_REPORT')
        reason=a.reason
        if a.reason_code:
            from eth_utils import keccak
            reason=keccak(text='SATOHSI_INCIDENT_REASON_V1:'+a.reason_code).hex()
        if reason is None:raise ValueError('INCIDENT_REPORT')
        details=json.loads(read_private(a.details_file))
        r=c.prepare_report('0' if a.scope=='custody' else '1',reason,details,incident_id=a.incident_id,
            bitcoin_block=a.bitcoin_block,bitcoin_tx=a.bitcoin_tx,evm_anchor=a.evm_anchor)
        atomic_private(a.output,(canonical(r)+'\n').encode())
        result=dict(status='INCIDENT_PRIVATE_REPORT_SAVED',report_id=report_id(r),report=public_report(r),file=a.output,ready=False)
    elif a.action=='report-send':
        r=json.loads(read_private(a.file)) if a.file else c.report(a.report_id)
        result=c.send_report(r,a.recipient,a.delivery)
    elif a.action=='report-import':
        r=json.loads(read_private(a.file));b,_,ctx,board,_=c.context()
        delivery_proof(c.w3,ctx,board.address,public_report(r),a.message_id,b.number,attempt=a.delivery)
        identifier=c._save_report(r,ctx,b.number);c.stable(b);c.store.put('incident-proof:'+identifier,dict(message_id=a.message_id,**({'attempt':a.delivery} if a.delivery!='0' else {})))
        result=dict(status='INCIDENT_REPORT_IMPORTED',report_id=identifier,ready=False)
    elif a.action=='report-export':
        if not a.output:raise ValueError('INCIDENT_REPORT')
        atomic_private(a.output,(canonical(c.report(a.report_id))+'\n').encode())
        result=dict(status='INCIDENT_PRIVATE_REPORT_EXPORTED',report_id=a.report_id,file=a.output,ready=False)
    elif a.action=='reports':
        with c.store.journal.db() as db:rows=db.execute("SELECT body FROM address_meta WHERE id LIKE 'incident-report:%' ORDER BY rowid").fetchall()
        result=dict(status='INCIDENT_REPORTS',reports=[dict(report_id=report_id(json.loads(r[0])),report=public_report(json.loads(r[0]))) for r in rows],ready=False)
    elif a.action=='receive-once':result=c.receive_once()
    elif a.action=='flush':result=c.transmit()
    elif a.action=='notify':result=c.notify(a.report_id)
    elif a.action=='backup':
        if not a.output:raise ValueError('INCIDENT_REPORT')
        c.store.backup(a.output);result=dict(status='INCIDENT_BACKUP_SAVED',ready=False)
    else:
        driver=Driver(c,a.registration_board)
        if a.action=='status-preview':
            if not a.output:raise ValueError('INCIDENT_DECISION_OUTPUT_REQUIRED')
            result=driver.preview(a.report_id,a.next_state,a.close_issuance,a.deadline)
            atomic_private(a.output,(canonical(result['decision'])+'\n').encode())
        elif a.action=='status-prepare':result=driver.prepare(a.report_id,a.next_state,a.close_issuance,a.deadline)
        elif a.action=='status-adopt':result=driver.adopt(a.session)
        elif a.action=='status-approve':result=driver.approve(a.operation_id,a.accept)
        elif a.action=='status-retry':result=driver.retry(a.operation_id,a.deadline)
        elif a.action=='status':result=driver.status(a.operation_id)
        else:result=getattr(driver,a.action.removeprefix('status-'))(a.operation_id)
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        from .participant_errors import participant_exception_code
        print(json.dumps(dict(status='ERROR',error=participant_exception_code(exc))))
        raise SystemExit(1) from None
