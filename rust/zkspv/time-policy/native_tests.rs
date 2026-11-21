// Included only by the native qualification runner under verifier:: tests.
use super::{BitcoinVerifier, ValidationError};
use super::satoshifi_time_policy::{verify_batch, BatchError};
use crate::spec::{header::{BitcoinHeaderWrapper, HeaderWrapper}, RollupParams};
use borsh::BorshDeserialize;
use sov_rollup_interface::{da::{BlockHeaderTrait, DaVerifier, LatestDaState}, Network};
use std::ops::Deref;

fn verifier() -> BitcoinVerifier {
    BitcoinVerifier::new(RollupParams { reveal_tx_prefix: vec![], network: Network::Nightly })
}

fn headers(text: &str, first: u64) -> Vec<HeaderWrapper> {
    let result: Vec<_> = text.lines().enumerate().map(|(index, line)| {
        let bytes = hex::decode(line).unwrap();
        assert_eq!(bytes.len(), 80);
        let inner = BitcoinHeaderWrapper::deserialize(&mut bytes.as_slice()).unwrap();
        HeaderWrapper::new(*inner.deref(), 0, first + index as u64, [0; 32])
    }).collect();
    assert_eq!(result.len(), 2037);
    result
}

fn advance(state: &LatestDaState, header: &HeaderWrapper) -> Result<(LatestDaState, u32), ValidationError> {
    let next = verifier().verify_header_chain_mainnet(state, header)?;
    Ok((next, u32::try_from(header.time().secs()).unwrap()))
}

fn same_state(left: &LatestDaState, right: &LatestDaState) {
    assert_eq!(left.block_hash, right.block_hash);
    assert_eq!(left.block_height, right.block_height);
    assert_eq!(left.total_work, right.total_work);
    assert_eq!(left.current_target_bits, right.current_target_bits);
    assert_eq!(left.epoch_start_time, right.epoch_start_time);
    assert_eq!(left.prev_11_timestamps, right.prev_11_timestamps);
}

fn historical(initial: LatestDaState, text: &str) {
    let blocks = headers(text, initial.block_height + 1);
    let (final_state, summary) = verify_batch(initial.clone(), &blocks, advance).unwrap();
    let mut direct = initial.clone();
    for block in &blocks { direct = advance(&direct, block).unwrap().0; }
    same_state(&final_state, &direct);
    assert_eq!(summary.count(), 2037);
    assert_eq!(summary.maximum(), blocks.iter().map(|block| block.time().secs() as u32).max().unwrap());
    assert_eq!(summary.last(), blocks.last().unwrap().time().secs() as u32);

    // Every tested split includes both sides; state continuity is checked here.
    for split in [1, 11, 2016, blocks.len() - 1] {
        let (middle, left) = verify_batch(initial.clone(), &blocks[..split], advance).unwrap();
        let (end, right) = verify_batch(middle, &blocks[split..], advance).unwrap();
        same_state(&end, &final_state);
        assert_eq!(left.then(right).unwrap(), summary);
    }
    println!("time policy native headers={} maximum={} last={} abi={}",
        summary.count(), summary.maximum(), summary.last(), hex::encode(summary.encode_abi()));
}

#[test]
fn historical_mainnet_old_summary_and_chunks() { historical(initial_old(), OLD); }

#[test]
fn historical_mainnet_recent_summary_and_chunks() { historical(initial_new(), NEW); }

#[test]
fn real_nonmonotonic_header_times_preserve_an_earlier_maximum() {
    let initial = initial_old();
    let blocks = headers(OLD, initial.block_height + 1);
    let position = blocks.windows(2).position(|pair| pair[0].time().secs() > pair[1].time().secs()).unwrap() + 1;
    let (_, summary) = verify_batch(initial, &blocks[..=position], advance).unwrap();
    assert!(summary.maximum() > summary.last());
}

#[test]
fn rejected_transition_returns_no_state_summary_or_prefix() {
    let initial = initial_old();
    let blocks = headers(OLD, initial.block_height + 1);
    let result = verify_batch(initial.clone(), [&blocks[0], &blocks[0], &blocks[1]], advance);
    assert!(matches!(result, Err(BatchError::Header(ValidationError::NonConsecutiveBlockHeight))));
    same_state(&initial, &initial_old());
}

#[test]
fn upstream_mtp_check_is_required_before_summary_accumulation() {
    let mut context = initial_old();
    let blocks = headers(OLD, context.block_height + 1);
    // Context boundary test using an unchanged valid historical header.
    let time = blocks[0].time().secs() as u32;
    context.prev_11_timestamps = [time; 11];
    let result = verify_batch(context.clone(), [&blocks[0]], advance);
    assert!(matches!(result, Err(BatchError::Header(ValidationError::InvalidTimestamp))));
    context.prev_11_timestamps = [time - 1; 11];
    assert_eq!(verify_batch(context, [&blocks[0]], advance).unwrap().1.last(), time);
}
