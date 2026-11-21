"""Local EVM: named issuance survives restart and signer checks actual ERC20 metadata."""
import json
from pathlib import Path
import pytest
from signer_agent.bootstrap_tokens import check
from signer_agent.bootstrap_distribution import STATUS
from test_dkg_processes import deploy
from test_ulr_distribution_pause import prepare

ROOT=Path(__file__).resolve().parents[2]


def test_named_issuance_distribution_pause_and_share_metadata(tmp_path):
    names=json.loads((ROOT/'config/token-metadata.example.json').read_text())
    w3,people,runner,apply,first=prepare(tmp_path,1,names)
    assert first['distribution']['token_metadata']==names['governance']
    assert apply()['status']==runner().apply(verify=True)['status']==STATUS
    b=runner();b.apply(verify=True);token=b.contract('dao_token')
    assert check(w3,token.address,'governance',dict(token_metadata=names),w3.eth.block_number)==names['governance']
    assert token.functions.balanceOf(people[1][0].address).call()==10_000_000*10**18
    # Real share-token metadata; pool authority remains a separate address/code binding.
    for role,kind in [('federation_share','MultiPoolShareToken'),('confederation_share','ConfederationToken')]:
        shared=deploy(w3,kind,names[role]['name'],names[role]['symbol'],people[0][0].address)
        check(w3,shared.address,role,dict(token_metadata=names),w3.eth.block_number)
        fake=deploy(w3,kind,'Fake Shares','FAKE',people[0][0].address)
        with pytest.raises(ValueError,match='TOKEN_METADATA_MISMATCH'):
            check(w3,fake.address,role,dict(token_metadata=names),w3.eth.block_number)
    fingerprint=apply()['distribution']['fingerprint']
    assert apply(ulr_distribution_complete=True,accept_ulr_distribution=fingerprint)['status']=='VERIFIED'
    assert runner().apply(verify=True)['status']=='VERIFIED'
