"""Real Anvil transactions with an explicitly simulated historical mainnet Core.

This proves V4 on-chain enrollment, constructor origins and bootstrap gates. It is
not a live Bitcoin mainnet payment. Mainnet PoW headers are authentic stored
fixtures; only the Python observation clock/Core RPC adapter is simulated here.
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3

from signer_agent.bootstrap import Bootstrap, artifact
from signer_agent.bitcoin_network import MAINNET_GENESIS, header_hash, relay_constructor_args
from signer_agent.contract_provenance import prove_creation
from signer_agent.dkg_protocol import PROTOCOL_ID
from signer_agent.dkg import Registry
from signer_agent.dkg_protocol import Manifest, PURPOSES
from signer_agent.onboarding import ROLES, chain_main, fingerprint, verify_profile
from signer_agent.onchain_transport import assembled_profile, invitation
from signer_agent.onchain_transport import transact, ceremony_bundle
from signer_agent.operations import Journal
from signer_agent.secure_state import SecureState
from test_dkg_processes import RPC, authorize_ceremony, deploy

ROOT = Path(__file__).resolve().parents[2]


class HistoricalFixtureCore:
    """Small deterministic Core-shaped adapter, never contacted by production code."""
    def __init__(self):
        folder = ROOT / "web3/test/fixtures/mainnet"
        self.headers = (folder / "headers-32246-36288.bin").read_bytes()
        self.records = (folder / "expected-32246-36288.bin").read_bytes()
        self.height = 32269
        self.network = "mainnet"
        self.policy = None
        self.calls = []
        self.by_hash = {self.record(height)["hash"]: height for height in range(32246, self.height + 1)}

    def raw(self, height):
        offset = (height - 32246) * 80
        return self.headers[offset:offset + 80]

    def record(self, height):
        raw = self.raw(height)
        offset = (height - 32246) * 68
        entry = self.records[offset:offset + 68]
        return dict(hash=header_hash(raw)[::-1].hex(), height=height,
                    confirmations=self.height - height + 1, time=int.from_bytes(raw[68:72], "little"),
                    bits=raw[72:76][::-1].hex(), chainwork=entry[32:64].hex())

    def call(self, method, *args):
        self.calls.append((method, args))
        tip = self.record(self.height)
        if method == "getblockchaininfo":
            return dict(chain="main", blocks=self.height, headers=self.height, initialblockdownload=False,
                        pruned=False, bestblockhash=tip["hash"], chainwork=tip["chainwork"])
        if method == "getblockhash":
            return MAINNET_GENESIS if args[0] == 0 else self.record(args[0])["hash"]
        if method == "getblockheader":
            height = self.by_hash[args[0]]
            return self.record(height) if args[1] else self.raw(height).hex()
        if method == "getindexinfo":
            return dict(txindex=dict(synced=True, best_block_height=self.height))
        raise AssertionError(method)

    def public_policy(self):
        return dict(version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
                    checkpoint_header=self.raw(32256).hex(), checkpoint_height="32256",
                    checkpoint_chainwork=self.record(32256)["chainwork"],
                    checkpoint_predecessors=[self.raw(i).hex() for i in range(32246, 32256)],
                    max_reorg_depth="12", max_tip_age_seconds="86400",
                    max_relay_lag_blocks="0", max_headers_per_sync="4")


def test_mainnet_v4_board_enrollment_origins_and_fail_closed_readiness(tmp_path, monkeypatch, capsys, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    core = HistoricalFixtureCore()
    policy = core.public_policy()
    # Simulate Core observation time only. Anvil keeps its real time, so its
    # independent SPV guard must still reject these historical headers.
    monkeypatch.setattr("signer_agent.bitcoin_network.time", SimpleNamespace(time=lambda: core.record(core.height)["time"] + 1))
    accounts = [Account.create() for _ in range(3)]
    identities = []
    for index, account in enumerate(accounts):
        w3.provider.make_request("anvil_setBalance", [account.address, hex(10**22)])
        path = tmp_path / f"identity-{index}.json"
        path.write_text(json.dumps(dict(private_key=account.key.hex())))
        path.chmod(0o600)
        identities.append(path)
    invite = dict(version="4", chain_id="31337", bitcoin_network="mainnet", mode="backed",
                  deployment_id="mainnet-simulated-onchain", operator=accounts[0].address,
                  treasury=accounts[0].address, approval_deadline="4102444800",
                  federation_id="mainnet-simulated-fed", confederation_id="mainnet-simulated-conf",
                  participants=[account.address for account in accounts], threshold="3", epoch="1",
                  sessions={role: Web3.keccak(text=str(tmp_path) + role).hex() for role in ROLES},
                  protocol_id=PROTOCOL_ID.hex(), phase_duration="3600",
                  min_p2wpkh_sats="294", min_p2tr_sats="330", min_custodians="2", bitcoin_confirmations="6",
                  wrap_fee_bps="100", unwrap_fee_bps="100", wrap_staker_bps="2500", deploy_staker_bps="5000",
                  converter_numerator="1", converter_denominator="100", converter_funding="1000",
                  dao_token_recipient=accounts[0].address, dao_use_reserve=False, integration_fee_exemption=True,
                  bitcoin_policy=policy, evm_confirmations="1",
                  reward_token=dict(address=w3.eth.accounts[7], runtime_hash="0x" + "ab" * 32, decimals="6"),
                  btc_usd_feed=dict(address=w3.eth.accounts[8], runtime_hash="0x" + "cd" * 32))
    invite_file = tmp_path / "operator-only-invite.json"
    invite_file.write_text(json.dumps(invite))
    chain_main(SimpleNamespace(action="publish", rpc=RPC, invite=invite_file, identity_file=identities[0],
                               state=tmp_path / "publish.sqlite"))
    published = json.loads(capsys.readouterr().out)
    board_address = published["board"]
    assert published["status"] == "VERIFIED"
    chain_main(SimpleNamespace(action="discover", rpc=RPC, operator=accounts[0].address, from_block=0))
    discovered = json.loads(capsys.readouterr().out)
    assert any(item["board"] == board_address and item["invitation"] == invite for item in discovered["invitations"])
    own_checks = []
    def own_core(args, found_invite):
        assert found_invite == invite
        assert args.bitcoin_rpc == "http://own-core.invalid"
        own_checks.append(str(args.identity_file))
        return core
    monkeypatch.setattr("signer_agent.onboarding.local_bitcoin", own_core)
    for index, identity in enumerate(identities):
        # Each friend supplies only their local identity/Core and the board found
        # from a known EVM address. They never receive the operator's JSON file.
        chain_main(SimpleNamespace(action="join-chain", rpc=RPC, board=board_address,
                    accept_fingerprint=fingerprint(invite), identity_file=identity,
                    directory=tmp_path / f"friend-{index}", bitcoin_rpc="http://own-core.invalid",
                    bitcoin_cookie="local-only-cookie"))
        assert json.loads(capsys.readouterr().out)["status"] == "ENROLLED_ONCHAIN"
    assert len(own_checks) == 3
    profile = assembled_profile(w3, board_address)
    assert verify_profile(profile) == invite
    assert profile["bitcoin_policy"] == policy
    _, fetched = invitation(w3, board_address)
    assert fetched == invite

    # The first BTC DKG is real Rust cryptography over the real Anvil mailbox.
    # Only the join-time Core check above is simulated; participant processes
    # read their V4 bundles and every HPKE envelope from the EVM board.
    dkg = deploy(w3, "DKGCoordinator")
    board, _ = invitation(w3, board_address)
    transact(w3, identities[0], tmp_path / "bind-registry.sqlite", board, "bindRegistry", [dkg.address])
    registry = Registry(RPC, dkg.address, accounts[0].key.hex())
    registry.identity_file = identities[0]
    registry.transaction_journal = str(tmp_path / "create-ceremony.sqlite")
    registry.bitcoin_graph = dict(version="2", bitcoin_network="mainnet", bitcoin_policy=policy, evm_confirmations="1")
    ceremony = profile["btc_ceremony"]
    session = bytes.fromhex(ceremony["id"].removeprefix("0x"))
    authorize_ceremony(w3, dkg, session, accounts[0].address)
    registry.send("createCeremony", session, ceremony["participants"], int(ceremony["threshold"]),
                  [bytes.fromhex(item.removeprefix("0x")) for item in ceremony["transport_keys"]],
                  int(ceremony["epoch"]), PURPOSES["BTC_SPEND"], PROTOCOL_ID)
    generated = [row["request"]["profile"] for row in Journal(registry.transaction_journal).list()
                 if "profile" in row["request"]]
    assert generated and all(item["mode"] == "backed" and item["bitcoin_network"] == "mainnet" for item in generated)
    command_arguments, dkg_output = [], []
    executable = str(Path(sys.executable).with_name("frost-signer"))
    def participant(index, action):
        directory = tmp_path / f"friend-{index}"
        arguments = ["dkg", action, "--board", board_address, "--role", "btc_ceremony", "--rpc", RPC,
                     "--identity-file", str(identities[index]), "--state", str(directory / "btc_ceremony.enc"),
                     "--password-file", str(directory / "password")]
        command_arguments.append(arguments)
        process = subprocess.run([executable, *arguments], cwd=directory, capture_output=True,
                                 text=True, timeout=180)
        dkg_output.extend((process.stdout, process.stderr))
        assert process.returncode == 0, process.stdout + process.stderr
        return json.loads(process.stdout)
    for _ in range(8):
        statuses = [participant(index, "run-once")["status"] for index in range(3)]
        if statuses == ["ACTIVE"] * 3:
            break
    assert statuses == ["ACTIVE"] * 3
    manifest = Manifest.from_dict(ceremony_bundle(w3, board_address, "btc_ceremony"))
    completed = dkg.functions.getCompletedResult(session).call()
    assert bytes(completed[3]) == PURPOSES["BTC_SPEND"]
    public_corpus = list(dkg_output)
    private_values = []
    def secrets(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name in {"secret", "signing_share", "key_package", "nonces", "hiding", "binding"}:
                    if isinstance(item, str) and len(item) >= 64:
                        private_values.append(item)
                secrets(item)
        elif isinstance(value, list):
            for item in value:
                secrets(item)
    envelopes = 0
    for index, account in enumerate(accounts):
        assert participant(index, "verify")["status"] == "VERIFIED"
        directory = tmp_path / f"friend-{index}"
        state = SecureState(str(directory / "btc_ceremony.enc"), (directory / "password").read_text().strip())
        local = state.get(manifest.context.hex())
        assert local["stage"] == "active" and local["result_hash"] == bytes(completed[1]).hex()
        assert local["result"]["group"][2:] == bytes(completed[4]).hex()
        secrets(state._load())
        for kind in (0, 1):
            public_corpus.append(bytes(board.functions.packet(session, kind, account.address, "0x" + "00" * 20).call()).decode())
        for recipient in accounts:
            if recipient.address == account.address:
                continue
            packet = bytes(board.functions.packet(session, 2, account.address, recipient.address).call()).decode()
            assert set(json.loads(packet)) == {"envelope", "hash", "signature"}
            public_corpus.append(packet)
            envelopes += 1
    corpus = "\n".join(public_corpus + dkg_output)
    boundary_passed = bool(private_values) and not any(value in corpus for value in private_values)
    assert boundary_passed, "MAINNET_DKG_SECRET_BOUNDARY_FAILED"
    assert envelopes == 6
    assert all(not {"--package", "--manifest", "--mailbox"}.intersection(args) for args in command_arguments)
    profile["bitcoin_policy"] = dict(policy, max_reorg_depth="13")
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(profile)

    # Mainnet board discovery cannot outrun the enrolled EVM finality depth.
    pending_invite = dict(invite, deployment_id="mainnet-pending-board", evm_confirmations="3")
    pending_file = tmp_path / "operator-pending-invite.json"
    pending_file.write_text(json.dumps(pending_invite))
    pending_state = tmp_path / "pending-publish.sqlite"
    args = SimpleNamespace(action="publish", rpc=RPC, invite=pending_file, identity_file=identities[0], state=pending_state)
    nonce_before = w3.eth.get_transaction_count(accounts[0].address)
    chain_main(args)
    assert json.loads(capsys.readouterr().out)["status"] == "CONFIRMING"
    saved = Journal(pending_state).get("mainnet-pending-board-invitation:board")["result"]
    pending_address = w3.eth.get_transaction_receipt(saved["tx_hash"]).contractAddress
    with pytest.raises(ValueError, match="EVM_CONFIRMATIONS_PENDING"):
        invitation(w3, pending_address)
    w3.provider.make_request("anvil_mine", ["0x2"])
    assert invitation(w3, pending_address)[1] == pending_invite
    chain_main(args)
    assert json.loads(capsys.readouterr().out)["board"] == pending_address
    assert w3.eth.get_transaction_count(accounts[0].address) == nonce_before + 1

    # Deploy the actual relay and guard with the enrolled constructor arguments.
    graph = dict(deployment_id="mainnet-simulated-readiness", mode="backed", bitcoin_network="mainnet",
                 chain_id="31337", evm_rpc=RPC, evm_confirmations="1", bitcoin_policy=policy,
                 steps=[dict(id="relay", kind="deploy", artifact="MainnetHeaderRelay",
                             args=Bootstrap.wire(relay_constructor_args(policy))),
                        dict(id="spv_guard", kind="deploy", artifact="MainnetSPVGuard",
                             args=["$relay", policy["max_tip_age_seconds"]]),
                        dict(id="ready", kind="bitcoin_ready"),
                        dict(id="consumer_must_not_deploy", kind="deploy", artifact="CustodianDAOToken",
                             args=["Unreachable", "NO", accounts[0].address])])
    state = tmp_path / "bootstrap.sqlite"
    first = Bootstrap(graph, state, bitcoin=core).apply(identities[0])
    assert first["status"] == "AWAITING_BITCOIN_RELAY"
    assert first["bitcoin"]["submitted"] == "4"
    bootstrap = None
    for _ in range(3):
        bootstrap = Bootstrap(graph, state, bitcoin=core)
        result = bootstrap.apply(identities[0])
        assert result["status"] == "AWAITING_BITCOIN_RELAY"
        assert int(result["bitcoin"]["submitted"]) <= 4
    assert result["bitcoin"]["reason"] == "SPV_GUARD_NOT_FRESH"
    relay = bootstrap.contract("relay")
    guard = bootstrap.contract("spv_guard")
    assert relay.functions.getMainchainHeight().call() == core.height
    assert not guard.functions.isFresh().call()
    assert guard.functions.getBlockStatus(header_hash(core.raw(32256))).call() == [False, 0]
    assert "consumer_must_not_deploy" not in bootstrap.contracts
    with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"):
        bootstrap.apply(verify=True)
    prove_creation(w3, relay.address, "MainnetHeaderRelay", relay_constructor_args(policy), w3.eth.block_number)
    prove_creation(w3, guard.address, "MainnetSPVGuard", [relay.address, int(policy["max_tip_age_seconds"])], w3.eth.block_number)

    # Public Bitcoin does not forbid explicit test assets on EVM 31337. Their
    # runtime pins and decimal scale remain mandatory before any transaction.
    for name, artifact_name, field in (("usd", "DAOTestUSD", "reward_token"), ("feed", "DAOMockFeed", "btc_usd_feed")):
        fixture = deploy(w3, artifact_name)
        component = dict(address=fixture.address, runtime_hash="0x" + Web3.keccak(w3.eth.get_code(fixture.address)).hex().removeprefix("0x"))
        if name == "usd":
            component["decimals"] = "6"
        explicit = dict(graph, deployment_id="explicit-test-" + name, steps=[],
                   contracts={name: dict(address=fixture.address, artifact=artifact_name)}, **{field: component})
        nonce_before = w3.eth.get_transaction_count(accounts[0].address)
        Bootstrap(explicit, tmp_path / (name + ".sqlite"), bitcoin=core)
        changed_hash = dict(explicit, deployment_id="wrong-pin-" + name,
                            **{field: dict(component, runtime_hash="0x" + "00" * 32)})
        with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_CODE_HASH"):
            Bootstrap(changed_hash, tmp_path / (name + "-wrong-pin.sqlite"), bitcoin=core)
        if name == "usd":
            changed_scale = dict(explicit, deployment_id="wrong-scale-" + name,
                                 reward_token=dict(component, decimals="7"))
            with pytest.raises(ValueError, match="EXTERNAL_COMPONENT_DECIMALS"):
                Bootstrap(changed_scale, tmp_path / (name + "-wrong-scale.sqlite"), bitcoin=core)
        assert w3.eth.get_transaction_count(accounts[0].address) == nonce_before
    record_property("mainnet_bootstrap_simulation", json.dumps(dict(
        scope="real Anvil / authentic historical mainnet headers / simulated Core and observation clock; no live Bitcoin payment",
        onchain_enrollments=3, mainnet_policy_committed=True, no_friend_file_exchange=True,
        first_bitcoin_dkg="real Rust + real on-chain HPKE transport, 3/3 participants active",
        hpke_envelopes=envelopes, dkg_private_values_scanned=len(private_values),
        dkg_secret_boundary_passed=boundary_passed,
        pending_board_enforces_enrolled_evm_finality=True,
        exact_relay_and_guard_constructor_origins=True, bounded_headers_per_resume=4,
        stale_guard_blocks_consumer_bootstrap=True, explicit_test_evm_components_accepted=True,
        external_component_hash_and_decimal_checks=True)))
