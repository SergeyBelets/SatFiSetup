// Synthetic transition fixtures; actual mainnet checkpoint validation only.
#[path="../../../resume-policy/tests/support/mod.rs"] mod support;
use support::*;
use satoshifi_resume_policy::{self as bounded,archive::Archive,checkpoint::ValidatedCheckpoint,fork::Hash};
use satoshifi_resume_transition::{self as transition,Deployment};
use bitcoin::hashes::{sha256d,Hash as _};

fn hex(b:&[u8])->String {format!("0x{}",b.iter().map(|b|format!("{b:02x}")).collect::<String>())}
fn strings(values:impl IntoIterator<Item=String>)->String {values.into_iter().map(|s|format!("\"{s}\"")).collect::<Vec<_>>().join(",")}
fn cp(height:u64)->ValidatedCheckpoint {
    let at=(height-32246) as usize;
    ValidatedCheckpoint::validate(&HEADERS[at*80..(at+1)*80],height,EXPECTED[at*68+32..at*68+64].try_into().unwrap(),
        &HEADERS[(at-10)*80..at*80].chunks_exact(80).map(<[u8]>::to_vec).collect::<Vec<_>>()).unwrap()
}
fn member(a:&Archive,height:u64)->String {
    let p=a.membership(a.state().view.head,height).unwrap();
    format!("{{\"height\":{},\"raw\":\"{}\",\"siblings\":[{}]}}",height,hex(&p.raw_header),strings(p.siblings.iter().map(|h|hex(h))))
}
fn apply(a:&mut Archive,headers:Vec<Vec<u8>>,name:&str,first:bool) {
    let before=a.state().clone();let proofs=a.prepare_batch(&headers,verify).unwrap();
    let d=Deployment {evm_chain_id:word(31337),state_address:[0x11;20],checkpoint_descriptor:before.checkpoint_descriptor};
    let out=transition::execute(d,before.view.state_commitment,&before,&headers,&proofs,verify).unwrap();
    assert_eq!(&out.state,a.state());
    if !first {println!(",");}
    println!("{{\"name\":\"{name}\",\"journal\":\"{}\",\"anchor\":{},\"head\":{}}}",hex(&out.journal.encode_abi()),
        member(a,before.view.anchor_height),member(a,out.state.view.height));
}
fn main() {
    println!("{{\"schema\":\"satoshifi.state-adapter-v2.vectors\",\"authentication\":\"NONE: synthetic transitions, no ZK proof; checkpoint headers are actual mainnet\",\"checkpoints\":[");
    for (i,height) in [32256,34272,36288].into_iter().enumerate() {
        if i>0 {println!(",");}let cp=cp(height);let (s,_)=bounded::initialize(&cp);let at=(height-32246) as usize;
        println!("{{\"height\":{height},\"work\":\"{}\",\"raw\":\"{}\",\"prior\":[{}],\"commitment\":\"{}\",\"canonicalRoot\":\"{}\",\"knownRoot\":\"{}\",\"knownLog\":\"{}\",\"descriptor\":\"{}\"}}",
            hex(&s.view.work),hex(cp.raw_header()),strings(HEADERS[(at-10)*80..at*80].chunks_exact(80).map(hex)),
            hex(&s.view.state_commitment),hex(&s.view.canonical_root),hex(&s.known_root),hex(&s.known_log),hex(&s.checkpoint_descriptor));
    }
    println!("],\"steps\":[");
    let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;
    let mut first=raw(anchor,1,10,1300000000);let mut txs=vec![0x11;32];txs.extend([0x22;32]);
    first[36..68].copy_from_slice(sha256d::Hash::hash(&txs).as_byte_array());let mut tip=hash(&first);
    apply(&mut a,vec![first],"append_merkle_pair",true);
    apply(&mut a,vec![raw(anchor,2,1,1300005000)],"losing_branch",false);
    for i in 2..=3 {let next=raw(tip,i+1,1,1300000000+i);tip=hash(&next);apply(&mut a,vec![next],"append",false);}
    let b=raw(anchor,5,50,1300000004);tip=hash(&b);apply(&mut a,vec![b],"shorter_heavier",false);
    for (start,end) in [(2,65),(66,100)] {
        let mut batch=vec![];
        for i in start..=end {let h=raw(tip,i+10,1,1300000000+i);tip=hash(&h);batch.push(h);}
        apply(&mut a,batch,"extend_for_halt",false);
    }
    let safe=raw(tip,1000,1,1300000101);let candidate=raw(anchor,1001,151,1300006000);
    apply(&mut a,vec![safe,candidate,vec![1]],"safe_prefix_halt",false);
    println!("],\"old_tip\":[");let mut a=Archive::new(&checkpoint());
    apply(&mut a,vec![raw(anchor,2000,1,1262153465)],"old_tip_catchup",true);
    println!("],\"nonmonotonic\":[");let mut a=Archive::new(&checkpoint());
    let h=raw(anchor,3000,1,1300000000);let tip:Hash=hash(&h);
    apply(&mut a,vec![h,raw(tip,3001,1,1299999990)],"nonmonotonic",true);
    println!("]}}");
}
