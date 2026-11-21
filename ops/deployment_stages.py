"""Run explicit Foundry deployment stages with a confirmed public manifest.

PRIVATE_KEY is an explicit local environment input. RPC credentials, keys and
Foundry's sensitive cache never enter the public manifest. Each broadcast has an
isolated journal; --resume-broadcast continues only that exact stage and plan.
"""
import argparse
import base64
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import rlp
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "signer-agent"))
sys.path.insert(0, str(ROOT / "ops"))
from ethereum_rpc_budget import budget_rpc
from signer_agent.bootstrap import artifact
from signer_agent.contract_provenance import prove_creation
from signer_agent.deployment_limits import check_deployment, check_portable_metadata
from signer_agent.safe_errors import exception_code
from signer_agent.evm_network import read_block, receipt_finalized, transaction_policy, check_transaction_gas


class DeploymentError(ValueError):
    """Only fixed public diagnostics raised by this runner may be displayed."""

ADDRESS_ENV = {
    "FROSTAuthorizationDeployer": "FROST_AUTHORIZATION_DEPLOYER",
    "MultiPoolShareTokenDeployer": "MULTIPOOL_SHARE_TOKEN_DEPLOYER",
    "SimpleMultiPoolDeployer": "SIMPLE_MULTIPOOL_DEPLOYER",
    "ConfederationTokenDeployer": "CONFEDERATION_TOKEN_DEPLOYER",
    "MultiPoolDeployer": "MULTIPOOL_DEPLOYER",
    "FROSTCustodianDeployer": "FROST_CUSTODIAN_DEPLOYER",
    "FederationFeeManagerDeployer": "FEDERATION_FEE_MANAGER_DEPLOYER",
    "FeeDistributionCalculatorDeployer": "FEE_DISTRIBUTION_CALCULATOR_DEPLOYER",
    "FederationNFTModifierDeployer": "FEDERATION_NFT_MODIFIER_DEPLOYER",
    "ProjectWalletRegistryDeployer": "PROJECT_WALLET_REGISTRY_DEPLOYER",
    "FeeSystemDeployer": "FEE_SYSTEM_DEPLOYER",
    "FederationDeployer": "FEDERATION_DEPLOYER",
    "ConfederationDeployer": "CONFEDERATION_DEPLOYER",
    "DKGCoordinator": "DKG_COORDINATOR",
    "FROSTVerifier": "FROST_VERIFIER",
    "FROSTCoordinator": "FROST_COORDINATOR",
    "GlobalProjectWalletRegistry": "GLOBAL_PROJECT_WALLET_REGISTRY",
    "FederationFactory": "FEDERATION_FACTORY",
    "ConfederationFactory": "CONFEDERATION_FACTORY",
    "DKGAuthorization": "PROJECT_AUTHORIZATION",
    "CustodianDAOIncomeVault": "PROJECT_INCOME_VAULT",
    "CustodianDAOFeeRouter": "DEPLOYMENT_FEE_ROUTER",
}
# A matching runtime alone cannot establish what its immutable address words
# mean. Follow the actual public bindings and compare every explicitly selected
# dependency; discover omitted dependencies only through these canonical getters.
DEPENDENCIES = {
    "SimpleMultiPoolDeployer": {"frostDeployer": "FROSTAuthorizationDeployer", "shareTokenDeployer": "MultiPoolShareTokenDeployer"},
    "MultiPoolDeployer": {"shareTokenDeployer": "ConfederationTokenDeployer"},
    "FeeSystemDeployer": {"feeManagerDeployer": "FederationFeeManagerDeployer", "feeDistributorDeployer": "FeeDistributionCalculatorDeployer",
        "nftModifierDeployer": "FederationNFTModifierDeployer", "walletRegistryDeployer": "ProjectWalletRegistryDeployer"},
    "FederationDeployer": {"multiPoolDeployer": "SimpleMultiPoolDeployer", "feeSystemDeployer": "FeeSystemDeployer"},
    "ConfederationDeployer": {"frostCustodianDeployer": "FROSTCustodianDeployer", "multiPoolDeployer": "MultiPoolDeployer", "feeSystemDeployer": "FeeSystemDeployer"},
    "FROSTCoordinator": {"defaultVerifier": "FROSTVerifier", "dkgRegistry": "DKGCoordinator"},
    "FederationFactory": {"dkgCoordinator": "DKGCoordinator", "deployer": "FederationDeployer", "frostCoordinator": "FROSTCoordinator", "deploymentFeeRouter": "CustodianDAOFeeRouter"},
    "ConfederationFactory": {"federationFactory": "FederationFactory", "dkgCoordinator": "DKGCoordinator", "deployer": "ConfederationDeployer", "frostCoordinator": "FROSTCoordinator", "deploymentFeeRouter": "CustodianDAOFeeRouter"},
    "DKGAuthorization": {"coordinator": "FROSTCoordinator", "registry": "DKGCoordinator"},
    "CustodianDAOIncomeVault": {"authorization": "DKGAuthorization"},
    "CustodianDAOFeeRouter": {"incomeVault": "CustodianDAOIncomeVault", "authorization": "DKGAuthorization"},
}
UNSET_SETUP_BINDINGS = {("FROSTCoordinator", "dkgRegistry"),
    ("FederationFactory", "deploymentFeeRouter"), ("ConfederationFactory", "deploymentFeeRouter")}
