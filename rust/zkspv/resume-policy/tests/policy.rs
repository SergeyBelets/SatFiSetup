mod support;
use support::*;
use satoshifi_resume_policy::{self as policy,archive::Archive,Error,Witness,LcaWitness,fork};

#[test] fn v2_checkpoint_keeps_canonical_root_but_never_reuses_v1_state_commitment() {
    let cp=checkpoint();let v1=cp.initialize(Clone::clone).unwrap();let (state,_)=policy::initialize(&cp);
    same_policy(&v1.view(),&state);assert_ne!(state.view.state_commitment,v1.view().state_commitment);
    assert_eq!(state.known_count,1);assert_eq!(state.commitment(),state.view.state_commitment);
}
#[test] fn bounded_append_matches_full_reference_and_checks_only_new_headers() {
    let cp=checkpoint();let mut archive=Archive::new(&cp);let mut full=cp.initialize(Clone::clone).unwrap();
    let a=archive.state().view.head;let first=raw(a,1,1,1300000000);let second=raw(hash(&first),2,1,1299999990);
    let input=vec![first.clone(),first,second];let before=archive.state().clone();
    let proofs=archive.prepare_batch(&input,verify).unwrap();let mut calls=0;
    let out=policy::resume(before.view.state_commitment,&before,&input,&proofs,|c,r,h|{calls+=1;verify(c,r,h)}).unwrap();
    full.apply_batch(&input,verify).unwrap();same_policy(&full.view(),&out.state);assert_eq!(&out.state,archive.state());
    assert_eq!(calls,2);assert_eq!(out.summary.count(),2);assert_eq!(out.summary.maximum(),1300000000);
    assert_eq!(out.summary.last(),1299999990);
}
#[test] fn inclusion_cannot_be_used_as_absence_or_duplicate_work() {
    let mut a=Archive::new(&checkpoint());let root=a.state().view.head;let h=raw(root,1,1,1300000000);
    a.prepare_batch(&[h.clone()],verify).unwrap();let before=a.state().clone();let known=a.opening(hash(&h)).unwrap();
    let bad=Witness::New {absence:known.siblings.clone(),parent:a.opening(root).unwrap(),head:known,
        empty_history_path:vec![[0;32];64],lca:None};
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&[h],&[bad],verify),Err(Error::IndexProof)));
}
#[test] fn every_native_context_field_is_authenticated_before_verifier_use() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];
    let before=a.state().clone();let proofs=a.prepare_batch(&input,verify).unwrap();
    for field in 0..16 {
        let mut p=proofs.clone();let Witness::New {parent,..}=&mut p[0] else {panic!()};let c=&mut parent.entry.context;
        match field {0=>c.block_hash[0]^=1,1=>c.block_height+=1,2=>c.total_work[31]^=1,
            3=>c.current_target_bits^=1,4=>c.epoch_start_time^=1,i=>c.prev_11_timestamps[i-5]^=1}
        let mut calls=0;assert!(policy::resume(before.view.state_commitment,&before,&input,&p,|c,r,h|{calls+=1;verify(c,r,h)}).is_err());
        assert_eq!(calls,0);
    }
}
#[test] fn stale_and_changed_state_metadata_are_rejected() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];
    let before=a.state().clone();let proofs=a.prepare_batch(&input,verify).unwrap();
    for field in 0..12 {
        let mut altered=before.clone();match field {0=>altered.known_root[0]^=1,1=>altered.known_count+=1,
            2=>altered.checkpoint_descriptor[0]^=1,3=>altered.view.anchor[0]^=1,4=>altered.view.anchor_height+=2016,
            5=>altered.view.head[0]^=1,6=>altered.view.height+=1,7=>altered.view.work[31]^=1,
            8=>altered.view.timestamp^=1,9=>altered.view.canonical_root[0]^=1,10=>altered.known_log[0]^=1,_=>altered.view.halted=true}
        assert!(matches!(policy::resume(before.view.state_commitment,&altered,&input,&proofs,verify),Err(Error::State)));
    }
    assert!(matches!(policy::resume([0;32],&before,&input,&proofs,verify),Err(Error::State)));
}
#[test] fn map_paths_and_empty_history_paths_are_exact_and_root_bound() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];
    let before=a.state().clone();let proofs=a.prepare_batch(&input,verify).unwrap();
    for field in 0..5 {for length in [false,true] {
        let mut p=proofs.clone();let Witness::New {absence,parent,head,empty_history_path,lca}=&mut p[0] else {panic!()};
        let target=match field {0=>absence,1=>&mut parent.siblings,2=>&mut head.siblings,3=>empty_history_path,_=>&mut lca.as_mut().unwrap().common_on_candidate};
        if length {target.pop();}else{target[0][0]^=1;}
        assert!(policy::resume(before.view.state_commitment,&before,&input,&p,verify).is_err());
    }}
}
#[test] fn lower_but_non_last_common_ancestor_cannot_create_false_halt() {
    let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;let mut serial=0;
    let head=chain(&mut a,anchor,100,&mut serial,1);
    let parent=hash(&a.membership(head,32256+50).unwrap().raw_header);
    let input=vec![raw(parent,101,100,1300000200)];let before=a.state().clone();let mut proofs=a.prepare_batch(&input,verify).unwrap();
    let candidate=hash(&input[0]);let common=a.membership(head,32256).unwrap();
    let wrong=LcaWitness {common_on_head:common,common_on_candidate:a.membership(candidate,32256).unwrap().siblings,
        children:Some((a.membership(head,32257).unwrap(),a.membership(candidate,32257).unwrap()))};
    let Witness::New {lca,..}=&mut proofs[0] else {panic!()};*lca=Some(wrong);
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&input,&proofs,verify),Err(Error::Lca)));
    assert!(!a.state().view.halted);assert_eq!(a.state().view.height,32307);
}
#[test] fn exact_100_switch_and_101_safe_prefix_halt_match_reference() {
    for safe_prefix in [false,true] {
        let cp=checkpoint();let mut a=Archive::new(&cp);let anchor=a.state().view.anchor;let mut serial=0;
        let old=chain(&mut a,anchor,100,&mut serial,1);let mut full=cp.initialize(Clone::clone).unwrap();
        for chunk in a.snapshot_headers().chunks(64) {full.apply_batch(chunk,verify).unwrap();}
        let input=if safe_prefix {vec![raw(old,101,1,1300000101),raw(anchor,102,102,1300099999),vec![1]]}
            else {vec![raw(anchor,101,101,1300000101)]};
        let (_,proofs,result)=run(&mut a,&input);let expected=full.apply_batch(&input,verify).unwrap();
        same_policy(&expected.view,&result.state);assert_eq!(result.halt_at,expected.halt_at);
        assert_eq!(result.state.halt,expected.halt);assert_eq!(proofs.len(),result.processed);
        if safe_prefix {assert_eq!(result.halt_at,Some(1));assert_eq!(result.summary.maximum(),1300099999);}
    }
}
#[test] fn losing_and_equal_work_branches_keep_head_and_can_resume_later() {
    let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;let main=add(&mut a,anchor,1,10);
    let side=raw(anchor,2,9,1400000000);let (_,_,out)=run(&mut a,&[side.clone()]);
    assert_eq!(out.state.view.head,main);assert_eq!(out.summary.maximum(),1400000000);assert!(out.state.halt.is_none());
    let tie=raw(hash(&side),3,1,1300000000);let (_,_,out)=run(&mut a,&[tie.clone()]);assert_eq!(out.state.view.head,main);
    let winner=raw(hash(&tie),4,1,1300000001);let (_,_,out)=run(&mut a,&[winner.clone()]);assert_eq!(out.state.view.head,hash(&winner));
}
#[test] fn invalid_batch_rolls_back_archive_and_returns_no_bounded_prefix() {
    let mut a=Archive::new(&checkpoint());let before=a.state().clone();let nodes=a.retained_nodes();
    let good=raw(before.view.head,1,1,1300000000);let bad=raw(hash(&good),2,0,1300000001);
    assert!(a.prepare_batch(&[good,bad],verify).is_err());assert_eq!(a.state(),&before);assert_eq!(a.retained_nodes(),nodes);
    assert!(a.snapshot_headers().is_empty());
}
#[test] fn proofs_for_different_step_order_or_old_index_root_fail() {
    let mut a=Archive::new(&checkpoint());let first=raw(a.state().view.head,1,1,1300000000);let next=raw(hash(&first),2,1,1300000001);
    let input=vec![first,next];let before=a.state().clone();let proofs=a.prepare_batch(&input,verify).unwrap();
    let mut reversed=proofs.clone();reversed.reverse();assert!(policy::resume(before.view.state_commitment,&before,&input,&reversed,verify).is_err());
    let stale=vec![proofs[0].clone(),proofs[0].clone()];assert!(policy::resume(before.view.state_commitment,&before,&input,&stale,verify).is_err());
}
#[test] fn resource_limits_missing_extra_witnesses_and_noops_fail_closed() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];let before=a.state().clone();
    let proofs=a.prepare_batch(&input,verify).unwrap();
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&input,&[],verify),Err(Error::WitnessCount)));
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&input,&vec![proofs[0].clone();2],verify),Err(Error::Limits)));
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&vec![vec![0;80];65],&[],verify),Err(Error::Limits)));
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&[vec![0;5121]],&[],verify),Err(Error::Limits)));
    let state=a.state().clone();let raw=input[0].clone();let known=Witness::Known(a.opening(hash(&raw)).unwrap());
    assert!(matches!(policy::resume(state.view.state_commitment,&state,&[raw],&[known],verify),Err(Error::NoChange)));
}
#[test] fn snapshot_rebuild_preserves_v2_index_context_and_membership() {
    let cp=checkpoint();let mut a=Archive::new(&cp);let anchor=a.state().view.anchor;let main=add(&mut a,anchor,1,10);
    let side=add(&mut a,anchor,2,1);add(&mut a,side,3,1);
    let mut restored=Archive::new(&cp);restored.prepare_batch(&a.snapshot_headers(),verify).unwrap();assert_eq!(restored.state(),a.state());
    assert!(fork::verify_membership(&a.state().view,&a.membership(main,32256).unwrap()));
    let h=raw(side,4,11,1300000010);let (_,_,x)=run(&mut a,&[h.clone()]);let (_,_,y)=run(&mut restored,&[h]);assert_eq!(x.state,y.state);
}
#[test] fn witness_bound_does_not_grow_with_archive_history() {
    let mut sizes=vec![];
    for length in [8,512] {
        let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;let mut serial=0;
        let tip=chain(&mut a,anchor,length,&mut serial,1);let input=vec![raw(tip,serial+1,1,1300010000)];
        let before=a.state().clone();let proofs=a.prepare_batch(&input,verify).unwrap();let mut calls=0;
        policy::resume(before.view.state_commitment,&before,&input,&proofs,|c,r,h|{calls+=1;verify(c,r,h)}).unwrap();
        assert_eq!(calls,1);sizes.push(witness_hashes(&proofs[0]));
    }
    assert_eq!(sizes,vec![960,960]); // 3*256 index + 64 update + 2*64 LCA
}
#[test] fn generated_fork_graph_matches_reference_at_every_transition() {
    let cp=checkpoint();let mut a=Archive::new(&cp);let mut full=cp.initialize(Clone::clone).unwrap();
    let mut hashes=vec![a.state().view.anchor];let mut seed=0x12345678u64;
    for serial in 1..=200 {
        seed=seed.wrapping_mul(6364136223846793005).wrapping_add(1);
        let parent=hashes[(seed as usize)%hashes.len()];let delta=((seed>>32)%20+1) as u32;
        let input=vec![raw(parent,serial,delta,1300000000+serial)];let (_,_,out)=run(&mut a,&input);
        let expected=full.apply_batch(&input,verify).unwrap();same_policy(&expected.view,&out.state);
        assert_eq!(out.state.halt,expected.halt);hashes.push(hash(&input[0]));
    }
}
#[test] fn halted_v2_state_is_terminal_even_with_a_well_formed_next_witness() {
    let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;let mut serial=0;
    chain(&mut a,anchor,101,&mut serial,1);let (_,_,out)=run(&mut a,&[raw(anchor,102,102,1300000200)]);
    assert!(out.state.view.halted);
    assert!(matches!(policy::resume(out.state.view.state_commitment,&out.state,&[vec![0;80]],&[],verify),Err(Error::Halted)));
}

