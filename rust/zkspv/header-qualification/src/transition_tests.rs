use bitcoin::{CompactTarget, Target};
use crypto_bigint::{CheckedAdd, Encoding, U256};

const OLD: &str = include_str!("../../vendor/citrea-2ed54463/crates/bitcoin-da/test_data/mainnet/headers-40310-42346.txt");
const NEW: &str = include_str!("../../vendor/citrea-2ed54463/crates/bitcoin-da/test_data/mainnet/headers-872918-874954.txt");
const TESTNET: &str = include_str!("../fixtures/testnet4-40310-42346.txt");

fn headers(text: &str, start: u64) -> Vec<adapters::HeaderWrapper> {
    let result: Vec<_> = text.lines().enumerate().map(|(index, line)|
        adapters::HeaderWrapper::decode(line, start + index as u64)).collect();
    assert_eq!(result.len(), 2037, "do not silently truncate the fixture with zip");
    result
}

fn replay(initial: LatestDaState, text: &str, testnet4: bool) -> Vec<LatestDaState> {
    let blocks = headers(text, initial.block_height + 1);
    let mut original = initial.clone();
    let mut corrected = initial.clone();
    let mut work = U256::from_be_bytes(initial.total_work);
    let mut history = vec![initial];
    let mut retargets = 0;
    for header in blocks {
        let previous = corrected.clone();
        original = original::step(&original, &header, testnet4).expect("original historical transition");
        corrected = corrected::step(&corrected, &header, testnet4).expect("corrected historical transition");
        assert_eq!(original, corrected, "historical state at {}", header.height());

        // Independent rust-bitcoin hashing, PoW and per-block work checks.
        let target = Target::from_compact(CompactTarget::from_consensus(header.bits()));
        assert!(target > Target::ZERO && target <= Target::MAX_ATTAINABLE_MAINNET);
        assert_eq!(header.inner().validate_pow(target).unwrap(), header.inner().block_hash());
        let added = U256::from_be_bytes(header.inner().work().to_be_bytes());
        work = Option::<U256>::from(work.checked_add(&added)).unwrap();
        assert_eq!(corrected.total_work, work.to_be_bytes());
        assert_eq!(corrected.block_hash, header.hash().to_byte_array());
        assert_eq!(corrected.block_height, previous.block_height + 1);
        assert_eq!(header.prev_hash().to_byte_array(), previous.block_hash);
        let mut median = previous.prev_11_timestamps;
        median.sort_unstable();
        assert!(header.time().secs() > i64::from(median[5]));
        let mut timestamps = previous.prev_11_timestamps;
        timestamps[header.height() as usize % 11] = header.time().secs() as u32;
        assert_eq!(corrected.prev_11_timestamps, timestamps);
        assert_eq!(corrected.epoch_start_time,
            if header.height() % 2016 == 0 { header.time().secs() as u32 }
            else { previous.epoch_start_time });

        // current_target_bits is the NEXT header's policy, including the last
        // block of an epoch. Compare mainnet adjustment with pinned rust-bitcoin.
        if header.height() % 2016 == 2015 {
            retargets += 1;
            if !testnet4 {
                let elapsed = header.time().secs() - i64::from(corrected.epoch_start_time);
                assert!(elapsed >= 0, "historical fixture span");
                let next = CompactTarget::from_next_work_required(header.inner().bits,
                    elapsed as u64, bitcoin::Network::Bitcoin).to_consensus();
                assert_eq!(corrected.current_target_bits, next);
            }
        }
        history.push(corrected.clone());
    }
    assert!(retargets >= 1);
    println!("historical testnet4={testnet4} start={} end={} headers={} retargets={retargets} final_hash={} final_work={}",
        history[0].block_height, corrected.block_height, history.len() - 1,
        hex::encode(corrected.block_hash), hex::encode(corrected.total_work));
    history
}

#[test]
fn historical_mainnet_40310_through_42346() { replay(initial_old(), OLD, false); }

