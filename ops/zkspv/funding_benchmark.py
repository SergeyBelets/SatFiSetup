"""Reproduce local funding-parser tests and actual Osaka transaction receipts.

Usage: python -m ops.zkspv.funding_benchmark --forge /path/to/forge --anvil /path/to/anvil

Only a temporary copied Foundry project is built. No production build directory,
consumer, chain, or account is used. Existing local solc and Rust binaries are
required; this runner neither downloads dependencies nor starts a network fork.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from eth_abi import decode, encode
from eth_hash.auto import keccak


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "ops/artifacts/zkspv"
GAS_LIMIT = 16_777_216
SOURCE_FILES = (
    "web3/foundry.toml",
    "web3/contracts/src/core/BitcoinFundingParser.sol",
    "web3/contracts/src/core/BitcoinTxParser.sol",
    "web3/test/BitcoinFundingParser.t.sol",
    "web3/test/fixtures/funding-stripped-v1.json",
    "tests/zkspv/funding_vectors.py",
    "tests/zkspv/test_funding_parser_vectors.py",
    "ops/zkspv/funding_benchmark.py",
)
FORGE_STD = Path("web3/lib/openzeppelin-contracts/lib/forge-std/src")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source_hashes() -> dict[str, str]:
    files = [ROOT / name for name in SOURCE_FILES]
    files.extend(sorted((ROOT / "web3/test/fixtures/funding-stripped-v1").glob("*")))
    return {str(path.relative_to(ROOT)): sha(path) for path in files if path.is_file()}


def dependency_hashes() -> dict[str, str]:
    return {str(path.relative_to(ROOT)): sha(path)
            for path in sorted((ROOT / FORGE_STD).rglob("*.sol"))}


def run_logged(command: list[str], cwd: Path, log: Path, env: dict[str, str]) -> None:
    with log.open("a") as output:
        output.write("$ " + shlex.join(command) + "\n")
        output.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log.name}")


def copy_project(project: Path) -> None:
    for name in SOURCE_FILES[:5]:
        source = ROOT / name
        destination = project / source.relative_to(ROOT / "web3")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copytree(ROOT / FORGE_STD, project / FORGE_STD.relative_to("web3"))
    # Real copies keep fixture access within this project's explicit read grant.
    shutil.copytree(ROOT / "web3/test/fixtures/funding-stripped-v1",
                    project / "test/fixtures/funding-stripped-v1")


def summarize_tests(value: dict) -> dict:
    results = []
    for suite in value.values():
        if isinstance(suite, dict):
            results.extend(suite.get("test_results", {}).values())
    counts = {
        "passed": sum(item.get("status") == "Success" for item in results),
        "failed": sum(item.get("status") == "Failure" for item in results),
        "skipped": sum(item.get("status") == "Skipped" for item in results),
    }
    if not results or sum(counts.values()) != len(results):
        raise RuntimeError("unrecognized or empty Foundry result schema")
    counts["status"] = "PASS" if counts["passed"] and not counts["failed"] and not counts["skipped"] else "FAIL"
    counts["log"] = "evidence/funding-parser-forge.log"
    counts["results"] = "evidence/funding-parser-forge-results.json"
    return counts


class Rpc:
    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}"
        self.counter = 0

    def call(self, method: str, params=None):
        self.counter += 1
        request = Request(self.url, json.dumps({"jsonrpc": "2.0", "id": self.counter,
                          "method": method, "params": params or []}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            value = json.load(response)
        if "error" in value:
            raise RuntimeError(f"local RPC {method}: {value['error']}")
        return value["result"]

    def receipt(self, tx_hash: str) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            receipt = self.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                if int(receipt["status"], 16) != 1:
                    raise RuntimeError(f"local transaction failed: {tx_hash}")
                return receipt
            time.sleep(0.05)
        raise TimeoutError("local transaction receipt timeout")


@contextmanager
def local_node(anvil: str, project: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [anvil, "--host", "127.0.0.1", "--port", str(port), "--hardfork", "osaka",
               "--gas-limit", str(GAS_LIMIT), "--enable-tx-gas-limit", "--chain-id", "31337",
               "--accounts", "1", "--timestamp", "1800000000"]
    # The default banner includes disposable account keys. Never persist it.
    process = subprocess.Popen(command, cwd=project, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        rpc = Rpc(port)
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("temporary loopback Anvil exited before readiness")
            try:
                chain_id = int(rpc.call("eth_chainId"), 16)
                break
            except (URLError, ConnectionError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("temporary loopback Anvil readiness timeout")
                time.sleep(0.1)
        if chain_id != 31337:
            raise RuntimeError("unexpected local chain id")
        yield rpc, command
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def calldata_accounting(data: bytes) -> dict:
    zero = data.count(0)
    nonzero = len(data) - zero
    tokens = zero + 4 * nonzero
    return {"calldata_bytes": len(data), "zero_bytes": zero, "nonzero_bytes": nonzero,
            "calldata_tokens": tokens, "standard_intrinsic_gas": 21_000 + 4 * tokens,
            "eip7623_floor_gas": 21_000 + 10 * tokens,
            "calldata_sha256": hashlib.sha256(data).hexdigest()}


def transaction_cases(rpc: Rpc, address: str, account: str, cases) -> list[dict]:
    records = []
    selected = [(case, case.target_vout, case.name) for case in cases]
    selected.extend((case, 0, case.name + "_selected_long_script")
                    for case in cases if case.name in ("script_65536", "stripped_100000"))
    selector = keccak(b"inspect(bytes,uint32)")[:4]
    for case, target, name in selected:
        raw = case.serialize()
        expected = case.manifest_entry()["expected"]
        script = case.outputs[target].script
        data = selector + encode(["bytes", "uint32"], [raw, target])
        tx = {"from": account, "to": address, "data": "0x" + data.hex(),
              "gas": hex(GAS_LIMIT), "gasPrice": hex(2_000_000_000)}
        returned = decode(["bytes32", "bytes32", "uint256", "uint256", "uint64", "bytes32"],
                          bytes.fromhex(rpc.call("eth_call", [tx, "latest"])[2:]))
        wanted = (bytes.fromhex(expected["raw_sha256d"]), bytes.fromhex(expected["inputs_abi_keccak256"]),
                  len(case.inputs), len(case.outputs), case.outputs[target].value, keccak(script))
        if returned != wanted:
            raise RuntimeError(f"eth_call golden mismatch: {name}")
        tx_hash = rpc.call("eth_sendTransaction", [tx])
        receipt = rpc.receipt(tx_hash)
        gas = int(receipt["gasUsed"], 16)
        accounting = calldata_accounting(data)
        if not max(accounting["standard_intrinsic_gas"], accounting["eip7623_floor_gas"]) <= gas <= GAS_LIMIT:
            raise RuntimeError(f"receipt gas outside asserted local Osaka bounds: {name}")
        records.append({"name": name, "source_fixture": case.name, "target_vout": target,
                        "stripped_bytes": len(raw), "target_script_bytes": len(script),
                        "input_count": len(case.inputs), "output_count": len(case.outputs),
                        "status": "PASS", "golden_eth_call": "PASS", "gas_used": gas,
                        "gas_limit": GAS_LIMIT, "gas_headroom": GAS_LIMIT - gas,
                        "receipt_minus_standard_intrinsic_gas": gas - accounting["standard_intrinsic_gas"],
                        "receipt_equals_data_floor": gas == accounting["eip7623_floor_gas"],
                        "calldata": accounting,
                        "returned": {"raw_sha256d": returned[0].hex(), "inputs_abi_keccak256": returned[1].hex(),
                                     "input_count": returned[2], "output_count": returned[3],
                                     "target_value": returned[4], "target_script_keccak256": returned[5].hex()},
                        "receipt": receipt})
    return records


def benchmark(args) -> dict:
    out = args.output_dir.resolve()
    evidence = out / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    log = evidence / "funding-parser-forge.log"
    log.write_text("")
    report = {"schema": "satoshifi.zkspv.funding-parser.v1", "status": "FAIL",
              "recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "LOCAL_EVM_PARSER_ONLY",
              "production_ready": False, "address_ready": False, "target_network": None,
              "source_sha256": source_hashes(), "forge_std_source_sha256": dependency_hashes(),
              "tests": {"status": "UNKNOWN"}, "receipts": {"status": "UNKNOWN"},
              "limits": {"custody_per_input_storage": "UNKNOWN", "integrated_memory_and_storage_cost": "UNKNOWN",
                         "spv_inclusion_canonicality_depth_fresh": "UNKNOWN", "funding_address_ready": "UNKNOWN",
                         "bitcoin_script_execution_and_unspentness": "NOT_TESTED",
                         "production_network_gas_acceptance": "UNKNOWN"}}
    try:
        sys.path.insert(0, str(ROOT / "tests/zkspv"))
        from funding_vectors import check_rust, expected_artifacts, vectors
        cases = vectors()
        for path, expected in expected_artifacts().items():
            if path.read_text() != expected:
                raise RuntimeError(f"fixture is not reproducible: {path.relative_to(ROOT)}")
        for case in cases:
            check_rust(case)
        report["reference_parser"] = {"status": "PASS", "parser": "rust-bitcoin-0.32.7", "cases": len(cases)}
        report["toolchain"] = {"python": sys.version, "platform": platform.platform()}
        for name in ("forge", "anvil"):
            binary = Path(getattr(args, name)).resolve()
            report["toolchain"][name] = {"path": str(binary), "sha256": sha(binary),
                "version": subprocess.check_output([str(binary), "--version"], text=True).strip()}
        with tempfile.TemporaryDirectory(prefix="satohsi-funding-parser-") as directory:
            project = Path(directory)
            copy_project(project)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("FOUNDRY_", "DAPP_", "FORGE_"))}
            env["FOUNDRY_PROFILE"] = "default"
            common = ["--root", str(project), "--offline", "--out", "out", "--cache-path", "cache"]
            test_command = [args.forge, "test", *common, "--match-contract", "BitcoinFundingParserTest",
                            "--gas-limit", str(GAS_LIMIT), "--enable-tx-gas-limit", "--isolate",
                            "--fuzz-seed", "0x01", "--fuzz-runs", "256", "--color", "never", "-vv",
                            "--json-file", str(project / "test-results.json")]
            run_logged(test_command, project, log, env)
            results = json.loads((project / "test-results.json").read_text())
            report["tests"] = summarize_tests(results)
            write_json(evidence / "funding-parser-forge-results.json", results)
            if report["tests"]["status"] != "PASS":
                raise RuntimeError("isolated parser tests did not all pass")
            # Explicit normal compilation supplies deployable harness bytecode.
            build_command = [args.forge, "build", *common, "--force", "--build-info",
                             "--build-info-path", "build-info", "--color", "never"]
            run_logged(build_command, project, log, env)
            artifact = json.loads((project / "out/BitcoinFundingParser.t.sol/FundingParserHarness.json").read_text())
            metadata = artifact.get("metadata") or json.loads(artifact["rawMetadata"])
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            initcode = bytes.fromhex(artifact["bytecode"]["object"].removeprefix("0x"))
            runtime = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
            report["compilation"] = {"compiler": metadata["compiler"], "settings": metadata["settings"],
                "source_config_sha256": sha(ROOT / "web3/foundry.toml"),
                "copied_config_sha256": sha(project / "foundry.toml"),
                "private_out_and_cache": True, "production_artifact_directories_used": False,
                "initcode_bytes": len(initcode), "runtime_bytes": len(runtime),
                "initcode_sha256": hashlib.sha256(initcode).hexdigest(),
                "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
                "artifact": "evidence/funding-parser-harness.json"}
            write_json(evidence / "funding-parser-harness.json", artifact)
            with local_node(args.anvil, project) as (rpc, command):
                genesis = rpc.call("eth_getBlockByNumber", ["latest", False])
                if int(genesis["gasLimit"], 16) != GAS_LIMIT:
                    raise RuntimeError("unexpected local block gas limit")
                account = rpc.call("eth_accounts")[0]
                deploy_hash = rpc.call("eth_sendTransaction", [{"from": account, "data": "0x" + initcode.hex(),
                    "gas": hex(GAS_LIMIT), "gasPrice": hex(2_000_000_000)}])
                deployment = rpc.receipt(deploy_hash)
                address = deployment["contractAddress"]
                actual_code = bytes.fromhex(rpc.call("eth_getCode", [address, "latest"])[2:])
                if actual_code != runtime:
                    raise RuntimeError("deployed runtime differs from normal compiler artifact")
                records = transaction_cases(rpc, address, account, cases)
                receipt_document = {"schema": "satoshifi.zkspv.funding-parser-receipts.v1",
                    "mode": "LOCAL_EVM_PARSER_ONLY", "hardfork": "osaka", "chain_id": 31337,
                    "block_gas_limit": GAS_LIMIT, "transaction_gas_limit_enforced": True,
                    "anvil_command": command, "client_version": rpc.call("web3_clientVersion"),
                    "contract_address": address, "deployment_receipt": deployment, "cases": records}
                write_json(evidence / "funding-parser-receipts.json", receipt_document)
            report["receipts"] = {"status": "PASS", "cases": len(records), "fixture_cases": len(cases),
                "selected_long_script_cases": len(records) - len(cases),
                "artifact": "evidence/funding-parser-receipts.json", "gas_limit": GAS_LIMIT,
                "max_gas_used": max(item["gas_used"] for item in records),
                "measurements": [{key: item[key] for key in ("name", "gas_used", "gas_headroom",
                    "input_count", "output_count", "stripped_bytes", "target_script_bytes")}
                    for item in records]}
            report["commands"] = {"test": test_command, "build": build_command}
        if source_hashes() != report["source_sha256"]:
            raise RuntimeError("source or fixtures changed during measurement; rerun")
        report["gas_accounting"] = {"hardfork": "osaka", "transaction_type": "legacy; no access list",
            "token_formula": "zero_calldata_bytes + 4 * nonzero_calldata_bytes",
            "standard_intrinsic_gas": "21000 + 4 * calldata_tokens",
            "eip7623_floor_gas": "21000 + 10 * calldata_tokens",
            "receipt_formula": "21000 + max(4 * calldata_tokens + execution_gas_after_refund, 10 * calldata_tokens)",
            "receipt_delta_limit": "receipt_minus_standard_intrinsic_gas includes any floor uplift; it is not pure EVM execution gas",
            "sources": ["https://eips.ethereum.org/EIPS/eip-7623", "https://eips.ethereum.org/EIPS/eip-7825"],
            "scope": "Local inspect transactions only. Deployment gas is recorded separately; production target is unspecified."}
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["evidence_sha256"] = {str(path.relative_to(out)): sha(path)
            for path in sorted(evidence.glob("funding-parser-*")) if path.is_file()}
        write_json(out / "funding-parser-report.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forge", default=shutil.which("forge") or "/private/tmp/satohsi-tools/forge")
    parser.add_argument("--anvil", default=shutil.which("anvil") or "/private/tmp/satohsi-tools/anvil")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    report = benchmark(args)
    print(json.dumps({"status": report["status"], "tests": report["tests"], "receipts": report["receipts"],
                      "error": report.get("error")}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
