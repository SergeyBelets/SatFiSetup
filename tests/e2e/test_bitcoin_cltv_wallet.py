"""Opt-in CLTV wallet, ordinary FROST transaction and separate cold PSBT signing on Core."""

import base64
from contextlib import ExitStack, contextmanager
from pathlib import Path
import uuid

from coincurve import PrivateKey
import pytest

from signer_agent import crypto
from signer_agent.bitcoin_rpc import BitcoinRPC, satoshis
from test_bitcoin_processes import BTC_RPC, COOKIE


@contextmanager
def wallet(node):
    name = "recovery-" + uuid.uuid4().hex
    node.call("createwallet", name)
    try:
        yield BitcoinRPC(BTC_RPC + "/wallet/" + name, COOKIE, network="regtest")
    finally:
        node.call("unloadwallet", name)


def dkg(policy=None):
    participants = (1, 2, 3)
    first = {i: crypto.call("part1", index=i, n=3, t=2) for i in participants}

    def rounds(i):
        return {str(peer): first[peer]["package"] for peer in participants if peer != i}

    second = {i: crypto.call("part2", secret=first[i]["secret"], round1=rounds(i)) for i in participants}
    extra = {"recovery_policy": policy} if policy is not None else {}
    return {i: crypto.call(
        "part3", secret=second[i]["secret"], round1=rounds(i),
        round2={str(peer): second[peer]["packages"][f"{i:064x}"] for peer in participants if peer != i},
        **extra,
    ) for i in participants}


def spend_normally(package, results):
    built = crypto.call("bitcoin_build", **package)
    indexes = (1, 2)
    nonces = {i: crypto.call("nonce", share=results[i]["signing_share"]) for i in indexes}
    commitments = {str(i): nonces[i]["commitments"] for i in indexes}
    message = built["sighashes"][0]
    shares = {str(i): crypto.call("sign", key_package=results[i]["key_package"], nonces=nonces[i]["nonces"],
                                  commitments=commitments, message=message)["share"] for i in indexes}
    signature = crypto.call("aggregate", public_package=results[1]["public_package"],
                            commitments=commitments, shares=shares, message=message)
    return crypto.call("bitcoin_attach", unsigned=built["unsigned"], package=package,
                       signatures=[signature["signature"]])


def wif(secret):
    """Regtest test vectors only."""
    from hashlib import sha256

    data = b"\xef" + secret + b"\x01"
    encoded = data + sha256(sha256(data).digest()).digest()[:4]
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    result = ""
    value = int.from_bytes(encoded, "big")
    while value:
        value, digit = divmod(value, 58)
        result = alphabet[digit] + result
    return result


