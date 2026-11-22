import hashlib,json,sqlite3,time
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
root=Path('/Users/dmitrysubbotin/Downloads/satohsi_test_repo 4')
p=sorted(Path('/private/tmp').glob('pytest-of-*/pytest-*/test_canonical_mint*/bootstrap.sqlite'), key=lambda p:p.stat().st_mtime)[-1].resolve()
with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as db:
 request, result=db.execute('select request,result from operations where id=?',('bitcoin-onchain-e2e',)).fetchone()
 profile=json.loads(request)['profile']
 manifest=json.loads(result)
 step_raw, receipt_raw=db.execute('select request,result from operations where id=?',('bitcoin-onchain-e2e:mint_admission',)).fetchone()
step=json.loads(step_raw)
assert urlparse(profile['evm_rpc']).hostname in ('127.0.0.1','localhost')
instance=Bootstrap.__new__(Bootstrap)
instance.profile=profile
instance.contracts=manifest['contracts']
instance.values=manifest['values']
instance.w3=Web3(Web3.HTTPProvider(profile['evm_rpc']))
assert instance.w3.eth.chain_id==31337
board=instance.contract('mint_admission')
for _ in range(450):
 current=board.functions.operator().call()
 if current!=profile['operator']:break
 time.sleep(2)
else:raise RuntimeError('TRANSFER_NOT_OBSERVED')
receipt=instance.w3.eth.get_transaction_receipt(json.loads(receipt_raw)['tx_hash'])
try:
 instance.verify_checks(step,receipt)
except ValueError as exc:
 assert str(exc)=='BINDING_CHECK_FAILED'
else:raise AssertionError('OLD_PLAN_UNEXPECTEDLY_MATCHED_MUTATED_OPERATOR')
updated=deepcopy(step)
for check in updated['checks']:
 if check['method']=='operator':check['at_receipt']=True
instance.verify_checks(updated,receipt)
block=instance.w3.eth.get_block('latest')
result=dict(status='PASS',scope='actual deployed admission after legitimate threshold operator transfer; historical initial operator plus current immutable pool check',chain_id=31337,block_number=block.number,block_hash=block.hash.hex(),receipt_block=receipt.blockNumber,receipt_hash=receipt.blockHash.hex(),initial_operator=profile['operator'],current_operator=current,old_plan_status='BINDING_CHECK_FAILED',revised_plan_status='PASS',checks=updated['checks'],bootstrap_runtime_sha256=hashlib.sha256((root/'signer-agent/signer_agent/bootstrap.py').read_bytes()).hexdigest())
(root/'ops/artifacts/zkspv/mint-admission-receipt-check.json').write_text(json.dumps(result,indent=2)+'\n')
print('PASS: actual post-transfer receipt-bound operator check')
