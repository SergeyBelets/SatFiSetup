"""Cross-platform launch wizard with an optional remote RPC preflight."""
from contextlib import redirect_stdout
import io
from copy import deepcopy
from datetime import date,timedelta
import json
from pathlib import Path
import tempfile
import time

from web3 import Web3
from .launch_economics import DEFAULTS, economics, encoded, integer, units
from .bootstrap_tokens import require_literal, validate as token_metadata
from .launch_rpc import REMOTE_PROFILE, PreflightError, check_rpc
from .operations import public_data
from .safe_errors import DiagnosticArgumentParser, emit_debug

ROOT=Path(__file__).resolve().parents[2]
CHAINS={'mainnet':'1','sepolia':'11155111','local':'31337'}
LAUNCH_ERRORS = frozenset("""
LAUNCH_BACKEND_POLICY_MISMATCH LAUNCH_CHOICE LAUNCH_CONFIG_SCHEMA LAUNCH_DEPLOYMENT_ID
LAUNCH_BITCOIN_POLICY_FILE LAUNCH_DISCOVERY_START_BLOCK
ADDRESS_SCAN_START_HEIGHT
LAUNCH_DEPLOYMENT_MODE LAUNCH_INPUTS_REQUIRED LAUNCH_MAINNET_DEPLOYMENT_FEE_REQUIRED
LAUNCH_NETWORK_PAIR LAUNCH_REGTEST_ZK_UNSUPPORTED LAUNCH_REUSE_V2_POLICY_REQUIRED
LAUNCH_RPC_PROFILE LAUNCH_RPC_PROFILE_NETWORKS LAUNCH_RPC_INVITATION_MISMATCH
LAUNCH_SHARED_ECONOMICS_MISMATCH LAUNCH_STARTUP_FUNDING_PRECISION LAUNCH_STARTUP_MUST_START_IN_FUTURE
""".split())


def defaults():
    names=json.loads((ROOT/'config/token-metadata.example.json').read_text())
    return dict(version='1',ethereum_network='sepolia',bitcoin_network='testnet4',bitcoin_backend='relay',
        rpc_profile='manual',
        income_model='legacy',default_miner_fee_sats=None,
        bitcoin_recovery=None,
        deployment_mode='create',deployment_id='project-v1',bitcoin_confirmations='6',evm_confirmations='1',
        evm_discovery_from_block=None,bitcoin_watch_start_height='0',
        token_metadata=names,economics=deepcopy(DEFAULTS),wrap_fee_bps='100',unwrap_fee_bps='100',
        participants=[],threshold='3',custody_threshold='2',min_custodians='2',treasury=None,token_recipient=None,
        reward_token=None,bitcoin_policy_file=None,reuse_policy_file=None,family_root_policy_file=None,
        approval_deadline=None,converter_numerator='1',converter_denominator='10000000000',converter_funding_usd='1000',
        deployment_fee=None,phase_duration=None,protocol_fee_cap_sats='100000',max_vbytes='10000',max_packet_bytes='1024',
        qualification_report=str(ROOT/'rust/zkspv/derivation-qualification/qualification-report.json'),
        service_policy_file=str(ROOT/'config/bitcoin-bootstrap-service-policy.example.json'),custody_sources=[])