def test_cltv_reload_preserves_principal_without_hot_shares():
    if not Path(COOKIE).is_file():
        pytest.skip("isolated Bitcoin Core regtest required")
    node = BitcoinRPC(BTC_RPC, COOKIE, network="regtest")
    node.check_network()

    # Separate stores hold one cold key each. No cold signing via the FROST key.
    secrets = [PrivateKey() for _ in range(3)]
    secret_for = {key.public_key_xonly.format().hex(): key.secret for key in secrets}
    sorted_keys = sorted(secret_for)
    with ExitStack() as stores:
        payer = stores.enter_context(wallet(node))
        sponsor_wallet = stores.enter_context(wallet(node))
        cold_wallets = [stores.enter_context(wallet(node)) for _ in sorted_keys]
        funding = payer.call("getnewaddress")
        node.call("generatetoaddress", 101, funding)
        height = node.call("getblockcount") + 6
        policy = dict(height=str(height), threshold="2", keys=sorted_keys)
        results = dkg(policy)
        descriptor = results[1]["recovery_descriptor"]
        assert all(record["group"] == "02" + descriptor["output_key"] and
                   record["recovery_descriptor"] == descriptor for record in results.values())
        assert all(descriptor["internal_key"] != record["raw_dkg_group"][2:] for record in results.values())

        core_descriptor = node.call("getdescriptorinfo", descriptor["descriptor"])["descriptor"]
        recovery_address = node.call("deriveaddresses", core_descriptor)[0]
        assert node.call("validateaddress", recovery_address)["scriptPubKey"] == descriptor["script_pubkey"]
        for public_key, cold in zip(sorted_keys, cold_wallets):
            private_descriptor = descriptor["descriptor"].replace(public_key, wif(secret_for[public_key]))
            checksum = node.call("getdescriptorinfo", private_descriptor)["checksum"]
            result = cold.call("importdescriptors", [dict(desc=private_descriptor + "#" + checksum, timestamp="now")])
            assert result[0]["success"], result

        sponsor_address = sponsor_wallet.call("getnewaddress", "", "bech32m")
        successor_dkg = dkg()
        successor_script = "5120" + successor_dkg[1]["group"][2:]
        assert successor_script != descriptor["script_pubkey"] and all(
            "5120" + record["group"][2:] == successor_script for record in successor_dkg.values())
        payout_address = payer.call("getnewaddress", "", "bech32m")
        payout_script = node.call("validateaddress", payout_address)["scriptPubKey"]
        sponsor_script = node.call("validateaddress", sponsor_address)["scriptPubKey"]

        def fund(address, amount):
            # A fresh regtest has no fee-estimation history.
            return payer.call("sendtoaddress", address, amount, "", "", False, True, None, "unset", None, "2")

        active_deposits = [fund(recovery_address, "0.01000000") for _ in range(3)]
        sponsor_deposit = fund(sponsor_address, "0.00030000")
        node.call("generatetoaddress", 1, funding)

        def output(txid, script):
            tx = node.call("getrawtransaction", txid, True)
            matches = [item for item in tx["vout"] if item["scriptPubKey"]["hex"] == script]
            assert len(matches) == 1
            item = matches[0]
            return dict(outpoint=f"{txid}:{item['n']}", script=script, amount=str(satoshis(item["value"])))

        old_outputs = [output(txid, descriptor["script_pubkey"]) for txid in active_deposits]
        sponsor_outpoint = output(sponsor_deposit, sponsor_script)
        regular = spend_normally(dict(inputs=old_outputs[:1], wallet_script=descriptor["script_pubkey"],
                                       outputs=[dict(script=payout_script, amount="999000")], fee="1000",
                                       max_fee="1000"), results)
        assert node.call("sendrawtransaction", regular["signed"]) == regular["txid"]
        node.call("generatetoaddress", 1, funding)
        assert node.call("getrawtransaction", regular["txid"], True)["confirmations"] == 1
        del results

        request = dict(policy=policy, internal_key=descriptor["internal_key"], inputs=old_outputs[1:],
                       sponsor=sponsor_outpoint, successor=successor_script, fee="2200")
        plan = crypto.call("bitcoin_recovery_plan_v1", **request)
        assert plan["principal_sats"] == "2000000"
        psbt = base64.b64encode(bytes.fromhex(plan["psbt"])).decode("ascii")

        def sign_part(wallet_rpc):
            result = wallet_rpc.call("walletprocesspsbt", psbt, True, "DEFAULT", True, False)
            return base64.b64decode(result["psbt"]).hex()

        cold_one, cold_two, sponsor = [sign_part(party) for party in (cold_wallets[0], cold_wallets[1], sponsor_wallet)]
        with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
            crypto.call("bitcoin_recovery_attach_v1", **request, partials=[cold_one, sponsor])
        recovered = crypto.call("bitcoin_recovery_attach_v1", **request, partials=[cold_one, cold_two, sponsor])
        assert recovered["txid"] == plan["txid"]
        assert crypto.call("bitcoin_recovery_verify_v1", **request, signed=recovered["signed"])["txid"] == plan["txid"]
        with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
            crypto.call("bitcoin_recovery_verify_v1", **dict(request, fee="2201"), signed=recovered["signed"])

        before = node.call("testmempoolaccept", [recovered["signed"]])[0]
        assert before["allowed"] is False and before["reject-reason"] == "non-final"
        node.call("generatetoaddress", height - node.call("getblockcount"), funding)
        after = node.call("testmempoolaccept", [recovered["signed"]])[0]
        assert after["allowed"] is True and satoshis(after["fees"]["base"]) == 2200
        assert node.call("sendrawtransaction", recovered["signed"]) == recovered["txid"]
        node.call("generatetoaddress", 1, funding)
        assert node.call("getrawtransaction", recovered["txid"], True)["confirmations"] == 1
        migrated = output(recovered["txid"], successor_script)
        assert migrated["amount"] == "2000000"
        assert output(recovered["txid"], sponsor_script)["amount"] == "27800"

        resumed = spend_normally(dict(inputs=[migrated], wallet_script=successor_script,
            outputs=[dict(script=payout_script, amount="999000"),
                     dict(script=successor_script, amount="1000000")], fee="1000", max_fee="1000"), successor_dkg)
        assert node.call("sendrawtransaction", resumed["signed"]) == resumed["txid"]
        node.call("generatetoaddress", 1, funding)
        assert node.call("gettxout", recovered["txid"], 0) is None
        assert output(resumed["txid"], payout_script)["amount"] == "999000"
        assert output(resumed["txid"], successor_script)["amount"] == "1000000"
