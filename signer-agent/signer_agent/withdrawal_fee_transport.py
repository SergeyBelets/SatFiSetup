"""Resume bounded PUBLIC publication; partial bytes never authorize signing."""
from .btc_fee_reserve import read

def publish_intent(board,session,raw,send):
    existing=bytes(read(board,'latest','intents',session))
    if existing:
        if existing!=raw:raise ValueError('WITHDRAWAL_FEE_PUBLIC_INTENT_CONFLICT')
        return
    partial=bytes(read(board,'latest','feeIntentPartial',session))
    _prefix(partial,raw,131072)
    for offset in range(len(partial),len(raw),8192):
        index=offset//8192;send('appendFeeIntent',[session,len(raw),index,raw[offset:offset+8192]],'intent-chunk-'+str(index))
    send('commitFeeIntent',[session],'intent-commit')
    if bytes(read(board,'latest','intents',session))!=raw:raise ValueError('WITHDRAWAL_FEE_PUBLIC_INTENT_CONFLICT')

def publish_plan(board,batch,publisher,raw,send):
    existing=bytes(read(board,'latest','plan',batch))
    if existing:
        if existing!=raw:raise ValueError('BTC_PLAN_CONFLICT')
        return
    partial=bytes(read(board,'latest','feePlanPartial',batch,publisher))
    _prefix(partial,raw,65536)
    for offset in range(len(partial),len(raw),8192):
        index=offset//8192;send('appendFeePlan',[batch,len(raw),index,raw[offset:offset+8192]],'plan-chunk-'+str(index))
    send('commitFeePlan',[batch],'plan-commit')
    if bytes(read(board,'latest','plan',batch))!=raw:raise ValueError('BTC_PLAN_CONFLICT')

def _prefix(partial,raw,limit):
    if (not raw or len(raw)>limit or partial!=raw[:len(partial)] or len(partial)>len(raw)
            or len(partial)<len(raw) and len(partial)%8192):raise ValueError('WITHDRAWAL_FEE_PUBLIC_PARTIAL_CONFLICT')
