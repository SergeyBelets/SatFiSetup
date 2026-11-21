#![allow(dead_code)]
use satoshifi_resume_policy::{self as policy, archive::Archive, checkpoint::{InitialContext, ValidatedCheckpoint}, fork::{Hash, View}};
use bitcoin::{block::{Header, Version}, BlockHash, TxMerkleNode, CompactTarget, consensus::{serialize,deserialize}, hashes::Hash as _};
pub const HEADERS:&[u8]=include_bytes!("../../../../../web3/test/fixtures/mainnet/headers-32246-36288.bin");
pub const EXPECTED:&[u8]=include_bytes!("../../../../../web3/test/fixtures/mainnet/expected-32246-36288.bin");
pub fn checkpoint()->ValidatedCheckpoint {
    ValidatedCheckpoint::validate(&HEADERS[800..880],32256,EXPECTED[712..744].try_into().unwrap(),
        &HEADERS[..800].chunks_exact(80).map(<[u8]>::to_vec).collect::<Vec<_>>()).unwrap()
}
pub fn word(n:u128)->Hash {let mut r=[0;32];r[16..].copy_from_slice(&n.to_be_bytes());r}
pub fn value(n:Hash)->u128 {assert_eq!(&n[..16],&[0;16]);u128::from_be_bytes(n[16..].try_into().unwrap())}
pub fn raw(parent:Hash,serial:u32,delta:u32,time:u32)->Vec<u8> {
    let mut root=[0;32];root[28..].copy_from_slice(&serial.to_be_bytes());
    serialize(&Header {version:Version::from_consensus(1),prev_blockhash:BlockHash::from_byte_array(parent),
        merkle_root:TxMerkleNode::from_byte_array(root),time,bits:CompactTarget::from_consensus(0x1d00ffff),nonce:delta})
}
pub fn hash(raw:&[u8])->Hash {deserialize::<Header>(raw).unwrap().block_hash().to_byte_array()}
// Only test graph arithmetic. This callback does not validate Bitcoin consensus.
pub fn verify(c:&InitialContext,raw:&[u8;80],height:u64)->Result<InitialContext,&'static str> {
    let h:Header=deserialize(raw).unwrap();if h.nonce==0 {return Err("synthetic invalid");}
    let mut c=c.clone();c.block_hash=h.block_hash().to_byte_array();c.block_height=height;
    c.total_work=word(value(c.total_work)+u128::from(h.nonce));c.prev_11_timestamps[(height%11) as usize]=h.time;Ok(c)
}
pub fn same_policy(reference:&View,state:&policy::State) {
    let mut actual=state.view.clone();actual.state_commitment=reference.state_commitment;
    assert_eq!(&actual,reference);
}
pub fn add(archive:&mut Archive,parent:Hash,serial:u32,delta:u32)->Hash {
    let h=raw(parent,serial,delta,1300000000+serial);let hash=hash(&h);
    archive.prepare_batch(&[h],verify).unwrap();hash
}
pub fn chain(archive:&mut Archive,mut parent:Hash,count:u32,serial:&mut u32,delta:u32)->Hash {
    for _ in 0..count {*serial+=1;parent=add(archive,parent,*serial,delta);}parent
}
pub fn run(archive:&mut Archive,headers:&[Vec<u8>])->(policy::State,Vec<policy::Witness>,policy::Outcome) {
    let before=archive.state().clone();let witnesses=archive.prepare_batch(headers,verify).unwrap();
    let result=policy::resume(before.view.state_commitment,&before,headers,&witnesses,verify).unwrap();
    assert_eq!(&result.state,archive.state());(before,witnesses,result)
}
pub fn witness_hashes(w:&policy::Witness)->usize {
    match w {
        policy::Witness::Known(p)=>p.siblings.len(),
        policy::Witness::New {absence,parent,head,empty_history_path,lca}=>absence.len()+parent.siblings.len()+head.siblings.len()+empty_history_path.len()
            +lca.as_ref().map_or(0,|p|p.common_on_head.siblings.len()+p.common_on_candidate.len()
                +p.children.as_ref().map_or(0,|(a,b)|a.siblings.len()+b.siblings.len())),
    }
}
