"""DKG v1 wire hashes. All preimages are Ethereum ABI, never JSON."""
from dataclasses import dataclass
from typing import Sequence
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from coincurve import PublicKey

PROTOCOL_NAME = 'SATOHSI_DKG_FROST_SECP256K1_TR_2_2_0_POSTCARD_HPKE_X25519_CHACHA_V1'
PROTOCOL_ID = keccak(text=PROTOCOL_NAME)
PURPOSES = {name: keccak(text=name) for name in ('EVM_AUTH', 'BTC_SPEND')}
TAGS = {name: keccak(text='SATOHSI_DKG_' + name + '_V1') for name in (
    'CONTEXT', 'VSS_COMMITMENT', 'ROUND1_PACKAGE', 'HPKE_INFO', 'HPKE_AAD', 'ENCRYPTED_SHARE', 'RESULT')}
MAX_PARTICIPANTS = 16


def digest(types, values):
    return keccak(encode(types, values))


def raw(value, size=None):
    value = bytes.fromhex(value.removeprefix('0x')) if isinstance(value, str) else bytes(value)
    if size is not None and len(value) != size:
        raise ValueError('INVALID_BYTE_LENGTH')
    return value


@dataclass(frozen=True)
class Manifest:
    chain_id: int
    registry: str
    session_id: bytes
    epoch: int
    purpose: str
    participants: tuple
    transport_keys: tuple
    threshold: int
    protocol_id: bytes = PROTOCOL_ID

    def __post_init__(self):
        if self.chain_id <= 0 or not 0 <= self.epoch < 2**64:
            raise ValueError('INVALID_CONTEXT')
        if self.protocol_id != PROTOCOL_ID or self.purpose not in PURPOSES:
            raise ValueError('UNSUPPORTED_PROTOCOL')
        if not 2 <= self.threshold <= len(self.participants) <= MAX_PARTICIPANTS:
            raise ValueError('INVALID_THRESHOLD')
        addresses = tuple(to_checksum_address(x) for x in self.participants)
        if len(set(addresses)) != len(addresses) or int(self.registry, 16) == 0 or any(int(x, 16) == 0 for x in addresses):
            raise ValueError('INVALID_ROSTER')
        keys = tuple(raw(x, 32) for x in self.transport_keys)
        if len(keys) != len(addresses) or len(set(keys)) != len(keys) or bytes(32) in keys:
            raise ValueError('INVALID_TRANSPORT_KEYS')
        object.__setattr__(self, 'participants', addresses)
        object.__setattr__(self, 'registry', to_checksum_address(self.registry))
        object.__setattr__(self, 'session_id', raw(self.session_id, 32))
        object.__setattr__(self, 'transport_keys', keys)
        if self.session_id == bytes(32):
            raise ValueError('INVALID_SESSION')

    @classmethod
    def from_dict(cls, data):
        return cls(int(data['chain_id']), data['registry'], raw(data['session_id'], 32),
                   int(data['epoch']), data['purpose'], tuple(data['participants']),
                   tuple(data['transport_keys']), int(data['threshold']), raw(data['protocol_id'], 32))

    def to_dict(self):
        return dict(chain_id=str(self.chain_id), registry=self.registry, session_id=self.session_id.hex(),
                    epoch=self.epoch, purpose=self.purpose, participants=list(self.participants),
                    transport_keys=[x.hex() for x in self.transport_keys], threshold=self.threshold,
                    protocol_id=self.protocol_id.hex())

    @property
    def preimage(self):
        return encode(['bytes32','uint256','address','bytes32','uint64','bytes32','bytes32',
                       'bytes32','bytes32','uint16','uint16'],
                      [TAGS['CONTEXT'],self.chain_id,self.registry,self.session_id,self.epoch,
                       PURPOSES[self.purpose],self.protocol_id,
                       digest(['address[]'],[self.participants]),digest(['bytes32[]'],[self.transport_keys]),
                       len(self.participants),self.threshold])

    @property
    def context(self):
        return keccak(self.preimage)

    def index(self, address):
        return self.participants.index(to_checksum_address(address)) + 1


def commitment(context, sender, coefficient_index, point):
    point = raw(point, 33)
    if PublicKey(point).format(compressed=True) != point:
        raise ValueError('NONCANONICAL_POINT')
    return digest(['bytes32','bytes32','address','uint32','bytes'],
                  [TAGS['VSS_COMMITMENT'],context,sender,coefficient_index,point])


def round1(context, sender, package):
    return digest(['bytes32','bytes32','address','bytes'],[TAGS['ROUND1_PACKAGE'],context,sender,package])


def hpke_context(context, sender, recipient):
    if sender.lower() == recipient.lower():
        raise ValueError('SELF_SHARE')
    return (encode(['bytes32','bytes32','address','address'],[TAGS['HPKE_INFO'],context,sender,recipient]),
            encode(['bytes32','bytes32','uint8','address','address','uint64'],
                   [TAGS['HPKE_AAD'],context,2,sender,recipient,0]))


def envelope(context, sender, recipient, enc, ciphertext):
    hpke_context(context, sender, recipient)
    return encode(['uint16','bytes32','uint8','address','address','uint64','bytes','bytes'],
                  [1,context,2,sender,recipient,0,raw(enc,32),ciphertext])


def share(context, sender, recipient, envelope_bytes):
    return digest(['bytes32','bytes32','address','address','bytes'],
                  [TAGS['ENCRYPTED_SHARE'],context,sender,recipient,envelope_bytes])


def result(manifest, round1_hashes, share_hashes, negated, group_x, public_shares):
    n = len(manifest.participants)
    if len(round1_hashes) != n or len(share_hashes) != n*(n-1) or len(public_shares) != n:
        raise ValueError('INCOMPLETE_TRANSCRIPT')
    for point in public_shares:
        if len(point) != 33 or PublicKey(point).format(compressed=True) != point:
            raise ValueError('INVALID_PUBLIC_SHARE')
    PublicKey(b'\x02' + raw(group_x,32))
    return digest(['bytes32','bytes32','bytes32','bytes32','bool','bytes32','bytes32'],
                  [TAGS['RESULT'],manifest.context,digest(['bytes32[]'],[round1_hashes]),
                   digest(['bytes32[]'],[share_hashes]),negated,group_x,
                   digest(['address[]','bytes[]'],[manifest.participants,public_shares])])


def typed_message(manifest, kind, **fields):
    definitions = {
        'DKGTransportKey': [('contextHash','bytes32'),('participant','address'),('transportPublicKey','bytes32')],
        'DKGEnvelope': [('contextHash','bytes32'),('sender','address'),('recipient','address'),('shareHash','bytes32')],
        'DKGReceipt': [('contextHash','bytes32'),('sender','address'),('recipient','address'),('shareHash','bytes32'),('verified','bool')],
    }
    return dict(domain=dict(name='Satohsi DKG Transport', version='1', chainId=manifest.chain_id,
                            verifyingContract=manifest.registry),
                types={kind:[dict(name=n,type=t) for n,t in definitions[kind]]},
                primaryType=kind,message=dict(contextHash=manifest.context,**fields))
