// Actual native verifier integration; no synthetic PoW or alternate header core.
use super::{BitcoinVerifier, ValidationError};
use super::satoshifi_fork_policy::{self as policy, Context, ForkState, Hash, Error};
use crate::spec::{header::{BitcoinHeaderWrapper, HeaderWrapper}, RollupParams};
use borsh::BorshDeserialize;
use sov_rollup_interface::{da::{DaVerifier, LatestDaState}, Network};
use std::ops::Deref;

#[derive(Clone)]
struct NativeContext(LatestDaState);
impl Context for NativeContext {
    fn work(&self)->Hash { self.0.total_work }
    fn commitment(&self)->Hash {
        let timestamps:Vec<u8>=self.0.prev_11_timestamps.iter().flat_map(|t|t.to_be_bytes()).collect();
        policy::digest(b"citrea-LatestDaState-v1", &[&self.0.block_hash,&self.0.block_height.to_be_bytes(),
            &self.0.total_work,&self.0.current_target_bits.to_be_bytes(),&self.0.epoch_start_time.to_be_bytes(),&timestamps])
    }
}
fn advance(parent:&NativeContext,raw:&[u8;80],height:u64)->Result<NativeContext,ValidationError> {
    let inner=BitcoinHeaderWrapper::deserialize(&mut raw.as_slice()).unwrap();
    let header=HeaderWrapper::new(*inner.deref(),0,height,[0;32]);
    let verifier=BitcoinVerifier::new(RollupParams { reveal_tx_prefix:vec![],network:Network::Nightly });
    verifier.verify_header_chain_mainnet(&parent.0,&header).map(NativeContext)
}
fn setup(initial:LatestDaState,text:&str)->(Vec<u8>,u64,NativeContext,Vec<Vec<u8>>) {
    let all:Vec<Vec<u8>>=text.lines().map(|line|hex::decode(line).unwrap()).collect();
    assert_eq!(all.len(),2037);
    let first=initial.block_height+1;
    let height=((first+2015)/2016)*2016;
    let offset=(height-first) as usize;
    let mut ctx=NativeContext(initial);
    for (i,raw) in all[..=offset].iter().enumerate() { ctx=advance(&ctx,raw.as_slice().try_into().unwrap(),first+i as u64).unwrap(); }
    (all[offset].clone(),height,ctx,all[offset+1..].to_vec())
}
fn historical(initial:LatestDaState,text:&str) {
    let (anchor,height,ctx,remaining)=setup(initial,text);
    let mut state=ForkState::from_validated_anchor(&anchor,height,ctx.clone()).unwrap();
    let mut direct=ctx.clone();
    for (index,raw) in remaining.iter().enumerate() { direct=advance(&direct,raw.as_slice().try_into().unwrap(),height+index as u64+1).unwrap(); }
    for chunk in remaining.chunks(64) { state.apply_batch(chunk,advance).unwrap(); }
    assert_eq!(state.context(state.view().head).unwrap().commitment(),direct.commitment());
    assert_eq!(state.view().head,direct.0.block_hash); assert_eq!(state.view().work,direct.0.total_work);
    let view=state.view();
    for h in [height,height+1,height+2015,view.height] {
        let hash=state.ancestor(view.head,h).unwrap();
        assert!(policy::verify_membership(&view,&state.membership(hash).unwrap()));
        assert_eq!(state.confirmations(hash),Some(view.height-h+1));
    }
    let snapshot=state.snapshot_headers();
    let restored=ForkState::restore(ForkState::from_validated_anchor(&anchor,height,ctx).unwrap(),
        &snapshot,view.state_commitment,advance).unwrap();
    assert_eq!(restored.view(),view);
    println!("native fork policy anchor={height} verified descendants={} history_nodes={} root={}",
        remaining.len(),state.retained_history_nodes(),hex::encode(view.canonical_root));
}
#[test] fn historical_old_mainnet_history_and_restore() { historical(initial_old(),OLD); }
#[test] fn historical_recent_mainnet_history_and_restore() { historical(initial_new(),NEW); }
#[test] fn actual_verifier_batch_unknown_parent_rolls_back_all_history() {
    let (raw,height,ctx,remaining)=setup(initial_old(),OLD);
    let mut state=ForkState::from_validated_anchor(&raw,height,ctx).unwrap();
    let before=state.view(); let nodes=state.retained_history_nodes();
    assert!(matches!(state.apply_batch(&[remaining[0].clone(),remaining[2].clone()],advance),Err(Error::UnknownParent)));
    assert_eq!(state.view(),before); assert_eq!(state.retained_history_nodes(),nodes);
    let result=state.apply_batch(&[remaining[0].clone(),remaining[0].clone(),remaining[1].clone()],advance).unwrap();
    assert_eq!(result.new_headers,2); assert_eq!(result.processed,3);
}
#[test] fn snapshot_requires_authenticated_commitment_and_exact_checkpoint_context() {
    let (raw,height,ctx,remaining)=setup(initial_old(),OLD);
    let mut state=ForkState::from_validated_anchor(&raw,height,ctx.clone()).unwrap();
    state.apply_batch(&remaining[..4],advance).unwrap();
    let snapshot=state.snapshot_headers(); let expected=state.view().state_commitment;
    let mut altered=ctx.clone(); altered.0.total_work[31]^=1;
    assert!(ForkState::restore(ForkState::from_validated_anchor(&raw,height,altered).unwrap(),&snapshot,expected,advance).is_err());
    assert!(ForkState::restore(ForkState::from_validated_anchor(&raw,height,ctx).unwrap(),&snapshot,[0;32],advance).is_err());
}
