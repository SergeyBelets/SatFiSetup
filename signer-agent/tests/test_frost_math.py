from eth_utils import to_checksum_address

from signer_agent.frost_math import (
    NoncePair,
    SECP256K1_ORDER,
    add_points,
    aggregate_nonce,
    bip340_challenge,
    binding_factor,
    build_transcript,
    lagrange_coefficient,
    negate_point,
    participant_nonce,
    random_nonce_pair,
    scalar_base_mult,
    scalar_mult,
    signature_share,
    verify_signature_share,
)


def _even_group_secret() -> int:
    value = 7
    while scalar_base_mult(value).y & 1:
        value += 1
    return value


def test_enforced_frost_math_matches_coordinator_equation_with_parity_normalization():
    group_secret = _even_group_secret()
    slope = 11
    signing_shares = {
        1: (group_secret + slope) % SECP256K1_ORDER,
        2: (group_secret + 2 * slope) % SECP256K1_ORDER,
    }
    participants = [
        to_checksum_address("0x0000000000000000000000000000000000001001"),
        to_checksum_address("0x0000000000000000000000000000000000001002"),
    ]
    coordinator = to_checksum_address("0x0000000000000000000000000000000000002001")
    session_id = 42
    message_hash = bytes.fromhex("ab" * 32)
    group_pubkey = scalar_base_mult(group_secret).x.to_bytes(32, "big")

    for candidate in range(1, 100):
        nonce1 = NoncePair(candidate, candidate + 1)
        nonce2 = NoncePair(candidate + 2, candidate + 3)
        commitments = {
            participants[0]: (nonce1.hiding_point, nonce1.binding_point),
            participants[1]: (nonce2.hiding_point, nonce2.binding_point),
        }
        transcript = build_transcript(
            11155111,
            coordinator,
            session_id,
            group_pubkey,
            message_hash,
            participants,
            commitments,
        )
        aggregate = aggregate_nonce(transcript, session_id, participants, commitments)
        if aggregate.y & 1:
            break
    assert aggregate.y & 1
    normalized_aggregate = negate_point(aggregate)
    assert normalized_aggregate.y % 2 == 0

    challenge = bip340_challenge(
        aggregate.x, int.from_bytes(group_pubkey, "big"), message_hash
    )
    shares = []
    for index, participant in enumerate(participants, start=1):
        nonce = nonce1 if index == 1 else nonce2
        rho = binding_factor(transcript, session_id, participant)
        lagrange = lagrange_coefficient(participant, participants, commitments.keys())
        share = signature_share(
            signing_shares[index], nonce, rho, lagrange, challenge, negate_nonce=True
        )
        assert verify_signature_share(
            share,
            scalar_base_mult(signing_shares[index]),
            participant_nonce(
                nonce.hiding_point, nonce.binding_point, rho, negate=True
            ),
            lagrange,
            challenge,
        )
        shares.append(share)

    aggregate_share = sum(shares) % SECP256K1_ORDER
    expected = add_points(
        [normalized_aggregate, scalar_mult(scalar_base_mult(group_secret), challenge)]
    )
    assert scalar_base_mult(aggregate_share) == expected


def test_lagrange_coefficients_reconstruct_group_secret():
    secret = 19
    slope = 31
    participants = [
        to_checksum_address("0x0000000000000000000000000000000000001001"),
        to_checksum_address("0x0000000000000000000000000000000000001002"),
        to_checksum_address("0x0000000000000000000000000000000000001003"),
    ]
    committed = participants[:2]
    shares = [secret + slope, secret + 2 * slope]
    reconstructed = (
        sum(
            lagrange_coefficient(participant, participants, committed) * share
            for participant, share in zip(committed, shares)
        )
        % SECP256K1_ORDER
    )
    assert reconstructed == secret


def test_shared_evm_golden_transcripts_shares_and_bip340():
    import json
    from pathlib import Path
    from coincurve import PublicKeyXOnly
    from signer_agent import crypto

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[2] / "tests/vectors/signing-v1.json"
        ).read_text()
    )
    for vector in fixture["vectors"]:
        commitments = {}
        nonces = {}
        for signer in vector["signers"]:
            inputs = {k: int(v, 16) for k, v in signer["inputs"].items()}
            nonce = NoncePair(inputs["hiding"], inputs["binding"])
            nonces[signer["participant"]] = nonce
            commitments[signer["participant"]] = (
                nonce.hiding_point,
                nonce.binding_point,
            )
            assert [
                str(nonce.hiding_point.x),
                str(nonce.hiding_point.y),
                str(nonce.binding_point.x),
                str(nonce.binding_point.y),
            ] == signer["commitment"]
        transcript = build_transcript(
            int(vector["chain_id"]),
            vector["coordinator"],
            int(vector["session"]),
            bytes.fromhex(vector["group"]),
            bytes.fromhex(vector["message"]),
            vector["participants"],
            commitments,
        )
        assert transcript.hex() == vector["transcript"]
        aggregate = aggregate_nonce(
            transcript, int(vector["session"]), vector["participants"], commitments
        )
        assert bool(aggregate.y & 1) == vector["negate_nonce"]
        normalized = negate_point(aggregate) if vector["negate_nonce"] else aggregate
        assert [str(normalized.x), str(normalized.y)] == vector["aggregate"]
        challenge = bip340_challenge(
            aggregate.x, int(vector["group"], 16), bytes.fromhex(vector["message"])
        )
        scalars = []
        for signer in vector["signers"]:
            p = signer["participant"]
            inputs = signer["inputs"]
            rho = binding_factor(transcript, int(vector["session"]), p)
            lagrange = lagrange_coefficient(p, vector["participants"], commitments)
            assert "%064x" % rho == inputs["rho"]
            assert "%064x" % lagrange == inputs["lagrange"]
            assert "%064x" % challenge == inputs["challenge"]
            z = signature_share(
                int(inputs["share"], 16),
                nonces[p],
                rho,
                lagrange,
                challenge,
                vector["negate_nonce"],
            )
            assert "%064x" % z == signer["z"]
            assert (
                crypto.call(
                    "evm_share_v1", **inputs, negate_nonce=vector["negate_nonce"]
                )["share"]
                == signer["z"]
            )
            scalars.append(z)
        signature = aggregate.x.to_bytes(32, "big") + (
            sum(scalars) % SECP256K1_ORDER
        ).to_bytes(32, "big")
        assert signature.hex() == vector["signature"]
        verifier = PublicKeyXOnly(bytes.fromhex(vector["group"]))
        assert verifier.verify(signature, bytes.fromhex(vector["message"]))
        assert not verifier.verify(signature, b"\x00" * 32)
