"""Local provenance for self-computed proofs, not remote hardware attestation.

The private installation key and these records never belong in a distribution.
Cryptographic receipt verification remains mandatory after provenance checks.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


class LocalProofs:
    def __init__(self,config):
        folder=Path(config['directory'])
        info=folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
            raise ValueError('ZK_LOCAL_PROOF_PRIVATE_DIRECTORY')
        path=folder/'.local-proof-key'
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:pass
        else:
            with os.fdopen(fd,'wb') as output:output.write(os.urandom(32));output.flush();os.fsync(output.fileno())
            directory=os.open(folder,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
                raise ValueError('ZK_LOCAL_PROOF_KEY_PERMISSIONS')
            self.key=source.read(33)
        if len(self.key)!=32:raise ValueError('ZK_LOCAL_PROOF_KEY_INVALID')
        self.scope={k:config.get(k) for k in ('chain_id','zk_state','identity_file')}
        from eth_account import Account
        from .onboarding import read_private
        identity=json.loads(read_private(config['identity_file']))
        self.scope['participant']=Account.from_key(identity['private_key']).address

    def payload(self,base,receipt,*,stage=None,report=None,report_sha256=None):
        prep=json.loads((base/'preparation.json').read_text())
        digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        return dict(schema='satoshifi.local-proof-origin.v1',scope=self.scope,stage=stage,
            image_id=prep['program_image_id'],input_sha256=prep['input_sha256'],journal_sha256=prep['journal_sha256'],
            sources_sha256=hashlib.sha256(canonical(prep['source_sha256'])).hexdigest(),
            receipt_sha256=digest(receipt),report_sha256=report_sha256 or (digest(report) if report else None))

    def sign(self,payload):return dict(payload=payload,mac=hmac.new(self.key,canonical(payload),hashlib.sha256).hexdigest())

    def require(self,path,payload):
        if not path.is_file() or path.is_symlink():raise ValueError('ZK_LOCAL_PROOF_ORIGIN_REQUIRED')
        record=json.loads(path.read_text())
        if (record.get('payload')!=payload or not isinstance(record.get('mac'),str)
                or not hmac.compare_digest(record['mac'],self.sign(payload)['mac'])):
            raise ValueError('ZK_LOCAL_PROOF_ORIGIN_MISMATCH')
