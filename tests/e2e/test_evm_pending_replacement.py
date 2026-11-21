"""A fee replacement submitted through the SDK uses the original Anvil nonce."""

import json

from eth_account import Account
from web3 import Web3

from signer_agent import cli
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import save_signed
from signer_agent.bootstrap_recovery import identity_lane
from signer_agent.bootstrap_replacement import SUPERSEDED
from test_dkg_processes import RPC, deploy


def test_replace_pending_preserves_call_and_executes_once(tmp_path, capsys):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337, "requires an isolated Anvil test chain"
    token = deploy(w3, "DAOTestUSD")
    sender = Account.create()
    recipient = Account.create().address
    assert "error" not in w3.provider.make_request("anvil_setBalance", [sender.address, hex(10**20)])
    identity = tmp_path / "sender.json"
    identity.write_text(json.dumps(dict(private_key=sender.key.hex())))
    identity.chmod(0o600)
    _, _, lane = identity_lane(identity)

    step = dict(id="mint", kind="call", contract="token", method="mint", args=[recipient, "10"])
    profile = dict(deployment_id="test-fee-replacement", chain_id="31337", evm_rpc=RPC, mode="integration",
        bitcoin_network="regtest", steps=[step], contracts=dict(token=dict(address=token.address,
        artifact="DAOTestUSD", runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(token.address))))))
    bootstrap = Bootstrap(profile, tmp_path / "bootstrap.sqlite")
    key = profile["deployment_id"] + ":mint"
    bootstrap.journal.put(key, step)
    nonce = w3.eth.get_transaction_count(sender.address)
    tip = 1_000_000_000
    cap = max(w3.eth.gas_price * 2, tip * 2)
    gas = 100_000
    access_list = [{"address": token.address, "storageKeys": []}]
    original = sender.sign_transaction(token.functions.mint(recipient, 10).build_transaction({
        "type": 2, "from": sender.address, "chainId": 31337, "nonce": nonce, "gas": gas,
        "maxFeePerGas": cap, "maxPriorityFeePerGas": tip, "accessList": access_list,
    }))
    saved = dict(tx_hash=original.hash.hex(), raw_transaction=original.raw_transaction.hex(), nonce=str(nonce))
    save_signed(lane, bootstrap, key, step, saved, allow_fee_replacement=True)
    argv = ["bootstrap", "replace-pending", "--tx-hash", saved["tx_hash"], "--rpc", RPC,
            "--identity-file", str(identity), "--max-fee-per-gas-wei", str(cap * 2),
            "--max-priority-fee-per-gas-wei", str(tip * 2), "--max-cost-wei", str(gas * cap * 2)]

    assert "error" not in w3.provider.make_request("evm_setAutomine", [False])
    try:
        assert w3.eth.send_raw_transaction(original.raw_transaction) == original.hash
        assert cli.main(argv) == 0
        preview = json.loads(capsys.readouterr().out)
        assert preview["status"] == "EVM_REPLACEMENT_REVIEW"
        assert token.functions.balanceOf(recipient).call() == 0
        assert cli.main([*argv, "--confirm-replacement", preview["fingerprint"]]) == 0
        sent = json.loads(capsys.readouterr().out)
        assert sent["status"] == "EVM_REPLACEMENT_BROADCAST"
        bumped = w3.eth.get_transaction(sent["tx_hash"])
        assert bumped.nonce == nonce and bumped.to == token.address
        assert bumped.input == Web3.to_bytes(hexstr=token.encode_abi("mint", [recipient, 10]))
        assert bumped.accessList == access_list
        waiting = bootstrap.apply(identity)
        assert waiting["tx_hash"] == sent["tx_hash"] and waiting["original_tx_hash"] == original.hash.hex()
        assert "error" not in w3.provider.make_request("evm_mine", [])
    finally:
        assert "error" not in w3.provider.make_request("evm_setAutomine", [True])

    assert bootstrap.apply(identity)["status"] == "VERIFIED"
    assert bootstrap.apply(identity)["status"] == "VERIFIED"
    assert bootstrap.journal.get(key)["result"]["tx_hash"] == sent["tx_hash"]
    assert lane.get(original.hash.hex())["status"] == SUPERSEDED
    assert w3.eth.get_transaction_count(sender.address) == nonce + 1
    assert token.functions.balanceOf(recipient).call() == 10
