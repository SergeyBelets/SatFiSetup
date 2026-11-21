mod support;
use support::*;
use satoshifi_fork_policy::{Context, Error, ForkState, verify_membership};

#[test]
fn invalid_anchor_shape_is_rejected_without_claiming_checkpoint_validation() {
    for height in [0, 10, 32255, 32257] {
        assert!(ForkState::from_validated_anchor(&anchor_raw(), height, initial_context()).is_err());
    }
    assert!(ForkState::from_validated_anchor(&[],32256,initial_context()).is_err());
    let mut ctx=initial_context(); ctx.work=[0;32];
    assert!(ForkState::from_validated_anchor(&anchor_raw(),32256,ctx).is_err());
}

#[test]
fn equal_and_less_work_keep_head_but_side_branch_can_continue() {
    let mut s=state(); let a=s.view().anchor;
    let old=add(&mut s,a,1,10);
    let side=add(&mut s,a,2,9);
    assert_eq!(s.view().head,old);
    let side=add(&mut s,side,3,1);
    assert_eq!(s.view().head,old); // same total work, greater height
    let side=add(&mut s,side,4,1);
    assert_eq!(s.view().head,side);
    assert_eq!(s.confirmations(old),None);
    assert_eq!(s.confirmations(a),Some(4));
}

#[test]
fn reorg_exactly_100_is_accepted() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0;
    let old=chain(&mut s,a,100,&mut serial,1);
    let side=chain(&mut s,a,100,&mut serial,1);
    assert_eq!(s.view().head,old); assert!(!s.view().halted);
    let winner=add(&mut s,side,201,1);
    assert_eq!(s.view().head,winner); assert!(!s.view().halted);
    assert_eq!(s.common_ancestor(old,winner),Some(a));
}

#[test]
fn reorg_101_halts_only_when_candidate_becomes_strictly_heavier() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0;
    let old=chain(&mut s,a,101,&mut serial,1);
    let old_view=s.view(); let old_proof=s.membership(old).unwrap();
    let side=chain(&mut s,a,101,&mut serial,1);
    assert_eq!(s.view().head,old); assert!(!s.view().halted);
    let out=s.apply_batch(&[raw(side,203,1,1300001000)],verify).unwrap();
    assert!(out.view.halted); assert_eq!(out.view.head,old);
    assert_eq!(out.halt.unwrap().depth,101); assert_eq!(out.halt_at,Some(0));
    assert_eq!(out.view.canonical_root,old_view.canonical_root);
    assert_eq!(s.confirmations(a),None); assert!(!verify_membership(&out.view,&old_proof));
    assert!(matches!(s.apply_batch(&[anchor_raw()],verify),Err(Error::Halted)));
}

#[test]
fn shorter_heavier_branch_drops_old_tail_from_canonical_membership() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0;
    let old=chain(&mut s,a,100,&mut serial,1);
    let old_view=s.view(); let old_proof=s.membership(old).unwrap();
    let winner=add(&mut s,a,101,101);
    assert_eq!(s.view().height,32257); assert_eq!(s.view().head,winner);
    assert_eq!(s.confirmations(old),None); assert!(s.membership(old).is_err());
    assert!(verify_membership(&old_view,&old_proof));
    assert!(!verify_membership(&s.view(),&old_proof));
    assert!(verify_membership(&s.view(),&s.membership(winner).unwrap()));
}

#[test]
fn safe_prefix_survives_halt_and_suffix_cannot_hide_it() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0;
    let old=chain(&mut s,a,100,&mut serial,1);
    let safe=raw(old,101,1,1300000001); let safe_hash=hash(&safe);
    let forbidden=raw(a,102,102,1300099999);
    let back=raw(safe_hash,103,1000,1300000002);
    let out=s.apply_batch(&[safe,forbidden,back,vec![1]],verify).unwrap();
    assert_eq!(out.halt_at,Some(1)); assert_eq!(out.processed,2); assert_eq!(out.new_headers,2);
    assert_eq!(out.view.head,safe_hash); assert_eq!(out.view.height,32357);
    assert_eq!(out.maximum_timestamp,Some(1300099999));
    assert_eq!(out.halt.unwrap().depth,101);
    assert_eq!(s.known_count(),103); // anchor + old 100 + safe + forbidden
}

