// Test-only graph transitions: no synthetic PoW/MTP/difficulty qualification.
#![allow(dead_code)]
use bitcoin::{block::{Header, Version}, BlockHash, TxMerkleNode, CompactTarget,
    consensus::{serialize, deserialize}, hashes::Hash as _};
use satoshifi_transition_policy::{self as policy, Deployment, Execution, checkpoint::{ValidatedCheckpoint, InitialContext}, fork::{ForkState, Hash}};
pub const HEADERS: &[u8] = include_bytes!("../../../../../web3/test/fixtures/mainnet/headers-32246-36288.bin");
pub const EXPECTED: &[u8] = include_bytes!("../../../../../web3/test/fixtures/mainnet/expected-32246-36288.bin");
pub fn checkpoint() -> ValidatedCheckpoint {
    let prior=HEADERS[..800].chunks_exact(80).map(<[u8]>::to_vec).collect::<Vec<_>>();
    ValidatedCheckpoint::validate(&HEADERS[800..880],32256,EXPECTED[712..744].try_into().unwrap(),&prior).unwrap()
}
pub fn word(value:u128)->Hash { let mut out=[0;32]; out[16..].copy_from_slice(&value.to_be_bytes()); out }
pub fn value(word:Hash)->u128 { assert_eq!(&word[..16],&[0;16]); u128::from_be_bytes(word[16..].try_into().unwrap()) }
pub fn deployment()->Deployment { Deployment { evm_chain_id:word(31337),state_address:[0x11;20],checkpoint_descriptor:checkpoint().descriptor_commitment() } }
pub fn state()->ForkState<InitialContext> { checkpoint().initialize(Clone::clone).unwrap() }
pub fn hash(raw:&[u8])->Hash { deserialize::<Header>(raw).unwrap().block_hash().to_byte_array() }
pub fn raw(parent:Hash,serial:u32,delta:u32,time:u32)->Vec<u8> {
    let mut root=[0;32]; root[28..].copy_from_slice(&serial.to_be_bytes());
    serialize(&Header { version:Version::from_consensus(1),prev_blockhash:BlockHash::from_byte_array(parent),
        merkle_root:TxMerkleNode::from_byte_array(root),time,bits:CompactTarget::from_consensus(0x1d00ffff),nonce:delta })
}
pub fn verify(parent:&InitialContext,raw:&[u8;80],height:u64)->Result<InitialContext,&'static str> {
    let h:Header=deserialize(raw).unwrap(); if h.nonce==0 { return Err("synthetic rejection"); }
    let mut next=parent.clone(); next.block_hash=h.block_hash().to_byte_array(); next.block_height=height;
    next.total_work=word(value(parent.total_work)+u128::from(h.nonce));
    next.prev_11_timestamps[(height%11) as usize]=h.time;
    Ok(next)
}
pub fn run(s:&ForkState<InitialContext>,headers:&[Vec<u8>])->Result<Execution<InitialContext>,policy::Error<&'static str>> {
    policy::execute(&checkpoint(),deployment(),&s.view(),&s.snapshot_headers(),headers,Clone::clone,verify)
}
pub fn add(s:&mut ForkState<InitialContext>,parent:Hash,serial:u32,delta:u32,time:u32)->Hash {
    let header=raw(parent,serial,delta,time); let hash=hash(&header);
    s.apply_batch(&[header],verify).unwrap(); hash
}
pub fn chain(s:&mut ForkState<InitialContext>,mut parent:Hash,count:u32,serial:&mut u32,delta:u32)->Hash {
    for _ in 0..count { *serial+=1; parent=add(s,parent,*serial,delta,1300000000+*serial); } parent
}
pub fn scenarios()->Vec<(&'static str,Execution<InitialContext>)> {
    let mut out=vec![];
    let s=state(); let a=s.view().anchor; let first=raw(a,1,2,1300000000);
    out.push(("append",run(&s,&[first.clone()]).unwrap()));
    out.push(("nonmonotonic",run(&s,&[first.clone(),raw(hash(&first),2,1,1299999900)]).unwrap()));
    let mut s=state(); add(&mut s,a,1,100,1300000000);
    out.push(("losing_branch",run(&s,&[raw(a,2,1,1300099999)]).unwrap()));
    let mut s=state(); let mut serial=0; chain(&mut s,a,3,&mut serial,1);
    out.push(("shorter_heavier",run(&s,&[raw(a,4,4,1300000010)]).unwrap()));
    let mut s=state(); let mut serial=0; let old=chain(&mut s,a,100,&mut serial,1);
    out.push(("reorg100",run(&s,&[raw(a,101,101,1300000101)]).unwrap()));
    let safe=raw(old,101,1,1300000101); let forbidden=raw(a,102,102,1300099999);
    out.push(("halt_prefix",run(&s,&[safe,forbidden,vec![1]]).unwrap()));
    out
}
