// Helpers/types above are copied from the unchanged checkpoint integration.
use super::satoshifi_transition_policy as transition;
fn deployment()->transition::Deployment {
    let mut chain=[0;32]; chain[30..].copy_from_slice(&31337u16.to_be_bytes());
    transition::Deployment { evm_chain_id:chain,state_address:[0x11;20],checkpoint_descriptor:anchor().descriptor_commitment() }
}
fn prepared(height:u64)->ForkState<NativeContext> {
    let mut state=anchor().initialize(bind).unwrap();
    let records:Vec<Vec<u8>>=(32257..=height).map(header).collect();
    for chunk in records.chunks(64) { state.apply_batch(chunk,advance).unwrap(); }
    state
}
#[test] fn transition_actual_mainnet_restores_expected_bits_before_both_retargets() {
    for before_height in [34270,36286] {
        let state=prepared(before_height); let before=state.view(); let cp=anchor();
        let count=if before_height==34270 {3} else {2};
        let input:Vec<Vec<u8>>=(before_height+1..=before_height+count).map(header).collect();
        let snapshot=state.snapshot_headers();
        let result=transition::execute(&cp,deployment(),&before,&snapshot,&input,bind,advance).unwrap();
        let mut direct=state;
        direct.apply_batch(&input,advance).unwrap();
        assert_eq!(result.journal.before(),&before); assert_eq!(result.journal.after(),&direct.view());
        assert_eq!(result.state.context(direct.view().head).unwrap().commitment(),direct.context(direct.view().head).unwrap().commitment());
        assert_eq!(result.journal.after().work,work(before_height+count));
        assert_eq!(result.journal.after().timestamp,time(before_height+count));
        assert_eq!(result.journal.time_summary().count(),count);
        assert_eq!(result.journal.time_summary().maximum(),(before_height+1..=before_height+count).map(time).max().unwrap());
        assert!(result.journal.useful());
        println!("native complete journal transition {} -> {} restores retarget context",before_height,before_height+count);
    }
}
#[test] fn transition_actual_nonmonotonic_times_and_duplicate_overlap() {
    let height=(32257..36288).find(|h|time(*h)>time(*h+1)).unwrap();
    let state=prepared(height-1); let input=vec![header(height),header(height+1),header(height),header(32256)];
    let result=transition::execute(&anchor(),deployment(),&state.view(),&state.snapshot_headers(),&input,bind,advance).unwrap();
    let j=result.journal;
    assert_eq!(j.submitted(),4); assert_eq!(j.processed(),4); assert_eq!(j.time_summary().count(),2);
    assert_eq!(j.time_summary().maximum(),time(height)); assert_eq!(j.time_summary().last(),time(height+1));
    assert_eq!(j.after().timestamp,time(height+1)); assert_eq!(j.after().work,work(height+1));
    assert_eq!(&j.encode_abi()[22*32..26*32],&j.time_summary().encode_abi());
}
#[test] fn transition_actual_invalid_unknown_header_cannot_publish_safe_prefix() {
    let state=prepared(32256); let before=state.view(); let mut invalid=header(32258); invalid[76]^=1;
    let result=transition::execute(&anchor(),deployment(),&before,&[],&[header(32257),invalid],bind,advance);
    assert!(matches!(result,Err(transition::Error::Batch(policy::Error::Header(_)))));
    assert_eq!(state.view(),before); assert_eq!(state.known_count(),1);
}
#[test] fn transition_actual_replay_and_forged_accepted_metadata_fail_closed() {
    let state=prepared(32258); let snapshot=state.snapshot_headers();
    let no_change=transition::execute(&anchor(),deployment(),&state.view(),&snapshot,&[header(32257),header(32258)],bind,advance);
    assert!(matches!(no_change,Err(transition::Error::NoChange)));
    let mut accepted=state.view(); accepted.timestamp^=1;
    let result=transition::execute(&anchor(),deployment(),&accepted,&snapshot,&[header(32259)],bind,advance);
    assert!(matches!(result,Err(transition::Error::AcceptedStateMismatch)));
}