PARAMETERS = {
    "PROJECT_REGISTRY_OWNER", "PROJECT_WALLET_1", "PROJECT_WALLET_2",
    "PROJECT_WALLET_SHARES", "PROJECT_WALLET_LABELS", "PROJECT_DKG_ID",
    "PROJECT_REWARD_TOKEN", "PROJECT_REWARD_TOKEN_CODE_HASH", "PROJECT_REWARD_TOKEN_DECIMALS", "PROJECT_TREASURY", "WRAP_STAKER_BPS",
    "DEPLOY_STAKER_BPS", "INTEGRATION_FEE_EXEMPTION", "DEPLOYMENT_FEE_AMOUNT",
    "PAYER_OPERATION_NONCE", "PAYER_OPERATION_HASH", "FROST_SESSION_ID",
} | set(ADDRESS_ENV.values())
STAGES = ("base", "factories", "fee_router", "prepare_payer", "execute_payer", "fees", "exempt_all")
TRANSIENT = {"PAYER_OPERATION_NONCE", "PAYER_OPERATION_HASH", "FROST_SESSION_ID"}
SCHEMA = "satoshifi-deployment-v2"
HASH = re.compile(r"0x[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise DeploymentError("DEPLOYMENT_SCHEMA_FIELDS")


def integer(value, *, minimum=0):
    # Preserve JSON decimal strings used by existing public configuration files.
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or value < minimum:
        raise DeploymentError("DEPLOYMENT_INTEGER")
    return value


def hex_value(value, size=None):
    if not isinstance(value, (str, bytes, bytearray, memoryview)):
        raise DeploymentError("DEPLOYMENT_HEX")
    try:
        raw = bytes.fromhex(value.removeprefix("0x")) if isinstance(value, str) else bytes(value)
    except (TypeError, ValueError):
        raise DeploymentError("DEPLOYMENT_HEX") from None
    if size is not None and len(raw) != size:
        raise DeploymentError("DEPLOYMENT_HEX_LENGTH")
    return Web3.to_hex(raw)


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise DeploymentError("DEPLOYMENT_ADDRESS")
    return Web3.to_checksum_address(value)


def public_parameters(value):
    if not isinstance(value, dict) or set(value) - PARAMETERS or any(not isinstance(v, str) for v in value.values()):
        raise DeploymentError("PUBLIC_PARAMETERS_ONLY: use explicit string values")
    return value


def ethereum_mainnet_parameters(chain, confirmations, stage, parameters):
    """Reject unsafe Ethereum mainnet choices before reading a key or writing a manifest.

    Base and factories are nonfinancial stages; financial stages require their
    complete public asset/fee policy even when a previous broadcast is resumed.
    """
    if chain != 1:
        return
    if confirmations < 12:
        raise DeploymentError("ETHEREUM_MAINNET_CONFIRMATIONS")
    if stage == "exempt_all" or parameters.get("INTEGRATION_FEE_EXEMPTION", "false") != "false":
        raise DeploymentError("ETHEREUM_MAINNET_EXEMPTION_FORBIDDEN")
    if "DEPLOYMENT_FEE_AMOUNT" in parameters and integer(parameters["DEPLOYMENT_FEE_AMOUNT"]) == 0:
        raise DeploymentError("ETHEREUM_MAINNET_PAID_DEPLOYMENT_REQUIRED")
    if stage not in ("base", "factories"):
        required = {"INTEGRATION_FEE_EXEMPTION", "DEPLOYMENT_FEE_AMOUNT",
            "PROJECT_REWARD_TOKEN", "PROJECT_REWARD_TOKEN_CODE_HASH", "PROJECT_REWARD_TOKEN_DECIMALS"}
        if required - parameters.keys():
            raise DeploymentError("ETHEREUM_MAINNET_ASSET_POLICY_REQUIRED")
        if parameters.get("WRAP_STAKER_BPS") != "0" or parameters.get("DEPLOY_STAKER_BPS") != "0":
            raise DeploymentError("ETHEREUM_MAINNET_PROJECT_TREASURY_ONLY")
    if "PROJECT_REWARD_TOKEN" in parameters:
        if int(address(parameters["PROJECT_REWARD_TOKEN"]), 16) == 0:
            raise DeploymentError("ETHEREUM_MAINNET_REWARD_TOKEN")
        if not {"PROJECT_REWARD_TOKEN_CODE_HASH", "PROJECT_REWARD_TOKEN_DECIMALS"} <= parameters.keys():
            raise DeploymentError("ETHEREUM_MAINNET_ASSET_POLICY_REQUIRED")
        if int(hex_value(parameters["PROJECT_REWARD_TOKEN_CODE_HASH"], 32), 16) == 0:
            raise DeploymentError("ETHEREUM_MAINNET_REWARD_HASH")
        if integer(parameters["PROJECT_REWARD_TOKEN_DECIMALS"]) > 18:
            raise DeploymentError("ETHEREUM_MAINNET_REWARD_DECIMALS")


def check_mainnet_asset(w3, parameters, block):
    """Check the pinned external asset at the same finalized view as the graph."""
    if w3.eth.chain_id != 1 or "PROJECT_REWARD_TOKEN" not in parameters:
        return
    token = address(parameters["PROJECT_REWARD_TOKEN"])
    code = bytes(w3.eth.get_code(token, block_identifier=block))
    if not code or hex_value(Web3.keccak(code), 32) != hex_value(parameters["PROJECT_REWARD_TOKEN_CODE_HASH"], 32):
        raise DeploymentError("ETHEREUM_MAINNET_REWARD_HASH")
    known_mock = bytes.fromhex(artifact("DAOTestUSD")["deployedBytecode"]["object"].removeprefix("0x"))
    if code == known_mock:
        raise DeploymentError("ETHEREUM_MAINNET_TEST_TOKEN_FORBIDDEN")
    decimals_abi = [{"type": "function", "name": "decimals", "inputs": [],
        "outputs": [{"type": "uint8"}], "stateMutability": "view"}]
    value = w3.eth.contract(address=token, abi=decimals_abi).functions.decimals().call(block_identifier=block)
    if type(value) is not int or value != integer(parameters["PROJECT_REWARD_TOKEN_DECIMALS"]):
        raise DeploymentError("ETHEREUM_MAINNET_REWARD_DECIMALS")


def check_mainnet_stage_ready(w3, stage, parameters, addresses, confirmations):
    """Latest-state Forge simulation cannot replace finalized prerequisite proofs."""
    if w3.eth.chain_id != 1 or stage in ("base", "factories"):
        return
    block = read_block(w3, confirmations)
    height = block["number"]

    def read(name, getter, *arguments):
        if name not in addresses:
            raise DeploymentError("ETHEREUM_MAINNET_STAGE_DEPENDENCY")
        contract = w3.eth.contract(address=addresses[name]["address"], abi=artifact(name)["abi"])
        return getattr(contract.functions, getter)(*arguments).call(block_identifier=height)

    try:
        epoch = bytes.fromhex(hex_value(parameters.get("PROJECT_DKG_ID"), 32)[2:])
        result = read("DKGCoordinator", "getCompletedResult", epoch)
        if bytes(result[3]) != bytes(Web3.keccak(text="EVM_AUTH")) or int.from_bytes(result[1], "big") == 0:
            raise DeploymentError("ETHEREUM_MAINNET_PROJECT_DKG_NOT_FINALIZED")
        if stage in ("prepare_payer", "execute_payer", "fees"):
            factories = [addresses[name]["address"] for name in ("FederationFactory", "ConfederationFactory")]
            enabled = [read("CustodianDAOFeeRouter", "authorizedPayers", target) for target in factories]
            if stage == "fees" and not all(enabled):
                raise DeploymentError("ETHEREUM_MAINNET_PAYERS_NOT_FINALIZED")
            if stage == "execute_payer" and not all(enabled):
                target = factories[enabled.index(False)]
                nonce = read("CustodianDAOFeeRouter", "operationNonce")
                operation = read("CustodianDAOFeeRouter", "getSetPayerOperationHash", target, True)
                if nonce != integer(parameters.get("PAYER_OPERATION_NONCE")) or hex_value(operation, 32) != hex_value(parameters.get("PAYER_OPERATION_HASH"), 32):
                    raise DeploymentError("ETHEREUM_MAINNET_PAYER_OPERATION_CHANGED")
                router = addresses["CustodianDAOFeeRouter"]["address"]
                if not read("DKGAuthorization", "isOperationApproved", operation, router):
                    session = integer(parameters.get("FROST_SESSION_ID"), minimum=1)
                    signed = read("FROSTCoordinator", "getAuthorizationResult", session)
                    if (signed[2] is not True or bytes(signed[1]) != bytes(operation)
                            or address(signed[4]) != router
                            or bytes(read("FROSTCoordinator", "sessionDKGId", session)) != epoch
                            or bytes(read("FROSTCoordinator", "sessionDKGResult", session)) != bytes(result[1])):
                        raise DeploymentError("ETHEREUM_MAINNET_SESSION_NOT_FINALIZED")
    except (ContractLogicError, KeyError, IndexError, TypeError):
        raise DeploymentError("ETHEREUM_MAINNET_STAGE_NOT_FINALIZED") from None
    if bytes(w3.eth.get_block(height)["hash"]) != bytes(block["hash"]):
        raise DeploymentError("DEPLOYMENT_FINALIZED_ANCHOR_CHANGED")


def digest(value):
    return "0x" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeploymentError("DEPLOYMENT_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def new_state(chain, confirmations):
    return dict(schema=SCHEMA, chain_id=chain, confirmations=confirmations, sender=None,
                parameters={}, addresses={}, transactions={}, stages={}, runs={})


def validate_creation_proof(proof):
    """Allow only the public fields emitted by canonical constructor proofs."""
    common = ("version", "method", "address", "artifact", "block_number", "block_hash",
              "runtime_hash", "transaction_hash")
    if not isinstance(proof, dict) or proof.get("version") != "1":
        raise DeploymentError("MANIFEST_CREATION_PROOF")
    if proof.get("method") == "direct":
        fields(proof, (*common, "initcode_hash", "sender"))
        hex_value(proof["initcode_hash"], 32)
        address(proof["sender"])
    elif proof.get("method") == "canonical_leaf":
        fields(proof, (*common, "log_index", "leaf"))
        integer(proof["log_index"])
        if not isinstance(proof["leaf"], dict) or proof["leaf"].get("method") != "direct":
            raise DeploymentError("MANIFEST_CREATION_PROOF")
        validate_creation_proof(proof["leaf"])
    else:
        raise DeploymentError("MANIFEST_CREATION_PROOF")
    if proof["artifact"] not in ADDRESS_ENV:
        raise DeploymentError("MANIFEST_CREATION_PROOF")
    address(proof["address"])
    integer(proof["block_number"], minimum=1)
    for field in ("block_hash", "runtime_hash", "transaction_hash"):
        hex_value(proof[field], 32)


def validate_addresses(addresses, transactions):
    if not isinstance(addresses, dict):
        raise DeploymentError("DEPLOYMENT_SCHEMA_FIELDS")
    for name, item in addresses.items():
        if name not in ADDRESS_ENV:
            raise DeploymentError("MANIFEST_UNKNOWN_CONTRACT")
        if isinstance(item, dict) and "provenance" in item:
            fields(item, ("address", "runtime_hash", "provenance", "block_hash", "block_number"), ("creation_proof",))
            if item["provenance"] != "observed_existing":
                raise DeploymentError("MANIFEST_ADDRESS_PROVENANCE")
            hex_value(item["block_hash"], 32)
            integer(item["block_number"])
        else:
            fields(item, ("address", "runtime_hash", "tx_hash"), ("creation_proof",))
            if item["tx_hash"] not in transactions:
                raise DeploymentError("MANIFEST_ADDRESS_TRANSACTION")
        address(item["address"])
        hex_value(item["runtime_hash"], 32)
        if "creation_proof" in item:
            validate_creation_proof(item["creation_proof"])
            if (item["creation_proof"]["address"] != item["address"]
                    or item["creation_proof"]["artifact"] != name
                    or hex_value(item["creation_proof"]["runtime_hash"], 32) != item["runtime_hash"]):
                raise DeploymentError("MANIFEST_CREATION_PROOF_BINDING")


def validate_manifest(state):
    """Only explicitly public, typed records may be read or written as a manifest."""
    fields(state, ("schema", "chain_id", "confirmations", "sender", "parameters", "addresses", "transactions", "stages", "runs"),
           ("inflight_run", "project_dkg_result"))
    if state["schema"] != SCHEMA:
        raise DeploymentError("MANIFEST_CHAIN_OR_SCHEMA")
    integer(state["chain_id"], minimum=1)
    integer(state["confirmations"], minimum=1)
    if state["sender"] is not None:
        address(state["sender"])
    public_parameters(state["parameters"])
    for key in ("addresses", "transactions", "stages", "runs"):
        if not isinstance(state[key], dict):
            raise DeploymentError("DEPLOYMENT_SCHEMA_FIELDS")
    validate_addresses(state["addresses"], state["transactions"])
    for tx_hash, item in state["transactions"].items():
        fields(item, ("hash", "stage", "run_id", "status", "contract", "transaction_type", "intent_hash", "nonce"),
               ("block_hash", "block_number", "sizes"))
        if not HASH.fullmatch(tx_hash) or item["hash"] != tx_hash or item["run_id"] not in state["runs"]:
            raise DeploymentError("MANIFEST_TRANSACTION")
        if item["status"] not in ("PENDING", "CONFIRMED", "REVERTED") or item["transaction_type"] not in ("CREATE", "CALL"):
            raise DeploymentError("MANIFEST_TRANSACTION")
        if item["contract"] not in ADDRESS_ENV and item["contract"] != "":
            raise DeploymentError("MANIFEST_UNKNOWN_CONTRACT")
        if item["transaction_type"] == "CREATE" and not item["contract"]:
            raise DeploymentError("MANIFEST_UNKNOWN_CONTRACT")
        if item["stage"] != state["runs"][item["run_id"]]["stage"]:
            raise DeploymentError("MANIFEST_TRANSACTION_STAGE")
        hex_value(item["intent_hash"], 32)
        integer(item["nonce"])
        if "block_hash" in item:
            hex_value(item["block_hash"], 32)
            integer(item["block_number"])
        elif "block_number" in item or item["status"] != "PENDING":
            raise DeploymentError("MANIFEST_RECEIPT_ANCHOR")
        if "sizes" in item:
            fields(item["sizes"], ("runtime_bytes", "initcode_bytes", "runtime_margin", "initcode_margin"))
            for number in item["sizes"].values():
                integer(number)
    for run_id, run in state["runs"].items():
        fields(run, ("stage", "parameters_hash", "previous_parameters", "start_nonce", "transaction_hashes",
                     "plan_hash", "planned_count", "execution_success", "no_transactions", "confirmed"),
               ("previous_addresses",))
        if not RUN_ID.fullmatch(run_id) or run["stage"] not in STAGES:
            raise DeploymentError("MANIFEST_RUN")
        public_parameters(run["previous_parameters"])
        if "previous_addresses" in run:
            validate_addresses(run["previous_addresses"], state["transactions"])
        hex_value(run["parameters_hash"], 32)
        integer(run["start_nonce"])
        integer(run["planned_count"])
        if run["plan_hash"] is not None:
            hex_value(run["plan_hash"], 32)
        if run["execution_success"] is not None and type(run["execution_success"]) is not bool:
            raise DeploymentError("MANIFEST_RUN")
        if type(run["no_transactions"]) is not bool or type(run["confirmed"]) is not bool:
            raise DeploymentError("MANIFEST_RUN")
        hashes = run["transaction_hashes"]
        if not isinstance(hashes, list) or len(set(hashes)) != len(hashes) or len(hashes) > run["planned_count"]:
            raise DeploymentError("MANIFEST_RUN_TRANSACTIONS")
        for tx_hash in hashes:
            if tx_hash not in state["transactions"] or state["transactions"][tx_hash]["run_id"] != run_id:
                raise DeploymentError("MANIFEST_RUN_TRANSACTIONS")
    for tx_hash, item in state["transactions"].items():
        if tx_hash not in state["runs"][item["run_id"]]["transaction_hashes"]:
            raise DeploymentError("MANIFEST_RUN_TRANSACTIONS")
    for stage, item in state["stages"].items():
        fields(item, ("confirmed", "transaction_hashes", "run_id"))
        run = state["runs"].get(item["run_id"])
        if stage not in STAGES or run is None or run["stage"] != stage or type(item["confirmed"]) is not bool:
            raise DeploymentError("MANIFEST_STAGE")
        if item["transaction_hashes"] != run["transaction_hashes"]:
            raise DeploymentError("MANIFEST_STAGE_TRANSACTIONS")
    if "inflight_run" in state and state["inflight_run"] not in state["runs"]:
        raise DeploymentError("MANIFEST_INFLIGHT_RUN")
    if "project_dkg_result" in state:
        hex_value(state["project_dkg_result"], 32)


def save(path, value):
    validate_manifest(value)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_runtime(w3, name, deployed_address, block_identifier=None):
    """Mask only valid compiler-declared immutable words, consistently per ID."""
    build = artifact(name)
    metadata = build["metadata"]
    check_portable_metadata(json.loads(metadata) if isinstance(metadata, str) else metadata)
    deployed = build["deployedBytecode"]
    expected = bytes.fromhex(deployed["object"].removeprefix("0x"))
    actual = bytes(w3.eth.get_code(deployed_address) if block_identifier is None
                   else w3.eth.get_code(deployed_address, block_identifier=block_identifier))
    if not actual or len(actual) != len(expected):
        raise DeploymentError("DEPLOYED_CODE_MISMATCH: " + name)
    masked, occupied = bytearray(actual), set()
    references = deployed.get("immutableReferences", {})
    if not isinstance(references, dict):
        raise DeploymentError("INVALID_IMMUTABLE_REFERENCES")
    for entries in references.values():
        if not isinstance(entries, list) or not entries:
            raise DeploymentError("INVALID_IMMUTABLE_REFERENCES")
        previous = None
        for entry in entries:
            fields(entry, ("start", "length"))
            start, length = entry["start"], entry["length"]
            if type(start) is not int or type(length) is not int or start < 0 or length != 32 or start + length > len(expected):
                raise DeploymentError("INVALID_IMMUTABLE_REFERENCES")
            offsets = set(range(start, start + length))
            if occupied & offsets:
                raise DeploymentError("INVALID_IMMUTABLE_REFERENCES")
            occupied.update(offsets)
            word = actual[start:start + length]
            if previous is not None and word != previous:
                raise DeploymentError("INCONSISTENT_IMMUTABLE_VALUE: " + name)
            previous = word
            masked[start:start + length] = expected[start:start + length]
    if bytes(masked) != expected:
        raise DeploymentError("DEPLOYED_CODE_MISMATCH: " + name)
    return actual


def add_address(w3, state, name, deployed_address, tx_hash):
    if name not in ADDRESS_ENV:
        raise DeploymentError("UNKNOWN_DEPLOYED_CONTRACT")
    deployed_address = address(deployed_address)
    runtime_hash = hex_value(Web3.keccak(verify_runtime(w3, name, deployed_address)))
    previous = state["addresses"].get(name)
    if previous:
        if address(previous["address"]) != deployed_address:
            raise DeploymentError("CONFLICTING_DEPLOYMENT_ADDRESS: " + name)
        if previous["runtime_hash"] != runtime_hash:
            raise DeploymentError("DEPLOYMENT_CODE_CHANGED: " + name)
        previous["creation_proof"] = checked_creation(w3, name, deployed_address, previous)
        return  # Never replace the first confirmed receipt/code anchor.
    proof = checked_creation(w3, name, deployed_address)
    state["addresses"][name] = dict(address=deployed_address, runtime_hash=runtime_hash,
        tx_hash=tx_hash, creation_proof=proof)


def checked_creation(w3, name, target, recorded=None, *, latest=None):
    """Recompute public manifest claims before trusting constructor state."""
    saved = (recorded or {}).get("creation_proof")
    if saved is not None:
        validate_creation_proof(saved)
    proof = prove_creation(w3, target, name, None,
        w3.eth.block_number if latest is None else latest,
        birth_hint=saved["block_number"] if saved is not None else None)
    if saved is not None and proof != saved:
        raise DeploymentError("DEPLOYMENT_CREATION_PROOF_MISMATCH: " + name)
    return proof


def check_reusable_parameters(w3, name, target, parameters, *, block_identifier=None):
    """Recheck selected scalar policy even when --resume skips Solidity simulation."""
    relevant = {"DKGAuthorization", "GlobalProjectWalletRegistry", "CustodianDAOIncomeVault",
        "CustodianDAOFeeRouter", "FederationFactory", "ConfederationFactory"}
    if name not in relevant:
        return
    contract = w3.eth.contract(address=target, abi=artifact(name)["abi"])

    def read(getter, *args):
        call = getattr(contract.functions, getter)(*args)
        return call.call() if block_identifier is None else call.call(block_identifier=block_identifier)

    def require_equal(actual, expected, getter):
        if actual != expected:
            raise DeploymentError("DEPLOYMENT_PARAMETER_MISMATCH: " + name + "." + getter)

    address_parameters = {
        "GlobalProjectWalletRegistry": {"owner": "PROJECT_REGISTRY_OWNER"},
        "CustodianDAOIncomeVault": {"rewardToken": "PROJECT_REWARD_TOKEN"},
        "CustodianDAOFeeRouter": {"rewardToken": "PROJECT_REWARD_TOKEN", "treasury": "PROJECT_TREASURY"},
    }
    for getter, parameter in address_parameters.get(name, {}).items():
        if parameter in parameters:
            require_equal(address(read(getter)), address(parameters[parameter]), getter)
    if name == "DKGAuthorization" and "PROJECT_DKG_ID" in parameters:
        require_equal(hex_value(read("dkgId"), 32), hex_value(parameters["PROJECT_DKG_ID"], 32), "dkgId")
    elif name == "CustodianDAOIncomeVault":
        require_equal(int(read("settlement"), 16), 0, "settlement")
        require_equal(read("sources", read("DEPLOY_FEE"))[2], False, "deploymentSource")
    elif name == "CustodianDAOFeeRouter":
        require_equal(read("deployStakerBps"), 0, "deployStakerBps")
        for getter, parameter in (("wrapStakerBps", "WRAP_STAKER_BPS"), ("deployStakerBps", "DEPLOY_STAKER_BPS")):
            if parameter in parameters:
                require_equal(read(getter), integer(parameters[parameter]), getter)
    elif name in ("FederationFactory", "ConfederationFactory") and read("deploymentFeeConfigured"):
        if "DEPLOYMENT_FEE_AMOUNT" in parameters:
            require_equal(read("deploymentFee"), integer(parameters["DEPLOYMENT_FEE_AMOUNT"]), "deploymentFee")
        if "INTEGRATION_FEE_EXEMPTION" in parameters:
            value = parameters["INTEGRATION_FEE_EXEMPTION"]
            if value not in ("true", "false"):
                raise DeploymentError("DEPLOYMENT_BOOLEAN: INTEGRATION_FEE_EXEMPTION")
            require_equal(read("integrationFeeExemption"), value == "true", "integrationFeeExemption")
    elif name == "GlobalProjectWalletRegistry" and all(key in parameters for key in
            ("PROJECT_WALLET_1", "PROJECT_WALLET_2", "PROJECT_WALLET_SHARES", "PROJECT_WALLET_LABELS")):
        wallets = [address(parameters["PROJECT_WALLET_1"])]
        second = address(parameters["PROJECT_WALLET_2"])
        if int(second, 16):
            wallets.append(second)
        shares = [integer(value.strip()) for value in parameters["PROJECT_WALLET_SHARES"].split(",")]
        labels = [value.strip() for value in parameters["PROJECT_WALLET_LABELS"].split(",")]
        actual = read("getWallets")
        require_equal([address(item[0]) for item in actual], wallets, "wallets")
        require_equal([item[1] for item in actual], shares, "walletShares")
        require_equal([item[2] for item in actual], labels, "walletLabels")


def checked_reusable_addresses(w3, recorded, parameters, confirmations):
    """Verify the whole supplied graph before Forge, without mutating the manifest.

    Both existing and newly journaled contracts require canonical constructor
    proofs. Existing code observations stay separately pinned on every reuse.
    """
    snapshot_head = w3.eth.block_number
    finalized_view = read_block(w3, confirmations) if w3.eth.chain_id == 1 else None
    view_height = finalized_view["number"] if finalized_view is not None else None
    confirmed_height = view_height if view_height is not None else snapshot_head - confirmations + 1
    check_mainnet_asset(w3, parameters, view_height)
    selected = {name: address(item["address"]) for name, item in recorded.items()}
    for name, env_name in ADDRESS_ENV.items():
        if env_name not in parameters:
            continue
        supplied = address(parameters[env_name])
        if int(supplied, 16) == 0:
            continue
        if name in selected and selected[name] != supplied:
            raise DeploymentError("CONFIG_MANIFEST_ADDRESS_MISMATCH: " + name)
        selected[name] = supplied
    checked, result = set(), deepcopy(recorded)
    while selected.keys() - checked:
        name = sorted(selected.keys() - checked)[0]
        target = selected[name]
        code = verify_runtime(w3, name, target, block_identifier=view_height)
        runtime_hash = hex_value(Web3.keccak(code))
        if name in recorded:
            item = recorded[name]
            if item["runtime_hash"] != runtime_hash:
                raise DeploymentError("DEPLOYMENT_CODE_CHANGED: " + name)
            if item.get("provenance") == "observed_existing":
                if item["block_number"] > (view_height if view_height is not None else snapshot_head):
                    raise DeploymentError("DEPLOYMENT_EXISTING_ANCHOR_AHEAD: " + name)
                block = w3.eth.get_block(item["block_number"])
                if hex_value(block["hash"], 32) != item["block_hash"]:
                    raise DeploymentError("DEPLOYMENT_EXISTING_ANCHOR_REORG: " + name)
                if verify_runtime(w3, name, target, block_identifier=item["block_number"]) != code:
                    raise DeploymentError("DEPLOYMENT_CODE_CHANGED: " + name)
                if snapshot_head - item["block_number"] + 1 < confirmations:
                    height = confirmed_height
                    if height < 0 or verify_runtime(w3, name, target, block_identifier=height) != code:
                        raise DeploymentError("EXISTING_DEPLOYMENT_NOT_CONFIRMED: " + name)
        else:
            height = confirmed_height
            if height < 0:
                raise DeploymentError("EXISTING_DEPLOYMENT_NOT_CONFIRMED: " + name)
            block = w3.eth.get_block(height)
            previous_code = verify_runtime(w3, name, target, block_identifier=height)
            if previous_code != code:
                raise DeploymentError("DEPLOYMENT_CODE_CHANGED: " + name)
            result[name] = dict(address=target, runtime_hash=runtime_hash,
                provenance="observed_existing", block_hash=hex_value(block["hash"], 32), block_number=height)
        result[name]["creation_proof"] = checked_creation(
            w3, name, target, recorded.get(name), latest=view_height if view_height is not None else snapshot_head)
        bindings = DEPENDENCIES.get(name, {})
        if bindings:
            contract = w3.eth.contract(address=target, abi=artifact(name)["abi"])
            for getter, dependency in bindings.items():
                call = getattr(contract.functions, getter)()
                actual = address(call.call() if view_height is None else call.call(block_identifier=view_height))
                # Base can legitimately resume between coordinator creation and
                # its one-time registry binding. Other stages enforce readiness.
                if (name, getter) in UNSET_SETUP_BINDINGS and int(actual, 16) == 0:
                    continue
                if int(actual, 16) == 0 or (dependency in selected and selected[dependency] != actual):
                    raise DeploymentError("DEPLOYMENT_DEPENDENCY_MISMATCH: " + name + "." + getter)
                selected[dependency] = actual
        check_reusable_parameters(w3, name, target, parameters, block_identifier=view_height)
        checked.add(name)
    if finalized_view is not None and bytes(w3.eth.get_block(view_height)["hash"]) != bytes(finalized_view["hash"]):
        raise DeploymentError("DEPLOYMENT_FINALIZED_ANCHOR_CHANGED")
    return result


def check_coordinator_admin(w3, addresses, stage, sender, *, completed=False, confirmations=1):
    coordinator = addresses.get("FROSTCoordinator")
    if coordinator is None:
        return
    # Canonical FROSTCoordinator grants ADMIN only to its constructor sender and
    # grants no DEFAULT_ADMIN_ROLE. No later caller can grant another ADMIN.
    creator = coordinator["creation_proof"]["sender"]
    contract = w3.eth.contract(address=coordinator["address"], abi=artifact("FROSTCoordinator")["abi"])
    call = contract.functions.hasRole(Web3.keccak(text="ADMIN_ROLE"), creator)
    block = read_block(w3, confirmations)["number"] if w3.eth.chain_id == 1 else None
    if not (call.call() if block is None else call.call(block_identifier=block)):
        return
    # A partial base broadcast may stop between CREATE, bind, and renounce.
    # Only that same creator can finish those steps, before any later stage.
    if not completed and stage in ("base", "exempt_all") and creator == sender:
        return
    raise DeploymentError("COORDINATOR_ORIGINAL_ADMIN_RETAINED")


def tx_integer(value):
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return int(value, 16)
    return integer(value)


def transaction_intent(transaction, chain, sender):
    try:
        tx_chain, nonce = tx_integer(transaction["chainId"]), tx_integer(transaction["nonce"])
        tx_sender = address(transaction["from"])
        destination = transaction.get("to")
        destination = address(destination).lower() if destination is not None else None
        value = tx_integer(transaction.get("value", 0))
        data = hex_value(transaction.get("input", transaction.get("data", "0x")))
    except (KeyError, TypeError):
        raise DeploymentError("BROADCAST_TRANSACTION_FIELDS") from None
    if tx_chain != chain or tx_sender != sender:
        raise DeploymentError("BROADCAST_CHAIN_OR_SENDER")
    return digest(dict(chain_id=tx_chain, sender=tx_sender.lower(), nonce=nonce, to=destination,
                       value=value, input_hash=hex_value(Web3.keccak(hexstr=data)))), nonce, destination


def create_address(sender, nonce):
    return Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[-20:])


def journal_path(manifest, run_id, chain):
    if not RUN_ID.fullmatch(run_id):
        raise DeploymentError("MANIFEST_RUN")
    return manifest.parent / (manifest.stem + "-broadcast") / run_id / "DeployFederation.s.sol" / str(chain) / "run-latest.json"


def import_broadcast(path, state, run_id):
    """Bind every planned slot, including unsubmitted slots, to this one run."""
    run = state["runs"][run_id]
    if not path.exists():
        return False
    data = read_json(path)
    if tx_integer(data.get("chain")) != state["chain_id"] or not isinstance(data.get("transactions"), list):
        raise DeploymentError("BROADCAST_CHAIN_OR_SCHEMA")
    plan, records, hashes, encountered_unsent = [], {}, [], False
    for index, item in enumerate(data["transactions"]):
        intent, nonce, destination = transaction_intent(item["transaction"], state["chain_id"], state["sender"])
        kind, name = item.get("transactionType"), item.get("contractName")
        if kind not in ("CREATE", "CALL") or (destination is None) != (kind == "CREATE"):
            raise DeploymentError("BROADCAST_TRANSACTION_TYPE")
        if kind == "CREATE" and name not in ADDRESS_ENV:
            raise DeploymentError("UNKNOWN_DEPLOYED_CONTRACT")
        name = name if name in ADDRESS_ENV else ""
        if nonce != run["start_nonce"] + index:
            raise DeploymentError("BROADCAST_NONCE_SEQUENCE")
        plan.append(dict(intent_hash=intent, contract=name, transaction_type=kind))
        if not item.get("hash"):
            encountered_unsent = True
            continue
        if encountered_unsent:
            raise DeploymentError("BROADCAST_NONCONTIGUOUS_SUBMITTED_PREFIX")
        tx_hash = hex_value(item["hash"], 32)
        if tx_hash in hashes:
            raise DeploymentError("BROADCAST_DUPLICATE_TRANSACTION")
        hashes.append(tx_hash)
        record = dict(hash=tx_hash, stage=run["stage"], run_id=run_id, status="PENDING",
                      contract=name, transaction_type=kind, intent_hash=intent, nonce=nonce)
        previous = state["transactions"].get(tx_hash)
        if previous and any(previous[key] != value for key, value in record.items() if key != "status"):
            raise DeploymentError("BROADCAST_TRANSACTION_RUN_OR_INTENT")
        records[tx_hash] = previous or record
    plan_hash = digest(plan)
    if run["plan_hash"] is not None and plan_hash != run["plan_hash"]:
        raise DeploymentError("BROADCAST_PLAN_CHANGED")
    if hashes[:len(run["transaction_hashes"])] != run["transaction_hashes"]:
        raise DeploymentError("BROADCAST_SUBMITTED_PREFIX_CHANGED")
    # Validate the complete journal before modifying any records.
    state["transactions"].update(records)
    run.update(plan_hash=plan_hash, planned_count=len(plan), transaction_hashes=hashes)
    state["stages"][run["stage"]] = dict(confirmed=False, transaction_hashes=hashes, run_id=run_id)
    return True


def refresh_runs(state):
    for run_id, run in state["runs"].items():
        all_confirmed = all(state["transactions"][h]["status"] == "CONFIRMED" for h in run["transaction_hashes"])
        complete_plan = run["planned_count"] > 0 and len(run["transaction_hashes"]) == run["planned_count"]
        run["confirmed"] = run["execution_success"] is True and all_confirmed and (complete_plan or run["no_transactions"])
        current = state["stages"].get(run["stage"])
        if current and current["run_id"] == run_id:
            current.update(confirmed=run["confirmed"], transaction_hashes=list(run["transaction_hashes"]))
    run_id = state.get("inflight_run")
    if run_id:
        run = state["runs"][run_id]
        if run["confirmed"] or (run["no_transactions"] and run["execution_success"] is False):
            state.pop("inflight_run")


def reconcile(w3, state, confirmations):
    """Receipts must be canonical, sufficiently deep and match the recorded intent."""
    # This also restores trusted emitters when a manifest started from existing
    # infrastructure, so an internal projectAuth CREATE is not lost.
    state["addresses"] = checked_reusable_addresses(w3, state["addresses"], state["parameters"], confirmations)
    pending = []
    auth_topic = Web3.keccak(text="FROSTAuthorizationDeployed(address,address)")
    for item in state["transactions"].values():
        try:
            receipt = w3.eth.get_transaction_receipt(item["hash"])
        except TransactionNotFound:
            if "block_hash" in item:
                raise DeploymentError("DEPLOYMENT_RECEIPT_REORG") from None
            item["status"] = "PENDING"
            pending.append(item["hash"])
            continue
        block_hash = hex_value(receipt["blockHash"], 32)
        if hex_value(receipt["transactionHash"], 32) != item["hash"]:
            raise DeploymentError("DEPLOYMENT_RECEIPT_TRANSACTION")
        block = w3.eth.get_block(receipt["blockNumber"])
        if hex_value(block["hash"], 32) != block_hash or (item.get("block_hash") and item["block_hash"] != block_hash):
            raise DeploymentError("DEPLOYMENT_RECEIPT_REORG")
        transaction = w3.eth.get_transaction(item["hash"])
        intent, nonce, destination = transaction_intent(transaction, state["chain_id"], state["sender"])
        if intent != item["intent_hash"] or nonce != item["nonce"] or (destination is None) != (item["transaction_type"] == "CREATE"):
            raise DeploymentError("DEPLOYMENT_TRANSACTION_INTENT")
        if (w3.eth.block_number - receipt["blockNumber"] + 1 < confirmations
                or (state["chain_id"] == 1 and not receipt_finalized(w3, receipt["blockNumber"], receipt["blockHash"], confirmations))):
            item["status"] = "PENDING"
            pending.append(item["hash"])
            continue
        item.update(status="CONFIRMED" if receipt["status"] == 1 else "REVERTED",
                    block_hash=block_hash, block_number=receipt["blockNumber"])
        if item["status"] == "REVERTED":
            continue
        if item["transaction_type"] == "CREATE":
            if receipt.get("contractAddress") is None:
                raise DeploymentError("CREATE_RECEIPT_MISSING_ADDRESS")
            if address(receipt["contractAddress"]) != create_address(state["sender"], nonce):
                raise DeploymentError("CREATE_RECEIPT_ADDRESS_MISMATCH")
            code = w3.eth.get_code(receipt["contractAddress"])
            item["sizes"] = check_deployment(code, transaction["input"])
            add_address(w3, state, item["contract"], receipt["contractAddress"], item["hash"])
        for log in receipt["logs"]:
            topics = log["topics"]
            if len(topics) == 3 and bytes(topics[0]) == bytes(auth_topic):
                leaf = state["addresses"].get("FROSTAuthorizationDeployer")
                coordinator = state["addresses"].get("FROSTCoordinator")
                if leaf and address(log["address"]) == leaf["address"]:
                    if not coordinator or hex_value(topics[2], 32)[-40:].lower() != coordinator["address"][2:].lower():
                        raise DeploymentError("PROJECT_AUTHORIZATION_COORDINATOR")
                    auth = address("0x" + hex_value(topics[1], 32)[-40:])
                    add_address(w3, state, "DKGAuthorization", auth, item["hash"])
    state["addresses"] = checked_reusable_addresses(w3, state["addresses"], state["parameters"], confirmations)
    dkg, project = state["addresses"].get("DKGCoordinator"), state["parameters"].get("PROJECT_DKG_ID")
    if dkg and project:
        registry = w3.eth.contract(address=dkg["address"], abi=artifact("DKGCoordinator")["abi"])
        try:
            call = registry.functions.getCompletedResult(bytes.fromhex(project.removeprefix("0x")))
            result = call.call(block_identifier=read_block(w3, confirmations)["number"]) if state["chain_id"] == 1 else call.call()
        except ContractLogicError:
            if "project_dkg_result" in state:
                raise DeploymentError("PROJECT_DKG_RESULT_UNAVAILABLE") from None
            # Only an actual contract revert means an unfinished ceremony. RPC
            # failures and malformed results must not silently retain success.
        else:
            if result[3] != Web3.keccak(text="EVM_AUTH"):
                raise DeploymentError("PROJECT_DKG_PURPOSE")
            result_hash = hex_value(result[1], 32)
            if state.get("project_dkg_result", result_hash) != result_hash:
                raise DeploymentError("PROJECT_DKG_RESULT_CHANGED")
            state["project_dkg_result"] = result_hash
    refresh_runs(state)
    return pending


def connect_rpc(url):
    return Web3(Web3.HTTPProvider(url))


def signing_key():
    value = os.environ.get("PRIVATE_KEY")
    if value is None:
        raise DeploymentError("EXPLICIT_PRIVATE_KEY_ENV_REQUIRED")
    try:
        number = int(value, 16 if value.startswith("0x") else 10)
        key = number.to_bytes(32, "big")
        sender = Account.from_key(key).address
    except (ValueError, OverflowError):
        raise DeploymentError("INVALID_PRIVATE_KEY_ENV") from None
    return key, sender


def redact(output, rpc_url):
    secret_values = {os.environ.get("PRIVATE_KEY"), rpc_url}
    key = os.environ.get("PRIVATE_KEY")
    if key:
        try:
            number = int(key, 16 if key.startswith("0x") else 10)
            encoded = number.to_bytes(32, "big").hex()
        except (ValueError, OverflowError):
            pass
        else:
            secret_values.update((encoded, encoded.upper(), "0x" + encoded,
                                  "0x" + encoded.upper()))
            if len(str(number)) >= 16:
                secret_values.add(str(number))
    try:
        parsed = urlsplit(rpc_url)
        user, password = parsed.username, parsed.password
        if user:
            secret_values.update((user, unquote(user)))
        if user is not None and password is not None:
            credentials = f"{unquote(user)}:{unquote(password)}"
            secret_values.add(base64.b64encode(credentials.encode()).decode())
        if password:
            secret_values.update((password, unquote(password)))
        # HTTP libraries may remove URL userinfo or report just the failed
        # request path/query. Providers often place their access token there.
        secret_values.add(urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
                                     parsed.path, parsed.query, parsed.fragment)))
        secret_values.update(value for _, value in parse_qsl(parsed.query, keep_blank_values=False))
        # A failed request may be printed as a relative path, including short
        # provider tokens that should not be replaced as standalone common words.
        secret_values.update(path for path in (parsed.path, unquote(parsed.path))
                             if path not in ("", "/"))
        secret_values.update(segment for segment in parsed.path.split("/") if len(segment) >= 8)
    except ValueError:
        pass
    for secret in sorted((value for value in secret_values if value), key=len, reverse=True):
        if secret:
            output = output.replace(secret, "[REDACTED]")
    return output