#[test] fn bounded_wire_roundtrip_preserves_context_paths_and_transition() {
    let mut a=Archive::new(&checkpoint());let anchor=a.state().view.anchor;let main=add(&mut a,anchor,1,10);
    let input=vec![raw(anchor,2,11,1300000000),raw(main,3,1,1300000001)];let before=a.state().clone();
    let proofs=a.prepare_batch(&input,verify).unwrap();let bytes=policy::encode_witnesses(&proofs).unwrap();
    let decoded=policy::decode_witnesses(&bytes).unwrap();assert_eq!(decoded,proofs);
    assert_eq!(policy::resume(before.view.state_commitment,&before,&input,&decoded,verify).unwrap().state,*a.state());
    assert!(bytes.len()<=34+input.len()*35555);
    let maximum=policy::encode_witnesses(&vec![proofs[0].clone();64]).unwrap();
    assert_eq!(maximum.len(),policy::MAX_WITNESS_BYTES);
    assert_eq!(policy::decode_witnesses(&maximum).unwrap().len(),64);
}
#[test] fn wire_rejects_truncation_trailing_data_unknown_flags_and_excess_counts() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];
    let proofs=a.prepare_batch(&input,verify).unwrap();let bytes=policy::encode_witnesses(&proofs).unwrap();
    for cut in [0,1,31,32,33,34,35,100,8192,bytes.len()-1] {assert!(policy::decode_witnesses(&bytes[..cut]).is_err());}
    for index in [0,31,32,33,34] {let mut altered=bytes.clone();altered[index]=255;assert!(policy::decode_witnesses(&altered).is_err());}
    let mut extra=bytes.clone();extra.push(0);assert_eq!(policy::decode_witnesses(&extra),Err(policy::CodecError::Trailing));
    assert_eq!(policy::decode_witnesses(&vec![0;policy::MAX_WITNESS_BYTES+1]),Err(policy::CodecError::Limit));
}
#[test] fn wire_bit_change_cannot_authenticate_context() {
    let mut a=Archive::new(&checkpoint());let input=vec![raw(a.state().view.head,1,1,1300000000)];let before=a.state().clone();
    let proofs=a.prepare_batch(&input,verify).unwrap();let mut bytes=policy::encode_witnesses(&proofs).unwrap();
    bytes[34+1+256*32+80+32+8+31]^=1;
    let changed=policy::decode_witnesses(&bytes).unwrap();
    assert!(matches!(policy::resume(before.view.state_commitment,&before,&input,&changed,verify),Err(Error::IndexProof)));
}

#[test] fn state_binds_admission_order_even_when_map_and_head_are_identical() {
    let cp=checkpoint();let mut a=Archive::new(&cp);let mut b=Archive::new(&cp);let anchor=a.state().view.anchor;
    let main=raw(anchor,1,100,1300000000);let x=raw(anchor,2,1,1300000001);let y=raw(anchor,3,2,1300000002);
    a.prepare_batch(&[main.clone(),x.clone(),y.clone()],verify).unwrap();b.prepare_batch(&[main,y,x],verify).unwrap();
    assert_eq!(a.state().known_root,b.state().known_root);assert_eq!(a.state().view.head,b.state().view.head);
    assert_ne!(a.state().known_log,b.state().known_log);assert_ne!(a.state().view.state_commitment,b.state().view.state_commitment);
}
