"""User DAO reads and durable transactions. RPC routing is never part of shared config."""

import json
import re
from datetime import date, timedelta

from eth_account import Account
from web3 import Web3

from .bootstrap import Bootstrap, artifact
from .onboarding import read_private, read_public, write_public
from .operations import Journal
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation, check_provenance_anchors

DAO_NAMES = dict(
    token="CustodianDAOToken",
    staking="CustodianDAOStaking",
    controller="CustodianDAORateController",
    twap="ChainlinkBTCDailyTWAP",
    incomeVault="CustodianDAOIncomeVault",
    decisions="CustodianDAODecisionBoard",
    settlement="CustodianDAODailySettlement",
)
ZERO = "0x" + "00" * 20


def address(value):
    if not isinstance(value, str) or not Web3.is_address(value) or int(value, 16) == 0:
        raise ValueError("INVALID_ADDRESS")
    return Web3.to_checksum_address(value)


def units(value, decimals, allow_zero=False):
    # No float or Decimal-context rounding, exponent notation, signs or silent truncation.
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value
    ):
        raise ValueError("INVALID_AMOUNT_USE_DECIMAL_STRING")
    whole, _, fraction = value.partition(".")
    if len(fraction) > decimals:
        raise ValueError("AMOUNT_PRECISION_EXCEEDED")
    result = int(whole) * 10**decimals + int(fraction.ljust(decimals, "0") or "0")
    if result >= 2**256 or (result == 0 and not allow_zero):
        raise ValueError("INVALID_AMOUNT")
    return result


def display(value, decimals):
    whole, fraction = divmod(int(value), 10**decimals)
    return str(whole) + (
        ("." + str(fraction).zfill(decimals).rstrip("0")) if fraction else ""
    )


def day_number(value):
    if re.fullmatch(r"0|[1-9][0-9]*", value):
        result = int(value)
    else:
        try:
            result = (date.fromisoformat(value) - date(1970, 1, 1)).days
        except ValueError:
            raise ValueError("INVALID_UTC_DAY") from None
    if result < 0 or result > 2932896:
        raise ValueError("INVALID_UTC_DAY")
    return result


def day_label(day):
    return (date(1970, 1, 1) + timedelta(days=int(day))).isoformat()


def claim_days(values):
    days = [day_number(v) for v in values]
    if not days or len(days) > 32 or len(set(days)) != len(days):
        raise ValueError("CLAIM_REQUIRES_1_TO_32_UNIQUE_DAYS")
    return sorted(days)


