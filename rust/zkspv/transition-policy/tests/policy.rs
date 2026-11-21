mod support;
use support::*;
use satoshifi_transition_policy::{self as policy, Error, StepError, fork, ABI_LEN};

#[test] fn complete_journal_binds_both_views_and_deployment() {
    let s=state(); let before=s.view(); let input=raw(before.head,1,1,1300000000);
    let result=run(&s,&[input.clone()]).unwrap(); let j=&result.journal;
    assert_eq!(j.before(),&before); assert_eq!(j.after(),&result.state.view()); assert_eq!(j.deployment(),&deployment());
    assert_eq!(j.batch_commitment(),fork::digest(b"submitted-batch-v1",&[&input]));
    assert_eq!((j.submitted(),j.processed(),j.time_summary().count()),(1,1,1)); assert!(j.useful());
    assert_eq!(s.view(),before);
}
#[test] fn abi_has_exact_width_domains_padding_and_time_substatement() {
    let cases=scenarios(); let j=&cases[0].1.journal; let abi=j.encode_abi();
    assert_eq!(abi.len(),ABI_LEN); assert_eq!(&abi[..policy::DOMAIN.len()],policy::DOMAIN);
    assert_eq!(&abi[32..32+policy::NETWORK.len()],policy::NETWORK);
    assert_eq!(&abi[3*32..3*32+12],&[0;12]); assert_eq!(&abi[3*32+12..4*32],&[0x11;20]);
    assert_eq!(&abi[7*32..8*32],&j.before().state_commitment);
    assert_eq!(&abi[8*32..9*32],&j.after().state_commitment);
    assert_eq!(&abi[22*32..26*32],&j.time_summary().encode_abi());
    assert_eq!(&abi[26*32..],&[0;6*32]);
}
#[test] fn snapshot_and_exact_duplicates_do_not_enter_new_time_summary() {
    let mut s=state(); let a=s.view().anchor; let old=raw(a,1,1,1400000000);
    s.apply_batch(&[old.clone()],verify).unwrap(); let next=raw(hash(&old),2,1,1300000000);
    let r=run(&s,&[old.clone(),next.clone(),next,old]).unwrap(); let j=r.journal;
    assert_eq!((j.submitted(),j.processed(),j.time_summary().count()),(4,4,1));
    assert_eq!(j.time_summary().maximum(),1300000000);
}
#[test] fn nonmonotonic_time_and_canonical_time_remain_distinct() {
    let cases=scenarios(); let j=&cases[1].1.journal;
    assert_eq!(j.time_summary().maximum(),1300000000);
    assert_eq!(j.time_summary().last(),1299999900); assert_eq!(j.after().timestamp,1299999900);
}
#[test] fn losing_branch_is_retained_without_useful_head_refresh() {
    let cases=scenarios(); let j=&cases[2].1.journal;
    assert!(!j.useful()); assert_eq!(j.before().head,j.after().head);
    assert_eq!(j.before().canonical_root,j.after().canonical_root);
    assert_ne!(j.before().state_commitment,j.after().state_commitment);
    assert!(j.time_summary().maximum()>j.after().timestamp);
}
#[test] fn shorter_heavier_and_depth_100_switches_survive_integration() {
    let cases=scenarios();
    for i in [3,4] { let j=&cases[i].1.journal;
        assert!(j.useful()); assert!(j.after().height<j.before().height);
        assert_ne!(j.before().head,j.after().head); assert!(!j.after().halted);
    }
}
#[test] fn halt_keeps_safe_prefix_and_binds_stop_and_ignored_suffix() {
    let cases=scenarios(); let j=&cases[5].1.journal;
    assert_eq!((j.submitted(),j.processed(),j.time_summary().count(),j.halt_position()),(3,2,2,2));
    assert!(j.after().halted); assert!(j.useful()); assert_eq!(j.after().height,j.before().height+1);
    assert_eq!(j.halt().unwrap().depth,101); assert_eq!(j.halt().unwrap().old_head,j.after().head);
    assert_eq!(j.time_summary().maximum(),1300099999);
    let mut s=state(); let a=s.view().anchor; let mut serial=0; let old=chain(&mut s,a,100,&mut serial,1);
    let safe=raw(old,101,1,1300000101); let forbidden=raw(a,102,102,1300099999);
    let alternate=run(&s,&[safe,forbidden,vec![2]]).unwrap();
    assert_eq!(alternate.journal.after(),j.after()); assert_ne!(alternate.journal.batch_commitment(),j.batch_commitment());
}
#[test] fn invalid_unknown_header_returns_no_journal_or_partial_state() {
    let s=state(); let before=s.view(); let good=raw(before.head,1,1,1300000000);
    let bad=raw(hash(&good),2,0,1300000001);
    assert!(matches!(run(&s,&[good,bad]),Err(Error::Batch(fork::Error::Header(StepError::Header(_))))));
    assert_eq!(s.view(),before); assert_eq!(s.known_count(),1);
}
#[test] fn malformed_and_unknown_parent_are_not_noops() {
    let s=state();
    assert!(matches!(run(&s,&[vec![0;79]]),Err(Error::Batch(fork::Error::MalformedHeader))));
    assert!(matches!(run(&s,&[raw([77;32],1,1,1300000000)]),Err(Error::Batch(fork::Error::UnknownParent))));
}
#[test] fn pure_overlap_returns_explicit_no_change() {
    let s=state();
    assert!(matches!(run(&s,&[checkpoint().raw_header().to_vec()]),Err(Error::NoChange)));
}
#[test] fn every_accepted_view_field_must_match_reconstructed_state() {
    let s=state(); let cp=checkpoint(); let input=raw(s.view().head,1,1,1300000000);
    for field in 0..8 {
        let mut view=s.view();
        match field { 0=>view.anchor[0]^=1,1=>view.anchor_height+=2016,2=>view.head[0]^=1,
            3=>view.height+=1,4=>view.work[31]^=1,5=>view.timestamp^=1,6=>view.canonical_root[0]^=1,_=>view.state_commitment[0]^=1 }
        assert!(policy::execute(&cp,deployment(),&view,&[],&[input.clone()],Clone::clone,verify).is_err());
    }
}
#[test] fn checkpoint_descriptor_must_match_validated_configuration() {
    let s=state(); let cp=checkpoint(); let mut d=deployment(); d.checkpoint_descriptor[0]^=1;
    assert!(matches!(policy::execute(&cp,d,&s.view(),&[],&[raw(s.view().head,1,1,1300000000)],Clone::clone,verify),Err(Error::CheckpointDescriptor)));
}
#[test] fn missing_reordered_or_changed_restore_history_is_rejected() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0; chain(&mut s,a,3,&mut serial,1);
    let snapshot=s.snapshot_headers(); let mut changed=snapshot.clone(); changed[1][36]^=1;
    let mut reversed=snapshot.clone(); reversed.reverse();
    let next=raw(s.view().head,4,1,1300000010);
    for records in [snapshot[..2].to_vec(),changed,reversed] {
        assert!(matches!(policy::execute(&checkpoint(),deployment(),&s.view(),&records,&[next.clone()],Clone::clone,verify),Err(Error::Restore(_))));
    }
}
#[test] fn batch_limits_and_permanent_halt_are_enforced() {
    let s=state(); assert!(matches!(run(&s,&[]),Err(Error::InvalidBatch)));
    assert!(matches!(run(&s,&vec![checkpoint().raw_header().to_vec();65]),Err(Error::InvalidBatch)));
    let cases=scenarios(); let halted=&cases[5].1.state;
    assert!(matches!(run(halted,&[vec![1]]),Err(Error::Halted)));
}
