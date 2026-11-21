"""Typed user CLI; no FROST shares, unrestricted calldata or shared operator wallet."""

import argparse
import json
import os
from eth_account.messages import encode_typed_data
from web3 import Web3
from web3.exceptions import ContractLogicError

from .dao_client import (
    DAOClient,
    account_at,
    address,
    claim_days,
    consent_data,
    day_number,
    units,
    wire,
)
from .onboarding import read_public, write_public
from .operations import Journal
from .participant_errors import participant_exception_code
from .safe_errors import operator_error, DiagnosticArgumentParser, emit_debug


def parser():
    p = DiagnosticArgumentParser(
        prog="frost-signer dao",
        description="DAO wallet CLI. Read/simulate by default; --send explicitly broadcasts.",
    )
    sub = p.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--rpc",
        default=os.getenv("SATOHSI_DAO_RPC"),
        help="Local RPC URL, or SATOHSI_DAO_RPC; never saved in DAO config",
    )
    common.add_argument("--config", default=os.getenv("SATOHSI_DAO_CONFIG", "dao.json"))
    wallet = argparse.ArgumentParser(add_help=False)
    wallet.add_argument(
        "--identity-file", help="Own local EVM identity JSON, permissions 0600"
    )
    wallet.add_argument(
        "--sender", help="Public caller for simulation; must match identity if supplied"
    )
    wallet.add_argument(
        "--send",
        action="store_true",
        help="Broadcast; otherwise eth_call + gas estimate only",
    )
    wallet.add_argument("--state", help="Own durable journal; required with --send")
    wallet.add_argument(
        "--id",
        help="Unique operation ID; reuse unchanged for retry, new ID for a new operation",
    )
    c = sub.add_parser(
        "init", parents=[common], help="Discover and pin the registered DAO graph"
    )
    source = c.add_mutually_exclusive_group(required=True)
    source.add_argument("--factory")
    source.add_argument(
        "--board",
        help="Discover factory from the checked DKG board's registration intent",
    )
    c.add_argument("--confederation")
    c.add_argument("--chain-id", required=True)
    c.add_argument("--confirmations", type=int, default=1)
    c.add_argument("--from-block", type=int)
    c.add_argument(
        "--output", default="dao.json", help="New public config; refuses overwrite"
    )
    c = sub.add_parser(
        "status",
        parents=[common],
        help="Balances, signer, custodians, rate and reserve",
    )
    c.add_argument("--owner")
    c = sub.add_parser(
        "rewards",
        parents=[common],
        help="Settled UTC rewards, at most 32 days per page",
    )
    c.add_argument("--owner", required=True)
    c.add_argument("--from-day", type=day_number)
    c.add_argument("--limit", type=int, default=32)
    c = sub.add_parser(
        "day",
        parents=[common],
        help="Inspect a completed settlement, amounts in reward-token base units",
    )
    c.add_argument("--day", type=day_number, required=True)
    for kind in ("proposals", "decisions"):
        c = sub.add_parser(
            kind, parents=[common], help="Paginated on-chain governance records"
        )
        c.add_argument("--from-id", type=int, default=1)
        c.add_argument("--limit", type=int, default=20)
    for name in ("approve", "stake", "unstake", "transfer", "fund-reserve"):
        c = sub.add_parser(name, parents=[common, wallet])
        c.add_argument(
            "--amount",
            required=True,
            help="Human token units, e.g. 100.25; no exponent or comma",
        )
        if name == "transfer":
            c.add_argument("--to", required=True)
    c = sub.add_parser(
        "approve-reserve",
        parents=[common, wallet],
        help="Exact reward-token allowance for reserve funding",
    )
    c.add_argument("--amount", required=True)
    c = sub.add_parser("claim", parents=[common, wallet])
    c.add_argument(
        "--days",
        nargs="+",
        required=True,
        help="1–32 unique YYYY-MM-DD UTC dates or integer day numbers",
    )
    sub.add_parser("claim-lp", parents=[common, wallet])
    c = sub.add_parser(
        "signer-consent",
        parents=[common],
        help="Sign EIP-712 consent with your OTHER wallet; no transaction",
    )
    c.add_argument("--owner", required=True)
    c.add_argument("--identity-file", required=True)
    c.add_argument("--output", required=True)
    c = sub.add_parser("set-signer", parents=[common, wallet])
    c.add_argument("--signer", required=True, help="Address or 'self'")
    c.add_argument(
        "--consent", help="EIP-712 consent file for a non-custodian other wallet"
    )
    c = sub.add_parser("propose-rate", parents=[common, wallet])
    c.add_argument(
        "--percent",
        required=True,
        help="Daily percent, e.g. 0.012; precision 0.0001 percent",
    )
    c.add_argument(
        "--custodian",
        required=True,
        help="Custodian principal represented by the sending DAO signer",
    )
    c = sub.add_parser("propose-reserve", parents=[common, wallet])
    c.add_argument("--enabled", choices=("true", "false"), required=True)
    c.add_argument("--custodian", required=True)
    c = sub.add_parser(
        "vote",
        parents=[common, wallet],
        help="Approve a rate/reserve proposal as a custodian",
    )
    c.add_argument("--proposal", type=int, required=True)
    c.add_argument("--custodian", required=True)
    c = sub.add_parser(
        "execute",
        parents=[common, wallet],
        help="Apply an approved proposal starting next UTC day",
    )
    c.add_argument("--proposal", type=int, required=True)
    c = sub.add_parser(
        "decision-vote",
        parents=[common, wallet],
        help="Vote for one stake owner as their current DAO signer",
    )
    c.add_argument("--decision", type=int, required=True)
    c.add_argument("--owner", required=True)
    c.add_argument("--choice", choices=("against", "for", "abstain"), required=True)
    c = sub.add_parser("decision-finalize", parents=[common, wallet])
    c.add_argument("--decision", type=int, required=True)
    c = sub.add_parser(
        "tx-status",
        help="Read your local operation receipt; never outputs RPC or signed raw transactions",
    )
    c.add_argument("--state", required=True)
    c.add_argument("--id", required=True)
    return p