def wire(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    return value


def account_at(path):
    return Account.from_key(json.loads(read_private(path))["private_key"])


class DAOClient:
    def __init__(self, rpc, chain_id):
        if not rpc:
            raise ValueError("RPC_REQUIRED")
        # Match the existing reviewed durable executor's supported environments.
        if str(chain_id) not in ("1", "31337", "11155111"):
            raise ValueError("INTEGRATION_NETWORK_NOT_ALLOWED")
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        if self.w3.eth.chain_id != int(chain_id):
            raise ValueError("CHAIN_MISMATCH")
        from .evm_network import read_block
        self.block = read_block(self.w3, 12) if self.w3.eth.chain_id == 1 else self.w3.eth.get_block("latest")
        self.contracts = {}

    def contract(self, kind, target):
        return self.w3.eth.contract(address=address(target), abi=artifact(kind)["abi"])

    def read(self, contract, method, *args):
        c = self.contracts[contract] if isinstance(contract, str) else contract
        return getattr(c.functions, method)(*args).call(
            block_identifier=self.block.number
        )

    def stable(self):
        if self.w3.eth.get_block(self.block.number).hash != self.block.hash:
            raise ValueError("READ_REORG_RETRY")

    def graph(self, factory_address, confederation):
        factory = self.contract("ConfederationFactory", factory_address)
        checked_runtime(
            self.w3, factory.address, "ConfederationFactory", self.block.number
        )
        conf = self.read(factory, "getConfederation", confederation)
        if not conf[-1]:
            raise ValueError("CONFEDERATION_INACTIVE")
        dao = self.read(factory, "getCustodianDAO", confederation)
        if ZERO in dao:
            raise ValueError("DAO_NOT_REGISTERED")
        self.contracts = {
            k: self.contract(kind, a) for (k, kind), a in zip(DAO_NAMES.items(), dao)
        }
        self.contracts["factory"] = factory
        self.contracts["validator"] = self.contract("CustodianValidator", conf[8])
        self.contracts["authorization"] = self.contract("FROSTAuthorization", conf[7])
        auth_kind = "DKGAuthorization"
        try:
            checked_runtime(self.w3, conf[7], auth_kind, self.block.number)
        except ValueError:
            auth_kind = "FROSTAuthorization"
            checked_runtime(self.w3, conf[7], auth_kind, self.block.number)
        self.contracts["authorization"] = self.contract(auth_kind, conf[7])
        self.contracts["coordinator"] = self.contract(
            "FROSTCoordinator", self.read(factory, "frostCoordinator")
        )
        self.contracts["verifier"] = self.contract(
            "FROSTVerifier", self.read("coordinator", "defaultVerifier")
        )
        if auth_kind == "DKGAuthorization":
            self.contracts["dkg"] = self.contract(
                "DKGCoordinator", self.read("authorization", "registry")
            )
            result = self.read(
                "dkg", "getCompletedResult", self.read("authorization", "dkgId")
            )
            if (
                self.read("coordinator", "dkgRegistry") != self.contracts["dkg"].address
                or self.read(factory, "dkgCoordinator") != self.contracts["dkg"].address
                or self.read("authorization", "resultHash") != result[1]
                or result[3] != Web3.keccak(text="EVM_AUTH")
                or self.read("authorization", "groupPubkey") != result[4]
                or self.read("authorization", "getParticipants") != result[5]
                or self.read("authorization", "threshold") != result[6]
            ):
                raise ValueError("DAO_AUTHORIZATION_DKG_MISMATCH")
        reward = self.read("staking", "rewardToken")
        self.contracts["reward"] = self.contract("IERC20Metadata", reward)
        links = [
            ("staking", "stakeToken", dao[0]),
            ("staking", "distributor", dao[6]),
            ("staking", "custodianValidator", conf[8]),
            ("controller", "staking", dao[1]),
            ("controller", "validator", conf[8]),
            ("decisions", "staking", dao[1]),
            ("decisions", "authorization", conf[7]),
            ("validator", "frostMultisig", conf[7]),
            ("authorization", "coordinator", self.read(factory, "frostCoordinator")),
            ("authorization", "verifier", self.contracts["verifier"].address),
            ("incomeVault", "settlement", dao[6]),
            ("incomeVault", "rewardToken", reward),
            ("incomeVault", "authorization", conf[7]),
            ("settlement", "staking", dao[1]),
            ("settlement", "controller", dao[2]),
            ("settlement", "twap", dao[3]),
            ("settlement", "incomeVault", dao[4]),
            ("settlement", "rewardToken", reward),
            ("settlement", "authorization", conf[7]),
        ]
        for source, getter, expected in links:
            if self.read(source, getter) != expected:
                raise ValueError("DAO_BINDING_MISMATCH")
        kinds = dict(
            DAO_NAMES,
            factory="ConfederationFactory",
            validator="CustodianValidator",
            authorization=auth_kind,
            coordinator="FROSTCoordinator",
            verifier="FROSTVerifier",
            reward="IERC20Metadata",
        )
        if "dkg" in self.contracts:
            kinds["dkg"] = "DKGCoordinator"
        result = {}
        for name, c in self.contracts.items():
            code = self.w3.eth.get_code(c.address, block_identifier=self.block.number)
            if not code:
                raise ValueError("CONTRACT_CODE_MISSING")
            if name != "reward":
                checked_runtime(self.w3, c.address, kinds[name], self.block.number)
            result[name] = dict(
                address=c.address,
                artifact=kinds[name],
                runtime_hash=Web3.keccak(code).hex(),
            )
        self.stake_decimals = int(self.read("token", "decimals"))
        self.reward_decimals = int(self.read("reward", "decimals"))
        self.stake_symbol = self.read("token", "symbol")
        self.reward_symbol = self.read("reward", "symbol")
        if self.stake_decimals != 18 or self.reward_decimals > 18:
            raise ValueError("UNSUPPORTED_TOKEN_DECIMALS")
        if self.read("token", "totalSupply") != 10_000_000 * 10**18:
            raise ValueError("DAO_TOKEN_SUPPLY_MISMATCH")
        self.stable()
        return result

    def init_config(self, factory, confederation, output, confirmations):
        if not (12 <= confirmations <= 256 if self.w3.eth.chain_id == 1 else 1 <= confirmations <= 1000):
            raise ValueError("INVALID_CONFIRMATIONS")
        if self.w3.eth.chain_id == 1:
            from .evm_network import read_block
            self.block = read_block(self.w3, confirmations)
        contracts = self.graph(factory, confederation)
        provenance = {
            name: prove_creation(
                self.w3, item["address"], item["artifact"], None, self.block.number
            )
            for name, item in contracts.items()
            if name != "reward"
        }
        self.stable()
        config = dict(
            version="2",
            chain_id=str(self.w3.eth.chain_id),
            factory=address(factory),
            confederation_id=confederation,
            confirmations=str(confirmations),
            contracts=contracts,
            provenance=provenance,
            anchor=dict(number=str(self.block.number), hash=self.block.hash.hex()),
        )
        write_public(output, config)
        return dict(status="CONFIGURED", config=output, **config)

    @classmethod
    def load(cls, rpc, path):
        config = read_public(path)
        if config.get("version") == "1":
            raise ValueError("DAO_CONFIG_PROVENANCE_REQUIRED_RUN_INIT")
        if (
            set(config)
            != {
                "version",
                "chain_id",
                "factory",
                "confederation_id",
                "confirmations",
                "contracts",
                "anchor",
                "provenance",
            }
            or config["version"] != "2"
        ):
            raise ValueError("DAO_CONFIG_SCHEMA")
        c = cls(rpc, config["chain_id"])
        if not (12 <= int(config["confirmations"]) <= 256 if c.w3.eth.chain_id == 1 else 1 <= int(config["confirmations"]) <= 1000):
            raise ValueError("INVALID_CONFIRMATIONS")
        if c.w3.eth.chain_id == 1:
            from .evm_network import read_block
            c.block = read_block(c.w3, int(config["confirmations"]))
        if (
            c.w3.eth.get_block(int(config["anchor"]["number"])).hash.hex()
            != config["anchor"]["hash"]
        ):
            raise ValueError("DAO_ANCHOR_CHANGED")
        if (
            c.graph(config["factory"], config["confederation_id"])
            != config["contracts"]
        ):
            raise ValueError("DAO_CONFIG_BINDING_CHANGED")
        check_provenance_anchors(
            c.w3,
            {
                "contracts": {
                    name: item
                    for name, item in config["contracts"].items()
                    if name != "reward"
                }
            },
            config["provenance"],
        )
        # This JSON is public and may have been copied from elsewhere. Revalidate
        # its receipts/initcode, not just its claimed block hashes, before use.
        for name, saved in config["provenance"].items():
            item = config["contracts"][name]
            actual = prove_creation(
                c.w3,
                item["address"],
                item["artifact"],
                None,
                c.block.number,
                birth_hint=saved["block_number"],
            )
            if actual != saved:
                raise ValueError("DAO_CONFIG_PROVENANCE_MISMATCH")
        c.stable()
        c.config = config
        return c

    def money(self, amount, reward=False):
        return dict(
            base_units=str(amount),
            formatted=display(amount, self.reward_decimals if reward else 18),
            symbol=self.reward_symbol if reward else self.stake_symbol,
        )

    def status(self, owner=None):
        today = self.block.timestamp // 86400
        next_day = self.read("settlement", "nextSettlementDay")
        first = self.read("staking", "firstEligibleDay")
        result = dict(
            chain_id=str(self.w3.eth.chain_id),
            block=str(self.block.number),
            utc_day=day_label(today),
            token=self.contracts["token"].address,
            treasury=self.read("settlement", "treasury"),
            total_staked=self.money(self.read("staking", "totalStaked")),
            base_rate_ppm=str(self.read("controller", "baseRatePpm")),
            base_rate_percent=display(self.read("controller", "baseRatePpm"), 4),
            use_reserve=self.read("controller", "useReserve"),
            reserve_balance=self.money(self.read("settlement", "reserveBalance"), True),
            first_eligible_day=day_label(first),
            next_settlement_day=day_label(next_day),
            custodians=self.read("validator", "getCustodians"),
        )
        result["latest_settlement"] = (
            self.settlement(next_day - 1) if next_day > first else None
        )
        if owner:
            owner = address(owner)
            result["wallet"] = dict(
                owner=owner,
                token_balance=self.money(self.read("token", "balanceOf", owner)),
                staked=self.money(self.read("staking", "stakedBalance", owner)),
                eligible_today=self.money(
                    self.read("staking", "eligibleStake", owner, today)
                ),
                allowance=self.money(
                    self.read(
                        "token", "allowance", owner, self.contracts["staking"].address
                    )
                ),
                dao_signer=self.read("staking", "getDAOSigner", owner),
                is_custodian=self.read("validator", "isCustodian", owner),
                lp_claimable=self.money(
                    self.read("settlement", "lpClaimable", owner), True
                ),
            )
        return result

    def settlement(self, day):
        if day < self.read("staking", "firstEligibleDay") or day >= self.read(
            "settlement", "nextSettlementDay"
        ):
            raise ValueError("DAY_NOT_SETTLED")
        c = self.contracts["settlement"]
        names = next(f for f in c.abi if f.get("name") == "getSettlement")["outputs"][
            0
        ]["components"]
        return dict(
            day=day_label(day),
            **{
                f["name"]: wire(v)
                for f, v in zip(names, self.read(c, "getSettlement", day))
            },
        )

    def rewards(self, owner, start=None, limit=32):
        if not 1 <= limit <= 32:
            raise ValueError("PAGE_LIMIT_1_TO_32")
        owner = address(owner)
        first = self.read("staking", "firstEligibleDay")
        next_day = self.read("settlement", "nextSettlementDay")
        start = max(first, next_day - limit) if start is None else start
        if start < first:
            raise ValueError("DAY_BEFORE_DAO")
        rows = []
        for day in range(start, min(start + limit, next_day)):
            claimed = self.read("staking", "claimed", owner, day)
            amount = self.read("staking", "claimableReward", owner, [day])
            rows.append(
                dict(
                    day=day_label(day),
                    day_number=str(day),
                    claimed=claimed,
                    eligible=self.money(
                        self.read("staking", "eligibleStake", owner, day)
                    ),
                    claimable=self.money(amount, True),
                )
            )
        return dict(
            owner=owner,
            rows=rows,
            total_claimable=self.money(
                sum(int(r["claimable"]["base_units"]) for r in rows), True
            ),
            next_from_day=(
                day_label(start + limit) if start + limit < next_day else None
            ),
            history_first_day=day_label(first),
            next_settlement_day=day_label(next_day),
        )

    def listing(self, kind, start=1, limit=20):
        if start < 1 or not 1 <= limit <= 100:
            raise ValueError("INVALID_LIST_PAGE")
        name, counter, getter = (
            ("controller", "proposalCount", "proposals")
            if kind == "proposals"
            else ("decisions", "decisionCount", "decisions")
        )
        count = self.read(name, counter)
        outputs = next(f for f in self.contracts[name].abi if f.get("name") == getter)[
            "outputs"
        ]
        rows = [
            dict(
                id=str(i),
                **{
                    f["name"]: wire(v)
                    for f, v in zip(outputs, self.read(name, getter, i))
                },
            )
            for i in range(start, min(start + limit, count + 1))
        ]
        if kind == "proposals":
            for row in rows:
                parameter = int(row["parameter"])
                row["parameter"] = ["rate", "reserve"][parameter]
                row["superseded"] = (
                    int(row["id"]) <= self.read(name, "lastExecutedProposal", parameter)
                    and not row["executed"]
                )
                row["expired"] = self.block.timestamp > int(row["deadline"])
        return dict(
            count=str(count),
            rows=rows,
            next_from_id=str(start + limit) if start + limit <= count else None,
        )

    def transact(self, name, method, args, sender, identity, send, state, operation_id):
        sender = address(sender)
        c = self.contracts[name]
        fn = getattr(c.functions, method)(*args)
        request = dict(
            chain_id=self.config["chain_id"],
            factory=self.config["factory"],
            confederation=self.config["confederation_id"],
            sender=sender,
            contract=name,
            target=c.address,
            method=method,
            args=wire(args),
        )
        if not send:
            fn.call({"from": sender})
            gas = fn.estimate_gas({"from": sender})
            return dict(status="SIMULATED", gas=str(gas), request=request)
        if not identity or not state or not operation_id:
            raise ValueError("SEND_REQUIRES_IDENTITY_STATE_AND_ID")
        if account_at(identity).address != sender:
            raise ValueError("SENDER_IDENTITY_MISMATCH")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", operation_id):
            raise ValueError("INVALID_OPERATION_ID")
        journal = Journal(state)
        key = "dao-request-" + operation_id
        journal.put(
            key, request
        )  # A changed sender/amount/target cannot reuse this id.
        prefix = "dao-tx-" + operation_id
        try:
            saved = journal.get(prefix)
        except KeyError:
            fn.call({"from": sender})
            profile = dict(
                deployment_id=prefix,
                mode="backed" if self.w3.eth.chain_id == 1 else "integration",
                bitcoin_network="mainnet" if self.w3.eth.chain_id == 1 else "regtest",
                chain_id=self.config["chain_id"],
                evm_rpc=self.w3.provider.endpoint_uri,
                evm_confirmations=self.config["confirmations"],
                contracts=self.config["contracts"],
                steps=[
                    dict(
                        id="send",
                        kind="call",
                        contract=name,
                        method=method,
                        args=wire(args),
                    )
                ],
            )
        else:
            profile = saved["request"]["profile"]
        result = Bootstrap(profile, state).apply_confirmed(identity)
        tx = journal.get(prefix + ":send")
        public = dict(
            status=result["status"],
            id=operation_id,
            sender=sender,
            target=c.address,
            method=method,
            tx_hash=tx["result"].get("tx_hash"),
            block_number=tx["result"].get("block_number"),
        )
        journal.update(key, result["status"], public)
        return public


def consent_data(client, owner, signer):
    return dict(
        domain=dict(
            name="CustodianDAOStaking",
            version="1",
            chainId=client.w3.eth.chain_id,
            verifyingContract=client.contracts["staking"].address,
        ),
        types={
            "SignerConsent": [
                dict(name="owner", type="address"),
                dict(name="signer", type="address"),
                dict(name="nonce", type="uint256"),
            ]
        },
        message=dict(
            owner=address(owner),
            signer=address(signer),
            nonce=client.read("staking", "signerConsentNonce", address(owner)),
        ),
    )
