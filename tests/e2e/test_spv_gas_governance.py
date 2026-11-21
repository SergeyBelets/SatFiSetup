"""Real enrolled confederation DKG/FROST activation; SPV state remains synthetic."""
import json
import os
from copy import deepcopy
from pathlib import Path

from eth_account import Account
from coincurve import PublicKeyXOnly
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.gas_treasury import GasTreasuryClient
from test_bitcoin_onchain import _bootstrap, _sign_session


def test_enrolled_frost_gas_configuration_rotation_and_pause(tmp_path):
    env = _bootstrap(tmp_path)
    w3 = env.w3
    operator = env.addresses[0]
    auth = env.bootstrap.contract("conf_auth")
    coordinator = env.bootstrap.contract("coordinator")
    factory = env.bootstrap.contract("confederations")
    receipts = []
    signatures = []
    account = Account.from_key(json.loads(env.identities[0].read_text())["private_key"])

    def submit_fixture(fn):
        transaction = fn.build_transaction({"from": operator, "nonce": w3.eth.get_transaction_count(operator),
            "chainId": w3.eth.chain_id, "gas": 2**24})
        return w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(account.sign_transaction(transaction).raw_transaction))

    def record(receipt, label):
        assert receipt.status == 1
        assert receipt.gasUsed <= 2**24
        assert w3.eth.get_block(receipt.blockNumber).hash == receipt.blockHash
        receipts.append(dict(label=label, hash=Web3.to_hex(receipt.transactionHash),
            block_number=receipt.blockNumber, block_hash=Web3.to_hex(receipt.blockHash), gas_used=receipt.gasUsed))
        return receipt

    def deploy(name, args, fixture=False):
        data = (json.loads((Path(os.environ["SATOSHIFI_GAS_FIXTURE_OUT"]) / "SPVServicePayment.t.sol" / (name + ".json")).read_text())
                if fixture else artifact(name))
        creator = w3.eth.contract(abi=data["abi"], bytecode=data["bytecode"]["object"])
        receipt = record(submit_fixture(creator.constructor(*args)), "deploy " + name)
        return w3.eth.contract(address=receipt.contractAddress, abi=data["abi"]), receipt

    # SPV fixture is the only synthetic consensus component of this added flow.
    # Factory, DKG, authorization, verifier, sessions and signature shares are real.
    state, _ = deploy("GasStateFixture", [], fixture=True)
    registry, creation = deploy("SPVServicePaymentRegistry", [factory.address, env.config["confederation_id"], state.address])

    def call(fn, label):
        return record(submit_fixture(fn), label)

    def treasury(epoch):
        receipt = call(registry.functions.createTreasury(epoch, env.addresses, [1, 2, 3]), "treasury " + str(epoch))
        address = registry.events.TreasuryCreated().process_receipt(receipt)[0]["args"]["treasury"]
        return w3.eth.contract(address=address, abi=artifact("ConfederationGasTreasury")["abi"])

    def prepare(target=None, handover=None):
        options = ["--gas-registry", registry.address, "--deadline", w3.eth.get_block("latest").timestamp + 3600]
        if target is None:
            return env.participant("conf_ceremony", 0, "gas-prepare-pause", *options)
        config = dict(treasury=target.address, issuer=operator, tariffVersion="1", tariffHash=Web3.to_hex(Web3.keccak(text="reviewed local tariff")),
            amount=str(6 * 10**15), handoverCommitment=Web3.to_hex(handover or bytes(32)))
        path = env.homes[0] / ("config-" + target.address + ".json")
        path.write_text(json.dumps(config))
        return env.participant("conf_ceremony", 0, "gas-prepare-configure", *options, "--configuration", path)

    def propose(intent, name):
        path = env.homes[0] / (name + ".json"); path.write_text(json.dumps(intent))
        first = env.participant("conf_ceremony", 0, "gas-propose", "--package", path)
        assert first["status"] == "AWAITING_PARTICIPANTS"
        count = coordinator.functions.nextSessionId().call()
        nonce = w3.eth.get_transaction_count(operator)
        assert env.participant("conf_ceremony", 0, "gas-propose", "--package", path) == first
        assert coordinator.functions.nextSessionId().call() == count
        assert w3.eth.get_transaction_count(operator) == nonce
        return int(first["session"])

    def execute(session):
        op = bytes(coordinator.functions.getSession(session).call()[3])
        assert not auth.functions.executedOperations(op).call()
        _sign_session(env, session)
        finalized = coordinator.functions.getAuthorizationResult(session).call()
        assert PublicKeyXOnly(bytes(finalized[0])).verify(bytes(finalized[7]), bytes(finalized[1]))
        signatures.append(dict(session=str(session), group_pubkey=Web3.to_hex(finalized[0]),
            message_hash=Web3.to_hex(finalized[1]), signature=Web3.to_hex(finalized[7]),
            threshold=finalized[5], verified_shares=finalized[6],
            participants=coordinator.functions.getSessionParticipants(session).call()))
        result = env.participant("conf_ceremony", 1, "gas-execute", "--session", session)
        assert result["status"] == "GAS_GOVERNANCE_EXECUTED"
        record(w3.eth.get_transaction_receipt(result["transaction_hash"]), "threshold execution")
        nonce = w3.eth.get_transaction_count(env.addresses[1])
        assert env.participant("conf_ceremony", 1, "gas-execute", "--session", session)["status"] == "ALREADY_EXECUTED"
        assert w3.eth.get_transaction_count(env.addresses[1]) == nonce
        assert env.participant("conf_ceremony", 2, "sign-evm", "--session", session)["status"] == "ALREADY_EXECUTED"
        assert auth.functions.executedOperations(op).call()
        return result

    first = treasury(1)
    assert not registry.functions.enabled().call()
    activation = prepare(first)
    assert activation["gas_policy"]["treasury"]["weights"] == ["1", "2", "3"]
    # A canonical lookalike with the same registry/auth/epoch/payees remains
    # foreign unless this registry actually created/enrolled that treasury.
    foreign, _ = deploy("ConfederationGasTreasury", [registry.address, auth.address,
        registry.functions.confederationScope().call(), 1, env.addresses, [1, 2, 3]])
    substituted = deepcopy(activation)
    substituted["hash_args"][0][0] = foreign.address
    terms = [foreign.address, operator, 1,
        bytes.fromhex(substituted["hash_args"][0][3][2:]), int(substituted["hash_args"][0][4]), bytes(32)]
    operation = registry.functions.getConfigureOperationHash(terms).call()
    substituted["operation_hash"] = Web3.to_hex(operation)
    substituted["gas_policy"]["configuration"]["treasury"] = foreign.address
    substituted["gas_policy"]["treasury"]["address"] = foreign.address
    call(coordinator.functions.createSessionFromDKG(auth.functions.dkgId().call(), operation,
        registry.address, int(substituted["deadline"])), "untrusted treasury proposal")
    bad_session = coordinator.functions.nextSessionId().call()
    substituted["session"] = str(bad_session)
    board = w3.eth.contract(address=env.board, abi=artifact("DKGOnchainBoard")["abi"])
    call(board.functions.publishIntent(bad_session,
        json.dumps(substituted).encode()), "publish untrusted treasury intent")
    before = w3.eth.get_transaction_count(env.addresses[1])
    rejected = env.command("dkg", "sign-evm", "--board", env.board, "--role", "conf_ceremony",
        "--rpc", os.environ["SATOHSI_TEST_RPC"], "--identity-file", env.identities[1],
        "--state", env.homes[1] / "friend/conf_ceremony.enc", "--password-file", env.homes[1] / "friend/password",
        "--session", bad_session, member=1, expected=(2,))
    assert rejected["code"] == "GAS_TREASURY_PROVENANCE"
    assert coordinator.functions.getSession(bad_session).call()[11] == 0
    assert w3.eth.get_transaction_count(env.addresses[1]) == before
    session = propose(activation, "activate")
    execute(session)
    assert registry.functions.enabled().call()
    assert registry.functions.configuration().call()[0] == first.address
    assert registry.functions.configurationNonce().call() == 1

    binding = dict(schema="satoshifi.spv-gas-binding.v1", chain_id="31337", evm_confirmations="1",
        registry=registry.address, registry_creation_block=str(creation.blockNumber),
        registry_runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(registry.address))),
        factory=factory.address, confederation_id=env.config["confederation_id"], state_view=state.address,
        authorization=auth.address, domain=Web3.to_hex(registry.functions.domain().call()))
    client = GasTreasuryClient(w3, binding)
    conf = factory.functions.getConfederation(env.config["confederation_id"]).call()
    job = Web3.keccak(text="real authority paid job")
    deadline = w3.eth.get_block("latest").timestamp + 3600
    call(registry.functions.admit([job, Web3.keccak(text="semantic job"), Web3.keccak(text=conf[1][0]), operator,
        Web3.keccak(text="explicit local budget"), deadline], state.functions.digest().call(),
        state.functions.version().call(), deadline - 1, 0), "admit paid job")
    request = client.prepare_fund(job)
    funded = client.submit(request, env.identities[0])
    record(w3.eth.get_transaction_receipt(funded["transaction_hash"]), "fund under threshold configuration")
    assert registry.functions.paid(job).call()
    assert first.functions.claimable(env.addresses[2]).call() == 3 * 10**15

    second = treasury(2)
    rotation = propose(prepare(second, Web3.keccak(text="explicit local handover")), "rotate")
    execute(rotation)
    assert registry.functions.treasuryEpoch().call() == 2
    assert registry.functions.paid(job).call()
    claim = client.submit(client.prepare_claim(first.address, env.addresses[2]), env.identities[1])
    record(w3.eth.get_transaction_receipt(claim["transaction_hash"]), "claim old epoch")
    assert first.functions.released(env.addresses[2]).call() == 3 * 10**15

    # Keep an unexecuted configuration session; pause revokes its nonce. It may
    # not produce a new commitment/share after another governance action wins.
    pending = propose(prepare(second), "pending reconfiguration")
    assert env.participant("conf_ceremony", 0, "sign-evm", "--session", pending)["status"] == "committed"
    pause = propose(prepare(), "pause")
    call(state.functions.availability(False, True), "halt synthetic state")
    execute(pause)
    assert not registry.functions.enabled().call()
    count = w3.eth.get_transaction_count(env.addresses[1])
    rejected = env.command("dkg", "sign-evm", "--board", env.board, "--role", "conf_ceremony", "--rpc", os.environ["SATOHSI_TEST_RPC"],
        "--identity-file", env.identities[1], "--state", env.homes[1] / "friend/conf_ceremony.enc",
        "--password-file", env.homes[1] / "friend/password", "--session", pending, member=1, expected=(2,))
    assert rejected["code"] == "GAS_CONFIGURATION_CHANGED"
    assert w3.eth.get_transaction_count(env.addresses[1]) == count
    assert coordinator.functions.getSession(pending).call()[11] == 1

    output = os.environ.get("SATOSHIFI_GAS_GOVERNANCE_REPORT")
    if output:
        signing_receipts = []
        for number in range(creation.blockNumber, w3.eth.block_number + 1):
            for transaction in w3.eth.get_block(number, full_transactions=True).transactions:
                if transaction.get("to") in (coordinator.address, auth.address, env.board):
                    receipt = w3.eth.get_transaction_receipt(transaction.hash)
                    assert receipt.status == 1 and receipt.gasUsed <= 2**24
                    signing_receipts.append(dict(hash=Web3.to_hex(receipt.transactionHash),
                        block_number=number, block_hash=Web3.to_hex(receipt.blockHash), gas_used=receipt.gasUsed,
                        target=transaction["to"]))
        Path(output).write_text(json.dumps(dict(status="PASS", production_ready=False,
            mode="REAL_LOCAL_CONFEDERATION_DKG_FROST_SYNTHETIC_SPV_STATE",
            actual_threshold_configuration=True, actual_threshold_rotation=True, actual_threshold_pause=True,
            stale_session_no_private_share=True, state_implementation="GasStateFixture",
            real_zk_proof=False, real_spv_publication=False, registry=registry.address,
            authorization=auth.address, coordinator=coordinator.address, dkg_id=Web3.to_hex(auth.functions.dkgId().call()),
            completed_sessions=[session, rotation, pause], configuration_nonce=registry.functions.configurationNonce().call(),
            binding=binding, receipts=receipts, signatures=signatures, signing_receipts=signing_receipts), indent=2) + "\n")
