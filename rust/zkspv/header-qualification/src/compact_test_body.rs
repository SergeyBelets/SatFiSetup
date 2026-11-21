// Shared assertions for the extracted harness and actual native verifier.
// `decode` is supplied by the enclosing test module and uses checked_target.
use crypto_bigint::{Encoding, U256};

const CORE_VECTORS: &str = include_str!("../fixtures/compact-core-vectors.tsv");

fn limits() -> [[u8; 32]; 4] {
    [
        U256::from_be_hex("00000000ffff0000000000000000000000000000000000000000000000000000").to_be_bytes(),
        U256::from_be_hex("00000377ae000000000000000000000000000000000000000000000000000000").to_be_bytes(),
        U256::from_be_hex("7fffff0000000000000000000000000000000000000000000000000000000000").to_be_bytes(),
        U256::MAX.to_be_bytes(),
    ]
}

#[test]
fn checked_targets_match_executed_core_arithmetic() {
    let limits = limits();
    let mut total = 0;
    for line in CORE_VECTORS.lines() {
        let columns: Vec<_> = line.split('\t').collect();
        assert_eq!(columns.len(), 5);
        let bits = u32::from_str_radix(columns[0], 16).unwrap();
        for index in 0..4 {
            let expected = if columns[index + 1] == "-" { None }
                else { Some(hex::decode(columns[index + 1]).unwrap().try_into().unwrap()) };
            assert_eq!(decode(bits, limits[index]), expected, "Core compact={bits:08x}, limit={index}");
            total += 1;
        }
    }
    assert!(total > 40_000);
    println!("Executed Core target comparisons: {total}");
}

#[test]
fn range_guard_retains_core_permitted_encoding_aliases() {
    // Range validation must not introduce a new minimal-encoding consensus rule.
    // Header transition logic still separately requires its exact expected bits.
    let one = U256::ONE.to_be_bytes();
    for bits in [0x01010000, 0x02000100, 0x03000001] {
        assert_eq!(decode(bits, limits()[0]), Some(one));
    }
}

#[test]
fn pow_limit_is_inclusive_and_context_bound() {
    let mainnet = limits()[0];
    assert_eq!(decode(0x1d00ffff, mainnet), Some(mainnet));
    let smaller = U256::from_be_bytes(mainnet).wrapping_sub(&U256::ONE).to_be_bytes();
    assert_eq!(decode(0x1d00ffff, smaller), None);
    assert_eq!(decode(0x1d00ffff, U256::ZERO.to_be_bytes()), None);
    assert_eq!(decode(0x1e0377ae, mainnet), None);
    assert_eq!(decode(0x1e0377ae, limits()[1]), Some(limits()[1]));
}