def discover(client, board_address, from_block=None):
    from .onchain_transport import invitation

    w3 = client.w3
    board, invite = invitation(w3, address(board_address))
    first = client.read(board, "registryBlock") if from_block is None else from_block
    if first < 0 or first > client.block.number:
        raise ValueError("INVALID_FROM_BLOCK")
    candidates = set()
    from .board_intents import intent_rows
    for row in intent_rows(w3, board, client.block):
        intent = row["intent"]
        if (
            intent.get("artifact") != "ConfederationFactory"
            or intent.get("hash_method") != "getRegisterCustodianDAOOperationHash"
        ):
            continue
        if (
            intent.get("chain_id") != str(w3.eth.chain_id)
            or intent["hash_args"][0] != invite["confederation_id"]
        ):
            continue
        factory = address(intent["target"])
        client.graph(factory, invite["confederation_id"])
        if client.read("settlement", "treasury") != invite["treasury"]:
            raise ValueError("DAO_BOARD_TREASURY_MISMATCH")
        if (
            list(
                client.read(
                    "factory", "getCustodianDAO", invite["confederation_id"]
                )
            )
            != intent["hash_args"][1]
        ):
            raise ValueError("DAO_REGISTRATION_INTENT_MISMATCH")
        coordinator = client.contract(
            "FROSTCoordinator", client.read("authorization", "coordinator")
        )
        authorization = client.contract(
            "DKGAuthorization", client.contracts["authorization"].address
        )
        if client.read(coordinator, "dkgRegistry") != client.read(
            board, "registry"
        ) or bytes(client.read(authorization, "dkgId")).hex() != invite["sessions"][
            "conf_ceremony"
        ].removeprefix(
            "0x"
        ):
            raise ValueError("DAO_DKG_BINDING_MISMATCH")
        candidates.add(factory)
    if len(candidates) != 1:
        raise ValueError("DAO_DISCOVERY_REQUIRES_ONE_REGISTERED_FACTORY")
    return candidates.pop(), invite["confederation_id"]


def run(a):
    if a.action == "tx-status":
        entry = Journal(a.state).get("dao-request-" + a.id)
        # Inspect the executor entry too: process death may precede outer status update.
        try:
            tx = Journal(a.state).get("dao-tx-" + a.id + ":send")
            result = dict(
                status=tx["status"],
                tx_hash=tx["result"].get("tx_hash"),
                block_number=tx["result"].get("block_number"),
            )
        except KeyError:
            result = dict(status=entry["status"])
        return dict(id=a.id, request=entry["request"], **result)
    if a.action == "init":
        c = DAOClient(a.rpc, a.chain_id)
        if a.board:
            factory, conf = discover(c, a.board, a.from_block)
            if a.confederation and a.confederation != conf:
                raise ValueError("CONFEDERATION_MISMATCH")
        else:
            if not a.confederation:
                raise ValueError("CONFEDERATION_REQUIRED")
            factory, conf = a.factory, a.confederation
        return c.init_config(factory, conf, a.output, a.confirmations)
    c = DAOClient.load(a.rpc, a.config)
    if a.action == "status":
        result = c.status(a.owner)
    elif a.action == "rewards":
        result = c.rewards(a.owner, a.from_day, a.limit)
    elif a.action == "day":
        result = c.settlement(a.day)
    elif a.action in ("proposals", "decisions"):
        result = c.listing(a.action, a.from_id, a.limit)
    elif a.action == "signer-consent":
        account = account_at(a.identity_file)
        data = consent_data(c, a.owner, account.address)
        msg = encode_typed_data(data["domain"], data["types"], data["message"])
        signed = account.sign_message(msg)
        write_public(
            a.output,
            dict(version="1", **wire(data), signature="0x" + signed.signature.hex()),
        )
        result = dict(
            status="CONSENT_SIGNED",
            owner=address(a.owner),
            signer=account.address,
            output=a.output,
            nonce=str(data["message"]["nonce"]),
        )
    else:
        sender = (
            address(a.sender)
            if a.sender
            else (account_at(a.identity_file).address if a.identity_file else None)
        )
        if not sender:
            raise ValueError("SENDER_OR_IDENTITY_REQUIRED")
        if a.identity_file and account_at(a.identity_file).address != sender:
            raise ValueError("SENDER_IDENTITY_MISMATCH")
        name, method, args = transaction(c, a, sender)
        return c.transact(
            name, method, args, sender, a.identity_file, a.send, a.state, a.id
        )
    c.stable()
    result.setdefault("status", "OK")
    return result


