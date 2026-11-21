"""Offline before/after interface checks. No key, RPC, chain state, or transactions."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from zipfile import ZipFile

from eth_utils.abi import collapse_if_tuple
from web3 import Web3


METHODS = {
    "FROSTCoordinator": ["finalizeSession", "getAuthorizationResult", "getSession", "getResult",
                         "isFinalized", "sessionDKGId", "sessionDKGResult", "defaultVerifier", "dkgRegistry"],
    "BitcoinWithdrawalEscrow": ["depth", "payment", "settle", "nonce"],
    "BitcoinBatchRegistry": ["batchState", "withdrawalEscrow", "replacementOf", "versionCount"],
    "BitcoinSigningBoard": ["getPlanHash", "plan", "publishPlan"],
    "BitcoinAddressRegistry": ["registrationReceipts", "statusReceipts", "getStatusOperationHash"],
    "BitcoinFeeReserve": ["receipts", "getOperationHash"],
    "WithdrawalFeePayments": ["prepare"],
    "DKGAuthorization": ["hashOperation", "isOperationApproved", "approvedOperations",
                         "executedOperations", "operationTargets", "consumeOperation", "consumedSessions",
                         "registry", "coordinator", "dkgId", "resultHash"],
    "CustodianValidator": ["getSetMinCustodiansOperationHash", "operationNonce", "operationReceipts",
                           "isExecuted", "validateCustodianThreshold", "setMinCustodians", "addCustodian",
                           "removeCustodian", "getAddCustodianOperationHash", "getRemoveCustodianOperationHash"],
    "CustodianDAODecisionBoard": ["getCreateOperationHash", "createDecision", "creationReceipts",
                                  "decisions", "operationNonce"],
    "CustodianDAORateController": ["execute", "proposals"],
}


def sha(raw):
    return sha256(raw).hexdigest()


def value(param, seed, populated):
    typ = param["type"]
    array = re.fullmatch(r"(.+)\[(\d*)\]", typ)
    if array:
        child_type, fixed_size = array.groups()
        size = int(fixed_size) if fixed_size else (2 if populated else 0)
        return [value(dict(param, type=child_type), seed+i+1, populated) for i in range(size)]
    if typ == "tuple":
        return tuple(value(part, seed+i+1, populated) for i, part in enumerate(param["components"]))
    if typ.startswith("uint"):
        return (1 << int(typ[4:])) - 1 - seed if populated else 0
    if typ == "bool":
        return populated
    if typ == "address":
        return Web3.to_checksum_address(bytes([seed % 200 + 1 if populated else 0])*20)
    if typ == "string":
        return f"ipfs://checked-{seed}" if populated else ""
    if typ == "bytes":
        return bytes([seed % 256])*65 if populated else b""
    if typ.startswith("bytes"):
        return bytes([seed % 200+1 if populated else 0])*int(typ[5:])
    raise ValueError(f"Unimplemented fixture type {typ}")


def signatures(abi):
    return {f["name"] + "(" + ",".join(collapse_if_tuple(x) for x in f["inputs"]) + ")": f
            for f in abi if f["type"] == "function"}


def supported_entry(abi, name):
    result = [(sig, entry) for sig, entry in signatures(abi).items() if entry["name"] == name]
    if len(result) != 1:
        raise ValueError(f"Expected one unambiguous {name}, got {len(result)}")
    return result[0]


def check_method(w3, old_contract, new_contract, method):
    sig, original = supported_entry(old_contract.abi, method)
    new_sig, revised = supported_entry(new_contract.abi, method)
    assert new_sig == sig
    selector = Web3.keccak(text=sig)[:4]
    first = [collapse_if_tuple(x) for x in original["outputs"]]
    second = [collapse_if_tuple(x) for x in revised["outputs"]]
    assert first == second
    pairs = []
    for populated in (False, True):
        args = [value(param, i+1, populated) for i, param in enumerate(original["inputs"])]
        old_calldata = old_contract.encode_abi(sig, args=args)
        new_calldata = new_contract.encode_abi(sig, args=args)
        assert old_calldata == new_calldata and bytes.fromhex(old_calldata[2:])[:4] == selector
        old_fn, old_args = old_contract.decode_function_input(new_calldata)
        new_fn, new_args = new_contract.decode_function_input(old_calldata)
        assert old_fn.signature == new_fn.signature == sig and old_args == new_args
        result = [value(param, i+17, populated) for i, param in enumerate(original["outputs"])]
        old_result = w3.codec.encode(first, result)
        new_result = w3.codec.encode(second, result)
        assert old_result == new_result
        assert w3.codec.decode(first, new_result) == w3.codec.decode(second, old_result)
        pairs.append(dict(case="populated" if populated else "zero_empty",
                          calldata_sha256=sha(bytes.fromhex(old_calldata[2:])),
                          returndata_sha256=sha(old_result)))
    return dict(signature=sig, selector="0x"+selector.hex(), output_types=first, vectors=pairs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("original_zip", type=Path)
    parser.add_argument("new_sdk", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    w3 = Web3()
    directory = args.new_sdk / "contracts"
    with ZipFile(args.original_zip) as archive:
        prefix = "signer-agent/contracts/"
        refs = {Path(k).name: k for k in archive.namelist()
                if k.startswith(prefix) and k.endswith(".json")}
        assert set(refs) == {p.name for p in directory.glob("*.json")}
        digests = {}
        for name, path in sorted(refs.items()):
            old, new = archive.read(path), (directory/name).read_bytes()
            assert old == new, name
            digests[name] = sha(old)
        methods = {}
        evidence = {}
        archived_prefix = "rust/zkspv/deposit-integration/evidence/run-04/consumer-artifacts/"
        for name, group in METHODS.items():
            old = json.loads(archive.read(refs[name+".json"]))
            new = json.loads((directory/(name+".json")).read_bytes())
            assert isinstance(old, list) and isinstance(new, list)
            old_contract = w3.eth.contract(abi=old)
            new_contract = w3.eth.contract(abi=new)
            methods[name] = [check_method(w3, old_contract, new_contract, method) for method in group]
            path = archived_prefix+name+".json"
            if path not in archive.namelist():
                evidence[name] = dict(found=False)
                continue
            historical = json.loads(archive.read(path))
            hist_signatures = signatures(historical["abi"])
            matching = [d["signature"] for d in methods[name]
                        if d["signature"] in hist_signatures and
                        hist_signatures[d["signature"]] == signatures(new)[d["signature"]]]
            code = historical["deployedBytecode"]["object"].removeprefix("0x")
            evidence[name] = dict(found=True, path=path, historical_abi_equals_sdk=historical["abi"] == new,
                                  runtime_template_bytes=len(code)//2, matching_tested_abi_entries=matching)
    count = sum(len(x) for x in methods.values())
    report = dict(status="PASS", web3_version=w3.api, total_sdk_abi_files=len(digests), checked_contracts=len(methods),
                  methods=count, calldata_and_returndata_vectors=2*count, sdk_abi_sha256=digests,
                  missing_used_names_in_sdk=[name for name in ("ConfederationNFTPass","ConfederationPoolV2") if name+".json" not in refs],
                  calls=methods, isolated_historical_artifacts=evidence,
                  limits="Synthetic ABI vectors, including empty returndata. No eth_call, bytecode execution, "
                         "state transition, public RPC, deployed runtime or historical network log was used. "
                         "SDK contracts/*.json contains ABI arrays without creation or deployed bytecode.")
    args.output.write_text(json.dumps(report, indent=2)+"\n")
    print(f"PASS: {len(digests)} identical SDK ABIs, {len(methods)} contracts, {count} methods, {2*count} vectors.")


if __name__ == "__main__":
    main()
