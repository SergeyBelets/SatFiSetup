"""Explicit, reviewable native-gas payments/claims through the shared EOA journal.

No automatic funder or claim loop. New service payments require the exact enrolled
mainnet state/view binding. Old claims do not require an available SPV state.
"""
import argparse
import json
from pathlib import Path

from eth_account import Account
from web3 import Web3

from .bitcoin_trust import checked_runtime
from .bootstrap import Bootstrap, artifact
from .bootstrap_parallel import restore_signed
from .contract_provenance import prove_creation
from .evm_network import read_block
from .onboarding import read_private
from .operations import Journal, canonical


REGISTRY = "SPVServicePaymentRegistry"
TREASURY = "ConfederationGasTreasury"
SCHEMA = "satoshifi.spv-gas-request.v1"
ERROR_CODES = frozenset("""GAS_BINDING_SCHEMA GAS_OBSERVATION_REORG GAS_RUNTIME_BINDING
GAS_AUTHORIZATION_BINDING GAS_DOMAIN_BINDING GAS_TREASURY_PROVENANCE GAS_TREASURY_BINDING
GAS_SPV_PROFILE_NOT_QUALIFIED GAS_JOB_ALREADY_PAID GAS_PERMISSION_INACTIVE GAS_PERMISSION_EXPIRED
GAS_NOTHING_TO_CLAIM GAS_REQUEST_BINDING GAS_PAYER_IDENTITY GAS_CLAIM_VALUE GAS_REQUEST_CHANGED""".split())


def _hex(value):
    return Web3.to_hex(value)


