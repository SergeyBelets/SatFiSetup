"""Generated bootstrap hashes are 0x-prefixed; private guards compare their bytes."""
from types import SimpleNamespace as NS
import pytest
from signer_agent import btc_fee_policy,withdrawal_fee_policy


@pytest.mark.parametrize('module,field',[(btc_fee_policy,'btc_fee_binding'),(withdrawal_fee_policy,'withdrawal_fee_binding')])
@pytest.mark.parametrize('prefix',['','0x'])
def test_same_binding_hash_encoding_keeps_both_private_boundary_reviews(module,field,prefix,monkeypatch):
    calls=[];target='0x'+'11'*20;digest=b'a'*32
    monkeypatch.setattr(module,'review_binding',lambda w3,b,n:calls.append(n))
    client=NS(w3=NS(),_read_snapshot=NS(number=10),check_read_snapshot=lambda:calls.append('stable'))
    setattr(client,field,dict(target=target,operation_hash=prefix+digest.hex()))
    module.guard_signing_session(client,NS(origin_contract=target,message_hash=digest))
    assert calls==[10,'latest','stable']
    getattr(client,field)['operation_hash']='0x'+'bb'*32
    calls.clear()
    with pytest.raises(ValueError,match='HASH_MISMATCH'):
        module.guard_signing_session(client,NS(origin_contract=target,message_hash=digest))
    assert not calls
