mod support;
use support::*;
use satoshifi_resume_transition::{self as t,bounded::{self,archive::Archive},Request,InputError};

#[test] fn all_scenarios_bind_both_complete_v2_states() {
    for (_,r) in scenarios() {
        let result=execute(&r);let j=result.journal;let wire=j.encode_abi();
        assert_eq!(wire.len(),1216);assert_eq!(&wire[..t::DOMAIN.len()],t::DOMAIN);
        for (at,s) in [(7,j.before()),(8,j.after())] {assert_eq!(&wire[at*32..(at+1)*32],s.commitment());}
        for (at,s) in [(32,j.before()),(35,j.after())] {
            assert_eq!(&wire[at*32..(at+1)*32],s.known_root);assert_eq!(&wire[(at+1)*32..(at+2)*32],s.known_log);
            assert_eq!(&wire[(at+3)*32-8..(at+3)*32],s.known_count.to_be_bytes());
        }
        assert_eq!(j.after().known_count-j.before().known_count,j.time_summary().count());
    }
}
#[test] fn roundtrip_input_and_direct_execution_are_identical() {
    for (_,r) in scenarios() {let wire=r.encode().unwrap();let decoded=Request::decode(&wire).unwrap();
        assert_eq!(decoded.encode().unwrap(),wire);
        let direct=t::execute(r.deployment,r.expected,&r.before,&r.headers,&r.witnesses,verify).unwrap();
        let encoded=t::execute_encoded(&wire,verify).unwrap();assert_eq!(direct.state,encoded.state);
        assert_eq!(direct.journal.encode_abi(),encoded.journal.encode_abi());
    }
}
#[test] fn append_input_has_constant_size_and_one_core_call() {
    let r=append();let wire=r.encode().unwrap();assert_eq!(wire.len(),31805);
    let mut calls=0;t::execute_encoded(&wire,|c,r,h|{calls+=1;verify(c,r,h)}).unwrap();assert_eq!(calls,1);
}
#[test] fn checkpoint_mismatch_and_external_stale_commitment_fail_before_core() {
    let r=append();let mut d=r.deployment.clone();d.checkpoint_descriptor[0]^=1;
    let reject=|_:&_,_:&_,_|->Result<_,&str>{panic!("must not reach core")};
    assert!(matches!(t::execute(d,r.expected,&r.before,&r.headers,&r.witnesses,reject),Err(t::Error::CheckpointDescriptor)));
    assert!(matches!(t::execute(r.deployment,[0;32],&r.before,&r.headers,&r.witnesses,reject),Err(t::Error::Resume(bounded::Error::State))));
}
#[test] fn altered_state_fields_cannot_be_resealed_by_untrusted_input() {
    let base=append();for field in 0..12 {
        let mut r=base.clone();let s=&mut r.before;
        match field {0=>s.known_root[0]^=1,1=>s.known_log[0]^=1,2=>s.known_count+=1,
            3=>s.view.anchor[0]^=1,4=>s.view.anchor_height+=2016,5=>s.view.head[0]^=1,
            6=>s.view.height+=1,7=>s.view.work[31]^=1,8=>s.view.timestamp^=1,9=>s.view.canonical_root[0]^=1,
            10=>s.view.state_commitment[0]^=1,_=>s.checkpoint_descriptor[0]^=1}
        if field<10 {s.view.state_commitment=s.commitment();}
        assert!(r.encode().map(|w|t::execute_encoded(&w,verify).is_err()).unwrap_or(true));
    }
}
#[test] fn replay_and_noop_produce_no_journal() {
    let r=append();let next=execute(&r).state;
    assert!(t::execute(r.deployment.clone(),next.view.state_commitment,&next,&r.headers,&r.witnesses,verify).is_err());
    let mut a=Archive::new(&checkpoint());a.prepare_batch(&r.headers,verify).unwrap();
    let p=bounded::Witness::Known(a.opening(hash(&r.headers[0])).unwrap());let s=a.state();
    assert!(matches!(t::execute(r.deployment,s.view.state_commitment,s,&r.headers,&[p],verify),Err(t::Error::Resume(bounded::Error::NoChange))));
}
#[test] fn time_folds_all_new_branches_but_does_not_replace_canonical_tip_time() {
    for (name,r) in scenarios() {let j=execute(&r).journal;
        if ["nonmonotonic","losing_branch","halt_prefix"].contains(&name) {assert!(j.time_summary().maximum()>j.after().view.timestamp);}
        if name=="losing_branch" {assert!(!j.useful());assert_eq!(j.before().view.canonical_root,j.after().view.canonical_root);}
        if name=="shorter_heavier" {assert!(j.useful());assert!(j.after().view.height<j.before().view.height);}
    }
}
#[test] fn halt_retains_safe_prefix_commits_ignored_suffix_and_is_terminal() {
    let (_,mut r)=scenarios().pop().unwrap();let original=execute(&r);let j=&original.journal;
    assert!(original.state.view.halted);assert_eq!(j.halt_position(),2);assert_eq!(j.processed(),2);assert_eq!(j.submitted(),3);
    assert_eq!(j.halt().unwrap().depth,101);assert_eq!(j.halt().unwrap().old_head,j.after().view.head);
    r.headers[2]=vec![9,8,7];let changed=execute(&r);
    assert_eq!(changed.state,original.state);assert_ne!(changed.journal.batch_commitment(),j.batch_commitment());
    r.before=original.state;r.expected=r.before.view.state_commitment;
    assert_eq!(r.encode().unwrap_err(),InputError::Halted);
}
#[test] fn invalid_new_header_returns_no_prefix_or_mutation() {
    let mut r=append();let before=r.before.clone();r.headers[0][76..80].copy_from_slice(&0u32.to_le_bytes());
    assert!(t::execute_encoded(&r.encode().unwrap(),verify).is_err());assert_eq!(r.before,before);
}
#[test] fn duplicates_only_consume_positions_not_counts_or_time() {
    let mut a=Archive::new(&checkpoint());let one=raw(a.state().view.head,1,1,1300000000);
    let r=request(&mut a,vec![one.clone(),one.clone(),raw(hash(&one),2,1,1299999999)]);let j=execute(&r).journal;
    assert_eq!(j.processed(),3);assert_eq!(j.time_summary().count(),2);assert_eq!(j.time_summary().last(),1299999999);
    assert_eq!(j.after().known_count-j.before().known_count,2);
}
#[test] fn strict_input_domain_truncation_and_trailing_bytes() {
    let wire=append().encode().unwrap();
    for len in [0,31,32,115,147,399,401,402,403,483,487,wire.len()-1] {assert!(Request::decode(&wire[..len]).is_err());}
    let mut bad=wire.clone();bad[31]=1;assert_eq!(Request::decode(&bad).unwrap_err(),InputError::Format);
    let mut bad=wire;bad.push(0);assert_eq!(Request::decode(&bad).unwrap_err(),InputError::Trailing);
}
#[test] fn untrusted_counts_and_lengths_are_bounded_before_nested_allocation() {
    let wire=append().encode().unwrap();
    for n in [0u16,65,u16::MAX] {let mut bad=wire.clone();bad[400..402].copy_from_slice(&n.to_be_bytes());assert_eq!(Request::decode(&bad).unwrap_err(),InputError::Limit);}
    let mut bad=wire.clone();bad[402..404].copy_from_slice(&5121u16.to_be_bytes());assert_eq!(Request::decode(&bad).unwrap_err(),InputError::Limit);
    let mut bad=wire;bad[484..488].copy_from_slice(&u32::MAX.to_be_bytes());assert_eq!(Request::decode(&bad).unwrap_err(),InputError::Limit);
    assert_eq!(Request::decode(&vec![0;t::MAX_INPUT_BYTES+1]).unwrap_err(),InputError::Limit);
    assert_eq!(t::MAX_INPUT_BYTES,2281208);
}
#[test] fn maximum_64_headers_roundtrip_and_65_are_rejected() {
    let mut a=Archive::new(&checkpoint());let mut parent=a.state().view.head;let mut input=vec![];
    for i in 1..=64 {let h=raw(parent,i,1,1300000000+i);parent=hash(&h);input.push(h);}
    let mut r=request(&mut a,input);let j=execute(&r).journal;assert_eq!(j.time_summary().count(),64);
    r.headers.push(vec![]);assert_eq!(r.encode().unwrap_err(),InputError::Limit);
}
#[test] fn deployment_is_bound_in_output_without_host_clock_or_network_selector() {
    let mut r=append();let old=execute(&r).journal.encode_abi();r.deployment.evm_chain_id=word(1);r.deployment.state_address=[2;20];
    let new=execute(&r).journal.encode_abi();assert_eq!(&new[64..96],word(1));assert_eq!(&new[108..128],&[2;20]);
    assert_eq!(&old[128..],&new[128..]);assert_eq!(&old[32..64],&new[32..64]);assert_ne!(old,new);
}