def validate(value,*,complete=False):
    c=deepcopy(public_data(value))
    if isinstance(c,dict):
        c.setdefault('phase_duration',None)
        c.setdefault('evm_discovery_from_block',None)
        c.setdefault('bitcoin_watch_start_height','0')
        c.setdefault('rpc_profile','manual')
        c.setdefault('income_model','legacy')
        c.setdefault('default_miner_fee_sats',None)
        c.setdefault('bitcoin_recovery',None)
    if not isinstance(c,dict) or set(c)!=set(defaults()) or c['version']!='1':raise ValueError('LAUNCH_CONFIG_SCHEMA')
    if c['ethereum_network'] not in CHAINS or c['bitcoin_network'] not in ('mainnet','testnet4','regtest') or c['bitcoin_backend'] not in ('relay','zk-state-v2'):
        raise ValueError('LAUNCH_NETWORK_PAIR')
    if c['rpc_profile'] not in ('manual',REMOTE_PROFILE):raise ValueError('LAUNCH_RPC_PROFILE')
    if c['rpc_profile']==REMOTE_PROFILE and (c['ethereum_network'],c['bitcoin_network'])!=('sepolia','testnet4'):
        raise ValueError('LAUNCH_RPC_PROFILE_NETWORKS')
    if c['bitcoin_backend']=='zk-state-v2' and c['bitcoin_network']=='regtest':raise ValueError('LAUNCH_REGTEST_ZK_UNSUPPORTED')
    if c['income_model'] not in ('legacy','in-kind'):raise ValueError('LAUNCH_INCOME_MODEL')
    in_kind = c['income_model'] == 'in-kind'
    if in_kind and c['default_miner_fee_sats'] is not None:
        integer(c['default_miner_fee_sats'], 1, 21_000_000 * 10**8)
    if c['deployment_mode'] not in ('create','reuse'):raise ValueError('LAUNCH_DEPLOYMENT_MODE')
    require_literal(c['deployment_id'], 'LAUNCH_DEPLOYMENT_ID')
    if not c['deployment_id'].strip() or len(c['deployment_id'].encode())>128:raise ValueError('LAUNCH_DEPLOYMENT_ID')
    integer(c['bitcoin_confirmations'],6,2**32-1)
    if c['bitcoin_recovery'] is not None:
        from .bitcoin_recovery_policy import validate as validate_recovery
        if not in_kind:
            raise ValueError('BTC_RECOVERY_NATIVE_S_ENROLLMENT')
        validate_recovery(c['bitcoin_recovery'], payment_depth=int(c['bitcoin_confirmations']))
    integer(c['evm_confirmations'],12 if c['ethereum_network']=='mainnet' else 1,256)
    if c['evm_discovery_from_block'] is not None:
        try:
            integer(c['evm_discovery_from_block'],0,2**64-1)
        except ValueError:
            raise ValueError('LAUNCH_DISCOVERY_START_BLOCK') from None
    from .address_watch import MAX_SCAN_HEIGHT
    try:
        integer(c['bitcoin_watch_start_height'],0,MAX_SCAN_HEIGHT)
    except ValueError:
        raise ValueError('ADDRESS_SCAN_START_HEIGHT') from None
    if c['phase_duration'] is not None:
        from .onboarding import validate_phase_duration
        validate_phase_duration(CHAINS[c['ethereum_network']],c['phase_duration'])
    token_metadata(c['token_metadata'])
    policy=dict(version='2',economics=c['economics']);economics(policy)
    integer(c['wrap_fee_bps'],0,10000);integer(c['unwrap_fee_bps'],0,10000)
    if not complete:return c
    missing=[k for k in ('participants','treasury','token_recipient','approval_deadline') if not c[k]]
    backed=c['ethereum_network']=='mainnet' or c['bitcoin_network']!='regtest'
    if backed and not c['reward_token']:missing.append('reward_token')
    if c['bitcoin_network']!='regtest' and not c['bitcoin_policy_file']:missing.append('bitcoin_policy_file')
    if in_kind and c['default_miner_fee_sats'] is None:missing.append('default_miner_fee_sats')
    if c['deployment_mode']=='reuse':
        reuse_fields = ('reuse_policy_file',) if in_kind else ('reuse_policy_file','family_root_policy_file')
        missing += [k for k in reuse_fields if not c[k]]
    if missing:raise ValueError('LAUNCH_INPUTS_REQUIRED:'+','.join(missing))
    if not in_kind and c['deployment_mode']=='create' and c['economics']['startup']['enabled'] and int(c['economics']['startup']['start_day'])<=int(time.time())//86400:
        raise ValueError('LAUNCH_STARTUP_MUST_START_IN_FUTURE')
    if c['ethereum_network']=='mainnet' and not c['deployment_fee']:raise ValueError('LAUNCH_MAINNET_DEPLOYMENT_FEE_REQUIRED')
    return c


