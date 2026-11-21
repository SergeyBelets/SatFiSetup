"""Public intent encoding and native derivation, without address activation.

These helpers do not authenticate DKG provenance, recipient eligibility, current
generations, registration, watch coverage or recovery. Never infer Ready from
successful arithmetic. Existing live mint/signing routes remain unchanged.
"""
from dataclasses import dataclass
from hashlib import sha256

from . import crypto

PROFILE = "BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1"
DOMAIN = b"SATOHSI_DEPOSIT_INTENT_V1\0"
BINDING_DOMAIN = b"SATOHSI_DEPOSIT_BINDING_V1\0"
WALLET_DOMAIN = b"SATOHSI_DEPOSIT_WALLET_CONTEXT_V1\0"


def _fixed(value, size):
    if not isinstance(value, bytes) or len(value) != size or value == bytes(size):
        raise ValueError("DEPOSIT_INTENT_BYTES")
    return value


def _uint(value, size):
    if type(value) is not int or not 0 <= value < 1 << (size * 8):
        raise ValueError("DEPOSIT_INTENT_INTEGER")
    return value.to_bytes(size, "big")


def _tagged(tag, data):
    digest = sha256(tag).digest()
    return sha256(digest + digest + data).digest()


@dataclass(frozen=True)
class DepositIntentV1:
    bitcoin_genesis: bytes
    evm_chain_id: int
    deposit_registry: bytes
    pool: bytes
    mp_token: bytes
    custody_graph_id: bytes
    dkg_registry: bytes
    wallet_id: bytes
    wallet_result: bytes
    key_epoch: int
    requester: bytes
    recipient: bytes
    request_nonce: bytes
    service_policy_hash: bytes

    def encode(self):
        if self.evm_chain_id == 0:
            raise ValueError("DEPOSIT_INTENT_CHAIN")
        return b"".join((
            DOMAIN, b"\0\1\0\1", _fixed(self.bitcoin_genesis, 32),
            _uint(self.evm_chain_id, 32), _fixed(self.deposit_registry, 20),
            _fixed(self.pool, 20), _fixed(self.mp_token, 20),
            _fixed(self.custody_graph_id, 32), _fixed(self.dkg_registry, 20),
            _fixed(self.wallet_id, 32), _fixed(self.wallet_result, 32),
            _uint(self.key_epoch, 8), _fixed(self.requester, 20),
            _fixed(self.recipient, 20), _fixed(self.request_nonce, 32),
            _fixed(self.service_policy_hash, 32),
        ))

    @classmethod
    def decode(cls, raw):
        if not isinstance(raw, bytes) or len(raw) != 382 or raw[:30] != DOMAIN + b"\0\1\0\1":
            raise ValueError("DEPOSIT_INTENT_ENCODING")
        intent = cls(raw[30:62], int.from_bytes(raw[62:94], "big"), raw[94:114],
                     raw[114:134], raw[134:154], raw[154:186], raw[186:206],
                     raw[206:238], raw[238:270], int.from_bytes(raw[270:278], "big"),
                     raw[278:298], raw[298:318], raw[318:350], raw[350:382])
        if intent.encode() != raw:
            raise ValueError("DEPOSIT_INTENT_ENCODING")
        return intent

    @property
    def intent_id(self):
        return sha256(self.encode()).digest()

    @property
    def wallet_context(self):
        raw = self.encode()
        return sha256(WALLET_DOMAIN + raw[30:114] + raw[154:278]).digest()

    def base_input_key(self, base_group):
        """Provenance of BASE context must be checked against canonical DKG."""
        if not isinstance(base_group, bytes) or len(base_group) != 33 or base_group[0] != 2:
            raise ValueError("DEPOSIT_BASE_ENCODING")
        return dict(version=1, mode="BASE", base_group=base_group.hex(),
                    wallet_context=self.wallet_context.hex())

    def derive(self, base_group):
        self.base_input_key(base_group)
        raw = self.encode()
        result = crypto.call("bitcoin_derive_deposit_v1", base_group=base_group.hex(), intent=raw.hex())
        # Independent encoding/hash check; elliptic-curve arithmetic is delegated
        # to the pinned native libraries, never reimplemented in Python.
        leaf = b"\x6a\x20" + self.intent_id
        root = _tagged(b"TapLeaf", b"\xc0\x22" + leaf)
        tweak = _tagged(b"TapTweak", base_group[1:] + root)
        try:
            output = bytes.fromhex(result["output_key"])
            parity = result["output_parity_odd"]
            if len(output) != 32 or type(parity) is not bool:
                raise ValueError("descriptor output")
            script = b"\x51\x20" + output
            binding = sha256(BINDING_DOMAIN + raw + base_group + leaf + root + tweak
                             + output + bytes([parity]) + script).digest()
            expected = dict(profile=PROFILE, intent_id=self.intent_id.hex(), base_group=base_group.hex(),
                            leaf_script=leaf.hex(), merkle_root=root.hex(), tweak=tweak.hex(),
                            output_key=output.hex(), output_parity_odd=parity, script_pubkey=script.hex(),
                            binding_commitment=binding.hex(), wallet_context=self.wallet_context.hex(),
                            input_key=dict(version=1, mode="DERIVED", base_group=base_group.hex(),
                                           intent=raw.hex(), binding_commitment=binding.hex(),
                                           wallet_context=self.wallet_context.hex()), ready=False)
            if result != expected or result["ready"] is not False or output == base_group[1:]:
                raise ValueError("descriptor mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("DEPOSIT_DERIVATION_RESPONSE") from exc
        return result
