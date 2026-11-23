"""Execute the generated resumable graph, supplying actual independent DKG/signing processes at waits."""

import json, os, subprocess, sys
import pytest
from pathlib import Path
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_formation import expected_formation
from signer_agent.bootstrap_plan import build
from signer_agent.dkg_protocol import Manifest
from test_dkg_processes import RPC
from lifecycle_helpers import contract

ROOT = Path(__file__).resolve().parents[2]


def exercise_bootstrap(tmp_path, record_property, ethereum=None, system=None, shared=None, participant_keys=(1,2,3), custody=None, return_context=False, in_kind=None, guarded_nonces=False, recovery=None):
    w3 = Web3(Web3.HTTPProvider(RPC))
    ethereum_chain = int(ethereum.get("chain_id", 1)) if ethereum else None
    evm_depth = "12" if ethereum_chain == 1 else "1"
    groups = {}
    addresses = [Account.from_key(i.to_bytes(32, "big")).address for i in participant_keys]
    for a in addresses:
        w3.provider.make_request("anvil_setBalance", [a, hex(10**23)])

    # Exercise the installed entry point from separate working directories, as in
    # FIRST_DKG_ONCHAIN.md. No internal Bootstrap.apply calls drive the workflow.
    executable = str(Path(sys.executable).with_name("frost-signer"))
    homes = [tmp_path / ("terminal-" + str(i)) for i in range(3)]
    for home in homes:
        home.mkdir(mode=0o700)
    ledger_dirs = [tmp_path / ("independent-ledger-" + str(i)) for i in range(3)]
    if guarded_nonces:
        for directory in ledger_dirs:
            directory.mkdir(mode=0o700)
    operator_rpc = RPC.replace("://", "://fixture-user:fixture-rpc-token@", 1)
    friend_rpcs = [RPC, RPC.replace("127.0.0.1", "localhost"), RPC + "/"]

    def command(group, *args, member=0, expected=(0,)):
        args = list(args)
        if custody is not None and group == "setup" and args[0] == "invite":
            args += ["--bitcoin-custody-policy", str(tmp_path / "custody.json")]
        if recovery is not None and group == "setup" and args[0] == "invite":
            args += ["--bitcoin-recovery-policy", str(tmp_path / "bitcoin-recovery.json")]
        if (system or in_kind) and group == "setup" and args[0] == "invite":
            args[args.index("--wrap-staker-bps") + 1] = "0"
            args[args.index("--deploy-staker-bps") + 1] = "0"
            args[args.index("--dao-use-reserve") + 1] = "false"
            if in_kind:
                args += ["--ulr-policy", str(tmp_path / "in-kind.json"),
                         "--token-metadata", str(ROOT / "config/token-metadata.example.json")]
            else:
                args += ["--system-policy", str(tmp_path / "system.json")]
        if ethereum:
            if group == "setup" and args[0] == "invite":
                args[args.index("--integration-fee-exemption") + 1] = "false" if ethereum_chain == 1 else "true"
                token, feed = ethereum["token"], ethereum["feed"]
                args += ["--deadline", "4102444800", "--chain-id", str(ethereum_chain),
                    "--bitcoin-network", ethereum.get("bitcoin_network", "mainnet"),
                    "--bitcoin-policy", ethereum["policy"], "--evm-confirmations", evm_depth,
                    "--reward-token", token.address, "--reward-token-code-hash", "0x" + Web3.keccak(w3.eth.get_code(token.address)).hex().removeprefix("0x"),
                    "--reward-token-decimals", str(token.functions.decimals().call()), "--btc-usd-feed", feed.address,
                    "--btc-usd-feed-code-hash", "0x" + Web3.keccak(w3.eth.get_code(feed.address)).hex().removeprefix("0x")]
                if ethereum_chain == 1:
                    args += ["--deployment-fee-amount", "1234567", "--deployment-fee-treasury", addresses[2]]
            if ((group == "setup" and args[0] == "join-chain") or group in ("dkg", "flow")
                    or (group == "bootstrap" and args[0] != "plan")):
                args += ["--bitcoin-rpc", "http://127.0.0.1:1", "--bitcoin-cookie", ethereum["cookie"]]
            if group == "dao" and args[0] == "init":
                args[args.index("--chain-id") + 1] = str(ethereum_chain)
                args += ["--confirmations", evm_depth]
        launcher = [sys.executable, str(ethereum["wrapper"])] if ethereum else [executable]
        if ethereum:
            launcher += ["--fixture-bitcoin-network", ethereum.get("bitcoin_network", "mainnet")]
        if recovery is not None and group == "bootstrap":
            args.append("--debug")
        process = subprocess.run(
            [*launcher, group, *map(str, args)],
            cwd=homes[member],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert process.returncode in expected, process.stdout + process.stderr
        result = json.loads(process.stdout)
        assert result.get("status") != "ERROR", process.stdout + process.stderr
        return result

    def setup(*args, member=0):
        return command("setup", *args, member=member)

    if system:
        (tmp_path / "system.json").write_text(json.dumps(system))
    if in_kind:
        (tmp_path / "in-kind.json").write_text(json.dumps(in_kind))
    if custody is not None:
        (tmp_path / "custody.json").write_text(json.dumps(custody))
    if recovery is not None:
        (tmp_path / "bitcoin-recovery.json").write_text(json.dumps(recovery))
    identities = []
    for index,key in enumerate(participant_keys):
        reuse_identity = shared and participant_keys==shared['participant_keys']
        path = shared["identities"][index] if reuse_identity else tmp_path / ("identity-" + str(key) + ".json")
        if not reuse_identity:
            path.write_text(json.dumps({"private_key": "%064x" % key}))
            os.chmod(path, 0o600)
        identities.append(path)
    invite = tmp_path / "invitation.json"
    setup(
        "invite",
        "--participants",
        *addresses,
        "--threshold",
        "3",
        "--min-custodians",
        "2",
        "--min-p2wpkh-sats",
        "294",
        "--min-p2tr-sats",
        "330",
        "--treasury",
        addresses[2],
        "--bitcoin-confirmations", "6",
        "--wrap-fee-bps", "17", "--unwrap-fee-bps", "23",
        "--wrap-staker-bps", "1234", "--deploy-staker-bps", "6789",
        "--converter-numerator", "3", "--converter-denominator", "9999",
        "--converter-funding", "7654321",
        "--dao-token-recipient", addresses[1],
        "--dao-use-reserve", "true", "--integration-fee-exemption", "true",
        "--deployment-id",
        "full-bootstrap",
        "--output",
        invite
    )
    published = setup(
        "publish",
        "--rpc",
        operator_rpc,
        "--invite",
        invite,
        "--identity-file",
        identities[0],
        "--state",
        tmp_path / "invitation.sqlite",
    )
    assert "board" in published, published
    board = published["board"]
    assert "evm_rpc" not in json.loads(invite.read_text())
    initial = setup("status", "--board", board, "--rpc", RPC)
    assert not any(initial["enrolled"].values())
    assert all(c["status"] == "NOT_STARTED" for c in initial["ceremonies"].values())
    # Each participant starts with code, RPC and the operator's known address only.
    for i in range(3):
        discovered = setup(
            "discover", "--operator", addresses[0], "--rpc", friend_rpcs[i], member=i
        )
        candidate = next(x for x in discovered["invitations"] if x["board"] == board)
        setup(
            "join-chain",
            "--board",
            candidate["board"],
            "--rpc",
            friend_rpcs[i],
            "--accept-fingerprint",
            candidate["fingerprint"],
            "--identity-file",
            identities[i],
            "--directory",
            tmp_path / ("friend-" + str(i + 1)),
            *(("--nonce-ledger-dir", ledger_dirs[i]) if guarded_nonces else ()),
            member=i,
        )
    config_path = tmp_path / "profile.json"
    setup("assemble-chain", "--board", board, "--rpc", RPC, "--output", config_path)
    config = json.loads(config_path.read_text())
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        groups[role] = tmp_path
    assert config["evm_rpc"] == RPC  # Never inherits the operator's provider URL.
    profile = command("bootstrap", "plan", "--profile", config_path)
    assert profile == build(config)
    journal = tmp_path / "bootstrap.sqlite"
    identity = identities[0]

    def run_bootstrap(action):
        options = (
            ["--profile", config_path]
            if action == "apply"
            else ["--deployment", "full-bootstrap"]
        )
        return command(
            "bootstrap",
            action,
            *options,
            "--state",
            journal,
            "--identity-file",
            identity,
            expected=(0, 2)
        )

    def participant(role, i, action, *extra):
        directory = tmp_path / ("friend-" + str(i + 1))
        return command(
            "dkg",
            action,
            "--board",
            board,
            "--role",
            role,
            "--rpc",
            friend_rpcs[i],
            "--identity-file",
            identities[i],
            "--state",
            directory / (role + ".enc"),
            "--password-file",
            directory / "password",
            *extra,
            member=i
        )

    for _ in range(100):
        result = run_bootstrap("apply" if _ == 0 else "resume")
        if system:
            # Explicit operator trust of the root of these isolated test identities.
            # Enrollment never installs trust in the production signer.
            pin = result.get("manifest", {}).get("system", {}).get("family_root_policy")
            if pin:
                from signer_agent.family_registry import install, pin_path
                from signer_agent.bitcoin_continuity import Catalog
                for identity_path in identities:
                    if pin_path(identity_path).exists():
                        assert json.loads(pin_path(identity_path).read_text())==pin
                    else:
                        install(w3, Catalog(identity_file=identity_path), pin, w3.eth.block_number)
        if result["status"] == "VERIFIED":
            break
        progress = setup("status", "--board", board, "--rpc", friend_rpcs[1], member=1)
        active_roles = [
            r for r, c in progress["ceremonies"].items() if c["status"] == "IN_PROGRESS"
        ]
        if active_roles:
            assert len(active_roles) == 3
            # All three independent sessions exist before any participant starts.
            # Each friend drives all roles with one command and isolated secrets.
            for __ in range(16):
                statuses = [command("flow", "dkg", "--once", "--board", board,
                    "--rpc", friend_rpcs[i], "--identity-file", identities[i],
                    "--directory", tmp_path / ("friend-" + str(i + 1)),
                    member=i, expected=(0, 2))["status"] for i in range(3)]
                if statuses == ["ACTIVE"] * 3:
                    break
            assert statuses == ["ACTIVE"] * 3
        elif result["status"] == "AWAITING_PARTICIPANTS":
            pending = setup("pending", "--board", board, "--rpc", RPC)
            role = pending["intent"]["role"]
            options = ["--session", pending["session"]]
            # A pinned read view cannot see its own last share submission;
            # a third sweep can finalize the same session on any EVM network.
            role_members = [i for i, address in enumerate(addresses) if address in config[role]["participants"]]
            for i in role_members[:int(config[role]["threshold"])]:
                status = participant(role, i, "sign-evm", *options)["status"]
                assert status in ("committed", "finalized"), (result["step"], pending, status)
            for i in role_members[:int(config[role]["threshold"])]:
                assert participant(role, i, "sign-evm", *options)["status"] in (
                    "signed",
                    "finalized",
                )
        elif result["status"] == "FAMILY_ENROLLMENT_REQUIRED" and shared:
            shared["enroll"](result["manifest"]["contracts"]["address_registry"]["address"])
        elif result["status"] == "PAUSED_FOR_ULR_DISTRIBUTION":
            # This graph smoke test retains the signed single recipient.
            # Actual manual token distribution is covered separately.
            assert result['distribution']['transfers_confirmed']
            completed = command('bootstrap', 'resume', '--deployment', 'full-bootstrap',
                '--state', journal, '--identity-file', identity, '--ulr-distribution-complete',
                '--accept-ulr-distribution', result['distribution']['fingerprint'], expected=(0,2))
            assert completed['status'] not in ('ERROR','PAUSED_FOR_ULR_DISTRIBUTION')
            record_property('ulr_distribution_checkpoint_confirmed',True)
        elif result["status"] in ("CONFIRMING", "BROADCAST", "AWAITING_BITCOIN_RELAY"):
            if result["status"] == "AWAITING_BITCOIN_RELAY":
                assert ethereum
            continue
        else:
            assert result["status"] == "AWAITING_CUSTODIAN_APPROVALS"
            pending = setup("pending", "--board", board, "--rpc", RPC)
            for i in (1, 2):
                options = [
                    "approve-custodian",
                    "--board",
                    board,
                    "--rpc",
                    RPC,
                    "--session",
                    pending["session"],
                    "--operation-hash",
                    pending["intent"]["operation_hash"],
                    "--identity-file",
                    identities[i],
                    "--state",
                    tmp_path / ("custodian-" + str(i) + ".sqlite"),
                ]
                assert setup(*options)["status"] == "CUSTODIAN_APPROVED"
                assert setup(*options)["status"] == "CUSTODIAN_APPROVED"

    else:
        raise AssertionError("bootstrap did not finish")
    assert run_bootstrap("verify")["status"] == "VERIFIED"
    if guarded_nonces:
        import sqlite3
        from signer_agent.onboarding import password_file
        from signer_agent.secure_state import SecureState

        for role in ("evm_ceremony", "conf_ceremony"):
            spent_count = 0
            for i in range(3):
                if addresses[i] not in config[role]["participants"]:
                    continue
                directory = tmp_path / ("friend-" + str(i + 1))
                path = directory / (role + ".enc")
                password = password_file(directory / "password")
                signer = SecureState(str(path) + ".signing", password)
                report = SecureState(path, password).evm_nonce_ledger(signer)
                with sqlite3.connect(Path(report).as_uri() + "?mode=ro", uri=True) as ledger:
                    spent_count += ledger.execute("SELECT count(*) FROM spent").fetchone()[0]
                assert not Path(str(signer.path) + ".spent.sqlite").exists()
            assert spent_count > 0
    final_progress = setup("status", "--board", board, "--rpc", RPC)
    assert all(
        c["status"] == "COMPLETED" for c in final_progress["ceremonies"].values()
    )
    for role in groups:
        for i in range(3):
            if addresses[i] not in config[role]["participants"]:
                continue
            assert participant(role, i, "verify")["status"] == "VERIFIED"
    bootstrap = Bootstrap(profile, journal, bitcoin=ethereum["core"] if ethereum else None)
    assert bootstrap.apply(verify=True)["status"] == "VERIFIED"
    for kind, terms in expected_formation(config).items():
        factory = bootstrap.contract(kind + "s")
        title = kind.capitalize()
        assert bytes(getattr(factory.functions, f"reserved{title}Session")(terms["id"]).call()) == terms["session_id"]
        saved = getattr(factory.functions, f"reserved{title}Terms")(terms["id"]).call()
        assert [bytes(value) for value in saved] == [terms["formation_hash"], terms["token_hash"]]
    first = result["manifest"]["contracts"]
    assert run_bootstrap("resume")["manifest"]["contracts"] == first
    if (custody is not None or return_context) and not system:
        return dict(bootstrap=bootstrap, manifest=result["manifest"], identities=identities,
            command=command, participant=participant, setup=setup, board=board, addresses=addresses,
            participant_keys=participant_keys, config=config, homes=homes)
    if system:
        def enroll(registry):
            policy_path = tmp_path / "root-policy.json"
            if not policy_path.exists():policy_path.write_text(json.dumps(result["manifest"]["system"]["family_root_policy"]))
            plan_path = tmp_path / ("enroll-"+registry+".json")
            plan = command("family-root", "enroll-prepare", "--rpc", RPC, "--board", board,
                "--policy", policy_path, "--identity-file", identities[0], "--registry", registry,
                "--deadline", str(w3.eth.get_block("latest").timestamp+3600), "--output", plan_path)
            enrolled = command("bootstrap", "apply", "--profile", plan_path, "--state", journal,
                "--identity-file", identities[0], expected=(0,2))
            assert enrolled["status"] == "AWAITING_PARTICIPANTS", enrolled
            pending = setup("pending", "--board", board, "--rpc", RPC)
            conf_members = [i for i, address in enumerate(addresses) if address in config["conf_ceremony"]["participants"]]
            for sweep in range(2):
                for i in conf_members:
                    assert participant("conf_ceremony", i, "sign-evm", "--session", pending["session"])["status"] in (
                        ("committed", "finalized") if sweep == 0 else ("signed", "finalized"))
            # The last submission is outside its own pinned read snapshot.
            # Revisit the same session to finalize the public aggregate.
            for i in conf_members:
                assert participant("conf_ceremony", i, "sign-evm", "--session", pending["session"])["status"] in ('signed','finalized')
            for attempt in range(4):
                completed = command("bootstrap", "resume", "--deployment", plan["deployment_id"], "--state", journal,
                    "--identity-file", identities[0],expected=(0,2))
                if completed['status']=='VERIFIED':break
                assert completed['status'] in ('CONFIRMING','BROADCAST'),completed
            assert completed['status']=='VERIFIED'
        record_property("system_manifest", json.dumps(result["manifest"]))
        return dict(bootstrap=bootstrap, manifest=result["manifest"], identities=identities,
            command=command, participant=participant, setup=setup, board=board, enroll=enroll, addresses=addresses,
            participant_keys=participant_keys)
    # Every financial choice survives signed enrollment and reaches live state.
    from signer_agent.bootstrap_policy import POLICY_FIELDS

    invitation = json.loads(invite.read_text())
    assert all(config[field] == invitation[field] for field in POLICY_FIELDS)
    assert bootstrap.contract("fee_manager").functions.wrapFeeConfig().call() == [0, 17, 0, 0]
    assert bootstrap.contract("fee_manager").functions.unwrapFeeConfig().call() == [0, 23, 0, 0]
    assert bootstrap.contract("fee_router").functions.wrapStakerBps().call() == 1234
    assert bootstrap.contract("fee_router").functions.deployStakerBps().call() == 6789
    converter = bootstrap.contract("converter")
    assert converter.functions.numerator().call() == 3
    assert converter.functions.denominator().call() == 9999
    assert bootstrap.contract("usd").functions.balanceOf(converter.address).call() == 7654321
    dao_token = bootstrap.contract("dao_token")
    assert dao_token.functions.balanceOf(addresses[1]).call() == dao_token.functions.totalSupply().call()
    assert dao_token.functions.balanceOf(addresses[0]).call() == 0
    day = w3.eth.get_block("latest").timestamp // 86400
    assert bootstrap.contract("controller").functions.useReserveForDay(day).call() is True
    assert bootstrap.contract("escrow").functions.depth().call() == 6
    assert bootstrap.contract("deposits").functions.depth().call() == 6
    # Discover the approved Bitcoin graph on chain, then pin it independently.
    btc_fingerprints = []
    for i in range(3):
        reviewed = participant("btc_ceremony", i, "inspect-bitcoin-registry")
        assert reviewed["graph"]["contracts"]["batches"]["address"] == first["batches"]["address"]
        btc_fingerprints.append(reviewed["fingerprint"])
        assert participant("btc_ceremony", i, "trust-bitcoin-registry",
                           "--accept-fingerprint", reviewed["fingerprint"])["status"] == "BTC_REGISTRY_PINNED"
    assert len(set(btc_fingerprints)) == 1
    # Each friend discovers the registered DAO through EVM, without a shared manifest/RPC.
    for i in range(3):
        dao_config = homes[i] / "dao.json"
        discovered = command("dao", "init", "--board", board, "--rpc", friend_rpcs[i],
                             "--chain-id", "31337", "--output", dao_config, member=i)
        assert discovered["contracts"]["settlement"]["address"] == first["settlement"]["address"]
        assert "fixture-rpc-token" not in dao_config.read_text()
        assert command("dao", "status", "--config", dao_config, "--rpc", friend_rpcs[i],
                       "--owner", addresses[i], member=i)["wallet"]["owner"] == addresses[i]
    if ethereum:
        for action in ("approve", "stake", "unstake"):
            operated = command("dao", action, "--config", homes[1] / "dao.json", "--rpc", friend_rpcs[1],
                "--amount", "1", "--identity-file", identities[1], "--state", homes[1] / "dao-wallet.sqlite",
                "--id", "ethereum-" + action, "--send", member=1)
            assert operated["status"] == "VERIFIED", operated
            assert command("dao", action, "--config", homes[1] / "dao.json", "--rpc", friend_rpcs[1],
                "--amount", "1", "--identity-file", identities[1], "--state", homes[1] / "dao-wallet.sqlite",
                "--id", "ethereum-" + action, "--send", member=1)["tx_hash"] == operated["tx_hash"]
        assert bootstrap.contract("staking").functions.stakedBalance(addresses[1]).call() == 0
    assert (
        bootstrap.contract("governance_registry")
        .functions.governance(config["federation_id"])
        .call()
        == first["governance"]["address"]
    )
    assert (
        bootstrap.contract("confederations")
        .functions.getCustodianDAO(config["confederation_id"])
        .call()[-1]
        == first["settlement"]["address"]
    )
    record_property(
        "bootstrap_manifest",
        json.dumps(
            dict(
                contracts=first,
                values=bootstrap.values,
                steps=str(len(profile["steps"])),
                onchain_board=board,
                transport="EVM_HPKE",
            )
        ),
    )
    # Inspect every deployed runtime and actual CREATE transaction input, including
    # ABI constructor arguments. Internal factory CREATEs are enforced by Anvil.
    from signer_agent.deployment_limits import check_deployment

    runtime_sizes = []
    initcode_sizes = []
    for name, item in first.items():
        sizes = check_deployment(w3.eth.get_code(item["address"]), b"")
        assert sizes["runtime_bytes"] > 0
        runtime_sizes.append(
            dict(
                name=name,
                artifact=item["artifact"],
                runtime_bytes=sizes["runtime_bytes"],
                runtime_margin=sizes["runtime_margin"],
            )
        )
    for row in bootstrap.journal.list():
        result = row.get("result", {}) or {}
        if "tx_hash" not in result:
            continue
        transaction = w3.eth.get_transaction(result["tx_hash"])
        if transaction["to"] is None:
            receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
            sizes = check_deployment(
                w3.eth.get_code(receipt.contractAddress), transaction["input"]
            )
            initcode_sizes.append(dict(tx=result["tx_hash"], **sizes))
    expected_contracts = (set(profile.get('contracts', {}))
        | {s['id'] for s in profile['steps'] if s['kind']=='deploy'}
        | {o['name'] for s in profile['steps'] for o in s.get('outputs',[]) if o.get('artifact')})
    assert {row['name'] for row in runtime_sizes} == expected_contracts
    assert len(initcode_sizes) == sum(s['kind']=='deploy' for s in profile['steps'])
    if ethereum_chain == 1:
        for factory_name in ("federations", "confederations"):
            factory = bootstrap.contract(factory_name)
            assert not factory.functions.integrationFeeExemption().call()
            assert factory.functions.deploymentFee().call() == 1234567
            assert factory.functions.deploymentFeeRouter().call() == bootstrap.contract("project_router").address
            assert bootstrap.contract("usd").functions.allowance(addresses[0], factory.address).call() == 0
        assert bootstrap.contract("usd").functions.balanceOf(addresses[2]).call() == 2 * 1234567
        transactions = [w3.eth.get_transaction(row["result"]["tx_hash"])
            for row in bootstrap.journal.list() if (row.get("result") or {}).get("tx_hash")]
        assert transactions and all(tx.gas <= 2**24 for tx in transactions)
        record_property("ethereum_paid_bootstrap", json.dumps(dict(
            factory_fee_each="1234567", treasury_received=str(2 * 1234567),
            factory_allowances_consumed=True, contracts=len(runtime_sizes),
            max_transaction_gas=max(tx.gas for tx in transactions),
            bootstrap_steps=len(profile["steps"]), mainnet_guard_fresh=True)))
    record_property(
        "bootstrap_sizes",
        json.dumps(dict(runtime=runtime_sizes, creation=initcode_sizes)),
    )

    # Check actual chain packets against test participant secrets, without publishing either.
    from signer_agent.secure_state import SecureState
    from signer_agent.bootstrap import artifact

    public_board = w3.eth.contract(
        address=board, abi=artifact("DKGOnchainBoard")["abi"]
    )
    published_invite = bytes(public_board.functions.invitation().call()).decode()
    assert (
        "fixture-rpc-token" not in published_invite
        and "evm_rpc" not in published_invite
    )
    record_property(
        "runbook_cli",
        json.dumps(
            dict(
                installed_entrypoint=True,
                separate_working_directories=3,
                participant_rpc_routes=3,
                private_rpc_published=False,
                bootstrap_driver="CLI",
            )
        ),
    )
    ciphertext_count = 0
    corpus = []
    secrets = []
    for role in groups:
        sid = bytes.fromhex(config[role]["id"][2:])
        for i, address in enumerate(addresses, 1):
            directory = tmp_path / ("friend-" + str(i))
            state = SecureState(
                str(directory / (role + ".enc")),
                (directory / "password").read_text().strip(),
            )

            def collect(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if (
                            k
                            in (
                                "secret",
                                "signing_share",
                                "key_package",
                                "nonces",
                                "hiding",
                                "binding",
                            )
                            and isinstance(v, str)
                            and len(v) >= 64
                        ):
                            secrets.append(v)
                        collect(v)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)

            collect(state._load())
            for kind in (0, 1):
                corpus.append(
                    bytes(
                        public_board.functions.packet(
                            sid, kind, address, "0x" + "00" * 20
                        ).call()
                    ).decode()
                )
            for recipient in addresses:
                if address == recipient:
                    continue
                payload = bytes(
                    public_board.functions.packet(sid, 2, address, recipient).call()
                ).decode()
                assert set(json.loads(payload)) == {"envelope", "hash", "signature"}
                corpus.append(payload)
                ciphertext_count += 1
    assert not any(
        value in "\n".join(corpus) for value in secrets
    ), "SECRET_BOUNDARY_FAILED"
    record_property(
        "onchain_secret_boundary",
        json.dumps(
            dict(
                packets=len(corpus),
                hpke_envelopes=ciphertext_count,
                local_secret_values_checked=len(secrets),
                secret_values_published=False,
            )
        ),
    )
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        w3.provider.make_request(
            "anvil_setCode", [first["mp"]["address"], "0x60006000"]
        )
        with pytest.raises(ValueError, match="CODE_HASH_MISMATCH"):
            bootstrap.apply(verify=True)
    finally:
        assert w3.provider.make_request("evm_revert", [snapshot])["result"]
    return dict(bootstrap=bootstrap, manifest=bootstrap.manifest(), identities=identities,
        command=command, participant=participant, setup=setup, board=board, addresses=addresses,
        participant_keys=participant_keys, config=config, homes=homes)


def test_generated_bootstrap_runs_to_verified_with_real_approvals(tmp_path, record_property):
    exercise_bootstrap(tmp_path, record_property, guarded_nonces=True)