class GasTreasuryClient:
    def __init__(self, w3, manifest):
        self.w3, self.manifest = w3, manifest
        if manifest.get("schema") != "satoshifi.spv-gas-binding.v1":
            raise ValueError("GAS_BINDING_SCHEMA")
        self.registry = w3.eth.contract(address=Web3.to_checksum_address(manifest["registry"]),
                                       abi=artifact(REGISTRY)["abi"])

    def _call(self, contract, method, block, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=block.number)

    def _anchor(self, block):
        if bytes(self.w3.eth.get_block(block.number).hash) != bytes(block.hash):
            raise ValueError("GAS_OBSERVATION_REORG")

    def _binding(self):
        m, w3 = self.manifest, self.w3
        if str(w3.eth.chain_id) != m["chain_id"]:
            raise ValueError("CHAIN_MISMATCH")
        block = read_block(w3, int(m["evm_confirmations"]))
        runtime = checked_runtime(w3, self.registry.address, REGISTRY, block.number)
        if runtime.removeprefix("0x") != m["registry_runtime_hash"].removeprefix("0x").lower():
            raise ValueError("GAS_RUNTIME_BINDING")
        prove_creation(w3, self.registry.address, REGISTRY,
            [Web3.to_checksum_address(m["factory"]), m["confederation_id"], Web3.to_checksum_address(m["state_view"])],
            block.number, birth_hint=int(m["registry_creation_block"]))
        if self._call(self.registry, "authorization", block).lower() != m["authorization"].lower():
            raise ValueError("GAS_AUTHORIZATION_BINDING")
        if _hex(self._call(self.registry, "domain", block)).lower() != m["domain"].lower():
            raise ValueError("GAS_DOMAIN_BINDING")
        self._anchor(block)
        return block

    def _treasury(self, address, block):
        address = Web3.to_checksum_address(address)
        checked_runtime(self.w3, address, TREASURY, block.number)
        # Exact canonical registry constructor + immutable isTreasury membership
        # proves the child constructor ran; runtime matching alone would not.
        if not self._call(self.registry, "isTreasury", block, address):
            raise ValueError("GAS_TREASURY_PROVENANCE")
        treasury = self.w3.eth.contract(address=address, abi=artifact(TREASURY)["abi"])
        if self._call(treasury, "paymentRegistry", block) != self.registry.address:
            raise ValueError("GAS_TREASURY_BINDING")
        return treasury

    def status(self, job):
        block = self._binding()
        result = dict(paid=self._call(self.registry, "paid", block, job),
            payment=Bootstrap.wire(self._call(self.registry, "payments", block, job)),
            terms=Bootstrap.wire(self._call(self.registry, "jobs", block, job)),
            permission=Bootstrap.wire(self._call(self.registry, "permissions", block, job)),
            block_number=str(block.number), block_hash=_hex(block.hash))
        self._anchor(block)
        return result

    def _base(self, action):
        return dict(schema=SCHEMA, action=action, chain_id=self.manifest["chain_id"],
                    registry=self.registry.address, domain=self.manifest["domain"])

    def prepare_fund(self, job):
        if self.w3.eth.chain_id != 31337 and self.manifest.get("bitcoin_policy") is None:
            raise ValueError("GAS_SPV_PROFILE_NOT_QUALIFIED")
        block = self._binding()
        self.service_profile(block)
        terms = self._call(self.registry, "jobs", block, job)
        permission = self._call(self.registry, "permissions", block, job)
        config = self._call(self.registry, "configuration", block)
        if self._call(self.registry, "paid", block, job):
            raise ValueError("GAS_JOB_ALREADY_PAID")
        if (not self._call(self.registry, "enabled", block) or not permission[5]
                or permission[1] != self._call(self.registry, "generation", block)):
            raise ValueError("GAS_PERMISSION_INACTIVE")
        if min(terms[5], permission[4]) < block.timestamp:
            raise ValueError("GAS_PERMISSION_EXPIRED")
        treasury = self._treasury(config[0], block)
        request = dict(self._base("fund"), job_id=_hex(terms[0]), payer=terms[3],
            terms=Bootstrap.wire(terms), permission=Bootstrap.wire(permission),
            configuration=Bootstrap.wire(config),
            treasury_epoch=str(self._call(treasury, "epoch", block)),
            roster_hash=_hex(self._call(treasury, "rosterHash", block)), value=str(config[4]))
        # The production function validates full state/configuration atomically;
        # eth_call also catches a stale snapshot before preparing a paid command.
        self.registry.functions.fundRequest(job, permission[0]).call(
            {"from": terms[3], "value": config[4]}, block_identifier=block.number)
        self._anchor(block)
        return request

    def prepare_admit(self, terms, accept_by):
        """Prepare an issuer's exact permission; this never pays for the job."""
        if self.w3.eth.chain_id != 31337 and self.manifest.get("bitcoin_policy") is None:
            raise ValueError("GAS_SPV_PROFILE_NOT_QUALIFIED")
        from .bootstrap import encode_args
        from .operations import public_data
        names = ("jobId", "semanticKey", "federationIdHash", "payer", "budgetScope", "serviceDeadline")
        if not isinstance(terms, dict) or set(terms) != set(names): raise ValueError("GAS_REQUEST_BINDING")
        block = self._binding(); self.service_profile(block)
        view = self.w3.eth.contract(address=self.manifest["state_view"], abi=artifact("ISPVServiceStateView")["abi"])
        state_hash, version, initialized, halted = self._call(view, "snapshot", block)
        if not initialized or halted: raise ValueError("GAS_STATE_BINDING")
        permission = self._call(self.registry, "permissions", block, terms["jobId"])
        issuer = self._call(self.registry, "configuration", block)[1]
        args = [[terms[name] for name in names], _hex(state_hash), str(version), accept_by, str(permission[0])]
        abi = next(row for row in artifact(REGISTRY)["abi"] if row.get("name") == "admit")
        decoded = encode_args(abi["inputs"], args)
        self.registry.functions.admit(*decoded).call({"from": issuer}, block_identifier=block.number)
        self._anchor(block)
        return dict(self._base("admit"), terms=public_data(terms), accept_by=accept_by, issuer=issuer, args=args, value="0")

    def service_profile(self, block):
        policy = self.manifest.get("bitcoin_policy")
        if policy is None:
            if self.w3.eth.chain_id != 31337: raise ValueError("GAS_SPV_PROFILE_NOT_QUALIFIED")
            return  # Existing explicitly local component fixtures only.
        from .zk_service_binding import inspect_service_view
        from .zk_spv import validate_confederation
        validate_confederation(policy, self.manifest["confederation_id"])
        factory_address = Web3.to_checksum_address(self.manifest["factory"])
        checked_runtime(self.w3, factory_address, "ConfederationFactory", block.number)
        prove_creation(self.w3, factory_address, "ConfederationFactory", None, block.number)
        factory = self.w3.eth.contract(address=factory_address, abi=artifact("ConfederationFactory")["abi"])
        conf = self._call(factory, "getConfederation", block, self.manifest["confederation_id"])
        if not conf[-1] or conf[7] != self._call(self.registry, "authorization", block):
            raise ValueError("GAS_AUTHORIZATION_BINDING")
        view = inspect_service_view(self.w3, self.manifest["state_view"], policy, block.number)
        for name in ("state", "adapter", "policyHash"):
            if self._call(view, name, block) != self._call(self.registry, name, block):
                raise ValueError("GAS_STATE_BINDING")
        if self._call(self.registry, "confederationId", block) != self.manifest["confederation_id"]:
            raise ValueError("GAS_DOMAIN_BINDING")
        return view

    def prepare_claim(self, treasury, payee):
        block = self._binding()
        target = self._treasury(treasury, block)
        payee = Web3.to_checksum_address(payee)
        amount = self._call(target, "claimable", block, payee)
        if amount == 0:
            raise ValueError("GAS_NOTHING_TO_CLAIM")
        request = dict(self._base("claim"), treasury=target.address, payee=payee,
            treasury_epoch=str(self._call(target, "epoch", block)),
            roster_hash=_hex(self._call(target, "rosterHash", block)),
            released=str(self._call(target, "released", block, payee)),
            received_total=str(self._call(target, "receivedTotal", block)), claimable=str(amount), value="0")
        self._anchor(block)
        return request

    def submit(self, request, identity_file, *, budget=None, recover_only=False):
        action = request.get("action")
        if action not in ("fund", "claim", "admit") or any(request.get(k) != v for k, v in self._base(action).items()):
            raise ValueError("GAS_REQUEST_BINDING")
        # A manually reviewed request is immutable. New income/permission changes
        # require a new request; retries retain the original operation/EOA nonce.
        block = self._binding()
        if action in ("fund", "admit"):
            self.service_profile(block)
            account = Account.from_key(json.loads(read_private(Path(identity_file)))["private_key"])
            if account.address != request["payer" if action == "fund" else "issuer"]:
                raise ValueError("GAS_PAYER_IDENTITY")
            target, kind = self.registry.address, REGISTRY
            method = "fundRequest" if action == "fund" else "admit"
            args = [request["job_id"], request["permission"][0]] if action == "fund" else request["args"]
            if action == "admit" and request["value"] != "0": raise ValueError("GAS_CLAIM_VALUE")
        else:
            target, kind, method = self._treasury(request["treasury"], block).address, TREASURY, "claimFor"
            args = [request["payee"]]
            if request["value"] != "0":
                raise ValueError("GAS_CLAIM_VALUE")
        identity = Path(identity_file).resolve()
        operation = "spv-gas-" + Web3.keccak(text=canonical(request)).hex()
        step = dict(id="send", kind="call", contract="target", method=method, args=args, value=request["value"])
        mainnet = self.w3.eth.chain_id == 1
        profile = dict(deployment_id=operation, mode="backed" if mainnet else "integration",
            bitcoin_network="mainnet" if mainnet else "regtest", chain_id=str(self.w3.eth.chain_id),
            evm_confirmations=self.manifest["evm_confirmations"], evm_rpc=self.w3.provider.endpoint_uri,
            contracts={"target": dict(address=target, artifact=kind,
                runtime_hash=Web3.keccak(self.w3.eth.get_code(target, block_identifier=block.number)).hex())}, steps=[step])
        class BudgetedBootstrap(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce)
                if budget is not None:budget.reserve(operation,tx)
                return tx
        runner = BudgetedBootstrap(profile, str(identity) + ".spv-gas-transactions.sqlite")
        key = operation + ":send"
        entry = runner.journal.put(key, step)
        identity_journal = Journal(str(identity.with_suffix(".evm-nonce.lock")) + ".sqlite")
        reserved = entry["result"].get("tx_hash") or restore_signed(identity_journal, runner, key, step)
        if recover_only and not reserved:return dict(status="NOT_SIGNED",operation=operation)
        if not reserved:
            current = (self.prepare_fund(request["job_id"]) if action == "fund" else
                       self.prepare_admit(request["terms"], request["accept_by"]) if action == "admit" else
                       self.prepare_claim(request["treasury"], request["payee"]))
            if current != request:
                raise ValueError("GAS_REQUEST_CHANGED")
        # Recover an already signed transaction even after its permission expired
        # or another caller claimed. Never silently strand the shared nonce lane.
        result = runner.apply_confirmed(identity)
        if result["status"] != "VERIFIED":
            return dict(status=result["status"], operation=operation)
        receipt = runner.verify_receipt(runner.journal.get(key)["result"])
        return dict(status="CONFIRMED", transaction_hash=_hex(receipt.transactionHash),
                    block_number=str(receipt.blockNumber),block_hash=receipt.blockHash.hex(),gas_used=str(receipt.gasUsed))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--binding", required=True, help="Reviewed local binding JSON")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "prepare-fund"):
        commands.add_parser(name).add_argument("--job", required=True)
    admit = commands.add_parser("prepare-admit")
    admit.add_argument("--terms", required=True)
    admit.add_argument("--accept-by", required=True)
    claim = commands.add_parser("prepare-claim")
    claim.add_argument("--treasury", required=True)
    claim.add_argument("--payee", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--request", required=True)
    submit.add_argument("--identity-file", required=True)
    args = parser.parse_args(argv)
    try:
        client = GasTreasuryClient(Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20})),
                                   json.loads(Path(args.binding).read_text()))
        if args.action == "status":
            result = client.status(args.job)
        elif args.action == "prepare-fund":
            result = client.prepare_fund(args.job)
        elif args.action == "prepare-admit":
            result = client.prepare_admit(json.loads(Path(args.terms).read_text()), args.accept_by)
        elif args.action == "prepare-claim":
            result = client.prepare_claim(args.treasury, args.payee)
        else:
            result = client.submit(json.loads(Path(args.request).read_text()), args.identity_file)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        from .relay_keeper import error_code
        reason = (error.args[0] if type(error) is ValueError and len(error.args) == 1
                  and isinstance(error.args[0], str) and error.args[0] in ERROR_CODES else error_code(error))
        print(json.dumps(dict(status="BLOCKED", reason=reason), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
