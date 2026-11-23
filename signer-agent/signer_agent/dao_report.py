"""Reproduce a funded UTC-day report and exact evidence bytes from one pinned EVM block.
BTC TVL/liquidity inputs remain explicit signed accounting assertions, not SPV claims.
"""

import hashlib, json
from pathlib import Path
from web3 import Web3
from .evm_rpc import evm_web3
from .safe_errors import DiagnosticArgumentParser
from .bootstrap import artifact
from .operations import canonical, public_data


def prepare(w3, settlement_address, inputs):
    public_data(inputs)
    block = w3.eth.get_block("latest")
    height = block.number

    def c(name, address):
        return w3.eth.contract(address=address, abi=artifact(name)["abi"])

    def read(fn):
        return fn.call(block_identifier=height)

    settlement = c("CustodianDAODailySettlement", settlement_address)
    day = int(inputs["day"])
    if day != read(settlement.functions.nextSettlementDay()):
        raise ValueError("DAY_ORDER")
    vault = c("CustodianDAOIncomeVault", read(settlement.functions.incomeVault()))
    twap = c("ChainlinkBTCDailyTWAP", read(settlement.functions.twap()))
    rounds = [int(x) for x in inputs["round_ids"]]
    price = read(twap.functions.calculate(day, rounds))
    token = read(settlement.functions.rewardToken())
    if token.lower() != inputs["reward_token"].lower():
        raise ValueError("REWARD_TOKEN_POLICY")
    sources = read(vault.functions.getFundedYieldSources(day))
    income = [
        ["0x" + s.hex(), str(read(vault.functions.sourceIncome(day, s)))]
        for s in sources
    ]
    wrap = read(vault.functions.dayWrapFeeIncome(day))
    unwrap = read(vault.functions.dayUnwrapFeeIncome(day))
    deploy = read(vault.functions.dayDeployFeeIncome(day))
    evidence = canonical(
        dict(
            version="1",
            chain_id=str(w3.eth.chain_id),
            block_number=str(height),
            block_hash=block.hash.hex(),
            settlement=settlement_address,
            reward_token=token,
            inputs=inputs,
            price8=str(price),
            income=income,
            operational_fee_income=str(wrap),
            unwrap_component=str(unwrap),
            deploy_fee_income=str(deploy),
        )
    ).encode()
    digest = hashlib.sha256(evidence).digest()
    report = [
        str(day),
        inputs["average_btc_sats"],
        inputs["stablecoin_tvl"],
        inputs["opening_reserve_sats"],
        str(wrap),
        str(deploy),
        "0x" + digest.hex(),
        income,
        inputs["providers"],
    ]
    # Reuse ABI coercion: integer money/day/weights stay decimal strings in the public artifact.
    from .bootstrap import encode_args

    fn = next(
        x for x in settlement.abi if x.get("name") == "getSettlementOperationHash"
    )
    args = encode_args(fn["inputs"], [report, inputs["round_ids"]])
    op = read(settlement.functions.getSettlementOperationHash(*args))
    if w3.eth.get_block(height).hash != block.hash:
        raise ValueError("REPORT_REORG")
    intent = dict(
        chain_id=str(w3.eth.chain_id),
        target=settlement_address,
        artifact="CustodianDAODailySettlement",
        hash_method="getSettlementOperationHash",
        hash_args=[report, inputs["round_ids"]],
        operation_hash=op.hex(),
    )
    return evidence, intent


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("--rpc", required=True)
    p.add_argument("--settlement", required=True)
    p.add_argument("--in-kind", action="store_true", help="Use a new confederation DAO")
    p.add_argument("--stage-expenses", action="store_true", help="Prepare one chronological expense batch for a V3 DAO day")
    p.add_argument("--confirmations", type=int, default=12)
    p.add_argument("--inputs", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--intent", required=True)
    a = p.parse_args(argv)
    if a.stage_expenses and not a.in_kind:
        p.error("--stage-expenses requires --in-kind")
    w3 = evm_web3(a.rpc)
    address = Web3.to_checksum_address(a.settlement)
    inputs = json.loads(Path(a.inputs).read_text())
    if a.in_kind:
        from .confederation_dao_report import prepare as prepare_in_kind
        from .evm_network import read_block

        evidence, intent = prepare_in_kind(w3, address, inputs, read_block(w3, a.confirmations),
                                           stage=a.stage_expenses)
    else:
        evidence, intent = prepare(w3, address, inputs)
    for path, data in [
        (a.evidence, evidence),
        (a.intent, (json.dumps(intent, indent=2) + "\n").encode()),
    ]:
        with Path(path).open("xb") as f:
            f.write(data)
    print(
        json.dumps(
            {
                "status": "PREPARED",
                "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
                "operation_hash": intent["operation_hash"],
            }
        )
    )
    return 0
