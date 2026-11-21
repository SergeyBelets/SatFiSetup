"""Preflight and run isolated system test layers; never qualify a deployment.

python -m ops.system_modes preflight --mode all
python -m ops.system_modes run --mode evm --output /absolute/new-directory
Tools may be supplied on PATH or through SATOHSI_ANVIL / SATOHSI_BITCOIND /
SATOHSI_FORGE / SATOHSI_SOLC / SATOHSI_CRYPTO_BIN / SATOHSI_ZK_WORKER_HOST.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3
LAYERS = ("python", "contracts", "evm", "zk-cached", "bitcoin", "zk-testnet4", "zk-fresh")
SIGNER_TESTS = ("test_dkg_protocol.py", "test_signing_service.py", "test_secure_state.py",
    "test_security_crypto_review.py", "test_onboarding.py", "test_onboarding_policy_v3.py",
    "test_bootstrap_ulr.py", "test_bootstrap_system.py", "test_bootstrap_custody.py", "test_ulr_treasury.py", "test_family_registry_policy.py",
    "test_platform_support.py", "test_bootstrap_funding.py", "test_bootstrap_distribution.py")
TOOLS = {
    "anvil": ("SATOHSI_ANVIL", "ops/.local-tools/acceptance-20261102/anvil"),
    "forge": ("SATOHSI_FORGE", "ops/.local-tools/acceptance-20261102/forge"),
    "solc": ("SATOHSI_SOLC", "ops/.local-tools/acceptance-20261102/solc"),
    "bitcoind": ("SATOHSI_BITCOIND", "ops/.local-tools/bitcoin-core-30.3/bin/bitcoind"),
    "crypto": ("SATOHSI_CRYPTO_BIN", "frost-crypto/target/debug/satohsi-frost-crypto"),
    "zk-testnet4-host": ("SATOHSI_ZK_TESTNET4_WORKER_HOST", "ops/.local-tools/zk-worker/satoshifi-testnet4-zk-worker-host"),
    "zk-host": ("SATOHSI_ZK_WORKER_HOST", "ops/.local-tools/zk-worker/satoshifi-zk-worker-host"),
}
REQUIRED = {"python": ("crypto", "zk-host"), "contracts": ("forge", "solc"),
    "evm": ("anvil", "crypto"), "zk-cached": ("anvil", "crypto", "zk-host"),
    "zk-testnet4": ("anvil", "crypto", "zk-testnet4-host"),
    "bitcoin": ("anvil", "bitcoind", "crypto"), "zk-fresh": ("zk-host",), "signer": ("crypto",)}
SCOPE = {"python": "SDK unit fixtures and supported deployment profile matrix",
    "contracts": "all discoverable Solidity tests; qualified derivation only in test fixtures",
    "evm": "full relay/regtest graph create/reuse with independent native FROST processes and ULR owner CLI",
    "zk-cached": "historical mainnet receipt refusal by the active verifier on local EVM; optional locally generated 3.1 proof",
    "bitcoin": "isolated real Core regtest, native FROST and EVM lifecycle suite",
    "zk-fresh": "operator-bound live MAINNET_CANDIDATE contour; fresh proof and observations",
    "zk-testnet4": "testnet4 historical native worker and real verifier refusal on local Sepolia chain ID; no fresh proof",
    "signer": "portable signer/ULR/bootstrap SDK subset; no local prover required"}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()


def tool(name):
    variable, fallback = TOOLS[name]
    explicit = os.environ.get(variable) or (os.environ.get('FOUNDRY_SOLC') if name=='solc' else None)
    executable = {'crypto':'satohsi-frost-crypto', 'zk-host':'satoshifi-zk-worker-host', 'zk-testnet4-host':'satoshifi-testnet4-zk-worker-host'}.get(name, name)
    path = explicit or shutil.which(executable) or (str(ROOT / fallback) if fallback else None)
    if not explicit and name=='solc' and (not path or not Path(path).is_file()):
        svm = Path(os.environ.get('SVM_HOME',str(Path.home()/'.svm')))
        path = str(svm/'0.8.30/solc-0.8.30')
    return str(Path(path).resolve()) if path and Path(path).is_file() and os.access(path, os.X_OK) else None


def source_hashes():
    paths = list((ROOT / "signer-agent/signer_agent").glob("*.py"))
    paths += list((ROOT / "signer-agent/tests").glob("*.py"))
    paths += list((ROOT / "tests/e2e").glob("*.py"))
    paths += list((ROOT / "ops").rglob("*.py"))
    paths += list((ROOT / "web3/contracts/src").rglob("*.sol"))
    paths += list((ROOT / "web3/test").rglob("*.sol"))
    paths += list((ROOT / "config").glob("system-bootstrap-*.json"))
    paths += [ROOT / "config/zk-prover-linux-x86_64.json"]
    paths += [ROOT / "web3/foundry.toml", ROOT / "frost-crypto/src/main.rs",
        ROOT / "signer-agent/pyproject.toml"]
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(set(paths)) if p.is_file()}


def preflight(modes, directory, suite_plan=None):
    tools = {k: tool(k) for k in TOOLS}
    free = shutil.disk_usage(directory).free
    # Explicit harness resource floors, not a guarantee of proof capacity.
    minimum = {"python": GIB//4, "contracts": 2*GIB, "evm": GIB//2,
        "zk-cached": GIB//2, "bitcoin": GIB, "zk-testnet4": GIB//2, "zk-fresh": 8*GIB, "signer": GIB//4}
    deps = [n for n in ("pytest", "web3", "eth_tester", "cryptography") if importlib.util.find_spec(n) is None]
    rows = []
    for mode in modes:
        missing = ["tool:"+k for k in REQUIRED[mode] if not tools[k]]
        from signer_agent.platform_support import capabilities
        platform = capabilities()
        if not platform['signer']: missing.append(platform['reason'])
        if mode in ('zk-cached','zk-fresh') and not platform['local_prover_supported']:
            missing.append('platform:local prover requires macOS arm64 or Linux/WSL2 x86-64')
        if mode != "contracts": missing += ["python:"+n for n in deps]
        if free < minimum[mode]: missing.append("disk:"+str(minimum[mode]))
        if mode in ("evm", "bitcoin") and not Path(sys.executable).with_name("frost-signer").is_file():
            missing.append("installed:frost-signer (pip install --no-deps -e ./signer-agent)")
        if mode != "contracts":
            artifacts = [ROOT / "web3/contracts/out/ULRStaking.sol/ULRStaking.json"]
            if mode == "bitcoin":
                artifacts += [ROOT / "web3/contracts/out/SPVServicePayment.t.sol/GasStateFixture.json",
                    ROOT / "web3/contracts/out/GasCallerFixture.sol/GasCallerAvailabilityFixture.json"]
            if mode in ("python", "zk-cached", "zk-fresh"):
                artifacts += [ROOT / "rust/zkspv/state-adapter/out/ZkBitcoinStateV2.sol/ZkBitcoinStateV2.json"]
            missing += ["artifact:"+str(p.relative_to(ROOT)) for p in artifacts if not p.is_file()]
        if mode == "zk-testnet4":
            required = ROOT / "rust/zkspv/state-adapter/out/ZkBitcoinTestnet4StateV2.sol/ZkBitcoinTestnet4StateV2.json"
            if not required.is_file(): missing.append("artifact:"+str(required.relative_to(ROOT)))
        if mode == "zk-cached":
            evidence = ROOT / "rust/zkspv/state-proof-integration/evidence/local-state"
            missing += ["receipt:"+f for f in ("groth16/receipt.bin", "groth16/verified/seal.bin", "state.journal")
                if not (evidence / f).is_file()]
        if mode == "bitcoin" and tools["bitcoind"]:
            v = subprocess.run([tools["bitcoind"], "-version", "-nosettings"], capture_output=True, text=True, timeout=10)
            if v.returncode or "v30.3" not in v.stdout: missing.append("Core:30.3 required")
        if mode == "zk-fresh":
            if not suite_plan: missing.append("operator:--suite-plan required")
            else:
                try:
                    from ops.zkspv.contour_suite import validate
                    p = validate(json.loads(Path(suite_plan).read_text()))
                    if p["mode"] != "MAINNET_CANDIDATE": raise ValueError("MAINNET_CANDIDATE required")
                    if any(j["plan"] is None for j in p["jobs"]): raise ValueError("unbound jobs remain")
                    # Inspect configuration without constructing Suite (which writes journals).
                    from ops.zkspv.contour_suite_plan import verify_job
                    for j in p["jobs"]: verify_job(j, Path(p["directory"]))
                except (ValueError, OSError, KeyError) as e: missing.append("suite:"+str(e))
        rows.append(dict(mode=mode, scope=SCOPE[mode], status="BLOCKED" if missing else "READY_TO_RUN",
            missing=missing, minimum_free_bytes=minimum[mode]))
    return dict(version="1", qualified=False, free_bytes=free, modes=rows,
        tools={k: dict(path=v, sha256=sha(v)) if v else None for k,v in tools.items()},
        network_scope="local tests; public Ethereum/mainnet/Sepolia and Bitcoin testnet4 require separate bound deployment inputs")


def check_junit(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(not c.get("name") or any(e.tag in ("failure", "error", "skipped") for e in c) for c in cases):
        raise RuntimeError("TESTS_EMPTY_FAILED_OR_SKIPPED")
    if any(int(s.get("errors", "0")) or int(s.get("failures", "0")) or int(s.get("skipped", "0"))
           for s in root.iter("testsuite")):
        raise RuntimeError("TEST_SUITE_INCOMPLETE")
    return len(cases)


def prepare_gas_tests(directory, env):
    """Explicit synthetic SPV inputs, isolated from the production artifact tree."""
    fixtures = directory / "gas-fixtures"
    fixtures.mkdir(mode=0o700)
    for name in ("SPVServicePayment.t.sol", "GasCallerFixture.sol"):
        shutil.copytree(ROOT / "web3/contracts/out" / name, fixtures / name)
    env.update(SATOSHIFI_GAS_E2E_RPC=env["SATOHSI_TEST_RPC"],
        SATOSHIFI_GAS_FIXTURE_OUT=str(fixtures), SATOSHIFI_GAS_CALLER_FIXTURE_OUT=str(fixtures))


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


@contextmanager
def nodes(directory, env, bitcoin=False):
    from web3 import Web3
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix="nodes-", dir=directory) as temp:
        home = Path(temp); ep, bp = port(), port()
        try:
            evm_log = (directory / "anvil.log").open("w"); logs.append(evm_log)
            processes.append(subprocess.Popen([tool("anvil"), "--host", "127.0.0.1", "--port", str(ep),
                "--chain-id", "31337", "--hardfork", "osaka", "--gas-limit", "16777216", "--silent"],
                stdout=evm_log, stderr=subprocess.STDOUT))
            env["SATOHSI_TEST_RPC"] = f"http://127.0.0.1:{ep}"
            if bitcoin:
                btc_log = (directory / "core.log").open("w"); logs.append(btc_log)
                processes.append(subprocess.Popen([tool("bitcoind"), "-regtest", "-datadir="+temp,
                    "-server", "-txindex=1", "-listen=0", "-discover=0", "-dnsseed=0", "-dbcache=64",
                    "-printtoconsole=1", "-rpcbind=127.0.0.1", "-rpcport="+str(bp)], stdout=btc_log, stderr=subprocess.STDOUT))
                env.update(SATOHSI_BTC_RPC=f"http://127.0.0.1:{bp}", SATOHSI_BTC_COOKIE=str(home/"regtest/.cookie"))
            w3 = Web3(Web3.HTTPProvider(env["SATOHSI_TEST_RPC"], request_kwargs={"timeout": 1}))
            for _ in range(200):
                if any(p.poll() is not None for p in processes): raise RuntimeError("LOCAL_NODE_EXITED")
                core_ready = True
                if bitcoin:
                    from signer_agent.bitcoin_worker import BitcoinRPC
                    try: core_ready = BitcoinRPC(env["SATOHSI_BTC_RPC"], env["SATOHSI_BTC_COOKIE"]).call("getblockchaininfo")["chain"] == "regtest"
                    except (OSError, ValueError, RuntimeError): core_ready = False
                if w3.is_connected() and core_ready: break
                time.sleep(.1)
            else: raise RuntimeError("LOCAL_NODE_START_TIMEOUT")
            yield
        finally:
            for p in processes: p.terminate()
            for p in processes:
                try: p.wait(timeout=15)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
            for f in logs: f.close()


def execute(mode, directory, environment, suite_plan=None):
    env = dict(environment)
    junit = directory / "pytest.xml"
    base = [sys.executable, "-m", "pytest", "-x", "-q", "-o", "junit_family=legacy",
        "--basetemp="+str(directory/"pytest"), "--junitxml="+str(junit)]
    if mode == "python": command = base + ["signer-agent/tests"]
    elif mode == "signer": command = base + ['signer-agent/tests/'+name for name in SIGNER_TESTS]
    elif mode == "evm": command = base + ["tests/e2e/test_system_bootstrap.py"]
    elif mode == "zk-cached": command = base + ["tests/e2e/test_zk_bootstrap_e2e.py", "tests/e2e/test_zk_worker_e2e.py"]
    elif mode == "zk-testnet4": command = base + ["tests/e2e/test_zk_testnet4_e2e.py"]
    elif mode == "bitcoin":
        excluded = ("test_zk_testnet4_e2e.py", "test_system_bootstrap.py", "test_zk_bootstrap_e2e.py", "test_zk_worker_e2e.py")
        command = base + [str(p.relative_to(ROOT)) for p in sorted((ROOT/"tests/e2e").glob("test_*.py")) if p.name not in excluded]
    elif mode == "contracts": command = [sys.executable, "-m", "ops.system_modes", "_contracts", "--output", str(directory)]
    else:
        raise ValueError("Fresh proving uses the operator's durable contour; use its explicit run command after preflight")
    def run():
        with (directory/"command.log").open("w") as output:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=14400)
        if result.returncode: raise RuntimeError("COMMAND_FAILED: "+str(result.returncode)+"; see command.log")
    if mode in ("evm", "bitcoin"):
        with nodes(directory, env, bitcoin=mode=="bitcoin"):
            if mode == "bitcoin": prepare_gas_tests(directory, env)
            run()
    else: run()
    if mode == "contracts":
        report = json.loads((directory/"solidity.json").read_text())
        if report.get("status") != "passed" or not report.get("passed") or report.get("skipped"):
            raise RuntimeError("SOLIDITY_INCOMPLETE")
        count = report["passed"]
    else: count = check_junit(junit)
    return dict(status="PASSED", tests=count, command=command, scope=SCOPE[mode])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run", "_contracts"))
    parser.add_argument("--mode", choices=(*LAYERS, "signer", "relay", "local", "all"), default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--suite-plan", type=Path)
    args = parser.parse_args(argv)
    if args.action == "_contracts":
        from ops.local_acceptance import run_solidity_suites
        run_solidity_suites(args.output / "solidity.json")
        return 0
    modes = list(LAYERS if args.mode == "all" else LAYERS[:-1] if args.mode == "local" else
        ('signer','contracts','evm','bitcoin') if args.mode=='relay' else (args.mode,))
    if args.action == "run" and not args.output: parser.error("run requires a fresh --output directory")
    directory = args.output.resolve() if args.output else ROOT
    if args.action == "run": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = preflight(modes, directory, args.suite_plan)
    if args.action == "preflight":
        print(json.dumps(report, indent=2))
        return int(any(r["status"] == "BLOCKED" for r in report["modes"]))
    initial = source_hashes()
    report.update(source_sha256=initial, results={}, status="RUNNING")
    path = directory/"report.json"
    def save():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2)+"\n"); temporary.replace(path)
    save()
    environment = dict(os.environ, SATOHSI_REQUIRE_NATIVE="1", PYTEST_ADDOPTS="", PYTHONPATH=str(ROOT/"signer-agent")+os.pathsep+str(ROOT),
        PYTHONDONTWRITEBYTECODE="1")
    for name, spec in report["tools"].items():
        if spec: environment[TOOLS[name][0]] = spec["path"]
    paths = [str(Path(s["path"]).parent) for s in report["tools"].values() if s]
    environment["PATH"] = os.pathsep.join([*paths, os.environ.get("PATH", "")])
    if tool("solc"): environment["FOUNDRY_SOLC"] = tool("solc")
    for row in report["modes"]:
        mode = row["mode"]
        if row["status"] == "BLOCKED": report["results"][mode] = dict(status="BLOCKED", missing=row["missing"])
        elif mode == "zk-fresh":
            report["results"][mode] = dict(status="NOT_RUN", reason="Run operator-bound durable contour explicitly; preflight alone is not evidence")
        else:
            (directory/mode).mkdir(mode=0o700)
            try:
                if source_hashes() != initial: raise RuntimeError("SOURCE_CHANGED")
                if any(s and sha(s["path"]) != s["sha256"] for s in report["tools"].values()): raise RuntimeError("TOOL_CHANGED")
                print("RUN "+mode, flush=True)
                result = execute(mode, directory/mode, environment, args.suite_plan)
                if source_hashes() != initial: raise RuntimeError("SOURCE_CHANGED")
                report["results"][mode] = result
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                report["results"][mode] = dict(status="FAILED", error=str(e))
        save()
    report["status"] = "PASSED" if all(r["status"] == "PASSED" for r in report["results"].values()) else "INCOMPLETE"
    save(); print(json.dumps(dict(status=report["status"], report=str(path), qualified=False)))
    return int(report["status"] != "PASSED")


if __name__ == "__main__": raise SystemExit(main())
