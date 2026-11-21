import hashlib
import secrets
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Sequence, Tuple

from coincurve import PublicKey
from eth_abi import encode
from eth_utils import keccak, to_checksum_address


SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class NoncePair:
    hiding: int
    binding: int

    @property
    def hiding_point(self) -> Point:
        return scalar_base_mult(self.hiding)

    @property
    def binding_point(self) -> Point:
        return scalar_base_mult(self.binding)


def _scalar_bytes(value: int) -> bytes:
    if value <= 0 or value >= SECP256K1_ORDER:
        raise ValueError("scalar must be in [1, secp256k1_order)")
    return value.to_bytes(32, "big")


def _public_key(point: Point) -> PublicKey:
    prefix = b"\x03" if point.y & 1 else b"\x02"
    return PublicKey(prefix + point.x.to_bytes(32, "big"))


def _point(public_key: PublicKey) -> Point:
    raw = public_key.format(compressed=False)
    return Point(int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:65], "big"))


def scalar_base_mult(value: int) -> Point:
    return _point(PublicKey.from_valid_secret(_scalar_bytes(value)))


def scalar_mult(point: Point, value: int) -> Point:
    return _point(_public_key(point).multiply(_scalar_bytes(value)))


def add_points(points: Iterable[Point]) -> Point:
    keys = [_public_key(point) for point in points]
    if not keys:
        raise ValueError("at least one point is required")
    return _point(PublicKey.combine_keys(keys))


def negate_point(point: Point) -> Point:
    return Point(point.x, (-point.y) % SECP256K1_FIELD)


def random_nonce_pair() -> NoncePair:
    return NoncePair(
        hiding=secrets.randbelow(SECP256K1_ORDER - 1) + 1,
        binding=secrets.randbelow(SECP256K1_ORDER - 1) + 1,
    )


def build_transcript(
    chain_id: int,
    coordinator_address: str,
    session_id: int,
    group_pubkey: bytes,
    message_hash: bytes,
    participants: Sequence[str],
    commitments: Mapping[str, Tuple[Point, Point]],
) -> bytes:
    if len(message_hash) != 32:
        raise ValueError("message_hash must be 32 bytes")

    transcript = keccak(
        encode(
            ["string", "uint256", "address", "uint256", "bytes", "bytes32"],
            [
                "SATOHSI_FROST_NONCE_TRANSCRIPT_V1",
                chain_id,
                to_checksum_address(coordinator_address),
                session_id,
                group_pubkey,
                message_hash,
            ],
        )
    )
    normalized = {to_checksum_address(address): value for address, value in commitments.items()}
    for participant in participants:
        address = to_checksum_address(participant)
        pair = normalized.get(address)
        if pair is None:
            continue
        hiding, binding = pair
        transcript = keccak(
            encode(
                ["bytes32", "address", "uint256", "uint256", "uint256", "uint256"],
                [transcript, address, hiding.x, hiding.y, binding.x, binding.y],
            )
        )
    return transcript


def binding_factor(transcript: bytes, session_id: int, signer: str) -> int:
    value = int.from_bytes(
        keccak(
            encode(
                ["string", "uint256", "bytes32", "address"],
                [
                    "SATOHSI_FROST_BINDING_FACTOR_V1",
                    session_id,
                    transcript,
                    to_checksum_address(signer),
                ],
            )
        ),
        "big",
    ) % SECP256K1_ORDER
    return value or 1


def participant_nonce(hiding: Point, binding: Point, rho: int, negate: bool = False) -> Point:
    point = add_points([hiding, scalar_mult(binding, rho)])
    return negate_point(point) if negate else point


def aggregate_nonce(
    transcript: bytes,
    session_id: int,
    participants: Sequence[str],
    commitments: Mapping[str, Tuple[Point, Point]],
) -> Point:
    normalized = {to_checksum_address(address): value for address, value in commitments.items()}
    points: List[Point] = []
    for participant in participants:
        address = to_checksum_address(participant)
        pair = normalized.get(address)
        if pair is None:
            continue
        points.append(participant_nonce(pair[0], pair[1], binding_factor(transcript, session_id, address)))
    return add_points(points)


def lagrange_coefficient(signer: str, participants: Sequence[str], committed: Iterable[str]) -> int:
    signer = to_checksum_address(signer)
    indexes = {to_checksum_address(address): index + 1 for index, address in enumerate(participants)}
    committed_set = {to_checksum_address(address) for address in committed}
    xi = indexes.get(signer)
    if xi is None:
        raise ValueError("signer is not a session participant")

    coefficient = 1
    for other in participants:
        other = to_checksum_address(other)
        if other == signer or other not in committed_set:
            continue
        xj = indexes[other]
        numerator = (-xj) % SECP256K1_ORDER
        denominator = (xi - xj) % SECP256K1_ORDER
        coefficient = (
            coefficient * numerator * pow(denominator, SECP256K1_ORDER - 2, SECP256K1_ORDER)
        ) % SECP256K1_ORDER
    return coefficient


def bip340_challenge(aggregate_nonce_x: int, group_pubkey_x: int, message_hash: bytes) -> int:
    tag = hashlib.sha256(b"BIP0340/challenge").digest()
    digest = hashlib.sha256(
        tag
        + tag
        + aggregate_nonce_x.to_bytes(32, "big")
        + group_pubkey_x.to_bytes(32, "big")
        + message_hash
    ).digest()
    return int.from_bytes(digest, "big") % SECP256K1_ORDER


def signature_share(
    signing_share: int,
    nonces: NoncePair,
    rho: int,
    lagrange: int,
    challenge: int,
    negate_nonce: bool = False,
) -> int:
    nonce_scalar = (nonces.hiding + rho * nonces.binding) % SECP256K1_ORDER
    if negate_nonce:
        nonce_scalar = -nonce_scalar % SECP256K1_ORDER
    return (nonce_scalar + challenge * lagrange * signing_share) % SECP256K1_ORDER


def verify_signature_share(
    share: int,
    public_key_share: Point,
    participant_nonce_point: Point,
    lagrange: int,
    challenge: int,
) -> bool:
    left = scalar_base_mult(share)
    right = add_points(
        [participant_nonce_point, scalar_mult(public_key_share, challenge * lagrange % SECP256K1_ORDER)]
    )
    return left == right


def parse_signing_share(value: str) -> int:
    normalized = value.removeprefix("0x")
    if len(normalized) != 64:
        raise ValueError("FROST_SIGNING_SHARE_HEX must contain one 32-byte scalar")
    scalar = int(normalized, 16)
    _scalar_bytes(scalar)
    return scalar
