use satoshifi_header_time_policy::{verify_batch, BatchError, SummaryError, TimeSummary, DOMAIN};

// Scalar fixtures exercise only summary mechanics, not Bitcoin validation.
fn summary(times: &[u32]) -> TimeSummary {
    verify_batch((), times, |_, timestamp| Ok::<_, ()>(((), *timestamp))).unwrap().1
}

#[test]
fn empty_batch_has_no_state_or_summary() {
    assert_eq!(verify_batch(7, [], |_: &u32, _: u32| Ok::<_, ()>((8, 1))),
        Err(BatchError::Summary(SummaryError::EmptyBatch)));
}

#[test]
fn maximum_includes_earlier_headers_when_time_decreases() {
    let result = summary(&[100, 7500, 301]);
    assert_eq!((result.count(), result.maximum(), result.last()), (3, 7500, 301));
}

#[test]
fn one_header_and_full_uint32_range_are_preserved() {
    for timestamp in [0, 1, u32::MAX] {
        let result = summary(&[timestamp]);
        assert_eq!((result.count(), result.maximum(), result.last()), (1, timestamp, timestamp));
    }
}

#[test]
fn failed_middle_transition_returns_no_prefix_and_stops() {
    let mut visited = Vec::new();
    let initial = 20;
    let result = verify_batch(initial, [2, 3, 5], |state, item| {
        visited.push(item);
        if item == 3 { Err("invalid header") } else { Ok((state + item, 100)) }
    });
    assert_eq!(result, Err(BatchError::Header("invalid header")));
    assert_eq!(initial, 20);
    assert_eq!(visited, [2, 3]);
}

#[test]
fn failed_last_transition_cannot_return_a_success_summary() {
    let result = verify_batch(0, [1, 2], |state, item| {
        if item == 2 { Err(2) } else { Ok((state + 1, 8000)) }
    });
    assert_eq!(result, Err(BatchError::Header(2)));
}

#[test]
fn every_chunk_partition_preserves_maximum_count_and_last() {
    let times = [90, 0, 1000, 3, 2, 1, u32::MAX, 5];
    for split in 1..times.len() {
        assert_eq!(summary(&times[..split]).then(summary(&times[split..])).unwrap(), summary(&times));
    }
}

#[test]
fn chunk_composition_is_associative_but_last_remains_ordered() {
    let a = summary(&[9, 5]); let b = summary(&[6, 30]); let c = summary(&[4]);
    assert_eq!(a.then(b).unwrap().then(c), a.then(b.then(c).unwrap()));
    assert_ne!(a.then(b).unwrap().last(), b.then(a).unwrap().last());
}

#[test]
fn composition_rejects_unrepresentable_count() {
    let mut result = summary(&[1]);
    for _ in 0..63 { result = result.then(result).unwrap(); }
    assert_eq!(result.count(), 1u64 << 63);
    assert_eq!(result.then(result), Err(SummaryError::CountOverflow));
}

#[test]
fn abi_has_exact_domain_word_order_widths_and_zero_padding() {
    let bytes = summary(&[0x89abcdef, 0x12345678]).encode_abi();
    assert_eq!(bytes.len(), 128);
    assert_eq!(&bytes[..32], &DOMAIN);
    assert!(bytes[32..56].iter().all(|value| *value == 0));
    assert_eq!(&bytes[56..64], &2u64.to_be_bytes());
    assert!(bytes[64..92].iter().all(|value| *value == 0));
    assert_eq!(&bytes[92..96], &0x89abcdefu32.to_be_bytes());
    assert!(bytes[96..124].iter().all(|value| *value == 0));
    assert_eq!(&bytes[124..128], &0x12345678u32.to_be_bytes());
}

#[test]
fn deterministic_sequences_match_independent_maximum() {
    let mut seed = 0x12345678u32;
    for length in 1..128 {
        let times: Vec<_> = (0..length).map(|_| {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223); seed
        }).collect();
        let result = summary(&times);
        assert_eq!(result.maximum(), *times.iter().max().unwrap());
        assert_eq!(result.count(), length);
        assert_eq!(result.last(), *times.last().unwrap());
    }
}