def bitcoin_launch_policy(c):
    if c['bitcoin_network']=='regtest':
        return None
    from .zk_spv import policy_from_checkpoint,is_zk,validate_confederation
    from .bitcoin_network import validate_policy
    try:
        candidate=json.loads(Path(c['bitcoin_policy_file']).read_text())
    except (OSError, ValueError, TypeError):
        raise ValueError('LAUNCH_BITCOIN_POLICY_FILE') from None
    btc=validate_policy(candidate,c['bitcoin_network'])
    confederation=c['deployment_id']+'-conf'
    if c['bitcoin_backend']=='zk-state-v2' and not is_zk(btc):
        btc=policy_from_checkpoint(btc,confederation,'3600',str(min(7200,int(btc['max_tip_age_seconds']))))
    if is_zk(btc)!=(c['bitcoin_backend']=='zk-state-v2'):raise ValueError('LAUNCH_BACKEND_POLICY_MISMATCH')
    validate_confederation(btc,confederation)
    return btc


def scan_review(c, btc):
    from .bitcoin_network import header_hash
    return dict(evm_discovery_from_block=c['evm_discovery_from_block'],
        bitcoin_watch_start_height=c['bitcoin_watch_start_height'],
        bitcoin_checkpoint_height=btc['checkpoint_height'] if btc else None,
        bitcoin_checkpoint_hash=header_hash(bytes.fromhex(btc['checkpoint_header']))[::-1].hex() if btc else None)


def watch_start_height(path, service_config):
    if path is None:
        return None
    c=validate(json.loads(path.read_text()))
    if CHAINS[c['ethereum_network']]!=service_config['chain_id']:raise ValueError('CHAIN_MISMATCH')
    if c['bitcoin_network']!=service_config['bitcoin_network']:raise ValueError('BITCOIN_NETWORK_MISMATCH')
    return int(c['bitcoin_watch_start_height'])


def preflight_policy(config, invitation_path):
    if config['rpc_profile'] != REMOTE_PROFILE:
        raise ValueError('LAUNCH_RPC_PROFILE')
    if invitation_path is None:
        return bitcoin_launch_policy(config)
    from .onboarding import read_public, validate_invite
    from .zk_spv import is_zk

    invite = validate_invite(read_public(invitation_path))
    if (invite['chain_id'] != CHAINS[config['ethereum_network']]
            or invite['bitcoin_network'] != config['bitcoin_network']
            or invite['deployment_id'] != config['deployment_id']
            or invite.get('evm_confirmations') != config['evm_confirmations']
            or is_zk(invite.get('bitcoin_policy')) != (config['bitcoin_backend']=='zk-state-v2')):
        raise ValueError('LAUNCH_RPC_INVITATION_MISMATCH')
    return invite['bitcoin_policy']


