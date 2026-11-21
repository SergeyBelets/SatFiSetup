"""Build a private partial contour plan; performs no RPC, proving or transactions."""
import argparse
import json
from pathlib import Path
import secrets
import sys

from .contour import ROOT, CAPABILITIES, digest, validate


def source_paths():
    paths=set()
    for folder,suffix in [('signer-agent/signer_agent','.py'),('ops/zkspv','.py'),('web3/contracts/src','.sol'),
                          ('frost-crypto/src','.rs'),('rust/zkspv/worker-host/src','.rs'),
                          ('rust/zkspv/state-adapter/contracts','.sol'),('rust/zkspv/guest-integration/guest/src','.rs')]:
        paths.update(str(p.relative_to(ROOT)) for p in (ROOT/folder).rglob('*'+suffix))
    paths.update(['rust/zkspv/worker-host/Cargo.toml','rust/zkspv/worker-host/Cargo.lock',
        'ops/zkspv-required-scenarios.json',
        'rust/zkspv/guest-integration/report.json','rust/zkspv/groth16-integration/wrapper-build-report.json',
        'rust/zkspv/groth16-integration/container-pin.json','rust/zkspv/guest-integration/evidence/mainnet-transition.r0bf'])
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'signer-agent/contracts').glob('*.json'))
    return sorted(paths)


