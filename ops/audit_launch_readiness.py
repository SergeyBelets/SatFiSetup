"""Offline audit of public launch inputs and runbooks; never grants deployment readiness.

Run from a built checkout with the SDK installed. Only CLI --help is executed;
no RPC, participant identities or private worker configurations are read.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
RUNBOOKS = ('docs/COLD_START.md', 'docs/LAUNCH_CONFIGURATOR.md',
    'docs/FEDERATION_CONFEDERATION_SETUP.md', 'docs/NFT_CONFIGURATION.md',
    'docs/CROSS_PLATFORM_QUICKSTART.md', 'docs/SYSTEM_TEST_MODES.md',
    'docs/ZK_PROVER_LINUX.md',
    'docs/ULR_BOOTSTRAP.md', 'docs/ULR_STAKING.md', 'docs/ULR_TREASURIES.md',
    'docs/ULR_CANDIDATES_AND_VOTING.md', 'docs/FIRST_DKG_ONCHAIN.md',
    'docs/BOOTSTRAP.md', 'docs/CONFIGURATION.md', 'docs/NETWORKS_AND_AUTOMATION.md',
    'docs/BITCOIN_MAINNET_RUNBOOK.md', 'docs/ETHEREUM_MAINNET_RUNBOOK.md',
    'docs/BITCOIN_KEY_FAMILY.md', 'docs/ADDRESS_REQUESTS_WATCH.md', 'docs/ADDRESS_REGISTRATION.md',
    'docs/BITCOIN_CUSTODY_TRANSFER.md',
    'docs/BITCOIN_ADDRESS_SIGNING.md', 'docs/BITCOIN_ONCHAIN_RUNBOOK.md', 'docs/BITCOIN_WITHDRAWALS.md',
    'docs/ZK_SPV_WORKER.md', 'docs/ZKSPV_CONTOUR.md', 'docs/FEE_FLOW.md')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError('DUPLICATE_JSON_KEY:'+key)
            value[key] = item
        return value
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def cli_examples(body):
    """Extract command prefixes/flags without expanding shell variables or running examples."""
    for block in re.findall(r'^```(?:sh|bash)\n(.*?)^```', body, re.M | re.S):
        for line in block.replace('\\\n', ' ').splitlines():
            if not re.match(r'^\s*frost-signer\s+', line): continue
            tokens = shlex.split(line, comments=True)
            prefix = []
            for token in tokens[1:]:
                if not re.fullmatch(r'[a-z][a-z0-9-]*',token): break
                prefix.append(token)
            # The documented btc() wrapper forwards an action through "$@".
            # DKG actions share these base options; verify's help checks them
            # without choosing or executing a Bitcoin operation.
            if prefix==['dkg'] and len(tokens)>2 and tokens[2]=='$@': prefix.append('verify')
            # This existing CLI route selects the profile doctor by flag.
            if prefix==['doctor'] and '--profile' in tokens: prefix+=['--profile','__audit_help_only__']
            yield prefix, sorted({t.split('=',1)[0] for t in tokens if re.fullmatch(r'--[a-z][a-z0-9-]*(?:=.*)?',t)})


def check_cli(executable, docs):
    cache = {}; examples = []
    for name in docs:
        for prefix, flags in cli_examples((ROOT/name).read_text()):
            key = tuple(prefix)
            if key not in cache:
                result = subprocess.run([str(executable), *prefix, '--help'], cwd=ROOT,
                    capture_output=True, text=True, timeout=30)
                if result.returncode: raise ValueError('CLI_HELP_FAILED:'+' '.join(prefix))
                cache[key] = set(re.findall(r'--[a-z][a-z0-9-]*',result.stdout))
            unknown = set(flags)-cache[key]
            if unknown: raise ValueError('UNDOCUMENTED_CLI_FLAG:'+name+':'+' '.join(prefix)+':'+','.join(sorted(unknown)))
            examples.append(dict(file=name, command='frost-signer '+' '.join(prefix), flags=flags))
    return dict(examples=len(examples), commands=len(cache), entries=examples,
        execution='help only; example transactions were not executed',
        shell_wrappers='common DKG wrapper options checked via verify help; runtime values are not expanded')


def check_links(docs, output):
    count = 0; missing = []
    for name in docs:
        body = re.sub(r'^```[^\n]*\n.*?^```\s*$', '', (ROOT/name).read_text(), flags=re.M|re.S)
        body = re.sub(r'`+[^`]*`+', '', body)
        for match in re.finditer(r'\]\((?:<([^>]+)>|([^\s)]+))', body):
            url = match[1] or match[2]
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',url) or url.startswith('#'): continue
            target = unquote(url.split('#',1)[0])
            if not target: continue
            count += 1
            path = ((ROOT/name).parent/target).resolve()
            if not path.exists() and path != output/'validation.json': missing.append(dict(file=name,target=target))
    if missing: raise ValueError('MISSING_LOCAL_LINKS:'+json.dumps(missing,ensure_ascii=False))
    return dict(checked=count, scope='local file destinations; external URLs and heading anchors excluded')


def check_examples():
    from signer_agent.bootstrap_system import main as prepare
    from signer_agent.ulr_treasury import load_pin
    from ops.zkspv.contour_suite_plan import validate_spec
    configs = []
    for path in sorted((ROOT/'config').glob('*.json')):
        value = read_json(path)
        configs.append(dict(file=str(path.relative_to(ROOT)), version=value.get('version'),
            sha256=sha(path), kind='public template; live bindings must be supplied'))
    with tempfile.TemporaryDirectory(prefix='launch-profiles-') as directory:
        for name, ulr in [('testnet','create'),('zk','zk'),('zk-testnet4','zk')]:
            output = Path(directory)/(name+'.json')
            args = ['--ulr-policy',str(ROOT/f'config/ulr-bootstrap-{ulr}.example.json'),
                '--bitcoin-custody-policy',str(ROOT/'config/bitcoin-custody.example.json'),
                '--token-metadata',str(ROOT/'config/token-metadata.example.json'),
                '--qualification-report',str(ROOT/'rust/zkspv/derivation-qualification/qualification-report.json'),
                '--service-policy',str(ROOT/'config/bitcoin-bootstrap-service-policy.example.json'),
                '--protocol-fee-cap-sats','100000','--max-vbytes','10000','--max-packet-bytes','1024','--output',str(output)]
            with contextlib.redirect_stdout(io.StringIO()): prepare(args)
            if read_json(output)!=read_json(ROOT/f'config/system-bootstrap-{name}.example.json'):
                raise ValueError('SYSTEM_EXAMPLE_BUILD_DRIFT:'+name)
    load_pin(ROOT/'config/ulr-treasury.example.json')
    suite = read_json(ROOT/'config/zkspv-contour-suite.example.json')
    for job in suite['jobs']: validate_spec(job['spec'])
    return dict(json_count=len(configs), inventory=configs, full_system_examples_reproduced=3,
        ulr_pin_structure='PASS; example addresses are not live bindings',
        contour_job_specs_validated=len(suite['jobs']), runtime_inputs='operator configuration required')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New public report directory')
    parser.add_argument('--diagrams',type=Path,help='Manifest from render_docs_diagrams.cjs')
    args = parser.parse_args(argv)
    output = args.output.resolve(); output.mkdir(mode=0o700,parents=True,exist_ok=False)
    checks = {}; errors = []
    def check(name, action):
        try: checks[name] = dict(status='PASS',result=action())
        except (ValueError,OSError,KeyError,subprocess.SubprocessError) as error:
            checks[name] = dict(status='FAIL',error=str(error)); errors.append(name)
    def command(script, *options):
        result = subprocess.run([sys.executable,script,*options],cwd=ROOT,capture_output=True,text=True,timeout=120)
        (output/(Path(script).stem+('-zk' if '--zk' in options else '')+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode: raise ValueError('CHECK_FAILED:'+script)
        return dict(command=[sys.executable,script,*options],returncode=0)
    check('abi',lambda:command('ops/generate_abis.py','--check','--zk'))
    check('fee_runtime_hashes',lambda:command('ops/generate_fee_runtime_hashes.py','--check'))
    check('contract_sizes',lambda:command('ops/check_contract_sizes.py','--output',str(output/'contract-sizes.json')))
    check('examples',check_examples)
    docs = ['README.md','signer-agent/README.md','web3/README.md']+[str(p.relative_to(ROOT)) for p in sorted((ROOT/'docs').glob('*.md'))]
    check('local_links',lambda:check_links(docs,output))
    def shells():
        count = 0
        for name in RUNBOOKS:
            for code in re.findall(r'^```(?:sh|bash)\n(.*?)^```',(ROOT/name).read_text(),re.M|re.S):
                result = subprocess.run(['bash','-n'],input=code,capture_output=True,text=True,timeout=10)
                if result.returncode: raise ValueError('SHELL_SYNTAX:'+name)
                count += 1
        return dict(blocks=count,execution='syntax only')
    check('shell_examples',shells)
    check('cli_examples',lambda:check_cli(Path(sys.executable).with_name('frost-signer'),RUNBOOKS))
    def guest():
        from signer_agent.zk_worker_prover import preparation_sources,pinned_tool,ROOT as sdk_root
        build=read_json(sdk_root/'rust/zkspv/guest-integration/report.json')
        sources=preparation_sources(build)
        for name in ('r0vm','satoshifi-receipt-bridge','wrap_groth16'):pinned_tool(name)
        return dict(source_count=len(sources),guest_and_tool_pins='matched',new_proof=False,qualified=False)
    check('retained_zk_inputs',guest)
    if args.diagrams:
        def diagrams():
            manifest=read_json(args.diagrams); expected={}
            for name in ('docs/LAUNCH_READINESS.md',):
                for index,m in enumerate(re.finditer(r'^```mermaid[^\n]*\n([\s\S]*?)^```\s*$',(ROOT/name).read_text(),re.M),1):
                    expected[(name,index)]=hashlib.sha256(m[1].encode()).hexdigest()
            actual={}
            for figure in manifest['results']:
                if figure['out_of_bounds_labels']: raise ValueError('DIAGRAM_LABEL_CLIPPED')
                for field in ('source','svg'):
                    if sha(ROOT/figure[field])!=figure[field+'_sha256']: raise ValueError('DIAGRAM_HASH')
                actual[(figure['file'],figure['index'])]=figure['source_sha256']
            if actual!=expected: raise ValueError('DIAGRAM_SOURCE_SET')
            return dict(count=len(actual),manifest=str(args.diagrams),source_hashes='matched',clipped_labels=0)
        check('diagrams',diagrams)
    from ops.bootstrap_host import check as host_check
    from ops.system_modes import preflight,LAYERS,source_hashes
    report=dict(schema='satoshifi.launch-readiness-audit.v1',
        status='REPOSITORY_CHECKS_PASSED_OPERATOR_CONFIGURATION_REQUIRED' if not errors else 'CHECKS_FAILED',
        checks=checks,failed_checks=errors,launch_ready=False,qualified=False,
        host=host_check(),test_preflight=preflight(LAYERS,output),
        operator_inputs_required=['reviewed source version','participants/thresholds','own identity and original journals',
            'own EVM and Bitcoin RPC/Core','checkpoint policy','real USD address/runtime/decimals and funding',
            'actual verified manifest and independent pins','worker/service/archive/inventory inputs'],
        unconfirmed=['full clean Bitcoin layer after harness fixes','full fresh proving on native Linux and physical Windows/WSL2',
            'new monetary ZK contour','independent production qualification'],
        source_sha256=source_hashes(),documentation_sha256={n:sha(ROOT/n) for n in docs},
        protocol_tests_executed=False)
    (output/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],failed_checks=errors,report=str(output/'validation.json'),launch_ready=False)))
    return int(bool(errors))


if __name__=='__main__':raise SystemExit(main())
