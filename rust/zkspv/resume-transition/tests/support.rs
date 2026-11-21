#![allow(dead_code)]
#[path="../../resume-policy/tests/support/mod.rs"] mod graph;
pub use graph::*;
use satoshifi_resume_transition::{self as transition,Deployment,Request,Execution};
use satoshifi_resume_policy::archive::Archive;
pub fn deployment()->Deployment {Deployment {evm_chain_id:word(31337),state_address:[0x11;20],checkpoint_descriptor:checkpoint().descriptor_commitment()}}
pub fn request(a:&mut Archive,headers:Vec<Vec<u8>>)->Request {
    let before=a.state().clone();let witnesses=a.prepare_batch(&headers,verify).unwrap();
    Request {deployment:deployment(),expected:before.view.state_commitment,before,headers,witnesses}
}
pub fn execute(r:&Request)->Execution {transition::execute_encoded(&r.encode().unwrap(),verify).unwrap()}
pub fn append()->Request {let mut a=Archive::new(&checkpoint());let h=raw(a.state().view.head,1,2,1300000000);request(&mut a,vec![h])}
pub fn scenarios()->Vec<(&'static str,Request)> {
    let mut out=vec![("append",append())];let anchor=checkpoint().context().block_hash;
    let mut a=Archive::new(&checkpoint());let first=raw(anchor,1,2,1300000000);
    out.push(("nonmonotonic",request(&mut a,vec![first.clone(),raw(hash(&first),2,1,1299999900)])));
    let mut a=Archive::new(&checkpoint());add(&mut a,anchor,1,100);
    out.push(("losing_branch",request(&mut a,vec![raw(anchor,2,1,1300099999)])));
    let mut a=Archive::new(&checkpoint());chain(&mut a,anchor,3,&mut 0,1);
    out.push(("shorter_heavier",request(&mut a,vec![raw(anchor,4,4,1300000010)])));
    let mut a=Archive::new(&checkpoint());let old=chain(&mut a,anchor,100,&mut 0,1);
    out.push(("reorg100",request(&mut a,vec![raw(anchor,101,101,1300000101)])));
    let mut a=Archive::new(&checkpoint());chain(&mut a,anchor,100,&mut 0,1);
    out.push(("halt_prefix",request(&mut a,vec![raw(old,101,1,1300000101),raw(anchor,102,102,1300099999),vec![1]])));
    out
}
