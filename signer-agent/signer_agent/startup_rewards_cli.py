"""Inspect/simulate daily introductory payouts; sends only with explicit --send."""
import json
from web3 import Web3
from .safe_errors import DiagnosticArgumentParser
from . import ulr_treasury as p
from .evm_network import read_block
from .dao_client import address, account_at
from .ulr_treasury_cli import _executor


def main(argv=None):
    parser=DiagnosticArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','pay','refund'])
    for key in ('rpc','program','chain-id','evm-confirmations'):parser.add_argument('--'+key,required=True)
    for key in ('day','identity-file','sender','state','id'):parser.add_argument('--'+key)
    parser.add_argument('--send',action='store_true');a=parser.parse_args(argv)
    if a.chain_id not in ('1','11155111','31337'):raise ValueError('ULR_CHAIN_POLICY')
    confirmations=p.number(a.evm_confirmations)
    if not (12 if a.chain_id=='1' else 1)<=confirmations<=256:raise ValueError('ULR_FINALITY_POLICY')
    if a.send and (a.action=='status' or not a.identity_file or not a.state or not a.id):raise ValueError('STARTUP_EXPLICIT_SEND_INPUTS_REQUIRED')
    w3=Web3(Web3.HTTPProvider(a.rpc));block=read_block(w3,confirmations);at=block.number
    if str(w3.eth.chain_id)!=a.chain_id:raise ValueError('ULR_CHAIN_MISMATCH')
    program=p.contract(w3,'ULRStartupRewards',address(a.program))
    read=lambda c,name,*args:p.read(c,at,name,*args)
    staking=p.contract(w3,'ULRStaking',read(program,'staking'))
    p.proven(w3,'ULRStaking',staking.address,at,p.staking_args(staking,at))
    args=[staking.address,read(program,'apyBps'),read(program,'unitPrice'),read(program,'startDay'),
        read(program,'endDay')-read(program,'startDay'),read(program,'refundRecipient')]
    p.proven(w3,'ULRStartupRewards',program.address,at,args)
    if read(program,'rewardToken')!=read(staking,'rewardToken'):raise ValueError('ULR_REWARD_TOKEN_BINDING')
    if a.action=='status':
        token=p.contract(w3,'IERC20Metadata',read(program,'rewardToken'))
        result=dict(program=program.address,staking=staking.address,apy_bps=str(args[1]),unit_price_raw=str(args[2]),
            start_day=str(args[3]),end_day=str(read(program,'endDay')),daily_rate_wad=str(read(program,'dailyRateWad')),
            available_budget_raw=str(read(token,'balanceOf',program.address)),refund_recipient=args[5],unfunded_debt=False)
    else:
        sender=account_at(a.identity_file).address if a.identity_file else address(a.sender)
        pin=dict(chain_id=a.chain_id,snapshots=program.address,confirmations=a.evm_confirmations)
        client=_executor(w3,pin,block,program,kind='ULRStartupRewards')
        method,values=('payDay',[p.number(a.day)]) if a.action=='pay' else ('refundUnused',[])
        result=client.transact('target',method,values,sender,a.identity_file,a.send,a.state,a.id or 'startup-simulation')
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
