"""Generate PUBLIC synthetic ABI preimages. Never use these keys or identities with funds."""
import json
from pathlib import Path
from eth_abi import encode
from eth_utils import keccak
from coincurve import PublicKey
from signer_agent.dkg_protocol import Manifest, TAGS, PROTOCOL_ID, hpke_context
p=['0x'+'11'*20,'0x'+'22'*20,'0x'+'33'*20]
m=Manifest(31337,'0x'+'44'*20,bytes.fromhex('55'*32),7,'EVM_AUTH',tuple(p),tuple(bytes([x])*32 for x in (1,2,3)),2)
rows=[]
def add(name,types,values):
    pre=encode(types,values)
    def ser(v):
        if isinstance(v,bytes):return '0x'+v.hex()
        if isinstance(v,(list,tuple)):return [ser(x) for x in v]
        if isinstance(v,int) and not isinstance(v,bool):return str(v)
        return v
    rows.append(dict(name=name,types=types,values=[ser(v) for v in values],preimage=pre.hex(),hash=keccak(pre).hex()))
add('context',['bytes32','uint256','address','bytes32','uint64','bytes32','bytes32','bytes32','bytes32','uint16','uint16'],
    list(__import__('eth_abi').decode(['bytes32','uint256','address','bytes32','uint64','bytes32','bytes32','bytes32','bytes32','uint16','uint16'],m.preimage)))
points=[PublicKey.from_valid_secret(i.to_bytes(32,'big')).format() for i in (1,6)]
for i,point in enumerate(points):add('vss_'+str(i),['bytes32','bytes32','address','uint32','bytes'],[TAGS['VSS_COMMITMENT'],m.context,p[0],i,point])
add('round1',['bytes32','bytes32','address','bytes'],[TAGS['ROUND1_PACKAGE'],m.context,p[0],b'synthetic-public-package-not-a-valid-DKG'])
info,aad=hpke_context(m.context,p[0],p[1])
add('hpke_info',['bytes32','bytes32','address','address'],[TAGS['HPKE_INFO'],m.context,p[0],p[1]])
add('hpke_aad',['bytes32','bytes32','uint8','address','address','uint64'],[TAGS['HPKE_AAD'],m.context,2,p[0],p[1],0])
wire=encode(['uint16','bytes32','uint8','address','address','uint64','bytes','bytes'],[1,m.context,2,p[0],p[1],0,bytes.fromhex('aa'*32),bytes.fromhex('bb'*48)])
add('envelope',['uint16','bytes32','uint8','address','address','uint64','bytes','bytes'],[1,m.context,2,p[0],p[1],0,bytes.fromhex('aa'*32),bytes.fromhex('bb'*48)])
add('share',['bytes32','bytes32','address','address','bytes'],[TAGS['ENCRYPTED_SHARE'],m.context,p[0],p[1],wire])
add('result',['bytes32','bytes32','bytes32','bytes32','bool','bytes32','bytes32'],[TAGS['RESULT'],m.context,keccak(encode(['bytes32[]'],[[bytes([i])*32 for i in (1,2,3)]])),keccak(encode(['bytes32[]'],[[bytes([i])*32 for i in range(1,7)]])),True,points[0][1:],keccak(encode(['address[]','bytes[]'],[p,[points[0],points[1],points[0]]]))])
Path('tests/vectors/dkg-v1.json').write_text(json.dumps(dict(protocol_id=PROTOCOL_ID.hex(),manifest=m.to_dict(),vectors=rows),indent=2)+'\n')
