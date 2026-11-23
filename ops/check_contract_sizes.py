"""Run after forge build. Check every compiled project contract, excluding script/test harnesses.

Creation bytecode here excludes constructor arguments. Bootstrap checks the complete
encoded initcode before signing; broadcast E2E additionally checks actual transactions.
"""

import argparse
import json
from pathlib import Path
from signer_agent.deployment_limits import check_deployment, check_portable_metadata

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "ops/artifacts/contract-sizes.json"
    )
    parser.add_argument("--web3-out", type=Path, default=ROOT / "web3/contracts/out")
    parser.add_argument("--check", action="store_true", help="Validate sizes and compiler settings without a report file")
    args = parser.parse_args()
    rows = []
    for path in sorted(args.web3_out.glob("*/*.json")):
        artifact = json.loads(path.read_text())
        metadata = artifact.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        targets = metadata.get("settings", {}).get("compilationTarget", {})
        source = next((s for s in targets if s.startswith("contracts/src/")), None)
        if source is None or not (ROOT / "web3" / source).is_file():
            continue
        creation = artifact.get("bytecode", {}).get("object", "0x")
        if creation in ("", "0x"):
            continue  # Interfaces and abstract contracts cannot be deployed.
        if args.check:
            settings = metadata["settings"]
            if (metadata["compiler"]["version"] != "0.8.39+commit.ce47f2c8"
                    or settings["evmVersion"] != "osaka" or settings.get("viaIR") is not True
                    or settings["optimizer"] != {"enabled": True, "runs": 1}):
                raise ValueError(f"CONTRACT_COMPILER_PIN_DRIFT: {targets[source]}")
        check_portable_metadata(metadata)
        sizes = check_deployment(artifact["deployedBytecode"]["object"], creation)
        rows.append(
            dict(
                contract=targets[source],
                source=source,
                creation_bytes=sizes.pop("initcode_bytes"),
                constructor_argument_budget=sizes.pop("initcode_margin"),
                **sizes,
            )
        )
    if not rows:
        raise SystemExit("NO_PROJECT_ARTIFACTS: run forge build first")
    report = dict(
        runtime_limit=24576,
        initcode_limit=49152,
        creation_excludes_constructor_arguments=True,
        portable_metadata_checked=True,
        contracts=sorted(rows, key=lambda r: -r["runtime_bytes"]),
    )
    if not args.check:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{len(rows)} project contracts pass; tightest runtime margin: "
        f"{report['contracts'][0]['contract']} {report['contracts'][0]['runtime_margin']} bytes"
    )


if __name__ == "__main__":
    main()
