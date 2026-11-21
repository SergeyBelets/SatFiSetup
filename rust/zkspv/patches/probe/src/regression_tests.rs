#[cfg(test)]
mod regression_tests {
    use super::{corrected, original};
    use bitcoin::Target;
    use crypto_bigint::{Encoding, U256};

    include!("vectors.rs");

    fn bytes(hex: &str) -> [u8; 32] { U256::from_be_hex(hex).to_be_bytes() }

    #[test]
    fn original_defects_remain_reproducible_in_the_unmodified_functions() {
        let one = U256::ONE.to_be_bytes();
        assert_eq!(original::work(&one), U256::MAX.to_be_bytes());
        assert!(std::panic::catch_unwind(|| original::compact(&one)).is_err());
        let retarget = std::panic::catch_unwind(|| original::retarget(1_000_000, 999_999, 0x1c00ffff));
        if cfg!(debug_assertions) {
            assert!(retarget.is_err());
        } else {
            assert_eq!(original::compact(&retarget.unwrap()), 0x1c03fffc);
        }
    }

    #[test]
    fn corrected_compact_matches_core_equation_and_pinned_rust_bitcoin() {
        for &(hex, compact, _) in TARGET_VECTORS {
            let target = bytes(hex);
            assert_eq!(corrected::compact(&target), compact, "Core vector {hex}");
            assert_eq!(corrected::compact(&target),
                Target::from_be_bytes(target).to_compact_lossy().to_consensus(), "rust-bitcoin {hex}");
        }
    }

    #[test]
    fn compact_stays_const_for_zero_one_and_high_bit_targets() {
        const ZERO: u32 = corrected::compact(&[0; 32]);
        const ONE_TARGET: [u8; 32] = { let mut value = [0; 32]; value[31] = 1; value };
        const ONE: u32 = corrected::compact(&ONE_TARGET);
        const MAX: u32 = corrected::compact(&[0xff; 32]);
        assert_eq!((ZERO, ONE, MAX), (0, 0x01010000, 0x2100ffff));
    }

    #[test]
    fn corrected_work_matches_core_formula_for_nonzero_targets() {
        for &(hex, _, work) in TARGET_VECTORS {
            let target = bytes(hex);
            if target == [0; 32] { continue; }
            assert_eq!(corrected::work(&target), bytes(work), "Core vector {hex}");
            if target != U256::ONE.to_be_bytes() {
                assert_eq!(corrected::work(&target), Target::from_be_bytes(target).to_work().to_be_bytes());
            }
        }
    }

    #[test]
    fn target_one_uses_core_expected_value_even_though_pinned_rust_bitcoin_differs() {
        let one = U256::ONE.to_be_bytes();
        assert_eq!(corrected::work(&one), bytes(
            "8000000000000000000000000000000000000000000000000000000000000000"));
        assert_eq!(Target::from_be_bytes(one).to_work().to_be_bytes(), U256::MAX.to_be_bytes());
        assert_ne!(corrected::work(&one), original::work(&one));
    }

    #[test]
    fn zero_target_validation_is_explicitly_outside_this_patch() {
        // Core GetBlockProof rejects zero before applying its formula. This
        // helper's pre-existing zero convention remains unqualified, unchanged.
        assert_eq!(corrected::work(&[0; 32]), original::work(&[0; 32]));
        assert_eq!(corrected::work(&[0; 32]), U256::MAX.to_be_bytes());
    }

    #[test]
    fn retarget_signed_clamps_and_pow_limit_match_core_equation() {
        for &(start, end, bits, target, compact) in RETARGET_VECTORS {
            let actual = corrected::retarget(start, end, bits);
            assert_eq!(actual, bytes(target), "retarget {start} {end} {bits:08x}");
            assert_eq!(corrected::compact(&actual), compact);
        }
    }

    #[test]
    fn ordinary_mainnet_vectors_retain_original_behavior() {
        let pow_limit = corrected::target(0x1d00ffff);
        assert_eq!(corrected::compact(&pow_limit), original::compact(&pow_limit));
        assert_eq!(corrected::work(&pow_limit), original::work(&pow_limit));
        for end in [1_000_000, 1_302_400, 2_209_600, 5_838_400, u32::MAX] {
            assert_eq!(corrected::retarget(1_000_000, end, 0x1c00ffff),
                       original::retarget(1_000_000, end, 0x1c00ffff));
        }
    }
}
