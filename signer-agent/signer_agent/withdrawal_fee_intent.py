"""WITHDRAWAL_FEE is a distinct purpose; arithmetic alone never qualifies it."""
from dataclasses import dataclass
from hashlib import sha256
from . import crypto
from .deposit_intent import DepositIntentV1,_fixed
DOMAIN=b'SATOHSI_WITHDRAWAL_FEE_V1\0'
PROFILE='BASE_POST_DKG_EVEN_Y__WITHDRAWAL_FEE_TAPLEAF_V1'

@dataclass(frozen=True)
class WithdrawalFeeIntentV1:
    parent: DepositIntentV1
    request: bytes
    refund_script_hash: bytes
    credits_scope: bytes
    def encode(self):
        return DOMAIN+self.parent.encode()+_fixed(self.request,32)+_fixed(self.refund_script_hash,32)+_fixed(self.credits_scope,32)
    @classmethod
    def decode(cls,raw):
        if not isinstance(raw,bytes) or len(raw)!=len(DOMAIN)+478 or raw[:len(DOMAIN)]!=DOMAIN:raise ValueError('WITHDRAWAL_FEE_INTENT_ENCODING')
        start=len(DOMAIN);value=cls(DepositIntentV1.decode(raw[start:start+382]),raw[start+382:start+414],raw[start+414:start+446],raw[start+446:])
        if value.encode()!=raw:raise ValueError('WITHDRAWAL_FEE_INTENT_ENCODING')
        return value
    @property
    def intent_id(self):return sha256(self.encode()).digest()
    def derive(self,base):
        result=crypto.call('bitcoin_derive_withdrawal_fee_v1',intent=self.encode().hex(),base_group=base.hex())
        if result.get('profile')!=PROFILE or result.get('intent_id')!=self.intent_id.hex() or result.get('ready') is not False:
            raise ValueError('WITHDRAWAL_FEE_DERIVATION_BINDING')
        return result