def failure_message(error, rpc_url):
    message = str(error) if isinstance(error, DeploymentError) else exception_code(error)
    return redact(message, rpc_url)


def protect_transport_logs():
    # Match the signer CLI boundary: retries/warnings can bypass captured Forge
    # output and contain credentials even when no DEBUG logging was requested.
    for name in ("urllib3", "requests", "web3", "aiohttp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False


@contextmanager
def resume_wallet(environment, key):
    # --resume skips Solidity simulation, so vm.startBroadcast's local signer is
    # unavailable. Supply a temporary encrypted keystore without command-line keys.
    with tempfile.TemporaryDirectory(prefix="satoshifi-resume-wallet-") as folder:
        password = secrets.token_urlsafe(32)
        path = Path(folder) / "key.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(Account.encrypt(key, password), stream)
        password_path = Path(folder) / "password.txt"
        descriptor = os.open(password_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(password)
        # Foundry's ETH_PASSWORD setting is a password-file path, not the
        # password itself (forge script --help, wallet keystore options).
        environment.update(ETH_KEYSTORE=str(path), ETH_PASSWORD=str(password_path))
        yield


def private_directory(path):
    """Create/tighten an owned directory without following an existing symlink."""
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise DeploymentError("UNSAFE_DEPLOYMENT_PRIVATE_DIRECTORY") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise DeploymentError("UNSAFE_DEPLOYMENT_PRIVATE_DIRECTORY")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def prepare_paths(manifest, run_id, chain):
    journal = journal_path(manifest, run_id, chain)
    public_root = journal.parents[2]
    parent = manifest.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022):
        raise DeploymentError("UNSAFE_DEPLOYMENT_MANIFEST_DIRECTORY")
    private_parent = manifest.parent / (manifest.stem + "-private")
    private_directory(private_parent)
    private_directory(private_parent / run_id)
    cache = private_parent / run_id / "cache"
    private_directory(cache)
    private_directory(public_root.parent)
    private_directory(public_root)
    # Share only compiler caches; Foundry's sensitive per-script resume cache is
    # isolated. This avoids recompiling the graph for every deployment stage.
    for source in (ROOT / "web3/cache").glob("solidity-files-cache*"):
        target = cache / source.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
    return journal, public_root, cache


def main(argv=None):
    protect_transport_logs()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--resume-broadcast", action="store_true")
    args = parser.parse_args(argv)
    config = read_json(args.config)
    fields(config, ("chain_id", "confirmations", "parameters"))
    chain, confirmations = integer(config["chain_id"], minimum=1), integer(config["confirmations"], minimum=1)
    parameters = public_parameters(config["parameters"])
    ethereum_mainnet_parameters(chain, confirmations, args.stage, parameters)
    if args.resume_broadcast and not args.broadcast:
        raise DeploymentError("RESUME_REQUIRES_SAME_BROADCAST_STAGE")
    if args.stage == "prepare_payer" and args.broadcast:
        raise DeploymentError("PREPARE_PAYER_IS_READ_ONLY: omit --broadcast")
    key, sender = signing_key() if args.stage != "prepare_payer" else (None, None)
    w3 = connect_rpc(args.rpc_url)
    if w3.eth.chain_id != chain:
        raise DeploymentError("CHAIN_MISMATCH")
    args.manifest = args.manifest.resolve()
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.with_name(args.manifest.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(args.manifest) if args.manifest.exists() else new_state(chain, confirmations)
        validate_manifest(state)
        if state["chain_id"] != chain:
            raise DeploymentError("MANIFEST_CHAIN_OR_SCHEMA")
        if confirmations < state["confirmations"]:
            raise DeploymentError("CONFIRMATIONS_CANNOT_DECREASE")
        state["confirmations"] = confirmations
        if sender and state["sender"] not in (None, sender):
            raise DeploymentError("MANIFEST_SENDER_MISMATCH")
        if sender:
            state["sender"] = sender
        previous_run = state.get("inflight_run")
        if previous_run:
            # Older schema-v2 runs predate reusable-address snapshots. Preserve
            # their already recorded graph before reconciliation discovers any
            # candidate addresses from the interrupted run's parameters.
            state["runs"][previous_run].setdefault("previous_addresses", deepcopy(state["addresses"]))
            import_broadcast(journal_path(args.manifest, previous_run, chain), state, previous_run)
        try:
            pending = reconcile(w3, state, confirmations)
        except ValueError:
            # A pre-upgrade run may contain a candidate that the new runtime
            # preflight rejects before Forge can return its usual failure. Only
            # the same strict no-send proof permits discarding those candidates.
            # Submitted prefixes and any uncertain/advanced nonce stay in flight.
            if previous_run:
                old = state["runs"][previous_run]
                if state["sender"] is not None and not old["transaction_hashes"] and all(
                        w3.eth.get_transaction_count(state["sender"], tag) == old["start_nonce"]
                        for tag in ("latest", "pending")):
                    state["parameters"] = dict(old["previous_parameters"])
                    state["addresses"] = deepcopy(old["previous_addresses"])
                    old.update(execution_success=False, no_transactions=True)
                    refresh_runs(state)
                    save(args.manifest, state)
            raise
        save(args.manifest, state)
        proposed = dict(state["parameters"])
        for name, value in parameters.items():
            if name not in TRANSIENT and name in proposed and proposed[name] != value:
                raise DeploymentError("CONFLICTING_PUBLIC_PARAMETER: " + name)
            proposed[name] = value
        ethereum_mainnet_parameters(chain, confirmations, args.stage, proposed)
        checked_addresses = checked_reusable_addresses(w3, state["addresses"], proposed, confirmations)
        check_mainnet_stage_ready(w3, args.stage, proposed, checked_addresses, confirmations)
        check_coordinator_admin(w3, checked_addresses, args.stage, sender, confirmations=confirmations)
        inflight = state.get("inflight_run")
        if args.resume_broadcast:
            if not inflight or state["runs"][inflight]["stage"] != args.stage:
                raise DeploymentError("RESUME_REQUIRES_SAME_BROADCAST_STAGE")
            if digest(proposed) != state["runs"][inflight]["parameters_hash"]:
                raise DeploymentError("RESUME_PARAMETERS_CHANGED")
            run_id = inflight
        else:
            if pending:
                raise DeploymentError("PENDING_DEPLOYMENT_TRANSACTIONS: wait or resume the same broadcast stage")
            if inflight:
                old = state["runs"][inflight]
                if old["stage"] != args.stage or old["planned_count"] == 0 or len(old["transaction_hashes"]) != old["planned_count"]:
                    raise DeploymentError("INCOMPLETE_BROADCAST_RUN: use --resume-broadcast for the same stage")
                if not args.broadcast:
                    raise DeploymentError("INCOMPLETE_BROADCAST_RUN: complete the same broadcast stage first")
                # Every slot has a final receipt; a fresh same-stage simulation can
                # repair a revert or verify a prefix whose process exited late.
            run_id = secrets.token_hex(16)
        environment = dict(os.environ)
        for name in PARAMETERS:
            environment.pop(name, None)
        environment.update(proposed)
        for name, item in checked_addresses.items():
            env_name = ADDRESS_ENV[name]
            configured = environment.get(env_name)
            if configured and int(configured, 16) != 0 and address(configured) != item["address"]:
                raise DeploymentError("CONFIG_MANIFEST_ADDRESS_MISMATCH: " + name)
            environment[env_name] = item["address"]
        journal, broadcast_root, cache = prepare_paths(args.manifest, run_id, chain)
        environment.update(EXPECTED_CHAIN_ID=str(chain), DEPLOYMENT_STAGE=args.stage,
                           FOUNDRY_BROADCAST=str(broadcast_root), FOUNDRY_CACHE_PATH=str(cache))
        command = ["forge", "script", "script/DeployFederation.s.sol:DeployFederation", "--root", str(ROOT / "web3"),
                   "--rpc-url", args.rpc_url, "--slow", "--non-interactive"]
        if chain == 1 and args.stage != "prepare_payer":
            fees = transaction_policy(w3)
            # The RPC boundary below checks actual signed gas/fee values. Forge
            # gas-limit flags constrain its local script scaffold instead. Keep
            # the estimate margin for calls whose execution consumes more than
            # their net gas after refunds; the boundary enforces the hard cap.
            gas_limit = integer(os.environ.get("SATOSHIFI_MAX_TX_GAS", ""), minimum=1)
            check_transaction_gas(w3, {"gas": gas_limit})
            command.extend(["--with-gas-price", str(fees["maxFeePerGas"]), "--priority-gas-price",
                str(fees["maxPriorityFeePerGas"]), "--gas-estimate-multiplier", "130"])
        if args.broadcast:
            command.append("--broadcast")
            if not args.resume_broadcast:
                start_nonce = w3.eth.get_transaction_count(sender, "pending")
                if w3.eth.get_transaction_count(sender, "latest") != start_nonce:
                    raise DeploymentError("SENDER_HAS_PENDING_TRANSACTIONS")
                state["runs"][run_id] = dict(stage=args.stage, parameters_hash=digest(proposed),
                    previous_parameters=dict(state["parameters"]), start_nonce=start_nonce,
                    previous_addresses=deepcopy(state["addresses"]),
                    transaction_hashes=[], plan_hash=None, planned_count=0,
                    execution_success=None, no_transactions=False, confirmed=False)
                state["parameters"] = proposed
                state["stages"][args.stage] = dict(confirmed=False, transaction_hashes=[], run_id=run_id)
                state["inflight_run"] = run_id
            state["addresses"] = checked_addresses
            save(args.manifest, state)
        context = budget_rpc(w3, sender, fees, gas_limit) if chain == 1 and args.stage != "prepare_payer" else nullcontext(args.rpc_url)
        with context as forge_rpc:
            command[command.index("--rpc-url") + 1] = forge_rpc
            if args.resume_broadcast:
                command.append("--resume")
                with resume_wallet(environment, key):
                    result = subprocess.run(command, cwd=ROOT / "web3", env=environment, text=True, capture_output=True)
            else:
                result = subprocess.run(command, cwd=ROOT / "web3", env=environment, text=True, capture_output=True)
        output = (result.stdout + result.stderr).replace(forge_rpc, args.rpc_url)
        print(redact(output, args.rpc_url), end="")
        if args.broadcast:
            import_broadcast(journal, state, run_id)
            run = state["runs"][run_id]
            run["execution_success"] = result.returncode == 0
            # A missing/empty journal alone is never evidence of completion.
            # Prove the legitimate no-transaction case using both mined and
            # pending nonces. A partial or uncertain send stays in flight.
            if not run["transaction_hashes"]:
                unchanged = all(w3.eth.get_transaction_count(sender, tag) == run["start_nonce"] for tag in ("latest", "pending"))
                if unchanged and (run["planned_count"] == 0 or result.returncode != 0):
                    run["no_transactions"] = True
                    if result.returncode != 0:
                        state["parameters"] = dict(run["previous_parameters"])
                        if "previous_addresses" in run:
                            state["addresses"] = deepcopy(run["previous_addresses"])
            pending = reconcile(w3, state, confirmations)
            if run["confirmed"]:
                check_coordinator_admin(w3, state["addresses"], args.stage, sender, completed=True, confirmations=confirmations)
            save(args.manifest, state)
            if result.returncode == 0 and not run["confirmed"]:
                print("DEPLOYMENT_NOT_CONFIRMED: receipts or unsubmitted transactions remain; manifest retains the run")
                return 2
        return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Third-party ValueError text is untrusted too. Only this runner's fixed
        # DeploymentError diagnostics may be shown; everything else is a type code.
        rpc = sys.argv[sys.argv.index("--rpc-url") + 1] if "--rpc-url" in sys.argv else ""
        raise SystemExit(failure_message(error, rpc)) from None
