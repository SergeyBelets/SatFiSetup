"""Exercise the checked recipient-funded fee package on a disposable Bitcoin Core node."""

from decimal import Decimal
import json
from pathlib import Path
import uuid

import pytest

from signer_agent.bitcoin_cli import main
from signer_agent.bitcoin_rpc import BitcoinRPC, payment_in_mempool
from test_bitcoin_processes import BTC_RPC, COOKIE


@pytest.mark.parametrize("already_in_mempool", [False, True])
def test_recipient_child_pays_only_from_bound_payout(tmp_path, capsys, already_in_mempool):
    if not Path(COOKIE).is_file():
        pytest.skip("isolated Bitcoin Core regtest required")
    node = BitcoinRPC(BTC_RPC, COOKIE, network="regtest")
    node.check_network()
    name = "payout-package-" + uuid.uuid4().hex
    node.call("createwallet", name)
    try:
        wallet = BitcoinRPC(BTC_RPC + "/wallet/" + name, COOKIE, network="regtest")
        funding = wallet.call("getnewaddress", "", "bech32")
        recipient = wallet.call("getnewaddress", "", "bech32")
        destination = wallet.call("getnewaddress", "", "bech32")
        node.call("generatetoaddress", 101, funding)
        utxo = wallet.call("listunspent")[0]
        payout = Decimal("0.01000000")
        change = utxo["amount"] - payout - Decimal("0.00001000")
        source = {"txid": utxo["txid"], "vout": utxo["vout"]}
        parent_unsigned = wallet.call("createrawtransaction", [source], {recipient: str(payout), funding: str(change)})
        parent_signed = wallet.call("signrawtransactionwithwallet", parent_unsigned)
        assert parent_signed["complete"]
        parent_raw = parent_signed["hex"]
        parent = node.call("decoderawtransaction", parent_raw)
        payout_script = parent["vout"][0]["scriptPubKey"]["hex"]
        child_source = {"txid": parent["txid"], "vout": 0}
        child_unsigned = wallet.call("createrawtransaction", [child_source], {destination: str(payout - Decimal("0.00002000"))})
        prevout = dict(child_source, scriptPubKey=payout_script, amount=str(payout))
        child_signed = wallet.call("signrawtransactionwithwallet", child_unsigned, [prevout])
        assert child_signed["complete"]
        child_raw = child_signed["hex"]
        child = node.call("decoderawtransaction", child_raw)
        return_script = child["vout"][0]["scriptPubKey"]["hex"]

        parent_path = tmp_path / "parent.json"
        parent_path.write_text(json.dumps(dict(signed=parent_raw, txid=parent["txid"], wtxid=parent["hash"])))
        child_path = tmp_path / "child.hex"
        child_path.write_text(child_raw)
        args = [
            "cpfp-payout", "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE,
            "--bitcoin-network", "regtest", "--parent-raw", str(parent_path),
            "--parent-txid", parent["txid"], "--payout-vout", "0", "--payout-script", payout_script,
            "--child-raw", str(child_path), "--child-txid", child["txid"], "--return-script", return_script,
        ]
        if already_in_mempool:
            assert node.call("sendrawtransaction", parent_raw) == parent["txid"]
        wrong_payout = args[:]
        wrong_payout[wrong_payout.index("--payout-script") + 1] = return_script
        with pytest.raises(ValueError, match="^BTC_CPFP_PAYOUT_BINDING$"):
            main(wrong_payout + ["--max-child-fee-sats", "2000", "--submit"])
        with pytest.raises(ValueError, match="^BTC_CPFP_FEE_LIMIT$"):
            main(args + ["--max-child-fee-sats", "1999", "--submit"])
        reviewed = args + ["--max-child-fee-sats", "2000"]
        assert main(reviewed) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "PACKAGE_ACCEPTABLE"
        assert payment_in_mempool(node, parent["txid"]) == already_in_mempool
        assert not payment_in_mempool(node, child["txid"])

        assert main(reviewed + ["--submit"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert (result["status"], result["child_fee_sats"]) == ("PACKAGE_IN_MEMPOOL", "2000")
        assert payment_in_mempool(node, parent["txid"])
        assert payment_in_mempool(node, child["txid"])
        assert main(reviewed + ["--submit"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "PACKAGE_IN_MEMPOOL"
        node.call("generatetoaddress", 1, funding)
        assert node.call("getrawtransaction", parent["txid"], True)["confirmations"] == 1
        assert node.call("getrawtransaction", child["txid"], True)["confirmations"] == 1
        assert main(reviewed + ["--submit"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "PAYOUT_CONFIRMED"
    finally:
        node.call("unloadwallet", name)