#[test]
fn invalid_unknown_input_atomically_restores_all_state_and_allocations() {
    let mut s=state(); let a=s.view().anchor; let before=s.view(); let nodes=s.retained_history_nodes();
    let good=raw(a,1,1,1300000000); let bad=raw(hash(&good),2,0,1300000001);
    assert!(matches!(s.apply_batch(&[good,bad],verify),Err(Error::Header(_))));
    assert_eq!(s.view(),before); assert_eq!(s.known_count(),1); assert_eq!(s.retained_history_nodes(),nodes);
    assert!(s.snapshot_headers().is_empty());
}

#[test]
fn malformed_and_unknown_parent_are_not_skipped_after_known_prefix() {
    let mut s=state(); let a=s.view().anchor; let good=raw(a,1,1,1300000000);
    s.apply_batch(&[good.clone()],verify).unwrap(); let before=s.view();
    assert!(matches!(s.apply_batch(&[good.clone(),vec![0;79]],verify),Err(Error::MalformedHeader)));
    assert!(matches!(s.apply_batch(&[good,raw([77;32],2,1,1300000001)],verify),Err(Error::UnknownParent)));
    assert_eq!(s.view(),before);
}

#[test]
fn exact_retries_do_not_add_work_or_time_or_change_commitment() {
    let mut s=state(); let a=s.view().anchor; let good=raw(a,1,1,1300000000);
    s.apply_batch(&[good.clone()],verify).unwrap(); let before=s.view();
    let out=s.apply_batch(&[anchor_raw(),good.clone(),good],verify).unwrap();
    assert_eq!(out.view,before); assert_eq!(out.previous_commitment,before.state_commitment);
    assert_eq!(out.new_headers,0); assert_eq!(out.maximum_timestamp,None); assert_eq!(out.processed,3);
}

#[test]
fn contexts_are_retained_per_branch_and_resumed_from_the_parent() {
    let mut s=state(); let a=s.view().anchor;
    let main=add(&mut s,a,1,100); let side=add(&mut s,a,2,1);
    let expected=verify(s.context(side).unwrap(),&raw(side,3,1,1300000003).try_into().unwrap(),32258).unwrap();
    let child=add(&mut s,side,3,1);
    assert_eq!(s.context(child),Some(&expected));
    assert_ne!(s.context(main).unwrap().commitment(),s.context(side).unwrap().commitment());
}

#[test]
fn witnesses_bind_anchor_height_position_hash_merkle_root_and_exact_depth() {
    let mut s=state(); let a=s.view().anchor; let h=add(&mut s,a,1,1);
    let view=s.view(); let proof=s.membership(h).unwrap(); assert!(verify_membership(&view,&proof));
    for offset in [0,4,36,68,72,76] {
        let mut p=proof.clone(); p.raw_header[offset]^=1; assert!(!verify_membership(&view,&p));
    }
    let mut p=proof.clone(); p.height-=1; assert!(!verify_membership(&view,&p));
    let mut p=proof.clone(); p.height=32255; assert!(!verify_membership(&view,&p));
    let mut p=proof.clone(); p.siblings.pop(); assert!(!verify_membership(&view,&p));
    let mut p=proof.clone(); p.siblings.push([0;32]); assert!(!verify_membership(&view,&p));
    let mut p=proof.clone(); p.siblings.reverse(); assert!(!verify_membership(&view,&p));
    for field in 0..4 {
        let mut v=view.clone(); match field { 0=>v.anchor[0]^=1, 1=>v.anchor_height+=2016,
            2=>v.canonical_root[0]^=1, _=>v.head[0]^=1 };
        assert!(!verify_membership(&v,&proof));
    }
}

#[test]
fn canonical_ancestor_can_be_reproved_after_append_without_deleting_history() {
    let mut s=state(); let a=s.view().anchor; let first=add(&mut s,a,1,1);
    let old=s.membership(first).unwrap(); let view=s.view();
    add(&mut s,first,2,1);
    assert!(verify_membership(&view,&old)); assert!(!verify_membership(&s.view(),&old));
    assert!(verify_membership(&s.view(),&s.membership(first).unwrap()));
    assert_eq!(s.confirmations(first),Some(2));
}

