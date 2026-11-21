"""Installed CLI against a real DKG-registered DAO; only feed/USD are test fixtures."""

import json
import subprocess
import sys
from pathlib import Path

from eth_account import Account
from web3 import Web3
from test_dkg_processes import RPC, tx


def exercise_dao_cli(
    tmp_path,
    w3,
    factory,
    gov,
    staking,
    controller,
    board,
    settlement,
    usd,
    feed,
    vault,
    approve,
):
    home = tmp_path / "dao-user"
    home.mkdir()
    config = home / "dao.json"
    journal = home / "user.sqlite"
    user = Account.create()
    other = Account.create()
    identities = {}
    for account in (
        user,
        other,
        Account.from_key((2).to_bytes(32, "big")),
        Account.from_key((3).to_bytes(32, "big")),
    ):
        identity = home / (account.address + ".json")
        identity.write_text(json.dumps(dict(private_key=account.key.hex())))
        identity.chmod(0o600)
        identities[account.address] = identity
        w3.provider.make_request("anvil_setBalance", [account.address, hex(10**22)])

    def cli(action, *args, actor=None, send=False, operation=None, ok=True):
        options = [
            str(Path(sys.executable).with_name("frost-signer")),
            "dao",
            action,
            "--config",
            str(config),
            "--rpc",
            RPC,
            *map(str, args),
        ]
        if actor:
            options += ["--identity-file", str(identities[actor.address])]
        if send:
            options += ["--send", "--state", str(journal), "--id", operation]
        result = subprocess.run(
            options, cwd=home, capture_output=True, text=True, timeout=90
        )
        assert result.returncode == (0 if ok else 2), result.stdout + result.stderr
        return json.loads(result.stdout)

    initialized = cli(
        "init",
        "--chain-id",
        "31337",
        "--factory",
        factory.address,
        "--confederation",
        "integration-conf",
        "--output",
        config,
    )
    assert initialized["contracts"]["staking"]["address"] == staking.address
    assert "evm_rpc" not in config.read_text()
    cli(
        "init",
        "--chain-id",
        "31337",
        "--factory",
        factory.address,
        "--confederation",
        "integration-conf",
        "--output",
        config,
        ok=False,
    )
    # Funding is fixture setup; thereafter all user mutations go through installed CLI.
    tx(w3, gov.functions.transfer(user.address, 101 * 10**18))
    cli(
        "transfer",
        "--to",
        other.address,
        "--amount",
        "1",
        actor=user,
        send=True,
        operation="gift",
    )
    assert gov.functions.balanceOf(other.address).call() == 10**18
    nonce = w3.eth.get_transaction_count(user.address)
    assert cli("approve", "--amount", "100", actor=user)["status"] == "SIMULATED"
    assert w3.eth.get_transaction_count(user.address) == nonce
    cli("approve", "--amount", "100", actor=user, send=True, operation="allowance")
    cli("stake", "--amount", "100", actor=user, send=True, operation="stake")
    nonce = w3.eth.get_transaction_count(user.address)
    cli("stake", "--amount", "100", actor=user, send=True, operation="stake")
    assert w3.eth.get_transaction_count(user.address) == nonce
    assert (
        cli(
            "stake",
            "--amount",
            "99",
            actor=user,
            send=True,
            operation="stake",
            ok=False,
        )["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    assert (
        cli("status", "--owner", user.address)["wallet"]["staked"]["formatted"] == "100"
    )
    assert cli("unstake", "--amount", "101", actor=user, ok=False)["code"].startswith(
        "CONTRACT_"
    )

    consent = home / "consent.json"
    cli("signer-consent", "--owner", user.address, "--output", consent, actor=other)
    cli(
        "set-signer",
        "--signer",
        other.address,
        "--consent",
        consent,
        actor=user,
        send=True,
        operation="delegate",
    )
    cli(
        "set-signer",
        "--signer",
        other.address,
        "--consent",
        consent,
        actor=user,
        send=True,
        operation="delegate",
    )
    assert staking.functions.getDAOSigner(user.address).call() == other.address
    cli("unstake", "--amount", "1", actor=other, ok=False)
    cli("set-signer", "--signer", "self", actor=user, send=True, operation="self")
    cli(
        "set-signer",
        "--signer",
        other.address,
        "--consent",
        consent,
        actor=user,
        ok=False,
    )  # old nonce
    bob, carol = [Account.from_key(i.to_bytes(32, "big")) for i in (2, 3)]
    cli(
        "set-signer",
        "--signer",
        bob.address,
        actor=user,
        send=True,
        operation="custodian-signer",
    )
    cli(
        "propose-rate",
        "--percent",
        "0.013",
        "--custodian",
        user.address,
        actor=user,
        ok=False,
    )
    cli(
        "propose-rate",
        "--percent",
        "0.013",
        "--custodian",
        bob.address,
        actor=bob,
        send=True,
        operation="rate",
    )
    proposal = controller.functions.proposalCount().call()
    assert cli("proposals")["rows"][-1]["requiredVotes"] == "2"
    cli(
        "vote",
        "--proposal",
        proposal,
        "--custodian",
        bob.address,
        actor=bob,
        send=True,
        operation="vote-bob",
    )
    cli("execute", "--proposal", proposal, actor=user, ok=False)
    cli(
        "vote",
        "--proposal",
        proposal,
        "--custodian",
        carol.address,
        actor=carol,
        send=True,
        operation="vote-carol",
    )
    cli("execute", "--proposal", proposal, actor=user, send=True, operation="execute")
    assert controller.functions.baseRatePpm().call() == 120
    cli(
        "propose-reserve",
        "--enabled",
        "true",
        "--custodian",
        bob.address,
        actor=bob,
        send=True,
        operation="reserve",
    )
    reserve_proposal = controller.functions.proposalCount().call()
    for actor in (bob, carol):
        cli(
            "vote",
            "--proposal",
            reserve_proposal,
            "--custodian",
            actor.address,
            actor=actor,
            send=True,
            operation="reserve-" + actor.address,
        )
    cli(
        "execute",
        "--proposal",
        reserve_proposal,
        actor=user,
        send=True,
        operation="execute-reserve",
    )
    tx(w3, usd.functions.mint(user.address, 1_000_000))
    cli(
        "approve-reserve",
        "--amount",
        "1",
        actor=user,
        send=True,
        operation="reserve-allowance",
    )
    cli(
        "fund-reserve",
        "--amount",
        "1",
        actor=user,
        send=True,
        operation="reserve-funding",
    )

    first = staking.functions.firstEligibleDay().call()
    eligible_day = w3.eth.get_block("latest").timestamp // 86400 + 1
    w3.provider.make_request(
        "evm_setNextBlockTimestamp", [(eligible_day + 1) * 86400 + 1]
    )
    w3.provider.make_request("evm_mine", [])
    assert cli("status")["base_rate_ppm"] == "130"
    assert cli("status")["use_reserve"] is True
    assert cli("status")["reserve_balance"]["formatted"] == "1"
    # Create a real FROST-approved staker decision; CLI delegate casts the vote.
    args = [
        "ipfs://dao-cli-test",
        "0x" + Web3.keccak(text="CLI decision").hex(),
        86400,
        5000,
    ]
    op = approve(board, "CustodianDAODecisionBoard", "getCreateOperationHash", args)
    tx(
        w3,
        board.functions.createDecision(
            args[0], bytes.fromhex(args[1][2:]), args[2], args[3], op
        ),
    )
    decision = board.functions.decisionCount().call()
    assert cli("decisions")["rows"][-1]["id"] == str(decision)
    cli(
        "decision-vote",
        "--decision",
        decision,
        "--owner",
        user.address,
        "--choice",
        "for",
        actor=user,
        ok=False,
    )
    cli(
        "decision-vote",
        "--decision",
        decision,
        "--owner",
        user.address,
        "--choice",
        "for",
        actor=bob,
        send=True,
        operation="decision-vote",
    )
    assert board.functions.decisions(decision).call()[7] == 100 * 10**18

    # Fund an actual yield source and settle in order; inputs remain explicit fixture accounting.
    source = Web3.keccak(text="CLI_YIELD")
    source_args = [
        "0x" + source.hex(),
        w3.eth.accounts[0],
        0,
        True,
        "0x" + "00" * 20,
        0,
    ]
    op = approve(
        vault, "CustodianDAOIncomeVault", "getSetSourceOperationHash", source_args
    )
    tx(
        w3,
        vault.functions.setSource(
            source, source_args[1], 0, True, source_args[4], 0, op
        ),
    )
    tx(w3, usd.functions.mint(w3.eth.accounts[0], 10_000_000))
    tx(w3, usd.functions.approve(vault.address, 10_000_000))
    tx(w3, vault.functions.deposit(eligible_day, source, 10_000_000))
    for day in range(first, eligible_day + 1):
        tx(w3, feed.functions.setRound(1, 50_000 * 10**8, day * 86400))
        tx(w3, feed.functions.setRound(2, 50_000 * 10**8, (day + 1) * 86400))
        sources = vault.functions.getFundedYieldSources(day).call()
        income = [
            ["0x" + s.hex(), vault.functions.sourceIncome(day, s).call()]
            for s in sources
        ]
        report = [
            day,
            0,
            0,
            0,
            vault.functions.dayWrapFeeIncome(day).call(),
            vault.functions.dayDeployFeeIncome(day).call(),
            "0x" + Web3.keccak(text=str(day)).hex(),
            income,
            [[user.address, 1]],
        ]
        op = approve(
            settlement,
            "CustodianDAODailySettlement",
            "getSettlementOperationHash",
            [report, [1, 2]],
        )
        # Helpers' ABI coercion accepts explicit strings, while direct Web3 uses integer tuples.
        report[6] = bytes.fromhex(report[6][2:])
        report[7] = [[bytes.fromhex(s[2:]), amount] for s, amount in income]
        tx(w3, settlement.functions.settleDay(report, [1, 2], op))
    rewards = cli("rewards", "--owner", user.address, "--from-day", eligible_day)
    assert rewards["total_claimable"]["formatted"] == "5"
    assert cli("day", "--day", eligible_day)["stakerIncome"] == "5000000"
    cli("claim", "--days", eligible_day, actor=user, send=True, operation="claim")
    cli("claim", "--days", eligible_day, actor=user, send=True, operation="claim")
    assert usd.functions.balanceOf(user.address).call() == 5_000_000
    cli("claim-lp", actor=user, send=True, operation="claim-lp")
    assert usd.functions.balanceOf(user.address).call() >= 10_000_000
    cli("unstake", "--amount", "25", actor=user, send=True, operation="withdraw")
    assert staking.functions.stakedBalance(user.address).call() == 75 * 10**18
    w3.provider.make_request("evm_increaseTime", [86401])
    w3.provider.make_request("evm_mine", [])
    cli(
        "decision-finalize",
        "--decision",
        decision,
        actor=user,
        send=True,
        operation="decision-finalize",
    )
    assert cli("decisions")["rows"][-1]["passed"] is True
    # Changing a pinned code hash is rejected before wallet use or approval.
    original = config.read_text()
    altered = json.loads(original)
    altered["contracts"]["staking"]["runtime_hash"] = "00" * 32
    config.write_text(json.dumps(altered))
    assert cli("status", ok=False)["code"] == "DAO_CONFIG_BINDING_CHANGED"
    config.write_text(original)
