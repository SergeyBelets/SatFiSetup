"""Verify an exported genuine Groth16 receipt on disposable loopback Anvil.

--preflight deploys the real verifier, compares SDK parameters and records only
rejection of a zero seal. It can never report positive proof acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "rust/zkspv/guest-integration"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked_bundle(bundle, expected, require_groth16=True):
    """Bind exporter output to the already qualified SatoshiFi program/journal."""
    metadata = json.loads((bundle / "receipt.json").read_text())
    if metadata["status"] != "VERIFIED" or not metadata["dev_mode_disabled"]:
        raise ValueError("Unverified receipt export")
    for key in ("image_id", "program_sha256", "journal_sha256"):
        if metadata[key] != expected[key]:
            raise ValueError("Receipt binding mismatch: " + key)
    for name, field in (("receipt.bin", "receipt_sha256"), ("journal.bin", "journal_sha256")):
        if sha(bundle / name) != metadata[field]:
            raise ValueError("Export bytes changed: " + name)
    if len((bundle / "journal.bin").read_bytes()) != 1216:
        raise ValueError("Wrong journal length")
    if require_groth16:
        if metadata["receipt_kind"] != "groth16":
            raise ValueError("Genuine Groth16 required; composite/succinct are not EVM seals")
        seal = (bundle / "seal.bin").read_bytes()
        if len(seal) != 260 or seal.hex() != metadata["seal_hex"]:
            raise ValueError("Groth16 seal encoding mismatch")
        if seal[:4].hex() != metadata["groth16_selector"]:
            raise ValueError("Groth16 selector mismatch")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bridge", "anvil", "artifacts", "receipt", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    build = json.loads((GUEST / "report.json").read_text())
    state_path = ROOT / "rust/zkspv/state-adapter/report.json"
    state = json.loads(state_path.read_text())
    for report in (build, state):
        for name, digest in report["source_sha256"].items():
            if sha(ROOT / name) != digest:
                raise ValueError("Qualified source changed: " + name)
    args.output.mkdir(parents=True, exist_ok=False)
    bundle = args.output / "native-verification"
    program = GUEST / "evidence/mainnet-transition.r0bf"
    journal = GUEST / "evidence/mainnet-32257.journal"
    expected = dict(image_id=build["program_image_id"], program_sha256=sha(program), journal_sha256=sha(journal))
    env = dict(PATH="/usr/bin:/bin", RISC0_DEV_MODE="0", TMPDIR="/private/tmp" if os.uname().sysname == "Darwin" else "/tmp")
    # Independently decode and cryptographically verify the imported receipt before any EVM call.
    with (args.output / "native-verification.log").open("w") as log:
        subprocess.run([args.bridge, "inspect", program, journal, args.receipt, bundle],
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    metadata = checked_bundle(bundle, expected, require_groth16=not args.preflight)
    artifact_path = args.artifacts / "RiscZeroGroth16Verifier.sol/RiscZeroGroth16Verifier.json"
    artifact = json.loads(artifact_path.read_text())
    creation = bytes.fromhex(artifact["bytecode"]["object"].removeprefix("0x"))
    template = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
    pinned = state["artifacts"]["RiscZeroGroth16Verifier"]
    if hashlib.sha256(creation).hexdigest() != pinned["creation_sha256"] or hashlib.sha256(template).hexdigest() != pinned["runtime_template_sha256"]:
        raise ValueError("Ordinary verifier bytecode differs from qualified build")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    process = subprocess.Popen([args.anvil, "--host", "127.0.0.1", "--port", str(port),
        "--chain-id", "31337", "--hardfork", "osaka", "--gas-limit", "16777216", "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 20}))
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Disposable Anvil exited")
            if w3.is_connected():
                break
            time.sleep(.05)
        else:
            raise RuntimeError("Disposable Anvil unavailable")
        if w3.eth.chain_id != 31337:
            raise ValueError("Unexpected local chain")
        sender = w3.eth.accounts[0]
        factory = w3.eth.contract(abi=artifact["abi"], bytecode=creation)
        control = bytes.fromhex(metadata["control_root"])
        bn254 = bytes.fromhex(metadata["bn254_control_id_sdk_order"])[::-1]
        # Require pinned Solidity constants; constructing with SDK values alone would hide drift.
        if control.hex() != "a54dc85ac99f851c92d7c96d7318af41dbe7c0194edfcc37eb4d422a998c1f56" or bn254.hex() != "04446e66d300eb7fb45c9726bb53c793dda407a62e9601618bb43c5c14657ac0":
            raise ValueError("SDK and pinned Solidity ControlID differ")
        tx = factory.constructor(control, bn254).transact({"from": sender, "gas": 16777216})
        deployment = w3.eth.wait_for_transaction_receipt(tx, timeout=30)
        if deployment.status != 1:
            raise RuntimeError("Verifier deployment failed")
        verifier = w3.eth.contract(address=deployment.contractAddress, abi=artifact["abi"])
        code = bytes(w3.eth.get_code(verifier.address))
        actual = bytearray(code); normalized = bytearray(template)
        if len(code) != len(template) or len(code) > 24576:
            raise ValueError("Unexpected verifier runtime size")
        for references in artifact["deployedBytecode"].get("immutableReferences", {}).values():
            for ref in references:
                start, size = ref["start"], ref["length"]
                actual[start:start + size] = bytes(size); normalized[start:start + size] = bytes(size)
        if actual != normalized:
            raise ValueError("Runtime differs outside immutable slots")
        selector = bytes(verifier.functions.SELECTOR().call())
        if selector.hex() != metadata["groth16_selector"]:
            raise ValueError("SDK and EVM verifier selectors differ")
        image = bytes.fromhex(metadata["image_id"])
        digest = bytes.fromhex(metadata["journal_sha256"])
        seal = selector + bytes(256) if args.preflight else (bundle / "seal.bin").read_bytes()
        cases = [("zero_seal_rejected", seal, image, digest, 0)] if args.preflight else [
            ("genuine_proof_accepted", seal, image, digest, 1),
            ("wrong_image_rejected", seal, bytes([image[0] ^ 1]) + image[1:], digest, 0),
            ("wrong_journal_rejected", seal, image, bytes([digest[0] ^ 1]) + digest[1:], 0),
            ("modified_seal_rejected", seal[:-1] + bytes([seal[-1] ^ 1]), image, digest, 0)]
        receipts = []
        for name, proof_seal, image_id, journal_digest, expected_status in cases:
            tx = verifier.functions.verify(proof_seal, image_id, journal_digest).transact({"from": sender, "gas": 1000000})
            receipt = w3.eth.wait_for_transaction_receipt(tx, timeout=30)
            if receipt.status != expected_status:
                raise ValueError("Unexpected verification result: " + name)
            receipts.append(dict(case=name, status=receipt.status, gas_used=receipt.gasUsed, transaction_hash=tx.hex()))
        result = dict(schema="satoshifi.groth16-evm.v1", status="PREFLIGHT_PASS" if args.preflight else "LOCAL_GROTH16_EVM_PASS",
            production_ready=False, positive_proof="NOT_PERFORMED" if args.preflight else "PASS",
            receipt_kind=metadata["receipt_kind"], image_id=metadata["image_id"], journal_sha256=metadata["journal_sha256"],
            receipt_sha256=metadata["receipt_sha256"], selector=selector.hex(), verifier=verifier.address,
            runtime_keccak256=Web3.keccak(code).hex(), ordinary_runtime_match=True,
            deployment=dict(status=deployment.status, gas_used=deployment.gasUsed, transaction_hash=deployment.transactionHash.hex()),
            checks=receipts, artifact_sha256=sha(artifact_path), state_report_sha256=sha(state_path),
            bridge_sha256=sha(args.bridge), script_sha256=sha(__file__), public_transactions=0,
            state_transition_acceptance="NOT_PERFORMED",
            scope="Disposable Anvil verifier only. Existing journal binds test state address 0x1111111111111111111111111111111111111111; no production/state/consumer activation.")
        (args.output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(dict(status=result["status"], selector=result["selector"], positive_proof=result["positive_proof"])))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    main()
