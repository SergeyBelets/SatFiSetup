// Existing checkpoint native adapter is included above unchanged.
use super::satoshifi_resume_policy as bounded;
fn verify_context(c:&InitialContext,raw:&[u8;80],height:u64)->Result<InitialContext,ValidationError> {
    let n=advance(&bind(c),raw,height)?.0;
    Ok(InitialContext {block_hash:n.block_hash,block_height:n.block_height,total_work:n.total_work,
        current_target_bits:n.current_target_bits,epoch_start_time:n.epoch_start_time,prev_11_timestamps:n.prev_11_timestamps})
}
fn compare_state(full:&ForkState<NativeContext>,archive:&bounded::archive::Archive) {
    let mut actual=archive.state().view.clone();actual.state_commitment=full.view().state_commitment;
    assert_eq!(actual,full.view());
    assert_eq!(archive.entry(actual.head).unwrap().context.commitment(),full.context(actual.head).unwrap().commitment());
}
#[test] fn bounded_native_resume_at_both_retargets_uses_only_one_new_verification() {
    let cp=anchor();let mut archive=bounded::archive::Archive::new(&cp);let mut full=cp.initialize(bind).unwrap();
    let mut next=32257;
    for boundary in [34272,36288] {
        let headers:Vec<Vec<u8>>=(next..boundary).map(header).collect();
        for chunk in headers.chunks(64) {archive.prepare_batch(chunk,verify_context).unwrap();full.apply_batch(chunk,advance).unwrap();}
        compare_state(&full,&archive);
        let before=archive.state().clone();let input=vec![header(boundary)];
        let context=&archive.entry(before.view.head).unwrap().context;
        assert_eq!(context.current_target_bits,bits(boundary));
        assert_ne!(context.current_target_bits,bits(boundary-1));
        let witnesses=archive.prepare_batch(&input,verify_context).unwrap();let wire=bounded::encode_witnesses(&witnesses).unwrap();
        let witnesses=bounded::decode_witnesses(&wire).unwrap();let mut calls=0;
        let out=bounded::resume(before.view.state_commitment,&before,&input,&witnesses,|c,r,h|{calls+=1;verify_context(c,r,h)}).unwrap();
        assert_eq!(calls,1);assert_eq!(wire.len(),31317);assert_eq!(&out.state,archive.state());
        full.apply_batch(&input,advance).unwrap();compare_state(&full,&archive);
        assert_eq!(out.state.view.work,work(boundary));assert_eq!(out.summary.count(),1);
        println!("V2 native resume at {boundary}: archive headers={}, witness_bytes={}, new_verifier_calls={calls}",before.known_count,wire.len());
        next=boundary+1;
    }
}
#[test] fn bounded_native_overlap_and_history_rebuild_preserve_exact_context() {
    let cp=anchor();let mut archive=bounded::archive::Archive::new(&cp);
    let records:Vec<Vec<u8>>=(32257..=32320).map(header).collect();archive.prepare_batch(&records,verify_context).unwrap();
    let mut restored=bounded::archive::Archive::new(&cp);restored.prepare_batch(&archive.snapshot_headers(),verify_context).unwrap();
    assert_eq!(restored.state(),archive.state());
    let before=archive.state().clone();let input=vec![header(32257),header(32321),header(32321)];
    let a=archive.prepare_batch(&input,verify_context).unwrap();let b=restored.prepare_batch(&input,verify_context).unwrap();
    assert_eq!(bounded::encode_witnesses(&a).unwrap(),bounded::encode_witnesses(&b).unwrap());
    let mut calls=0;let out=bounded::resume(before.view.state_commitment,&before,&input,&a,|c,r,h|{calls+=1;verify_context(c,r,h)}).unwrap();
    assert_eq!(calls,1);assert_eq!(out.summary.count(),1);assert_eq!(out.state.view.work,work(32321));
}
#[test] fn bounded_native_invalid_pow_is_rejected_even_with_structurally_valid_witness() {
    let mut archive=bounded::archive::Archive::new(&anchor());let before=archive.state().clone();
    let mut invalid=header(32257);invalid[76]^=1;let input=vec![invalid];
    // Test-only witness construction; the actual verifier below must reject it.
    let witnesses=archive.prepare_batch(&input,|c,raw,height| {
        let valid:[u8;80]=header(32257).try_into().unwrap();let mut next=verify_context(c,&valid,height)?;
        let inner=BitcoinHeaderWrapper::deserialize(&mut raw.as_slice()).unwrap();
        next.block_hash=bitcoin::hashes::Hash::to_byte_array(inner.deref().block_hash());
        Ok::<_,ValidationError>(next)
    }).unwrap();
    assert!(matches!(bounded::resume(before.view.state_commitment,&before,&input,&witnesses,verify_context),Err(bounded::Error::Header(_))));
}
#[test] fn bounded_native_forged_mtp_context_fails_before_any_consensus_call() {
    let mut archive=bounded::archive::Archive::new(&anchor());let before=archive.state().clone();let input=vec![header(32257)];
    let mut witnesses=archive.prepare_batch(&input,verify_context).unwrap();
    let bounded::Witness::New {parent,..}=&mut witnesses[0] else {panic!()};parent.entry.context.prev_11_timestamps[0]^=1;
    let mut calls=0;
    assert!(matches!(bounded::resume(before.view.state_commitment,&before,&input,&witnesses,|c,r,h|{calls+=1;verify_context(c,r,h)}),Err(bounded::Error::IndexProof)));
    assert_eq!(calls,0);
}
