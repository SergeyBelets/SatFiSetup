//! Build explicitly synthetic fork/halt inputs using the real bounded policy code.
use satoshifi_test_policy_kernel::{execute,verify,transition};
use transition::{bounded::{archive::Archive,checkpoint::ValidatedCheckpoint},Deployment,Request};
use bitcoin::{block::{Header,Version},BlockHash,TxMerkleNode,CompactTarget,consensus::{serialize,deserialize},hashes::Hash as _};
use std::{fs,path::PathBuf};
fn hash(raw:&[u8])->[u8;32]{deserialize::<Header>(raw).unwrap().block_hash().to_byte_array()}
fn raw(parent:[u8;32],serial:u32,work:u32,time:u32)->Vec<u8>{
    let mut root=[0;32];root[28..].copy_from_slice(&serial.to_be_bytes());
    serialize(&Header{version:Version::from_consensus(1),prev_blockhash:BlockHash::from_byte_array(parent),
        merkle_root:TxMerkleNode::from_byte_array(root),time,bits:CompactTarget::from_consensus(0x1d00ffff),nonce:work})
}
fn main(){
    let args:Vec<_>=std::env::args_os().collect();assert_eq!(args.len(),4,"vectors HEADERS EXPECTED OUTPUT");
    let headers=fs::read(&args[1]).unwrap();let expected=fs::read(&args[2]).unwrap();let out=PathBuf::from(&args[3]);fs::create_dir_all(&out).unwrap();
    let cp=ValidatedCheckpoint::validate(&headers[800..880],32256,expected[712..744].try_into().unwrap(),
        &headers[..800].chunks_exact(80).map(<[u8]>::to_vec).collect::<Vec<_>>()).unwrap();
    for halt in [false,true]{
        let mut archive=Archive::new(&cp);let anchor=archive.state().view.anchor;let mut tip=anchor;
        for serial in 1..=100 {let h=raw(tip,serial,1,1300000000+serial);tip=hash(&h);archive.prepare_batch(&[h],verify).unwrap();}
        let before=archive.state().clone();
        let input=if halt {vec![raw(tip,101,1,1300000101),raw(anchor,102,102,1300099999),vec![1]]}
            else {vec![raw(anchor,1001,1,1300000200),raw(anchor,1002,100,1300000201),raw(anchor,1003,101,1300000202)]};
        if !halt {
            let mut observed=Archive::new(&cp);
            for chunk in archive.snapshot_headers().chunks(64){observed.prepare_batch(chunk,verify).unwrap();}
            for h in &input[..2]{observed.prepare_batch(&[h.clone()],verify).unwrap();assert_eq!(observed.state().view.head,tip);assert_eq!(observed.state().view.work,before.view.work);}
        }
        let witnesses=archive.prepare_batch(&input,verify).unwrap();let mut chain=[0;32];chain[30..].copy_from_slice(&31337u16.to_be_bytes());
        let request=Request{deployment:Deployment{evm_chain_id:chain,state_address:[0x11;20],checkpoint_descriptor:before.checkpoint_descriptor},
            expected:before.view.state_commitment,before,headers:input,witnesses};
        let bytes=request.encode().unwrap();let (result,journal)=execute(&bytes).unwrap();assert_eq!(&result.state,archive.state());
        if halt {
            assert!(result.state.view.halted);assert_eq!(result.state.halt.as_ref().unwrap().depth,101);
            assert_eq!(result.state.view.height,32357);assert_eq!(result.journal.processed(),2);assert_eq!(result.journal.submitted(),3);
            assert_eq!(result.journal.halt_position(),2);assert_eq!(result.journal.time_summary().count(),2);
        }else{assert!(!result.state.view.halted);assert_eq!(result.state.view.height,32257);assert!(result.journal.useful());assert_eq!(result.journal.processed(),3);}
        let name=if halt {"halt101-prefix"}else{"fork-work-reorg100"};
        let mut frame=(bytes.len() as u32).to_le_bytes().to_vec();frame.extend(bytes);
        fs::write(out.join(format!("{name}.input")),frame).unwrap();fs::write(out.join(format!("{name}.journal")),journal).unwrap();
        println!("TEST_ONLY {name}: synthetic-work, no PoW; bounded policy and terminal halt checks passed");
    }
}
