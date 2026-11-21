"""Execute archived run-04 contracts on an in-memory Py-EVM chain with test identities."""
import argparse
from hashlib import sha256
from pathlib import Path
import json
from zipfile import ZipFile

from web3 import EthereumTesterProvider, Web3
from eth_tester.exceptions import TransactionFailed

EVIDENCE = "rust/zkspv/deposit-integration/evidence/run-04/"
ARTIFACTS = EVIDENCE + "consumer-artifacts/"


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("archive", type=Path)
    cli.add_argument("sdk", type=Path)
    cli.add_argument("report", type=Path)
    args = cli.parse_args()
    w3 = Web3(EthereumTesterProvider())
    w3.provider.ethereum_tester.backend.chain.chain_id = 31337
    actors = w3.eth.accounts[:3]
    zero = bytes(32)
    saved = {"network":"ephemeral in-memory Py-EVM, EVM CHAINID 31337", "source":ARTIFACTS,
             "deployments":{}, "transactions":[], "calls":[],"limitations":[]}
    def safe(val):
        if isinstance(val, bytes):return Web3.to_hex(val)
        if isinstance(val,(list,tuple)):return list(map(safe,val))
        if isinstance(val,dict):return {k:safe(v) for k,v in val.items()}
        return val
    with ZipFile(args.archive) as z:
        prep=json.loads(z.read(EVIDENCE+"preparation.json"))
        def deploy(name,*parameters):
            path=(EVIDENCE.removesuffix("run-04/")+"contracts/" if name=="TestRegtestDepositState" else ARTIFACTS)+name+".json"
            raw=z.read(path); source=json.loads(raw)
            constructor=w3.eth.contract(abi=source["abi"],bytecode=source["bytecode"]["object"]).constructor(*parameters)
            tx=constructor.transact({"from":actors[0]})
            receipt=w3.eth.wait_for_transaction_receipt(tx); assert receipt.status==1
            address=receipt.contractAddress
            saved["deployments"][name]=dict(artifact_sha256=sha256(raw).hexdigest(),
                artifact_path=path,address=address, runtime_sha256=sha256(w3.eth.get_code(address)).hexdigest(),
                tx=Web3.to_hex(tx),status=receipt.status)
            return w3.eth.contract(abi=source["abi"],address=address)
        def send(method,params,who):
            tx=method(*params).transact({"from":who})
            rc=w3.eth.wait_for_transaction_receipt(tx);assert rc.status==1
            saved["transactions"].append(dict(method=method.fn_name,tx=Web3.to_hex(tx),status=rc.status))
        def pair(name,c,method,params,expected=None,expect_revert=False):
            original=json.loads(z.read("signer-agent/contracts/"+name+".json"))
            patched=json.loads((args.sdk/"contracts"/(name+".json")).read_bytes())
            a,b=[w3.eth.contract(address=c.address,abi=abi) for abi in (original,patched)]
            old=getattr(a.functions,method)(*params);new=getattr(b.functions,method)(*params)
            calldata=old._encode_transaction_data()
            assert calldata==new._encode_transaction_data()
            outcomes=[]
            for f in (old,new):
                try:outcomes.append(("ok",safe(f.call({"from":actors[0]}))))
                except TransactionFailed as exc:outcomes.append(("reverted",str(exc)))
            assert outcomes[0]==outcomes[1],method
            status,res=outcomes[0]
            if expect_revert: assert status=="reverted",method
            else:
                assert status=="ok",method
                if expected is not None: assert res==safe(expected),(name,method,res,expected)
            saved["calls"].append(dict(contract=name,method=method,selector=calldata[:10],status=status,result=res))
            return res
        d=deploy("DKGCoordinator");v=deploy("FROSTVerifier")
        c=deploy("FROSTCoordinator",v.address)
        send(c.functions.bindDKGRegistry,[d.address],actors[0])
        pair("FROSTCoordinator",c,"defaultVerifier",[],v.address)
        pair("FROSTCoordinator",c,"dkgRegistry",[],d.address)
        pair("FROSTCoordinator",c,"isFinalized",[999],False)
        pair("FROSTCoordinator",c,"getSession",[999])
        pair("FROSTCoordinator",c,"getResult",[999],expect_revert=True)
        pair("FROSTCoordinator",c,"getAuthorizationResult",[999],expect_revert=True)
        pair("FROSTCoordinator",c,"finalizeSession",[999,bytes(64),Web3.keccak(text="isolated test")],expect_revert=True)
        def ceremony(which):
            mf=prep[which+"_manifest"]; public=prep[which+"_public"]
            sid=Web3.keccak(text="ABI check "+which)
            group=bytes.fromhex(public["group"])[1:]
            keys=[bytes.fromhex(k) for k in mf["transport_keys"]]
            threshold=mf["threshold"]
            send(d.functions.createCeremony,[sid,actors,threshold,keys,1,Web3.keccak(text=mf["purpose"]),bytes.fromhex(mf["protocol_id"])],actors[0])
            def digest(tag):return Web3.keccak(text=which+" local fixture "+tag)
            for idx,actor in enumerate(actors):
                send(d.functions.submitRound1,[sid,digest(f"round1 {idx}"),[digest(f"commit {idx} {i}") for i in range(threshold)]],actor)
            for idx,actor in enumerate(actors):
                others=[p for p in actors if p!=actor]
                send(d.functions.submitShareHashes,[sid,others,[digest(f"share {actor} {peer}") for peer in others]],actor)
            for idx,actor in enumerate(actors):
                others=[p for p in actors if p!=actor]
                share=bytes.fromhex(public["public_shares"][f"{idx+1:064x}"])
                send(d.functions.submitReceiptsAndPublicKeyShare,[sid,others,[digest(f"share {peer} {actor}") for peer in others],share],actor)
            negated=public["normalization_negated"]; hash_=d.functions.computeResultHash(sid,negated,group).call()
            for actor in actors:send(d.functions.attestResult,[sid,negated,group,hash_],actor)
            return sid,group,threshold
        evm,group,threshold=ceremony("evm")
        auth=deploy("DKGAuthorization",c.address,evm,group,actors,threshold)
        for method,params,expected in [
            ("coordinator",[],c.address),("registry",[],d.address),("dkgId",[],evm),("resultHash",[],None),
            ("operationTargets",[zero],"0x"+"00"*20),("executedOperations",[zero],False),
            ("isOperationApproved",[zero,actors[1]],False),("consumedSessions",[999],False),
            ("hashOperation",[actors[1],bytes.fromhex("abcd1234"),zero,1],None)]:
            pair("DKGAuthorization",auth,method,params,expected)
        # Create a real session from the attested EVM DKG and exercise its state.
        op=Web3.keccak(text="local ABI smoke calldata")
        expiry=w3.eth.get_block("latest").timestamp + 7200
        session=c.functions.createSessionFromDKG(evm,op,auth.address,expiry).call({"from":actors[0]})
        send(c.functions.createSessionFromDKG,[evm,op,auth.address,expiry],actors[0])
        pair("FROSTCoordinator",c,"isFinalized",[session],False)
        session_row=pair("FROSTCoordinator",c,"getSession",[session])
        assert session_row[0]==session and session_row[3]==Web3.to_hex(op) and session_row[5]==threshold
        pair("FROSTCoordinator",c,"sessionDKGId",[session],evm)
        pair("FROSTCoordinator",c,"sessionDKGResult",[session],auth.functions.resultHash().call())
        pair("FROSTCoordinator",c,"finalizeSession",[session,bytes(64),op],expect_revert=True)
        pair("FROSTCoordinator",c,"isFinalized",[session],False)
        sid,_,_=ceremony("btc")
        batch=deploy("BitcoinBatchRegistry",auth.address,d.address,sid)
        pair("BitcoinBatchRegistry",batch,"batchState",[zero],0)
        pair("BitcoinBatchRegistry",batch,"walletId",[],sid)
        pair("BitcoinBatchRegistry",batch,"walletResult",[])
        pair("BitcoinBatchRegistry",batch,"authorization",[],auth.address)
        # Deploy the historical TEST_ONLY regtest state, which creates a genuine
        # adapter and verifier internally. No transition or proof is submitted.
        test_state=deploy("TestRegtestDepositState",*prep["deployments"]["TestRegtestDepositState"]["arguments"])
        adapter=test_state.functions.adapter().call()
        assert w3.eth.get_code(adapter) and w3.eth.get_code(test_state.functions.verifier().call())
        escrow=deploy("BitcoinWithdrawalEscrow",batch.address,adapter,6,294,330)
        pair("BitcoinWithdrawalEscrow",escrow,"depth",[],6)
        pair("BitcoinWithdrawalEscrow",escrow,"batches",[],batch.address)
        pair("BitcoinWithdrawalEscrow",escrow,"spv",[],adapter)
        pay=pair("BitcoinWithdrawalEscrow",escrow,"payment",[zero])
        assert pay[0]==Web3.to_hex(zero) and pay[2] is False
        pair("BitcoinWithdrawalEscrow",escrow,"nonce",[],0)
        pair("BitcoinWithdrawalEscrow",escrow,"settle",[zero,b"",zero,[],0],expect_revert=True)
    saved["limitations"]=["Two local DKG registry transcripts used deterministic test hashes, archived public shares and ephemeral participant attestations. No participant secret shares or FROST signing ceremony used.",
        "Relay-address constructor slot binds the actual archived adapter deployed by TEST_ONLY regtest state. No positive withdrawal, Bitcoin blockchain, SPV proof, funded DAO, or mainnet state was exercised.",
        "No bytecode was generated from updated SDK ABIs; both clients called the same original deployed bytecode."]
    saved["status"]="PASS"
    args.report.write_text(json.dumps(saved,indent=2)+"\n")
    print(len(saved["deployments"]),"deployments;",len(saved["transactions"]),"test calls with successful receipts;",len(saved["calls"]),"paired eth_calls")


if __name__=="__main__":
    main()
