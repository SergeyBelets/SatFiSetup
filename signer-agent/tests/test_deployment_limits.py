import pytest
from web3 import Web3
from signer_agent.deployment_limits import check_deployment, MAX_RUNTIME, MAX_INITCODE
from signer_agent.deployment_limits import check_portable_metadata


@pytest.mark.parametrize(
    "mapping",
    [
        "/Users/alice/project/lib/:forge-std/=lib/forge-std/src/",
        "forge-std/=/home/bob/project/lib/forge-std/src/",
        "forge-std/=C:\\project\\lib\\forge-std\\src\\",
    ],
)
def test_absolute_remappings_break_cross_host_discovery(mapping):
    with pytest.raises(ValueError, match="NONPORTABLE_BUILD_METADATA"):
        check_portable_metadata({"settings": {"remappings": [mapping]}})


def test_relative_remappings_are_portable():
    check_portable_metadata(
        {
            "settings": {"remappings": ["forge-std/=lib/forge-std/src/"]},
            "sources": {"contracts/src/DKGOnchainBoard.sol": {}},
        }
    )


def test_eip_boundaries_and_unlinked_bytecode():
    check_deployment(bytes(MAX_RUNTIME), bytes(MAX_INITCODE))
    with pytest.raises(ValueError, match="EIP170"):
        check_deployment(bytes(MAX_RUNTIME + 1), b"\x00")
    with pytest.raises(ValueError, match="EIP3860"):
        check_deployment(b"\x00", bytes(MAX_INITCODE + 1))
    with pytest.raises(ValueError, match="UNLINKED"):
        check_deployment("0x__$123$__", "0x00")


def test_constructor_arguments_count_toward_initcode_limit():
    # The creation object alone fits. ABI-encoded dynamic arguments push it over.
    creation = "0x" + "00" * (MAX_INITCODE - 64)
    constructor = (
        Web3()
        .eth.contract(
            abi=[dict(type="constructor", inputs=[dict(name="data", type="bytes")])],
            bytecode=creation,
        )
        .constructor(b"x")
    )
    check_deployment(b"\x00", creation)
    with pytest.raises(ValueError, match="EIP3860"):
        check_deployment(b"\x00", constructor.data_in_transaction)