def build(value,output):
    c=validate(value,complete=True);output=Path(output).resolve()
    if output.exists():raise FileExistsError(output)
    from .bootstrap_system import main as system_main
    from .onboarding import main as onboarding_main
    btc=bitcoin_launch_policy(c)
    in_kind = c['income_model'] == 'in-kind'
    ulr = dict(version='3' if in_kind else '2', mode=c['deployment_mode'], bitcoin_backend=c['bitcoin_backend'])
    if in_kind:
        ulr.update(candidate_threshold_wei=str(units(c['economics']['candidate_threshold'], 18)),
                   default_miner_fee_sats=c['default_miner_fee_sats'])
    else:
        ulr['economics'] = c['economics']
    if c['deployment_mode']=='reuse':
        old=json.loads(Path(c['reuse_policy_file']).read_text())
        if old.get('version')!=ulr['version'] or old.get('mode')!='reuse':
            raise ValueError('LAUNCH_REUSE_POLICY_VERSION')
        if not in_kind:
            for name in ('candidate_threshold','minimum_stake','startup'):
                if economics(old)[name]!=c['economics'][name]:raise ValueError('LAUNCH_SHARED_ECONOMICS_MISMATCH')
        ulr['roots']=old['roots']
    usd_decimals=integer(c['reward_token']['decimals'],0,18) if c['reward_token'] else 6
    if not in_kind:
        settings=encoded(ulr,usd_decimals)  # Reject sub-unit prices/budgets before writing.
        if settings['startup']['enabled'] and (
                not settings['startup']['unit_price'] or not settings['startup']['budget']):
            raise ValueError('LAUNCH_STARTUP_FUNDING_PRECISION')
    rpc_report = check_rpc(c, btc) if c['rpc_profile'] == REMOTE_PROFILE else None
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.launch-',dir=output.parent) as temp:
        folder=Path(temp)
        def write(name,data):
            p=folder/name;p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');return str(p)
        up=write('ulr-policy.json',ulr);names=write('token-metadata.json',c['token_metadata'])
        custody=write('bitcoin-custody.json',dict(version='1',mode='custodians',threshold=c['custody_threshold'],sources=c['custody_sources']))
        selected_policy = ['--ulr-policy',up,'--token-metadata',names,'--bitcoin-custody-policy',custody]
        if c['bitcoin_recovery'] is not None:
            selected_policy += ['--bitcoin-recovery-policy',write('bitcoin-recovery.json',c['bitcoin_recovery'])]
        if not in_kind:
            args=[*selected_policy,'--qualification-report',c['qualification_report'],
                '--service-policy',c['service_policy_file'],'--protocol-fee-cap-sats',c['protocol_fee_cap_sats'],
                '--max-vbytes',c['max_vbytes'],'--max-packet-bytes',c['max_packet_bytes'],
                '--output',str(folder/'system.json')]
            if c['family_root_policy_file']:args += ['--family-root-policy',c['family_root_policy_file']]
            with redirect_stdout(io.StringIO()):system_main(args)
            selected_policy = ['--system-policy',str(folder/'system.json')]
        args=['invite','--participants',*c['participants'],'--threshold',c['threshold'],'--treasury',c['treasury'],
            '--ethereum-network',c['ethereum_network'],'--bitcoin-network',c['bitcoin_network'],
            '--evm-confirmations',c['evm_confirmations'],'--bitcoin-confirmations',c['bitcoin_confirmations'],
            '--wrap-fee-bps',c['wrap_fee_bps'],'--unwrap-fee-bps',c['unwrap_fee_bps'],
            '--wrap-staker-bps','0','--deploy-staker-bps','0','--dao-use-reserve','false',
            '--dao-token-recipient',c['token_recipient'],'--integration-fee-exemption','false' if c['ethereum_network']=='mainnet' else 'true',
            '--min-p2wpkh-sats','294','--min-p2tr-sats','330','--min-custodians',c['min_custodians'],
            '--converter-numerator',c['converter_numerator'],'--converter-denominator',c['converter_denominator'],
            '--converter-funding',str(units(c['converter_funding_usd'],usd_decimals)),
            '--deadline',c['approval_deadline'],*selected_policy,
            '--deployment-id',c['deployment_id'],'--output',str(folder/'invitation.json')]
        if c['phase_duration'] is not None:args+=['--phase-duration',c['phase_duration']]
        if btc:args+=['--bitcoin-policy',write('bitcoin-policy.json',btc)]
        if c['reward_token']:
            r=c['reward_token'];args+=['--reward-token',r['address'],'--reward-token-code-hash',r['runtime_hash'],'--reward-token-decimals',r['decimals']]
        if c['deployment_fee']:args+=['--deployment-fee-amount',c['deployment_fee']['amount'],'--deployment-fee-treasury',c['deployment_fee']['treasury']]
        with redirect_stdout(io.StringIO()):onboarding_main(args)
        write('launch-config.json',c)
        write('review.json',dict(status='RPC_CHECKED_INPUTS_PREPARED' if rpc_report else 'OFFLINE_REVIEW_INPUTS_PREPARED',
            sends_transactions=False,production_ready=False,rpc_profile=c['rpc_profile'],
            **({'rpc_preflight':rpc_report} if rpc_report else {}),
            networks={k:c[k] for k in ('ethereum_network','bitcoin_network','bitcoin_backend')},
            bitcoin_confirmations=c['bitcoin_confirmations'],coinbase_mint_minimum=100,
            coinbase_spend_minimum=101,
            fee_ingress_coinbase_minimum=max(101,int(c['bitcoin_confirmations'])),
            fee_ingress_regular_minimum=max(6,int(c['bitcoin_confirmations'])),income_model=c['income_model'],
            economics=ulr if in_kind else c['economics'],
            **scan_review(c, btc)))
        Path(temp).rename(output)
    return dict(status='LAUNCH_CONFIGURED',directory=str(output),invitation=str(output/'invitation.json'),
                sends_transactions=False, **({'rpc_preflight':rpc_report} if rpc_report else {}))