def transaction(c, a, sender):
    if a.action in (
        "approve",
        "stake",
        "unstake",
        "transfer",
        "fund-reserve",
        "approve-reserve",
    ):
        reward = a.action in ("fund-reserve", "approve-reserve")
        amount = units(
            a.amount,
            c.reward_decimals if reward else c.stake_decimals,
            allow_zero=a.action in ("approve", "approve-reserve"),
        )
        if a.action == "transfer":
            return "token", "transfer", [address(a.to), amount]
        if a.action in ("approve", "approve-reserve"):
            return (
                ("reward" if reward else "token"),
                "approve",
                [c.contracts["settlement" if reward else "staking"].address, amount],
            )
        if a.action == "fund-reserve":
            return "settlement", "fundReserve", [amount]
        return "staking", a.action, [amount]
    if a.action == "claim":
        return "staking", "claim", [claim_days(a.days)]
    if a.action == "claim-lp":
        return "settlement", "claimLiquidityIncome", []
    if a.action == "set-signer":
        signer = sender if a.signer == "self" else address(a.signer)
        signature = b""
        if a.consent:
            data = read_public(a.consent)
            expected = wire(consent_data(c, sender, signer))
            if (
                data.get("version") != "1"
                or data.get("domain") != expected["domain"]
                or data.get("types") != expected["types"]
                or data["message"]["owner"] != sender
                or data["message"]["signer"] != signer
            ):
                raise ValueError("SIGNER_CONSENT_CONTEXT_MISMATCH")
            signature = bytes.fromhex(data["signature"].removeprefix("0x"))
        elif signer != sender and not c.read("validator", "isCustodian", signer):
            raise ValueError("OTHER_WALLET_CONSENT_REQUIRED")
        return "staking", "setDAOSigner", [signer, signature]
    if a.action in ("propose-rate", "propose-reserve"):
        value = (
            units(a.percent, 4, allow_zero=True)
            if a.action == "propose-rate"
            else int(a.enabled == "true")
        )
        if a.action == "propose-rate" and value > 1_000_000:
            raise ValueError("RATE_EXCEEDS_100_PERCENT")
        return (
            "controller",
            "propose",
            [int(a.action == "propose-reserve"), value, address(a.custodian)],
        )
    if a.action == "vote":
        return "controller", "vote", [a.proposal, address(a.custodian)]
    if a.action == "execute":
        return "controller", "execute", [a.proposal]
    if a.action == "decision-vote":
        return (
            "decisions",
            "vote",
            [
                a.decision,
                address(a.owner),
                ("against", "for", "abstain").index(a.choice),
            ],
        )
    if a.action == "decision-finalize":
        return "decisions", "finalize", [a.decision]
    raise ValueError("UNKNOWN_DAO_ACTION")


def error_code(exc):
    if isinstance(exc, ContractLogicError):
        data = getattr(exc, "data", "")
        if isinstance(data, str) and data.startswith("0x"):
            from .bootstrap import artifact
            from .dao_client import DAO_NAMES

            for kind in set(DAO_NAMES.values()) | {"IERC20Metadata"}:
                for item in artifact(kind)["abi"]:
                    if item.get("type") == "error":
                        sig = (
                            item["name"]
                            + "("
                            + ",".join(x["type"] for x in item["inputs"])
                            + ")"
                        )
                        if data[2:10] == Web3.keccak(text=sig).hex()[:8]:
                            return "CONTRACT_" + item["name"]
        return "TRANSACTION_SIMULATION_REVERTED"
    return participant_exception_code(exc)


def main(argv):
    if argv and argv[0] == "in-kind":
        from .confederation_dao_cli import main as in_kind
        return in_kind(argv[1:])
    a = parser().parse_args(argv)
    try:
        result = run(a)
    except Exception as exc:
        # Never include raw RPC exception text, endpoints, identities or calldata dumps.
        code = error_code(exc)
        emit_debug(exc, code=code)
        print(json.dumps(operator_error(code, "dao", exc), ensure_ascii=False))
        return 2
    print(json.dumps(wire(result), indent=2))
    return (
        0
        if result.get("status")
        in ("OK", "CONFIGURED", "CONSENT_SIGNED", "SIMULATED", "VERIFIED", "CONFIRMED")
        else 2
    )
