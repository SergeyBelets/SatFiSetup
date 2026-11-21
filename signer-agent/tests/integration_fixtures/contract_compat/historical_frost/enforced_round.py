"""Generate two test shares and verify them on the original run-04 contracts."""
import json
from pathlib import Path

from coincurve import PublicKeyXOnly
from web3 import Web3

from signer_agent.frost_math import (
    SECP256K1_ORDER, NoncePair, aggregate_nonce, binding_factor, bip340_challenge,
    build_transcript, lagrange_coefficient, scalar_base_mult, signature_share,
)

DEFAULT_ARTIFACTS = (
    Path(__file__).resolve().parents[5] / "rust/zkspv/deposit-integration/evidence/run-04/consumer-artifacts"
)
ZERO_ADDRESS = "0x" + "00" * 20


def setup_round(w3, artifacts):
    owner, first, second = w3.eth.accounts[:3]
    participants = [first, second]
    message = Web3.keccak(text="historical FROST local acceptance")

    def deploy(name, *args):
        artifact = json.loads((artifacts / (name + ".json")).read_text())
        factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
        tx = factory.constructor(*args).transact({"from": owner, "gas": 15_000_000})
        receipt = w3.eth.get_transaction_receipt(tx)
        assert receipt.status == 1, name
        return w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    def send(call, sender=owner):
        tx = call.transact({"from": sender, "gas": 25_000_000})
        assert w3.eth.get_transaction_receipt(tx).status == 1, call.fn_name

    verifier = deploy("FROSTVerifier")
    coordinator = deploy("FROSTCoordinator", verifier.address)
    api = coordinator.functions
    assert api.defaultVerifier().call() == verifier.address

    # Public, deterministic fixture values, recreated separately on each disposable chain.
    group_secret = 7
    while scalar_base_mult(group_secret).y & 1:
        group_secret += 1
    group = scalar_base_mult(group_secret).x.to_bytes(32, "big")
    for participant in participants:
        send(api.addCustodian(participant))
    send(api.grantRole(api.SESSION_CREATOR_ROLE().call(), owner))
    deadline = w3.eth.get_block("latest").timestamp + 20_000
    create = api.createSession(group, participants, 2, deadline, True, ZERO_ADDRESS, 0,
                               (owner, 0, 0, bytes(32)))
    session = create.call({"from": owner})
    send(create)
    assert api.getSession(session).call()[8] is True
    send(api.bindMessageHash(session, message))

    commitments, private_shares, nonces = {}, {}, {}
    for index, participant in enumerate(participants, start=1):
        private_shares[participant] = group_secret + 11 * index
        point = scalar_base_mult(private_shares[participant])
        send(api.registerParticipantPublicKeyShare(session, participant, point.x, point.y))
        pair = NoncePair(7 * index, 7 * index + 1)
        nonces[participant] = pair
        hiding, binding = pair.hiding_point, pair.binding_point
        commitments[participant] = hiding, binding
        send(api.submitNonceCommitments(session, hiding.x, hiding.y, binding.x, binding.y), participant)

    transcript = build_transcript(w3.eth.chain_id, coordinator.address, session, group, message,
                                  participants, commitments)
    nonce = aggregate_nonce(transcript, session, participants, commitments)
    challenge = bip340_challenge(nonce.x, int.from_bytes(group, "big"), message)
    scalars = []
    for participant in participants:
        scalar = signature_share(
            private_shares[participant], nonces[participant], binding_factor(transcript, session, participant),
            lagrange_coefficient(participant, participants, commitments), challenge, negate_nonce=bool(nonce.y & 1),
        )
        send(api.submitVerifiableSignatureShare(session, scalar), participant)
        assert api.getVerifiedShareStatus(session, participant).call() == [True, scalar]
        scalars.append(scalar)

    assert api.getNonceSetStatus(session).call()[4] == 2
    signature = nonce.x.to_bytes(32, "big") + (sum(scalars) % SECP256K1_ORDER).to_bytes(32, "big")
    assert PublicKeyXOnly(group).verify(signature, message)
    invalid = bytes([signature[0] ^ 1]) + signature[1:]
    failed = api.finalizeSession(session, invalid, message).transact({"from": first, "gas": 4_000_000})
    assert w3.eth.get_transaction_receipt(failed).status == 0
    assert not api.isFinalized(session).call()

    details = dict(session_id=session, verified_shares=2, independent_bip340_verify=True, invalid_signature_receipt=0)
    return coordinator, session, message, signature, details