#[test]
fn historical_mainnet_872918_through_874954() { replay(initial_new(), NEW, false); }

#[test]
fn historical_testnet4_40310_through_42346() { replay(initial_testnet(), TESTNET, true); }

fn resume_at_epoch_boundary(initial: LatestDaState, text: &str, testnet4: bool) {
    let all = headers(text, initial.block_height + 1);
    let mut state = initial.clone();
    let mut saved = Vec::new();
    for (index, header) in all.iter().enumerate() {
        state = corrected::step(&state, header, testnet4).unwrap();
        if header.height() % 2016 == 2015 {
            saved.push((index, state.clone()));
        }
    }
    assert_eq!(saved.len(), 2);
    for (index, checkpoint) in saved {
        assert_ne!(checkpoint.current_target_bits, all[index].bits(), "fixture changes difficulty");
        // The entire saved state is restored; reconstructing only the LCA hash
        // and its header bits would discard next-target/epoch/MTP context.
        let mut resumed = checkpoint;
        for header in &all[index + 1..] {
            resumed = corrected::step(&resumed, header, testnet4).unwrap();
        }
        assert_eq!(resumed, state);
    }
    // This is replay from a known checkpoint, NOT authenticated LCA/fork choice.
}

#[test]
fn mainnet_old_epoch_boundary_resume_preserves_complete_state() {
    resume_at_epoch_boundary(initial_old(), OLD, false);
}

#[test]
fn mainnet_new_epoch_boundary_resume_preserves_complete_state() {
    resume_at_epoch_boundary(initial_new(), NEW, false);
}

#[test]
fn testnet4_epoch_boundary_resume_preserves_complete_state() {
    resume_at_epoch_boundary(initial_testnet(), TESTNET, true);
}

#[test]
fn normal_header_order_and_explicit_prior_state_are_required() {
    let checkpoint = initial_new();
    let blocks = headers(NEW, checkpoint.block_height + 1);
    let first = corrected::step(&checkpoint, &blocks[0], false).unwrap();
    assert!(corrected::step(&first, &blocks[0], false).is_err());
    assert!(corrected::step(&checkpoint, &blocks[1], false).is_err());
    assert!(corrected::step(&initial_old(), &blocks[0], false).is_err());
}

#[test]
fn checked_work_keeps_representable_sum_and_reports_unrepresentable_sum() {
    let max = U256::MAX.to_be_bytes();
    let one = U256::ONE.to_be_bytes();
    let zero = U256::ZERO.to_be_bytes();
    assert_eq!(corrected::checked_work(max, zero).unwrap(), max);
    assert_eq!(corrected::checked_work(U256::MAX.wrapping_sub(&U256::ONE).to_be_bytes(), one).unwrap(), max);
    assert_eq!(corrected::checked_work(max, one), Err(corrected::ValidationError::ChainworkOverflow));
    assert_eq!(corrected::checked_work(max, max), Err(corrected::ValidationError::ChainworkOverflow));
}

#[test]
fn checked_work_matches_u128_reference_without_truncation() {
    for total in [0u128, 1, 255, 1 << 64, u128::MAX / 2] {
        for amount in [0u128, 1, 256, 1 << 64, u128::MAX / 2] {
            let expected = U256::from(total + amount).to_be_bytes();
            assert_eq!(corrected::checked_work(U256::from(total).to_be_bytes(),
                U256::from(amount).to_be_bytes()).unwrap(), expected);
        }
    }
}

#[test]
fn testnet4_minimum_difficulty_requires_valid_carried_target() {
    let mut checkpoint = initial_testnet();
    let blocks = headers(TESTNET, checkpoint.block_height + 1);
    assert_eq!(blocks[0].bits(), 0x1d00ffff, "historical minimum-difficulty exception");
    assert!(corrected::step(&checkpoint, &blocks[0], true).is_ok());
    checkpoint.current_target_bits = 0;
    assert_eq!(corrected::step(&checkpoint, &blocks[0], true),
        Err(corrected::ValidationError::InvalidBlockBits));
}
