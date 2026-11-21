"""TEST ONLY: historical Bitcoin Core adapter and fast local Ethereum slots.

No production module reads a simulation environment variable. The test launcher
explicitly replaces Core RPC and its observation clock inside this subprocess;
EVM contracts, finalized/depth policy, signatures and transactions remain real.
"""
import sys
from types import SimpleNamespace
from web3 import Web3
from test_network_matrix import HistoricalPublicCore
import signer_agent.bitcoin_network as bitcoin_network
from signer_agent.bitcoin_rpc import BitcoinRPC


def main():
    network = "mainnet"
    if len(sys.argv) > 2 and sys.argv[1] == "--fixture-bitcoin-network":
        network = sys.argv[2]
        del sys.argv[1:3]
    assert network in ("mainnet", "testnet4")
    core = HistoricalPublicCore(network)
    bitcoin_network.time = SimpleNamespace(time=lambda: core.record(core.height)["time"] + 1)
    BitcoinRPC.call = lambda self, method, *args: core.call(method, *args)
    original = Web3.HTTPProvider.make_request

    def mine_after_send(self, method, params):
        result = original(self, method, params)
        if "error" in result:
            print("TEST_RPC_ERROR", method, result["error"], file=sys.stderr)
        if method == "eth_sendRawTransaction" and "result" in result:
            chain_id = int(original(self, "eth_chainId", [])["result"], 16)
            assert chain_id in (1, 11155111)
            # Mine actual blocks on the isolated Anvil; production finality
            # functions are unmodified. Consensus itself is not simulated here.
            mined = original(self, "anvil_mine", ["0x80" if chain_id == 1 else "0x1", "0x0"])
            assert "error" not in mined
        return result

    Web3.HTTPProvider.make_request = mine_after_send
    from signer_agent.cli import main as cli
    sys.exit(cli())


if __name__ == "__main__":
    main()
