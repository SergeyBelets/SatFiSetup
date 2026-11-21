// Integration with the actual hash-pinned, five-correction bitcoin-da verifier.
use super::{BitcoinVerifier, ValidationError};
use super::satoshifi_checkpoint_policy::{ValidatedCheckpoint, InitialContext, Error};
use super::satoshifi_fork_policy::{self as policy, Context, ForkState, Hash};
use crate::spec::{header::{BitcoinHeaderWrapper, HeaderWrapper}, RollupParams};
use borsh::BorshDeserialize;
use sov_rollup_interface::{da::{DaVerifier, LatestDaState}, Network};
use std::ops::Deref;

#[derive(Clone)]
struct NativeContext(LatestDaState);
impl Context for NativeContext {
    fn work(&self) -> Hash { self.0.total_work }
    fn commitment(&self) -> Hash {
        let times: Vec<u8> = self.0.prev_11_timestamps.iter().flat_map(|t|t.to_be_bytes()).collect();
        policy::digest(b"citrea-LatestDaState-v1", &[&self.0.block_hash,&self.0.block_height.to_be_bytes(),
            &self.0.total_work,&self.0.current_target_bits.to_be_bytes(),&self.0.epoch_start_time.to_be_bytes(),&times])
    }
}
fn bind(c: &InitialContext) -> NativeContext {
    NativeContext(LatestDaState { block_hash:c.block_hash,block_height:c.block_height,total_work:c.total_work,
        current_target_bits:c.current_target_bits,epoch_start_time:c.epoch_start_time,prev_11_timestamps:c.prev_11_timestamps })
}
fn advance(parent:&NativeContext,raw:&[u8;80],height:u64)->Result<NativeContext,ValidationError> {
    let inner=BitcoinHeaderWrapper::deserialize(&mut raw.as_slice()).unwrap();
    let header=HeaderWrapper::new(*inner.deref(),0,height,[0;32]);
    let verifier=BitcoinVerifier::new(RollupParams { reveal_tx_prefix:vec![],network:Network::Nightly });
    verifier.verify_header_chain_mainnet(&parent.0,&header).map(NativeContext)
}
fn header(h:u64)->Vec<u8> { let i=(h-32246) as usize*80; HEADERS[i..i+80].to_vec() }
fn work(h:u64)->Hash { let i=(h-32246) as usize*68+32; EXPECTED[i..i+32].try_into().unwrap() }
fn time(h:u64)->u32 { u32::from_le_bytes(header(h)[68..72].try_into().unwrap()) }
fn bits(h:u64)->u32 { u32::from_le_bytes(header(h)[72..76].try_into().unwrap()) }
fn anchor()->ValidatedCheckpoint {
    ValidatedCheckpoint::validate(&header(32256),32256,work(32256),&(32246..32256).map(header).collect::<Vec<_>>()).unwrap()
}

#[test] fn checkpoint_factory_mainnet_two_retargets_every_field_and_restore() {
    let cp=anchor(); let mut state=cp.initialize(bind).unwrap();
    let all:Vec<Vec<u8>>=(32257..=36288).map(header).collect();
    for (i,raw) in all.iter().enumerate() {
        let h=32257+i as u64; state.apply_batch(&[raw.clone()],advance).unwrap();
        let ctx=&state.context(state.view().head).unwrap().0;
        let mut hash:Hash=EXPECTED[(h-32246) as usize*68..(h-32246) as usize*68+32].try_into().unwrap(); hash.reverse();
        assert_eq!(ctx.block_hash,hash); assert_eq!(ctx.block_height,h); assert_eq!(ctx.total_work,work(h));
        assert_eq!(ctx.epoch_start_time,time(h-h%2016));
        assert_eq!(ctx.current_target_bits,bits(if h%2016==2015 {h+1} else {h}));
        for prior in h-10..=h { assert_eq!(ctx.prev_11_timestamps[(prior%11) as usize],time(prior)); }
    }
    let view=state.view(); let restored=ForkState::restore(cp.initialize(bind).unwrap(),&state.snapshot_headers(),view.state_commitment,advance).unwrap();
    assert_eq!(restored.view(),view);
    assert_eq!(restored.context(view.head).unwrap().commitment(),state.context(view.head).unwrap().commitment());
    assert_eq!(restored.confirmations(cp.context().block_hash),Some(4033));
    println!("checkpoint factory: 4032 mainnet descendants, 2 retargets, every native state field, complete replay verified");
}

fn compare_prepared_context(initial:LatestDaState,text:&str) {
    let headers:Vec<Vec<u8>>=text.lines().map(|s|hex::decode(s).unwrap()).collect();
    let first=initial.block_height+1; let height=((first+2015)/2016)*2016; let at=(height-first) as usize;
    assert_eq!(at,10);
    let mut direct=NativeContext(initial);
    for (i,raw) in headers[..=at].iter().enumerate() { direct=advance(&direct,raw.as_slice().try_into().unwrap(),first+i as u64).unwrap(); }
    let cp=ValidatedCheckpoint::validate(&headers[at],height,direct.0.total_work,&headers[at-10..at]).unwrap();
    let mut state=cp.initialize(bind).unwrap();
    assert_eq!(state.context(state.view().head).unwrap().commitment(),direct.commitment());
    for (i,raw) in headers[at+1..at+13].iter().enumerate() {
        direct=advance(&direct,raw.as_slice().try_into().unwrap(),height+i as u64+1).unwrap();
        state.apply_batch(&[raw.clone()],advance).unwrap();
        assert_eq!(state.context(state.view().head).unwrap().commitment(),direct.commitment());
    }
}
#[test] fn checkpoint_factory_matches_old_native_preparation() { compare_prepared_context(initial_old(),OLD); }
#[test] fn checkpoint_factory_matches_recent_native_preparation() { compare_prepared_context(initial_new(),NEW); }

#[test] fn checkpoint_factory_rejects_native_context_binding_mismatch() {
    let cp=anchor();
    for field in 0..16 {
        assert!(matches!(cp.initialize(|c| {
            let mut n=bind(c);
            match field { 0=>n.0.block_hash[0]^=1,1=>n.0.block_height+=1,2=>n.0.total_work[31]^=1,
                3=>n.0.current_target_bits^=1,4=>n.0.epoch_start_time^=1,i=>n.0.prev_11_timestamps[i-5]^=1 }
            n
        }),Err(Error::ContextBinding)));
    }
}
#[test] fn checkpoint_factory_trusted_work_cannot_bypass_checked_addition() {
    let cp=ValidatedCheckpoint::validate(&header(32256),32256,[255;32],&(32246..32256).map(header).collect::<Vec<_>>()).unwrap();
    let mut state=cp.initialize(bind).unwrap(); let before=state.view();
    assert!(matches!(state.apply_batch(&[header(32257)],advance),Err(policy::Error::Header(ValidationError::ChainworkOverflow))));
    assert_eq!(state.view(),before); assert_eq!(state.known_count(),1);
}
