// Local synthetic policy vectors, not a Bitcoin header/proof generator.
#[path = "../../tests/support/mod.rs"] mod support;
use support::*;
use satoshifi_fork_policy::{Membership, View};

fn hex(bytes: &[u8]) -> String { bytes.iter().map(|b| format!("{b:02x}")).collect() }
fn proof(name: &str, view: &View, proof: &Membership, expected: bool) {
    println!("PROOF\t{name}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
        hex(&view.anchor),view.anchor_height,hex(&view.head),view.height,hex(&view.canonical_root),
        u8::from(view.halted),proof.height,hex(&proof.raw_header),
        proof.siblings.iter().map(|h| hex(h)).collect::<Vec<_>>().join(","),u8::from(expected));
}

fn trace(name: &str, count: u32, shorter: bool) {
    let mut state=state(); let a=state.view().anchor; let mut ids=vec![a];
    let mut operations=Vec::new();
    for i in 0..count { operations.push((i as usize,1)); }
    if shorter { operations.push((0,count+1)); }
    else {
        for i in 0..=count { operations.push((if i==0 {0} else {count as usize+i as usize},1)); }
    }
    for (i,(parent,delta)) in operations.iter().enumerate() {
        let old=state.view();
        let bytes=raw(ids[*parent],(i+1) as u32,*delta,1300000000+i as u32);
        let child=hash(&bytes);
        let result=state.apply_batch(&[bytes],verify).unwrap(); ids.push(child);
        let common=state.common_ancestor(old.head,child).unwrap();
        let common_index=ids.iter().position(|id| *id==common).unwrap();
        let common_height=32256+if common_index==0 {0} else {
            // Recover height directly from the ancestry identity, without assuming insertion order.
            let mut cur=common_index; let mut height=0;
            while cur!=0 { height+=1; cur=operations[cur-1].0; } height
        };
        let head=ids.iter().position(|id| *id==result.view.head).unwrap();
        println!("TRACE\t{name}\t{parent}\t{delta}\t{head}\t{}\t{}\t{common_index}\t{}",
            result.view.height,u8::from(result.view.halted),old.height-common_height);
        if result.view.halted { break; }
    }
}

fn main() {
    let mut s=state(); let a=s.view().anchor;
    let h=add(&mut s,a,1,10); let first=s.membership(h).unwrap(); let old_view=s.view();
    proof("initial_tip",&old_view,&first,true);
    let h2=add(&mut s,h,2,1);
    proof("ancestor_after_append",&s.view(),&s.membership(h).unwrap(),true);
    proof("stale_root_after_append",&s.view(),&first,false);
    let old_tail=s.membership(h2).unwrap();
    let side=add(&mut s,a,3,12);
    proof("shorter_winner",&s.view(),&s.membership(side).unwrap(),true);
    proof("checkpoint",&s.view(),&s.membership(a).unwrap(),true);
    proof("old_tail_above_head",&s.view(),&old_tail,false);
    proof("losing_header_same_height",&s.view(),&first,false);
    let mut changed=s.membership(side).unwrap(); changed.height-=1;
    proof("wrong_height",&s.view(),&changed,false);
    let mut halted=s.view(); halted.halted=true;
    proof("halted_view",&halted,&s.membership(side).unwrap(),false);
    trace("depth100",100,false); trace("depth101",101,false); trace("shorter",100,true);
}
