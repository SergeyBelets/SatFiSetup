"""Enrolled recovery uses the ordinary cold-start entry points."""

import json

from eth_keys import keys
from web3 import Web3

from test_bitcoin_onchain import RPC
from test_full_bootstrap import exercise_bootstrap


def test_bootstrap_enrolls_cltv_wallet_and_pins_recovery_factory(tmp_path, record_property):
    def public_x(number):
        return keys.PrivateKey(number.to_bytes(32, "big")).public_key.to_bytes()[:32].hex()

    terms = dict(height="5000", threshold="2", keys=sorted(public_x(n) for n in (21, 22, 23)),
                 successor_script="5120" + public_x(24), sponsor_script="5120" + public_x(25),
                 confirmations="12", max_fee_sats="3000",
                 pause_guardian=keys.PrivateKey((1).to_bytes(32, "big")).public_key.to_checksum_address())
    custody = dict(version="1", mode="custodians", threshold="2", sources=[])
    v3 = dict(version="3", mode="create", bitcoin_backend="relay",
              candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="1000")
    env = exercise_bootstrap(tmp_path, record_property, custody=custody, in_kind=v3, recovery=terms)
    bootstrap = env["bootstrap"]
    reserve, escrow = bootstrap.contract("mining"), bootstrap.contract("escrow")
    factory, module = bootstrap.contract("withdrawal_recovery_factory"), bootstrap.contract("withdrawal_recovery")
    assert escrow.functions.recoveryFactory().call() == factory.address
    assert factory.functions.deployed(module.address).call()
    assert escrow.functions.recovery().call() == module.address
    assert module.functions.reserve().call() == reserve.address

    exported = tmp_path / "wallet-public.json"
    env["participant"]("btc_ceremony", 1, "export", "--format", "public", "--output", exported)
    descriptor = json.loads(exported.read_text())["recovery_descriptor"]
    source = bytes(reserve.functions.payoutScript().call())
    assert descriptor["script_pubkey"] == source.hex()
    assert Web3.keccak(source) == module.functions.sourceScriptHash().call()

    reviewed = env["participant"]("btc_ceremony", 1, "inspect-bitcoin-registry")
    assert reviewed["graph"]["bitcoin_recovery"] == terms
    joined = env["participant"]("btc_ceremony", 1, "trust-bitcoin-registry",
                               "--accept-fingerprint", reviewed["fingerprint"])
    assert joined["status"] == "BTC_REGISTRY_PINNED"

    inspected = env["command"]("bitcoin", "recovery", "inspect", "--rpc", RPC, "--board", env["board"])
    assert inspected["policy"] == terms and inspected["recovery"] == module.address and not inspected["paused"]
    paused = env["command"]("bitcoin", "recovery", "pause", "--rpc", RPC, "--board", env["board"],
        "--accept-fingerprint", inspected["fingerprint"], "--identity-file", env["identities"][0],
        "--state", tmp_path / "emergency.sqlite", "--send")
    assert paused["status"] == "CONFIRMED"
    assert module.functions.paused().call()