def build(worker_config, request_file, directory, *, executable=sys.executable,worker_only=False,service_config=None,
          bootstrap_config=None,funding_config=None,mint_config=None,materialize=False,consumer_only=False,
          withdrawal_config=None,reconciliation_config=None,settlement_only=False,incident_config=None,epoch_config=None):
    from signer_agent.zk_worker import load_config
    from signer_agent.onboarding import read_private
    from signer_agent.operations import public_data
    config=load_config(worker_config)
    if incident_config is not None and (materialize or consumer_only or settlement_only or any(x is not None for x in
            (service_config,bootstrap_config,funding_config,mint_config,withdrawal_config,reconciliation_config,epoch_config))):
        raise ValueError('CONTOUR_INCIDENT_STANDALONE_REQUIRED')
    if epoch_config is not None and (materialize or consumer_only or settlement_only or any(x is not None for x in
            (service_config,bootstrap_config,funding_config,mint_config,withdrawal_config,reconciliation_config,incident_config))):
        raise ValueError('CONTOUR_EPOCH_STANDALONE_REQUIRED')
    if withdrawal_config is not None and (mint_config is not None or funding_config is not None or bootstrap_config is not None or consumer_only):
        raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
    if settlement_only and (withdrawal_config is None or any(x is not None for x in (bootstrap_config,service_config))):
        raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
    if consumer_only and (mint_config is None or any(x is not None for x in (bootstrap_config,funding_config,service_config))):
        raise ValueError('CONTOUR_CONSUMER_ONLY_CONFIG')
    if Path(request_file).stat().st_size>65536:raise ValueError('CONTOUR_CONFIG')
    request=json.loads(read_private(request_file));public_data(request)
    if (not isinstance(request,dict) or request.get('consumer') not in config['allowed_consumers']
            or request.get('kind') not in ('deposit','address_deposit','withdrawal_fee','withdrawal')):
        raise ValueError('CONTOUR_WORKER_NEED')
    if int(config['stage_timeout_seconds'])+600>14400:raise ValueError('CONTOUR_STAGE_TIMEOUT_MARGIN')
    paths=source_paths()
    phases=('consumer_request','proof_preparation','fresh_proof','state_update')
    setup_commands={}
    common_args=['--worker-config',str(Path(worker_config).resolve()),'--request-file',str(Path(request_file).resolve()),
                 '--worker-config-sha256',digest(config),'--request-sha256',digest(request)]
    if bootstrap_config is not None:
        from .contour_bootstrap import load_config as load_bootstrap,profile as bootstrap_profile
        bc=load_bootstrap(bootstrap_config);bp=bootstrap_profile(bc,config)
        setup_commands['bootstrap']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_bootstrap',*common_args,
            '--bootstrap-config',str(Path(bootstrap_config).resolve()),'--bootstrap-config-sha256',digest(bc),'--profile-sha256',digest(bp)]
    if funding_config is not None:
        from .contour_funding import load_config as load_funding,deposit_config
        fc=load_funding(funding_config)
        if request.get('kind') not in ('deposit','address_deposit','withdrawal_fee'):raise ValueError('CONTOUR_FUNDING_NEED')
        dc=deposit_config(fc,config,request)
        setup_commands['bitcoin_funding']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_funding',*common_args,
            '--funding-config',str(Path(funding_config).resolve()),'--funding-config-sha256',digest(fc),'--deposit-config-sha256',digest(dc)]
    phases=(*phases,*setup_commands)
    if withdrawal_config is not None:
        from .contour_withdrawal import load_config as load_withdrawal,inputs as withdrawal_inputs,wire_inputs
        wd=load_withdrawal(withdrawal_config);data=withdrawal_inputs(wd,config,request)
        withdrawal_cap='withdrawal_'+wd['mode'].lower();caps=('withdrawal_prepare',withdrawal_cap)
        for cap in caps:
            setup_commands[cap]=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_withdrawal',*common_args,
                '--withdrawal-config',str(Path(withdrawal_config).resolve()),'--withdrawal-config-sha256',digest(wd),
                '--inputs-sha256',digest(wire_inputs(data))]
        phases=(*phases,*caps);materialize=True
    if reconciliation_config is not None:
        from .contour_reconcile import load_config as load_reconciliation,inputs as reconciliation_inputs
        rc=load_reconciliation(reconciliation_config);rd=reconciliation_inputs(rc,config)
        setup_commands['reconciliation']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_reconcile',
            '--worker-config',str(Path(worker_config).resolve()),'--worker-config-sha256',digest(config),
            '--reconciliation-config',str(Path(reconciliation_config).resolve()),'--reconciliation-config-sha256',digest(rc),
            '--inputs-sha256',digest(rd)]
        phases=(*phases,'reconciliation')
    if materialize or mint_config is not None:
        phases=(*phases,'membership')
        setup_commands['membership']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_member',*common_args]
    if mint_config is not None:
        from .contour_mint import load_config as load_mint,inputs as mint_inputs
        mc=load_mint(mint_config);data=mint_inputs(mc,config,request)
        cap='smp1_mint' if mc['mode']=='SMP1' else 'address_mint';phases=(*phases,cap)
        setup_commands[cap]=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_mint',*common_args,
            '--mint-config',str(Path(mint_config).resolve()),'--mint-config-sha256',digest(mc),
            '--inputs-sha256',digest([data[0],data[2],data[3]])]
    service_args=[];service_phases=()
    if service_config is not None:
        from .contour_service import load_config as load_service,validate as validate_service
        from signer_agent.spv_transport import load_config as load_transport
        sc=load_service(service_config)
        if not config.get('transport'):raise ValueError('CONTOUR_SERVICE_SCOPE')
        rc=load_transport(sc['requester_config']);pc=load_transport(config['transport']['config_file'])
        validate_service(config,sc,rc,pc,request)
        service_phases=('consumer_request','encrypted_transport','admission_payment','service_status')
        service_args=['--service-config',str(Path(service_config).resolve()),'--service-config-sha256',digest(sc),
                      '--requester-config-sha256',digest(rc),'--peer-config-sha256',digest(pc)]
        phases=(*phases,'encrypted_transport','admission_payment','service_status')
    if consumer_only:
        phases=('membership','smp1_mint' if mc['mode']=='SMP1' else 'address_mint');worker_only=True
    if settlement_only:
        phases=('membership',withdrawal_cap,*(('reconciliation',) if reconciliation_config is not None else ()));worker_only=True
    if incident_config is not None:
        from .contour_incident import load_config as load_incident,inputs as incident_inputs
        ic=load_incident(incident_config);data=incident_inputs(ic,config)
        setup_commands['incident_recovery']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_incident',
            '--worker-config',str(Path(worker_config).resolve()),'--worker-config-sha256',digest(config),
            '--incident-config',str(Path(incident_config).resolve()),'--incident-config-sha256',digest(ic),'--inputs-sha256',digest(data)]
        phases=('incident_recovery',);worker_only=True
    if epoch_config is not None:
        from .contour_epoch import load_config as load_epoch,inputs as epoch_inputs
        ec=load_epoch(epoch_config);data=epoch_inputs(ec,config)
        setup_commands['old_epoch_service']=[str(Path(executable).absolute()),'-m','ops.zkspv.contour_epoch',
            '--worker-config',str(Path(worker_config).resolve()),'--worker-config-sha256',digest(config),
            '--epoch-config',str(Path(epoch_config).resolve()),'--epoch-config-sha256',digest(ec),'--inputs-sha256',digest(data)]
        phases=('old_epoch_service',);worker_only=True
    stages=[]
    for capability in CAPABILITIES:
        if worker_only and capability not in phases:continue
        command=setup_commands.get(capability)
        if capability in phases and command is None:
            command=[str(Path(executable).absolute()),'-m',
                'ops.zkspv.contour_service' if capability in service_phases else 'ops.zkspv.contour_worker','--phase',capability,
                *common_args]
            if capability in service_phases:command+=service_args
        timeout=(int(config['stage_timeout_seconds'])+600 if capability=='fresh_proof' else
                 900 if capability=='proof_preparation' else 600)
        stages.append(dict(id=capability,capability=capability,timeout_seconds=timeout,probe=command,run=command,recover=command))
    return validate(dict(version='1',run_id=secrets.token_hex(32),directory=str(Path(directory).absolute()),
                         source_paths=sorted(paths),stages=stages))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker-config',required=True);p.add_argument('--request-file',required=True)
    p.add_argument('--directory',required=True);p.add_argument('--output',required=True)
    p.add_argument('--worker-only',action='store_true',help='Partial connected pipeline; full missing capabilities remain explicit')
    p.add_argument('--service-config',help='Explicit private encrypted transport and PAID/CONFEDERATION policy')
    p.add_argument('--bootstrap-config',help='Generated mainnet ZK structural bootstrap and original deployment journal')
    p.add_argument('--funding-config',help='Observe exact funding or recover a previously accepted SMP1 quote')
    p.add_argument('--materialize',action='store_true',help='Connect current-root header materialization without mint')
    p.add_argument('--mint-config',help='Original participant journals, frozen SMP1 quote/address intent and admission policy')
    p.add_argument('--consumer-only',action='store_true',help='Partial membership/mint plan against an existing fresh authenticated graph')
    p.add_argument('--withdrawal-config',help='Accepted owner quote and exact reviewed V4 fee version')
    p.add_argument('--reconciliation-config',help='Original reserve observation/report journal and FROST authority')
    p.add_argument('--settlement-only',action='store_true',help='Partial membership/settlement against an existing signed family and fresh state')
    p.add_argument('--incident-config',help='Standalone partial reviewed incident/recovery plan without monetary/Fresh prerequisites')
    p.add_argument('--epoch-config',help='Standalone partial original-fleet observation and fresh retention review; no sending or rotation')
    a=p.parse_args(argv);plan=build(a.worker_config,a.request_file,a.directory,worker_only=a.worker_only,service_config=a.service_config,
        bootstrap_config=a.bootstrap_config,funding_config=a.funding_config,mint_config=a.mint_config,materialize=a.materialize,
        consumer_only=a.consumer_only,withdrawal_config=a.withdrawal_config,reconciliation_config=a.reconciliation_config,
        settlement_only=a.settlement_only,incident_config=a.incident_config,epoch_config=a.epoch_config)
    from signer_agent.dkg import atomic_private
    atomic_private(a.output,(json.dumps(plan,indent=2)+'\n').encode())
    print(json.dumps(dict(status='PARTIAL_CONTOUR_PLAN_CREATED',connected_capabilities=[s['capability'] for s in plan['stages'] if s['run']],
                         missing_capabilities=[cap for cap in CAPABILITIES if not any(s['capability']==cap and s['run'] for s in plan['stages'])],production_ready=False)))


if __name__=='__main__':main()
