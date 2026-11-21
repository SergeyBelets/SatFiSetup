use super::satoshifi_resume_transition as transition;
fn deployment(cp:&ValidatedCheckpoint)->transition::Deployment {
    let mut chain=[0;32];chain[30..].copy_from_slice(&31337u16.to_be_bytes());
    transition::Deployment {evm_chain_id:chain,state_address:[0x11;20],checkpoint_descriptor:cp.descriptor_commitment()}
}
fn request(a:&mut bounded::archive::Archive,input:Vec<Vec<u8>>)->transition::Request {
    let before=a.state().clone();let witnesses=a.prepare_batch(&input,verify_context).unwrap();
    transition::Request {deployment:deployment(&anchor()),expected:before.view.state_commitment,before,headers:input,witnesses}
}
#[test] fn v2_journal_native_both_retarget_inputs_execute_one_new_header() {
    let cp=anchor();let mut archive=bounded::archive::Archive::new(&cp);let mut next=32257;
    for boundary in [34272,36288] {
        let headers:Vec<Vec<u8>>=(next..boundary).map(header).collect();
        for chunk in headers.chunks(64) {archive.prepare_batch(chunk,verify_context).unwrap();}
        let r=request(&mut archive,vec![header(boundary)]);let bytes=r.encode().unwrap();assert_eq!(bytes.len(),31805);
        let mut calls=0;
        let out=transition::execute_encoded(&bytes,|c,r,h|{calls+=1;verify_context(c,r,h)}).unwrap();
        assert_eq!(calls,1);assert_eq!(&out.state,archive.state());assert_eq!(out.state.view.work,work(boundary));
        assert_eq!(out.journal.time_summary().count(),1);assert!(out.journal.useful());
        let journal=out.journal.encode_abi();assert_eq!(journal.len(),1216);
        assert_eq!(&journal[8*32..9*32],archive.state().commitment());
        assert_eq!(&journal[35*32..36*32],archive.state().known_root);
        println!("V2 journal native retarget {boundary}: request_bytes={}, journal_bytes={}, consensus_calls={calls}",bytes.len(),journal.len());
        next=boundary+1;
    }
}
#[test] fn v2_native_encoded_overlap_counts_only_unique_new_header() {
    let mut a=bounded::archive::Archive::new(&anchor());a.prepare_batch(&[header(32257)],verify_context).unwrap();
    let r=request(&mut a,vec![header(32257),header(32258),header(32258)]);
    let out=transition::execute_encoded(&r.encode().unwrap(),verify_context).unwrap();
    assert_eq!(out.journal.processed(),3);assert_eq!(out.journal.time_summary().count(),1);
    assert_eq!(out.state.known_count,r.before.known_count+1);assert_eq!(out.state.view.work,work(32258));
}
#[test] fn v2_native_invalid_pow_has_no_journal_with_structurally_valid_input() {
    let mut a=bounded::archive::Archive::new(&anchor());let before=a.state().clone();
    let mut invalid=header(32257);invalid[76]^=1;let input=vec![invalid];
    let witnesses=a.prepare_batch(&input,|c,raw,height| {
        let valid:[u8;80]=header(32257).try_into().unwrap();let mut next=verify_context(c,&valid,height)?;
        let inner=BitcoinHeaderWrapper::deserialize(&mut raw.as_slice()).unwrap();
        next.block_hash=bitcoin::hashes::Hash::to_byte_array(inner.deref().block_hash());Ok::<_,ValidationError>(next)
    }).unwrap();
    let r=transition::Request {deployment:deployment(&anchor()),expected:before.view.state_commitment,before,headers:input,witnesses};
    assert!(matches!(transition::execute_encoded(&r.encode().unwrap(),verify_context),
        Err(transition::RunError::Transition(transition::Error::Resume(bounded::Error::Header(_))))));
}
#[test] fn v2_native_stale_before_state_is_rejected_before_core() {
    let mut a=bounded::archive::Archive::new(&anchor());let mut r=request(&mut a,vec![header(32257)]);
    r.before.known_log[0]^=1;r.before.view.state_commitment=r.before.commitment();let mut calls=0;
    assert!(transition::execute_encoded(&r.encode().unwrap(),|c,r,h|{calls+=1;verify_context(c,r,h)}).is_err());assert_eq!(calls,0);
}
