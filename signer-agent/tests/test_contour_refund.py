"""Named BTC refunds preserve the original principal and original per-version owner consent."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from ops.zkspv.contour_refund import RefundOwner,quote
from ops.zkspv.contour import digest
from signer_agent.withdrawal_user import Q,ZERO
from test_withdrawal_user import workflow
from test_address_mint import Functions,address


@pytest.fixture
def named(workflow,monkeypatch):
    w,_,storage,ctx=workflow;original=deepcopy(storage['request']);refund=deepcopy(original)
    refund[0]=refund[2]=ZERO;refund[4]=500*Q
    ctx['escrow'].functions=Functions(dict(getRequest=lambda key:original if bytes(key)==b'o'*32 else refund))
    ctx['withdrawal_fees']['credits'].functions=Functions(dict(refundRequest=lambda _:True,refundOrigin=lambda _:b'o'*32,getBudget=lambda _:storage['budget']))
    monkeypatch.setattr(RefundOwner,'identity',lambda self:w.identity())
    q=quote(w.i,(b'r'*32).hex());r=RefundOwner(w.i);r.validate_quote(q,digest(q))
    return NS(r=r,q=q,original=original,refund=refund,ctx=ctx,storage=storage)


def test_named_refund_adopts_existing_owner_request_without_creating_a_token_primary(named):
    f=named;result=f.r.submit(f.q,digest(f.q))
    assert result['status']=='EXISTING_OWNER_REFUND_REQUEST' and result['cost']['principal_sats']=='500'
    assert f.q['original_principal_sats']=='10000' and f.original[4]==10000*Q
    assert f.r.status(f.q['operation_id'])['primary_state']=='PENDING'
    assert f.original[4]==10000*Q


@pytest.mark.parametrize('mutation',['principal','refund_amount','owner','script','origin','not_refund','source','token','graph','anchor','confirmation_hash'])
def test_refund_right_owner_graph_and_both_principals_are_bound(named,mutation):
    f=named;q=deepcopy(f.q);accepted=None
    if mutation=='principal':f.original[4]+=Q
    if mutation=='refund_amount':f.refund[4]+=Q
    if mutation=='owner':f.refund[3]=address(99)
    if mutation=='script':f.refund[5]=b'other'
    if mutation=='origin':f.ctx['withdrawal_fees']['credits'].functions.values['refundOrigin']=lambda _:b'x'*32
    if mutation=='not_refund':f.ctx['withdrawal_fees']['credits'].functions.values['refundRequest']=lambda _:False
    if mutation=='source':f.refund[0]=address(99)
    if mutation=='token':f.refund[2]=address(99)
    if mutation=='graph':q['graph']['reserve']=address(99)
    if mutation=='anchor':q['anchor']['hash']='00'*32
    if mutation=='confirmation_hash':accepted='00'*32
    with pytest.raises(ValueError):f.r.submit(q,accepted or digest(q))
    assert f.original[4]==(10001 if mutation=='principal' else 10000)*Q


def test_refund_uses_inherited_exact_version_approval_and_original_owner_sdk(named,monkeypatch):
    f=named;calls=[];exact=dict(request=f.q['operation_id'],principal_sats='500',revision='4',terms_hash='aa'*32)
    monkeypatch.setattr('signer_agent.withdrawal_fee_client.quote',lambda *args:exact)
    f.r.owner=NS(call=lambda method,args:calls.append((method,args)) or dict(status='OWNER_CALL'))
    assert f.r.approve_version(f.q['operation_id'],b'b'*32,'aa'*32)['status']=='OWNER_CALL'
    assert calls==[('approveVersion',['0x'+(b'b'*32).hex(),'0x'+f.q['operation_id'],'4','0x'+'aa'*32])]
    with pytest.raises(ValueError):f.r.approve_version(f.q['operation_id'],b'b'*32,'cc'*32)
    assert len(calls)==1 and f.original[4]==10000*Q


@pytest.mark.parametrize('state,expected',[(1,'PENDING'),(2,'BOUND'),(3,'PAID'),(4,'REFUNDED')])
def test_original_escrow_state_is_observed_without_mutation(named,state,expected):
    f=named;f.refund[7]=state
    assert f.r.status(f.q['operation_id'])['primary_state']==expected
    assert f.original[4]==10000*Q
