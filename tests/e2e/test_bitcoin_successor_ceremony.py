"""Create a separately enrolled successor BTC DKG through real local CLI entry points."""

import json
from secrets import token_hex
import subprocess
import sys

from eth_account import Account
from web3 import Web3

from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_tokens import resolve as token_metadata
from signer_agent.onboarding import assemble, fingerprint, join
from test_dkg_processes import RPC


def create_successor(tmp_path, participant_keys=(51, 52, 53)):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    addresses, identities = [], []
    for i in participant_keys:
        address = Account.from_key(i.to_bytes(32, "big")).address
        assert "error" not in w3.provider.make_request("anvil_setBalance", [address, hex(10**21)])
        identity = tmp_path / f"identity-{i}.json"
        identity.write_text(json.dumps({"private_key": f"{i:064x}"}))
        identity.chmod(0o600)
        addresses.append(address)
        identities.append(identity)

    deploy = Bootstrap(dict(deployment_id="successor-fixture", chain_id="31337", evm_rpc=RPC,
                            bitcoin_network="regtest", mode="integration",
                            steps=[dict(id="registry", kind="deploy", artifact="DKGCoordinator", args=[])]),
                       tmp_path / "registry.sqlite")
    assert deploy.apply_confirmed(identities[0])["status"] == "VERIFIED"
    dkg = deploy.contract("registry")

    def command(group, action, *args, fail=False):
        proc = subprocess.run([sys.executable, "-m", "signer_agent.cli", group, action, *map(str, args)],
                              text=True, capture_output=True, timeout=90)
        result = json.loads(proc.stdout)
        if fail:
            assert proc.returncode
        else:
            assert proc.returncode == 0, (result, proc.stderr)
        return result

    custody = tmp_path / "custody.json"
    custody.write_text(json.dumps(dict(version="1", mode="custodians", threshold="2", sources=[])))
    invitation = tmp_path / "successor.json"
    command("setup", "invite", "--participants", *addresses, "--threshold", "3", "--min-custodians", "2",
            "--min-p2wpkh-sats", "294", "--min-p2tr-sats", "330", "--treasury", addresses[-1],
            "--bitcoin-confirmations", "6", "--wrap-fee-bps", "17", "--unwrap-fee-bps", "23",
            "--wrap-staker-bps", "0", "--deploy-staker-bps", "0", "--converter-numerator", "3",
            "--converter-denominator", "9999", "--converter-funding", "7654321", "--dao-token-recipient",
            addresses[1], "--dao-use-reserve", "false", "--integration-fee-exemption", "true",
            "--bitcoin-custody-policy", custody, "--deployment-id", "successor-fixture", "--output", invitation)
    pub = command("setup", "publish", "--rpc", RPC, "--invite", invitation, "--identity-file", identities[0],
                  "--state", tmp_path / "publish.sqlite")
    base = ("--board", pub["board"], "--rpc", RPC)
    directories = [tmp_path / f"signer-{i}" for i in range(3)]
    for identity, directory in zip(identities, directories):
        command("setup", "join-chain", *base, "--accept-fingerprint", pub["fingerprint"],
                "--identity-file", identity, "--directory", directory)

    missing = command("dkg", "successor-create", *base, "--registry", dkg.address)
    assert not missing["bound"] and not missing["created"] and missing["completed"] is None
    submit = (*base, "--registry", dkg.address, "--identity-file", identities[0], "--state", tmp_path / "dkg.sqlite",
              "--send", "--accept-fingerprint")
    refused = command("dkg", "successor-create", *submit, "0" * 64, fail=True)
    assert "BTC_SUCCESSOR_CEREMONY_FINGERPRINT" in json.dumps(refused)
    ready = command("dkg", "successor-create", *submit, missing["fingerprint"])
    assert ready["status"] == "BTC_SUCCESSOR_CEREMONY_READY"

    def member(idx, action, *options):
        directory = directories[idx]
        return command("dkg", action, *base, "--role", "btc_ceremony",
                       "--identity-file", identities[idx], "--state", directory / "btc_ceremony.enc",
                       "--password-file", directory / "password", *options)

    for _ in range(8):
        states = [member(i, "run-once")["status"] for i in (1, 2)]
        if states == ["ACTIVE", "ACTIVE"]:
            break
    assert states == ["ACTIVE", "ACTIVE"]
    public = tmp_path / "wallet.json"
    member(1, "export", "--format", "public", "--output", public)
    public = json.loads(public.read_text())
    complete = command("dkg", "successor-create", *base, "--registry", dkg.address)
    assert complete["completed"] == dict(successor_script="5120" + public["group"][2:],
        successor_dkg=dict(registry=dkg.address, wallet_id=public["manifest"]["session_id"]),
        result_hash=public["result_hash"])
    assert complete["fingerprint"] == missing["fingerprint"]
    before = w3.eth.get_transaction_count(addresses[0])
    retried = command("dkg", "successor-create", *submit, complete["fingerprint"])
    assert retried["completed"] == complete["completed"]
    assert w3.eth.get_transaction_count(addresses[0]) == before
    return complete["completed"], member, dkg, invitation, addresses, identities


def test_successor_ceremony_publishes_completed_wallet_terms(tmp_path):
    complete, _, dkg, invitation, addresses, identities = create_successor(tmp_path)

    # A fresh primary enrollment binds the already completed successor and
    # uses the same verified registry rather than starting a fresh registry.
    from eth_keys import keys

    def public_x(index):
        return keys.PrivateKey(index.to_bytes(32, "big")).public_key.to_bytes()[:32].hex()

    staged = json.loads(invitation.read_text())
    in_kind = dict(version="3", mode="create", bitcoin_backend="relay",
                   candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="1000")
    primary = dict(staged, deployment_id="primary-with-successor",
                   sessions={role: token_hex(32) for role in staged["sessions"]},
                   ulr=in_kind, token_metadata=token_metadata({"ulr": in_kind}),
                   bitcoin_recovery=dict(height="5000", threshold="2", keys=sorted(public_x(i) for i in (56, 57, 58)),
                       confirmations="12", max_fee_sats="3000", pause_guardian=addresses[0],
                       sponsor_script="5120" + public_x(59), successor_script=complete["successor_script"],
                       successor_dkg=complete["successor_dkg"]))
    enrollments = [join(primary, fingerprint(primary), identity, tmp_path / f"primary-state-{i}")
                   for i, identity in enumerate(identities)]
    launch = build(assemble(primary, enrollments, RPC))
    assert launch["contracts"]["dkg"]["address"] == dkg.address
    assert not any(step["id"] == "dkg" for step in launch["steps"])
    Bootstrap(launch, tmp_path / "primary.sqlite").check_external_components()
