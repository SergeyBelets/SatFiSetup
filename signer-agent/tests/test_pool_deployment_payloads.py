"""Complete constructor payload gates for the built value-flow and script graph.

Run after ``forge build``. Boundaries use actual compiled creation objects and
the production deployment preflight, with jointly padded dynamic arguments.
"""

import json
from pathlib import Path

import pytest
from web3 import Web3

from signer_agent.deployment_limits import MAX_INITCODE, check_deployment


OUT = Path(__file__).resolve().parents[2] / "web3" / "contracts" / "out"
ADDRESS = Web3.to_checksum_address("0x" + "12" * 20)


def artifact(name):
    path = OUT / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        pytest.skip(f"Build production artifacts first with forge build: {path}")
    return json.loads(path.read_text())


def constructor(name, args):
    built = artifact(name)
    contract = Web3().eth.contract(abi=built["abi"], bytecode=built["bytecode"]["object"])
    return built["deployedBytecode"]["object"], contract.constructor(*args).data_in_transaction


def byte_size(encoded):
    return len(bytes.fromhex(encoded.removeprefix("0x")))


@pytest.mark.parametrize("name,addresses", [("SimpleMultiPool", 2), ("ConfederationMultiPool", 3)])
def test_maximum_pool_id_and_next_word_use_real_preflight(name, addresses):
    _, empty = constructor(name, ["", *[ADDRESS] * addresses])
    length = (MAX_INITCODE - byte_size(empty)) // 32 * 32
    runtime, supported = constructor(name, ["i" * length, *[ADDRESS] * addresses])
    accepted = check_deployment(runtime, supported)
    assert 0 <= accepted["initcode_margin"] < 32
    _, oversized = constructor(name, ["i" * (length + 32), *[ADDRESS] * addresses])
    assert byte_size(oversized) == byte_size(supported) + 32
    with pytest.raises(ValueError, match="EIP3860_INITCODE_TOO_LARGE"):
        check_deployment(runtime, oversized)


@pytest.mark.parametrize("name", ["MultiPoolShareToken", "ConfederationToken", "CustodianDAOToken"])
def test_maximum_joint_name_symbol_budget_and_next_word(name):
    _, empty = constructor(name, ["", "", ADDRESS])
    budget = (MAX_INITCODE - byte_size(empty)) // 32 * 32
    name_length = budget // 64 * 32
    symbol_length = budget - name_length
    runtime, supported = constructor(name, ["N" * name_length, "S" * symbol_length, ADDRESS])
    accepted = check_deployment(runtime, supported)
    assert 0 <= accepted["initcode_margin"] < 32
    _, oversized = constructor(name, ["N" * name_length, "S" * (symbol_length + 32), ADDRESS])
    with pytest.raises(ValueError, match="EIP3860_INITCODE_TOO_LARGE"):
        check_deployment(runtime, oversized)


def test_maximum_two_wallet_joint_label_budget_and_next_word():
    # DeployFederation supports at most two configured project wallets. The
    # registry's arrays and both dynamic labels must share one initcode budget.
    name = "GlobalProjectWalletRegistry"
    wallets = [ADDRESS, Web3.to_checksum_address("0x" + "34" * 20)]
    shares = [4_000, 6_000]
    _, empty = constructor(name, [ADDRESS, wallets, shares, ["", ""]])
    budget = (MAX_INITCODE - byte_size(empty)) // 32 * 32
    first_length = budget // 64 * 32
    labels = ["A" * first_length, "B" * (budget - first_length)]
    assert len(labels) == len(wallets) == len(shares) == 2
    assert all(labels)
    runtime, supported = constructor(name, [ADDRESS, wallets, shares, labels])
    assert byte_size(supported) == byte_size(empty) + budget
    accepted = check_deployment(runtime, supported)
    assert 0 <= accepted["initcode_margin"] < 32
    _, oversized = constructor(name, [ADDRESS, wallets, shares, [labels[0], labels[1] + "B" * 32]])
    assert byte_size(oversized) == byte_size(supported) + 32
    with pytest.raises(ValueError, match="EIP3860_INITCODE_TOO_LARGE"):
        check_deployment(runtime, oversized)


@pytest.mark.parametrize(
    "name,args,argument_bytes",
    [
        ("BitcoinWithdrawalEscrow", [ADDRESS, ADDRESS, 6, 294, 330], 160),
        ("CustodianDAODailySettlement", [ADDRESS] * 7, 224),
        ("SimpleMultiPoolDeployer", [ADDRESS, ADDRESS], 64),
        ("MultiPoolDeployer", [ADDRESS], 32),
    ],
)
def test_static_value_flow_constructors_include_every_argument(name, args, argument_bytes):
    built = artifact(name)
    runtime, initcode = constructor(name, args)
    assert byte_size(initcode) == byte_size(built["bytecode"]["object"]) + argument_bytes
    check_deployment(runtime, initcode)
