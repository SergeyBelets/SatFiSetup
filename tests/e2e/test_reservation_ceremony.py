"""Recover two reserved IDs through real invitations, FROST DKG and adoption on Anvil."""

import json
import subprocess
import sys
import time

import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError

from lifecycle_helpers import contract
from signer_agent.bootstrap import encode_args
from signer_agent.bootstrap_formation import expected_formation
from signer_agent.dkg_protocol import PROTOCOL_ID
from signer_agent.onchain_transport import assembled_profile
from test_dkg_processes import RPC, artifact, tx


def test_replacement_invitation_reaches_adoption_on_the_original_registry(tmp_path):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337, "local Anvil fixture only"
    fixture_keys = [i.to_bytes(32, "big") for i in (1, 2, 3)]
    participants = [Account.from_key(key).address for key in fixture_keys]
    operator = participants[0]
    identities = []
    for i, (member, key) in enumerate(zip(participants, fixture_keys)):
        assert "error" not in w3.provider.make_request("anvil_setBalance", [member, hex(10**23)])
        assert "error" not in w3.provider.make_request("anvil_impersonateAccount", [member])
        identity = tmp_path / f"identity-{i}.json"
        identity.write_text(json.dumps({"private_key": key.hex()}))
        identity.chmod(0o600)
        identities.append(identity)

    def command(group, *args):
        result = subprocess.run(
            [sys.executable, "-m", "signer_agent.cli", group, *map(str, args)],
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload.get("status") != "ERROR", result.stdout + result.stderr
        return payload

    def deploy(name, *args):
        compiled = artifact(name)
        node = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"]["object"])
        receipt = tx(w3, node.constructor(*args), sender=operator)
        return w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"])

    def allow(child, parent):
        tx(w3, child.functions.setAuthorizedDeployer(parent.address, True), sender=operator)

    registry = deploy("DKGCoordinator")
    verifier = deploy("FROSTVerifier")
    coordinator = deploy("FROSTCoordinator", verifier.address)
    tx(w3, coordinator.functions.bindDKGRegistry(registry.address), sender=operator)
    auth_leaf = deploy("FROSTAuthorizationDeployer")
    fed_token_leaf = deploy("MultiPoolShareTokenDeployer")
    conf_token_leaf = deploy("ConfederationTokenDeployer")
    custodians_leaf = deploy("FROSTCustodianDeployer")
    fees = deploy("FeeSystemDeployer", *[deploy(name).address for name in (
        "FederationFeeManagerDeployer", "FeeDistributionCalculatorDeployer",
        "FederationNFTModifierDeployer", "ProjectWalletRegistryDeployer",
    )])
    simple = deploy("SimpleMultiPoolDeployer", auth_leaf.address, fed_token_leaf.address)
    multi = deploy("MultiPoolDeployer", conf_token_leaf.address)
    fed_deployer = deploy("FederationDeployer", simple.address, fees.address)
    conf_deployer = deploy("ConfederationDeployer", custodians_leaf.address, multi.address, fees.address)
    federation = deploy("FederationFactory", registry.address, fed_deployer.address, coordinator.address)
    confederation = deploy(
        "ConfederationFactory", federation.address, registry.address, conf_deployer.address, coordinator.address
    )
    for child, parent in (
        (auth_leaf, simple), (fed_token_leaf, simple), (simple, fed_deployer),
        (fed_deployer, federation), (conf_token_leaf, multi), (multi, conf_deployer),
        (custodians_leaf, conf_deployer), (conf_deployer, confederation),
    ):
        allow(child, parent)
    for factory in (federation, confederation):
        tx(w3, factory.functions.configureDeploymentFee("0x" + "00" * 20, 0, True), sender=operator)

    custody = tmp_path / "custody-policy.json"
    custody.write_text(json.dumps(dict(version="1", mode="custodians", threshold="2", sources=[])))
    token_metadata = tmp_path / "token-metadata.json"
    token_metadata.write_text(json.dumps({
        "governance": {"name": "Recovery Governance", "symbol": "RGOV"},
        "mining": {"name": "Recovery Mining", "symbol": "RMIN"},
        "federation_share": {"name": "Recovery Federation", "symbol": "RFS"},
        "confederation_share": {"name": "Recovery Confederation", "symbol": "RCS"},
    }))
    original_invite = tmp_path / "original.json"
    deadline = max(int(time.time()), w3.eth.get_block("latest").timestamp) + 2 * 86400
    original = command(
        "setup", "invite", "--participants", *participants, "--threshold", 3,
        "--min-custodians", 2, "--treasury", participants[2], "--bitcoin-confirmations", 6,
        "--min-p2wpkh-sats", 294, "--min-p2tr-sats", 330, "--wrap-fee-bps", 17,
        "--unwrap-fee-bps", 23, "--wrap-staker-bps", 0, "--deploy-staker-bps", 0,
        "--converter-numerator", 1, "--converter-denominator", 1, "--converter-funding", 100,
        "--dao-token-recipient", participants[1], "--dao-use-reserve", "false",
        "--integration-fee-exemption", "true", "--bitcoin-custody-policy", custody,
        "--token-metadata", token_metadata, "--deployment-id", "rr11-original",
        "--deadline", deadline, "--output", original_invite,
    )

    def publish_and_enroll(invite, fingerprint, label):
        directory = tmp_path / label
        directory.mkdir(mode=0o700)
        published = command(
            "setup", "publish", "--rpc", RPC, "--invite", invite, "--identity-file", identities[0],
            "--state", directory / "invitation.sqlite",
        )
        assert published["status"] == "VERIFIED"
        assert published["fingerprint"] == fingerprint
        board = published["board"]
        for i, identity in enumerate(identities):
            joined = command(
                "setup", "join-chain", "--rpc", RPC, "--board", board, "--identity-file", identity,
                "--accept-fingerprint", fingerprint, "--directory", directory / f"participant-{i}",
            )
            assert joined["status"] == "ENROLLED_ONCHAIN"
            assert joined["participant"] == participants[i]
        return directory, board

    _, original_board_address = publish_and_enroll(original_invite, original["fingerprint"], "original-board")
    original_board = contract(w3, "DKGOnchainBoard", original_board_address)
    tx(w3, original_board.functions.bindRegistry(registry.address), sender=operator)
    tx(w3, original_board.functions.bindIntentCoordinator(coordinator.address), sender=operator)
    original_profile = assembled_profile(w3, original_board.address)
    original_terms = expected_formation(original_profile)
    role_names = {"federation": "evm_ceremony", "confederation": "conf_ceremony"}
    for kind, factory in (("federation", federation), ("confederation", confederation)):
        terms, role = original_terms[kind], original_profile[role_names[kind]]
        old = terms["session_id"]
        tx(w3, registry.functions.createCeremony(
            old, role["participants"], int(role["threshold"]),
            [bytes.fromhex(key.removeprefix("0x")) for key in role["transport_keys"]],
            int(role["epoch"]), Web3.keccak(text="EVM_AUTH"), PROTOCOL_ID,
        ), sender=operator)
        reserve = getattr(factory.functions, f"reserve{kind.capitalize()}Id")
        tx(w3, reserve(old, terms["id"], terms["formation_hash"], terms["token_hash"]), sender=operator)
        tx(w3, registry.functions.abortCeremony(old, Web3.keccak(text="fixture recovery")),
           sender=role["participants"][0])
        assert registry.functions.getSessionInfo(old).call()[4] == 6

    # The recovered federation is deployed first so the confederation uses
    # its real active parent graph, with only custodians in the new key.
    for kind, factory, token_kind in (
        ("federation", federation, "MultiPoolShareToken"),
        ("confederation", confederation, "ConfederationToken"),
    ):
        terms = original_terms[kind]
        title = kind.capitalize()
        old = terms["session_id"]
        state = tmp_path / f"{kind}-recovery.sqlite"
        fresh_invite = tmp_path / f"{kind}-replacement.json"
        replacement = command(
            "setup", "reservation-invite", "--invite", original_invite,
            "--accept-fingerprint", original["fingerprint"], "--deployment-id", f"rr11-{kind}-replacement",
            "--deadline", deadline + 86400, "--output", fresh_invite,
        )
        assert replacement["status"] == "RESERVATION_INVITED"
        assert replacement["fingerprint"] != original["fingerprint"]
        directory, board = publish_and_enroll(fresh_invite, replacement["fingerprint"], f"{kind}-board")
        ceremony_args = [
            "--kind", kind, "--factory", factory.address, "--from-session", Web3.to_hex(old),
            "--board", board, "--rpc", RPC, "--confirmations", 1,
        ]
        reviewed = command("setup", "reservation-ceremony", *ceremony_args)
        assert reviewed["status"] == "RESERVATION_CEREMONY_REVIEW"
        assert not reviewed["already_created"]
        review = reviewed["review"]
        assert review["registry"] == registry.address
        assert review["role"] == role_names[kind]
        assert review["entity_id"] == terms["id"]
        assert review["formation_hash"] == Web3.to_hex(terms["formation_hash"])
        assert review["token_hash"] == Web3.to_hex(terms["token_hash"])
        assert review["token_metadata"] == terms["token_metadata"]
        send = ["--send", "--identity-file", identities[0], "--state", state]
        ready = command("setup", "reservation-ceremony", *ceremony_args, *send,
                        "--accept-fingerprint", reviewed["fingerprint"])
        assert ready["status"] == "RESERVATION_CEREMONY_READY"
        assert ready["board"] == board
        onchain_board = contract(w3, "DKGOnchainBoard", board)
        assert onchain_board.functions.registry().call() == registry.address
        assert onchain_board.functions.intentCoordinator().call() == coordinator.address
        new = bytes.fromhex(ready["replacement_session"].removeprefix("0x"))
        assert new != old
        assert registry.functions.getSessionParticipants(new).call() == (
            participants if kind == "federation" else participants[1:]
        )
        assert registry.functions.ceremonies(new).call()[1] == int(original_profile[role_names[kind]]["epoch"]) + 1

        transfer_args = [
            "--kind", kind, "--factory", factory.address, "--entity-id", terms["id"],
            "--from-session", Web3.to_hex(old), "--to-session", Web3.to_hex(new),
            "--rpc", RPC, "--chain-id", 31337, "--confirmations", 1,
        ]
        transfer = command("setup", "reservation-transfer", *transfer_args)
        assert transfer["status"] == "RESERVATION_TRANSFER_REVIEW"
        result = command("setup", "reservation-transfer", *transfer_args, *send,
                         "--accept-fingerprint", transfer["fingerprint"])
        assert result["status"] == "RESERVATION_TRANSFERRED"

        adoption = review["adoption"]
        abi = next(row for row in factory.abi if row.get("name") == adoption["method"])
        arguments = encode_args(abi["inputs"], adoption["args"])
        adopt = getattr(factory.functions, adoption["method"])(*arguments)
        selected = range(3) if kind == "federation" else range(1, 3)
        participant = participants[selected.start]
        with pytest.raises(ContractLogicError):
            adopt.call({"from": participant})
        if kind == "confederation":
            publication = review["enable_custodian_publishers"]
            assert publication == dict(target=board, method="enableCustodianPublishers", args=[Web3.to_hex(new)])
            activate_publishers = getattr(onchain_board.functions, publication["method"])(*publication["args"])
            assert not any(onchain_board.functions.canPublishIntent(participants[i]).call() for i in selected)
            with pytest.raises(ContractLogicError):
                activate_publishers.call({"from": operator})

        def dkg(i, action):
            local = directory / f"participant-{i}"
            return command(
                "dkg", action, "--board", board, "--role", role_names[kind], "--rpc", RPC,
                "--identity-file", identities[i], "--state", local / f"{role_names[kind]}.enc",
                "--password-file", local / "password",
            )

        for _ in range(12):
            statuses = [dkg(i, "run-once")["status"] for i in selected]
            if statuses == ["ACTIVE"] * len(selected):
                break
        assert statuses == ["ACTIVE"] * len(selected)
        assert all(dkg(i, "verify")["status"] == "VERIFIED" for i in selected)
        _, _, _, result_hash = registry.functions.ceremonies(new).call()[:4]
        assert any(result_hash)
        assert registry.functions.getSessionInfo(old).call()[4] == 6
        if kind == "confederation":
            assert adoption["method"] == "adoptCustodianConfederationDKG"
            assert not registry.functions.isSessionParticipant(new, operator).call()
            altered = [*arguments[:-2], 1, arguments[-1]]
            with pytest.raises(ContractLogicError):
                getattr(factory.functions, adoption["method"])(*altered).call({"from": participant})
        tx(w3, adopt, sender=participant)
        adopted_id = getattr(factory.functions, f"get{title}ForSession")
        assert adopted_id(new).call() == terms["id"]
        assert adopted_id(old).call() == ""
        reserved_terms = getattr(factory.functions, f"reserved{title}Terms")(terms["id"])
        assert tuple(map(bytes, reserved_terms.call())) == (terms["formation_hash"], terms["token_hash"])

        deploy_entity = getattr(factory.functions, f"deploy{title}")
        with pytest.raises(ContractLogicError):
            deploy_entity(new, "Changed Shares", "BAD").call({"from": participant})
        names = terms["token_metadata"]
        tx(w3, deploy_entity(new, names["name"], names["symbol"]), sender=participant)
        config = getattr(factory.functions, f"get{title}")(terms["id"]).call()
        abi = next(row for row in factory.abi if row.get("name") == f"get{title}")
        data = dict(zip([field["name"] for field in abi["outputs"][0]["components"]], config))
        assert data["isActive"]
        token = contract(w3, token_kind, data["shareToken"])
        assert token.functions.name().call() == names["name"]
        assert token.functions.symbol().call() == names["symbol"]
        auth = contract(w3, "DKGAuthorization", data["frostMultisig"])
        assert bytes(auth.functions.dkgId().call()) == new
        assert bytes(auth.functions.resultHash().call()) == result_hash
        assert auth.functions.getParticipants().call() == [participants[i] for i in selected]

        if kind == "confederation":
            with pytest.raises(ContractLogicError):
                activate_publishers.call({"from": participant})
            tx(w3, activate_publishers, sender=operator)
            assert bytes(onchain_board.functions.custodyPublisherEpoch().call()) == new
            assert all(onchain_board.functions.canPublishIntent(participants[i]).call() for i in selected)
            outsider = w3.eth.accounts[1]
            assert not onchain_board.functions.canPublishIntent(outsider).call()
            assert "error" not in w3.provider.make_request("anvil_stopImpersonatingAccount", [operator])

            selector = Web3.keccak(text="fixture()")[:4]
            for i in selected:
                member = participants[i]
                digest = Web3.keccak(text=f"public intent from test member {i}")
                message = auth.functions.hashOperation(auth.address, selector, digest, 0).call()
                expire = w3.eth.get_block("latest").timestamp + 600
                tx(w3, coordinator.functions.createSessionFromDKG(new, message, auth.address, expire), sender=member)
                session = coordinator.functions.nextSessionId().call()
                payload = json.dumps(dict(
                    chain_id="31337", coordinator=coordinator.address, role=role_names[kind],
                    artifact="DKGAuthorization", target=auth.address, operation_hash=Web3.to_hex(message),
                    hash_method="hashOperation",
                    hash_args=[auth.address, Web3.to_hex(selector), Web3.to_hex(digest), "0"],
                )).encode()
                publish = onchain_board.functions.publishIntent(session, payload)
                other_custodians = [participants[j] for j in selected if j != i]
                for wrong_publisher in [*other_custodians, outsider]:
                    with pytest.raises(ContractLogicError):
                        publish.call({"from": wrong_publisher})
                tx(w3, publish, sender=member)
                assert bytes(onchain_board.functions.intents(session).call()) == payload
