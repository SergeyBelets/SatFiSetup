#[cfg(test)]
mod qualification_tests {
    use super::*;

    #[test]
    fn mainnet_pow_limit_matches_bitcoin_core_reference() {
        let target = bits_to_target(0x1d00ffff);
        assert_eq!(U256::from_be_bytes(target), U256::from_be_hex(
            "00000000ffff0000000000000000000000000000000000000000000000000000"));
        assert_eq!(target_to_bits(&target), 0x1d00ffff);
        assert_eq!(target_to_work(&target), U256::from_be_hex(
            "0000000000000000000000000000000000000000000000000000000100010001"));
    }

    // These tests PASS when they reproduce incompatibilities. Their names and
    // the report distinguish observed behavior from a compatibility PASS.
    #[test]
    fn observed_target_one_work_differs_from_bitcoin_core() {
        let target = U256::ONE.to_be_bytes();
        let bitcoin_core_expected = U256::from_be_hex(
            "8000000000000000000000000000000000000000000000000000000000000000");
        assert_eq!(target_to_work(&target), U256::MAX);
        assert_ne!(target_to_work(&target), bitcoin_core_expected);
    }

    #[test]
    fn observed_target_one_compact_encoder_panics() {
        assert!(std::panic::catch_unwind(|| target_to_bits(&U256::ONE.to_be_bytes())).is_err());
    }

    #[test]
    fn observed_reversed_epoch_timestamps_do_not_use_signed_clamp() {
        let pow_limit = U256::from_be_bytes(bits_to_target(0x1d00ffff));
        let result = std::panic::catch_unwind(|| {
            calculate_new_difficulty(1_000_000, 999_999, 0x1c00ffff, pow_limit, 1_209_600)
        });
        if cfg!(debug_assertions) {
            assert!(result.is_err(), "unmodified debug arithmetic must show underflow");
        } else {
            // Unsigned underflow wraps, then clamps to 4x. Bitcoin Core's signed
            // interval instead clamps this negative interval to one quarter.
            assert_eq!(target_to_bits(&result.unwrap()), 0x1c03fffc);
        }
    }

    #[test]
    fn signed_compact_target_is_zero_in_integration_snapshot() {
        assert_eq!(bits_to_target(0x1d80ffff), [0; 32]);
    }
}
