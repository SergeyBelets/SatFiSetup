"""Real Rust HPKE/FROST + immutable EVM board batches and durable crash recovery."""

import json
import os
from collections import Counter

import pytest
from eth_account import Account
from web3 import Web3

from signer_agent import crypto, onchain_transport
from signer_agent.dkg import Participant, Registry
from signer_agent.dkg_protocol import Manifest, PROTOCOL_ID
from signer_agent.operations import canonical
from signer_agent.secure_state import SecureState
from test_dkg_processes import RPC, artifact, deploy, tx


def test_onchain_batched_dkg_crash_retry_matches_public_result(tmp_path, monkeypatch, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    registry_contract = deploy(w3, "DKGCoordinator")
    accounts = [Account.from_key(i.to_bytes(32, "big")) for i in (81, 82, 83)]
    members = [a.address for a in accounts]
    transport_keys, identities, states, journals = [], [], [], []
    secrets = set()
    for i, account in enumerate(accounts):
        w3.provider.make_request("anvil_setBalance", [account.address, hex(10**22)])
        home = tmp_path / str(i); home.mkdir(mode=0o700)
        identity = home / "identity.json"
        identity.write_text(json.dumps(dict(private_key=account.key.hex()))); identity.chmod(0o600)
        state = SecureState(home / "state.enc", os.urandom(32).hex())
        transport = crypto.call("transport_key")
        state.put("transport", transport)
        secrets.add(transport["secret"])
        transport_keys.append(bytes.fromhex(transport["public"]))
        identities.append(identity); states.append(state); journals.append(home / "transactions.sqlite")
    session = os.urandom(32)
    manifest = Manifest(31337, registry_contract.address, session, 1, "EVM_AUTH",
                        tuple(members), tuple(transport_keys), 2)
    # Legacy invitation is deliberate: this exercises the transport on existing
    # local boards as well as new V6 enrollments covered by the full CLI lifecycle.
    invite = dict(version="2", chain_id="31337", bitcoin_network="regtest", mode="integration",
                  deployment_id="batching-e2e", operator=members[0], treasury=members[-1],
                  approval_deadline=str(w3.eth.get_block("latest").timestamp + 86400),
                  federation_id="batch-fed", confederation_id="batch-conf", participants=members,
                  threshold="2", epoch="1", protocol_id=PROTOCOL_ID.hex(), phase_duration="3600",
                  sessions=dict(evm_ceremony=session.hex(), conf_ceremony=os.urandom(32).hex(),
                                btc_ceremony=os.urandom(32).hex()))
    a = artifact("DKGOnchainBoard")
    deployment = w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"]["object"]).constructor(canonical(invite).encode(), members)

    def send_operator(function):
        transaction = function.build_transaction(dict(
            chainId=31337, nonce=w3.eth.get_transaction_count(members[0], "pending"), **{"from": members[0]}))
        receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(
            accounts[0].sign_transaction(transaction).raw_transaction))
        assert receipt.status == 1
        return receipt

    receipt = send_operator(deployment)
    board = w3.eth.contract(address=receipt.contractAddress, abi=a["abi"])
    send_operator(board.functions.bindRegistry(registry_contract.address))
    tx(w3, registry_contract.functions.createCeremony(session, members, 2, transport_keys, 1,
                                                      Web3.keccak(text="EVM_AUTH"), PROTOCOL_ID))
    starts = [w3.eth.get_transaction_count(a) for a in members]
    first_block = w3.eth.block_number + 1
    original_transact = onchain_transport.transact
    interrupted = False

    def interrupt_after_mined_batch(*args):
        nonlocal interrupted
        result = original_transact(*args)
        if not interrupted:
            interrupted = True
            raise ValueError("SIMULATED_CRASH_AFTER_MINED_PUBLICATION")
        return result

    monkeypatch.setattr(onchain_transport, "transact", interrupt_after_mined_batch)

    def participant(i):
        r = Registry(RPC, registry_contract.address, accounts[i].key)
        r.identity_file, r.transaction_journal = identities[i], journals[i]
        r.bitcoin_graph = dict(chain_id="31337", bitcoin_network="regtest", evm_confirmations="1")
        mailbox = onchain_transport.OnchainMailbox(r, board.address, identities[i], journals[i])
        return Participant(manifest, r, states[i], mailbox)

    with pytest.raises(ValueError, match="SIMULATED_CRASH_AFTER_MINED_PUBLICATION"):
        participant(0).run_once()
    saved_round1 = states[0].get(manifest.context.hex())
    assert saved_round1["round1"]["secret"]
    assert w3.eth.get_transaction_count(members[0]) - starts[0] == 1
    for _ in range(8):
        statuses = [participant(i).run_once()["status"] for i in range(3)]
        for state in states:
            value = state.get(manifest.context.hex())
            if value.get("round1", {}).get("secret"): secrets.add(value["round1"]["secret"])
            if value.get("result", {}).get("signing_share"): secrets.add(value["result"]["signing_share"])
        if statuses == ["ACTIVE"] * 3: break
    assert statuses == ["ACTIVE"] * 3
    assert states[0].get(manifest.context.hex())["round1"]["package"] == saved_round1["round1"]["package"]
    expected_nonces = [w3.eth.get_transaction_count(a) - start for a, start in zip(members, starts)]
    assert expected_nonces == [6, 6, 6]
    for i in range(3):
        assert participant(i).run_once()["status"] == "ACTIVE"
    assert [w3.eth.get_transaction_count(a) - start for a, start in zip(members, starts)] == [6, 6, 6]
    completed = registry_contract.functions.getCompletedResult(session).call()
    results = [s.get(manifest.context.hex()) for s in states]
    assert all(r["result_hash"] == bytes(completed[1]).hex() for r in results)
    assert len({r["result"]["group"] for r in results}) == 1
    calls = Counter()
    corpus = ""
    for number in range(first_block, w3.eth.block_number + 1):
        for transaction in w3.eth.get_block(number, full_transactions=True).transactions:
            target = board if transaction.to == board.address else registry_contract
            function, arguments = target.decode_function_input(transaction.input)
            calls[function.fn_name] += 1
            corpus += transaction.input.hex()
            if function.fn_name == "publishBatch":
                for payload in arguments["payloads"]:
                    body = json.loads(bytes(payload))
                    assert set(body) in ({"signature"}, {"package", "commitments"}, {"envelope", "hash", "signature"})
                    corpus += bytes(payload).decode()
    assert calls == Counter(publishBatch=6, submitRound1=3, submitShareHashes=3,
                            submitReceiptsAndPublicKeyShare=3, attestResult=3)
    assert all(secret not in corpus for secret in secrets)
    record_property("dkg_batching", canonical(dict(participants=3, threshold=2, transactions=18,
                        transactions_per_participant=6, publication_packets=12,
                        hpke_envelopes=6, crash_after_publication_recovered=True,
                        exact_round1_preserved=True, private_values_checked=len(secrets))))