#[test]
fn complete_snapshot_replays_side_branches_and_continuation_exactly() {
    let mut s=state(); let a=s.view().anchor; let main=add(&mut s,a,1,10); let side=add(&mut s,a,2,1);
    let snapshot=s.snapshot_headers(); let expected=s.view().state_commitment;
    let mut restored=ForkState::restore(state(),&snapshot,expected,verify).unwrap();
    assert_eq!(s.view(),restored.view()); assert_eq!(s.context(side),restored.context(side));
    let input=raw(side,3,10,1300000003);
    assert_eq!(s.apply_batch(&[input.clone()],verify).unwrap(),restored.apply_batch(&[input],verify).unwrap());
    assert_eq!(restored.confirmations(main),None);
}

#[test]
fn incomplete_reordered_duplicated_or_changed_snapshot_is_rejected() {
    let mut s=state(); let a=s.view().anchor; let h=add(&mut s,a,1,1); add(&mut s,h,2,1);
    let records=s.snapshot_headers(); let expected=s.view().state_commitment;
    assert!(ForkState::restore(state(),&records[..1],expected,verify).is_err());
    let mut changed=records.clone(); changed.swap(0,1); assert!(ForkState::restore(state(),&changed,expected,verify).is_err());
    let mut changed=records.clone(); changed.push(records[0].clone()); assert!(ForkState::restore(state(),&changed,expected,verify).is_err());
    let mut changed=records.clone(); changed[1][36]^=1; assert!(ForkState::restore(state(),&changed,expected,verify).is_err());
    assert!(ForkState::restore(state(),&records,[0;32],verify).is_err());
}

#[test]
fn halted_snapshot_remains_halted_and_rejects_unprocessed_suffix() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0; chain(&mut s,a,101,&mut serial,1);
    add(&mut s,a,102,102); let view=s.view(); let records=s.snapshot_headers();
    let restored=ForkState::restore(state(),&records,view.state_commitment,verify).unwrap();
    assert_eq!(restored.view(),view); assert!(restored.view().halted);
    let mut extra=records; extra.push(raw(a,103,200,1300000000));
    assert!(ForkState::restore(state(),&extra,view.state_commitment,verify).is_err());
}

#[test]
fn skip_lca_matches_independent_parent_walk_on_deterministic_graph() {
    let mut s=state(); let a=s.view().anchor; let mut nodes=vec![a]; let mut parents=vec![0usize];
    let mut seed=1234567u32;
    for serial in 1..=700 {
        seed=seed.wrapping_mul(1664525).wrapping_add(1013904223);
        let parent=(seed as usize)%nodes.len();
        nodes.push(add(&mut s,nodes[parent],serial,1)); parents.push(parent);
    }
    for i in 1..nodes.len() {
        let j=(i*137)%nodes.len(); let mut seen=std::collections::BTreeSet::new(); let mut cursor=i;
        loop { seen.insert(cursor); if cursor==0 { break; } cursor=parents[cursor]; }
        let mut common=j; while !seen.contains(&common) { common=parents[common]; }
        assert_eq!(s.common_ancestor(nodes[i],nodes[j]),Some(nodes[common]));
    }
}

#[test]
fn long_losing_branch_has_persistent_history_and_only_heavier_tip_halts() {
    let mut s=state(); let a=s.view().anchor; let mut serial=0;
    let old=chain(&mut s,a,101,&mut serial,100000);
    let side=chain(&mut s,a,8192,&mut serial,1);
    assert!(!s.view().halted); assert_eq!(s.view().head,old);
    assert_eq!(s.common_ancestor(old,side),Some(a));
    assert_eq!(s.retained_history_nodes(),1+65*s.known_count());
    add(&mut s,side,9000,11000000); assert!(s.view().halted); assert_eq!(s.view().head,old);
}

#[test]
fn batch_limits_and_nonincreasing_work_fail_without_mutation() {
    let mut s=state(); let a=s.view().anchor; let before=s.view();
    assert!(matches!(s.apply_batch(&[],verify),Err(Error::InvalidBatch)));
    assert!(matches!(s.apply_batch(&vec![anchor_raw();65],verify),Err(Error::InvalidBatch)));
    let result=s.apply_batch(&[raw(a,1,1,1300000000)],|ctx,_,_| Ok::<_,()>(ctx.clone()));
    assert!(matches!(result,Err(Error::NonIncreasingWork))); assert_eq!(s.view(),before);
}