def wizard(c):
    def ask(key,label,options=None):
        default=c[key];answer=input(f'{label}'+(f" ({'/'.join(options)})" if options else '')+f' [{default if default is not None else ""}]: ').strip()
        if answer:c[key]=answer
        if options and c[key] not in options:raise ValueError('LAUNCH_CHOICE:'+key)
    ask('rpc_profile','Профиль RPC',['manual',REMOTE_PROFILE])
    if c['rpc_profile']==REMOTE_PROFILE:
        c.update(ethereum_network='sepolia',bitcoin_network='testnet4')
    else:
        ask('ethereum_network','EVM-сеть',list(CHAINS));ask('bitcoin_network','Bitcoin-сеть',['mainnet','testnet4','regtest'])
    ask('bitcoin_backend','SPV backend',['relay','zk-state-v2']);ask('deployment_mode','Общие контракты',['create','reuse'])
    ask('income_model','Экономика конфедерации',['legacy','in-kind'])
    in_kind = c['income_model'] == 'in-kind'
    if in_kind:
        ask('default_miner_fee_sats','Лимит доли комиссии Bitcoin на заявку, sats')
    ask('deployment_id','Идентификатор запуска')
    ask('evm_discovery_from_block','Первый EVM-блок для поиска приглашений (пусто: последние 8192 блока)')
    ask('bitcoin_watch_start_height','Bitcoin-watch: первый блок полного сканирования (0 = от genesis)')
    c['evm_confirmations']='12' if c['ethereum_network']=='mainnet' else '1'
    ask('evm_confirmations','Подтверждения EVM');ask('bitcoin_confirmations','Подтверждения BTC (минимум 6; coinbase 100/101)')
    if c['ethereum_network']!='mainnet':
        ask('phase_duration','Секунд на фазу DKG (по умолчанию 3600, максимум 86400)')
    for role,token in c['token_metadata'].items():
        for field in ('name','symbol'):
            value=input(f'{role}: {field} [{token[field]}]: ').strip()
            if value:token[field]=value
    economic_fields = ('candidate_threshold',) if in_kind else (
        'candidate_threshold','minimum_stake','treasury_reserve_usd','treasury_payout_bps')
    for key in economic_fields:
        value=input(f'{key} [{c["economics"][key]}]: ').strip()
        if value:c['economics'][key]=value
    ask('wrap_fee_bps','Wrap fee, bps (100 = 1%)');ask('unwrap_fee_bps','Unwrap fee, bps')
    previous=c['economics']['startup']['enabled']
    answer='n' if in_kind else input('Дополнительный стартовый APY из своего USD-бюджета? '+('[Y/n]: ' if previous else '[y/N]: ')).strip().lower()
    enabled=previous if not answer else answer in ('y','yes','да','д')
    if enabled:
        s=c['economics']['startup'];s['enabled']=True
        for key,label,default in [('apy_bps','APY, bps (1000 = 10%)','1000'),('unit_price_usd','Фиксированная USD-цена governance-токена','1'),
            ('duration_days','Срок в UTC-днях','30'),('budget_usd','Заранее внесённый USD-бюджет','1000')]:
            default=s[key] if previous else default
            s[key]=input(f'{label} [{default}]: ').strip() or default
        default_date=(date(1970,1,1)+timedelta(days=int(s['start_day']))).isoformat() if previous else ''
        day=input(f'Первый день выплат UTC, YYYY-MM-DD [{default_date}]: ').strip() or default_date
        s['start_day']=str((date.fromisoformat(day)-date(1970,1,1)).days)
    else:c['economics']['startup']=deepcopy(DEFAULTS['startup'])
    participants=input('Адреса участников через пробел (оператор первым): ').split()
    if participants:c['participants']=participants
    if c['participants']:
        c['treasury']=c['treasury'] or c['participants'][0];c['token_recipient']=c['token_recipient'] or c['participants'][0]
        if participants:
            c['threshold']=str(len(c['participants']));c['custody_threshold']=str(max(1,((len(c['participants'])-1)*7+9)//10))
    for key,label in [('threshold','Порог federation DKG'),('custody_threshold','Порог custodian DKG'),('min_custodians','Минимум кастодианов'),
                      ('treasury','Адрес казны'),('token_recipient','Получатель всех 10 млн governance-токенов'),
                      ('converter_numerator','Числитель котировки конвертера'),('converter_denominator','Знаменатель котировки конвертера'),
                      ('converter_funding_usd','Начальное финансирование конвертера, USD')]:ask(key,label)
    if c['bitcoin_network']!='regtest':ask('bitcoin_policy_file','Файл собственного Bitcoin checkpoint policy')
    if c['ethereum_network']=='mainnet' or c['bitcoin_network']!='regtest':
        r=c['reward_token'] or dict(address='',runtime_hash='',decimals='6')
        for key in r:r[key]=input(f'Общий USD token: {key} [{r[key]}]: ').strip() or r[key]
        c['reward_token']=r
    else:c['reward_token']=None;c['bitcoin_policy_file']=None
    if c['ethereum_network']=='mainnet':
        fee=c['deployment_fee'] or dict(amount='',treasury='')
        c['deployment_fee']={k:input(f'Deployment fee {k} [{v}]: ').strip() or v for k,v in fee.items()}
    if c['deployment_mode']=='reuse':
        ask('reuse_policy_file','Файл проверенного reuse policy')
        if not in_kind:
            ask('family_root_policy_file','Файл сохранённого family root policy')
    c['approval_deadline']=c['approval_deadline'] or str(int(time.time())+7*86400)
    ask('approval_deadline','Дедлайн одобрения, Unix seconds')
    return validate(c)


def _main(argv=None):
    p=DiagnosticArgumentParser(description=__doc__);p.add_argument('action',choices=['init','wizard','validate','check-rpc','build'])
    p.add_argument('--config',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--invite',type=Path,help='use the shared invitation checkpoint with check-rpc')
    a=p.parse_args(argv);c=json.loads(a.config.read_text()) if a.config else defaults()
    if a.invite is not None and a.action!='check-rpc':p.error('--invite requires check-rpc')
    if a.action=='build':
        if not a.config or not a.output:p.error('build requires --config and a new --output directory')
        result=build(c,a.output)
    elif a.action=='check-rpc':
        if not a.config:p.error('check-rpc requires --config')
        c=validate(c,complete=a.invite is None)
        result=check_rpc(c, preflight_policy(c,a.invite))
    elif a.action=='validate':
        c=validate(c,complete=True)
        result=dict(status='LAUNCH_CONFIG_VALID',config=c,sends_transactions=False,
                    **scan_review(c, bitcoin_launch_policy(c)))
    else:
        if not a.output:p.error('init/wizard requires --output')
        c=wizard(c) if a.action=='wizard' else validate(c)
        with a.output.open('x') as stream:stream.write(json.dumps(c,ensure_ascii=False,indent=2)+'\n')
        result=dict(status='LAUNCH_DRAFT_SAVED',config=str(a.output),sends_transactions=False)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


def main(argv=None):
    try:return _main(argv)
    except PreflightError as error:
        emit_debug(error, code=str(error))
        print(json.dumps(dict(status='ERROR',code=str(error),**error.details),ensure_ascii=False))
        return 2
    except ValueError as error:
        if len(error.args) != 1 or type(error.args[0]) is not str:raise
        code, _, detail = error.args[0].partition(':')
        if code not in LAUNCH_ERRORS:raise
        emit_debug(error, code=code)
        result=dict(status='ERROR',code=code)
        if code=='LAUNCH_INPUTS_REQUIRED':
            result['missing']=[k for k in detail.split(',') if k in defaults()]
        print(json.dumps(result,ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
