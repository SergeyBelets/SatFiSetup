mod support;
use support::*;
use satoshifi_checkpoint_policy::{ValidatedCheckpoint as Checkpoint, Error, fork::Context};
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _};

fn validate(raw: &[u8], height: u64, work: [u8; 32], prior: &[Vec<u8>]) -> Result<Checkpoint, Error> {
    Checkpoint::validate(raw, height, work, prior)
}
fn invalid(raw: &[u8], height: u64, work: [u8; 32], prior: &[Vec<u8>], error: Error) {
    assert_eq!(validate(raw, height, work, prior).unwrap_err(), error);
}

#[test] fn real_mainnet_epoch_checkpoints_and_exact_work() {
    for height in [32256, 34272, 36288] {
        let anchor = checkpoint(height);
        let state = anchor.initialize(Clone::clone).unwrap();
        let parsed: Header = deserialize(&header(height)).unwrap();
        let c = anchor.context();
        assert_eq!(c.block_hash, parsed.block_hash().to_byte_array());
        let mut expected_hash: [u8; 32] = EXPECTED[(height-FIRST) as usize*68..(height-FIRST) as usize*68+32].try_into().unwrap();
        expected_hash.reverse(); assert_eq!(c.block_hash, expected_hash);
        assert_eq!(c.block_height, height);
        assert_eq!(c.total_work, work(height));
        assert_eq!(c.current_target_bits, parsed.bits.to_consensus());
        assert_eq!(c.epoch_start_time, parsed.time);
        assert_eq!(state.view().work, work(height));
        assert_eq!(state.known_count(), 1);
        assert_eq!(state.confirmations(c.block_hash), Some(1));
    }
}

#[test] fn exact_checkpoint_length() {
    for length in [0, 1, 79, 81, 160] {
        invalid(&vec![0; length],32256,work(32256),&prior(32256),Error::HeaderLength);
    }
}
#[test] fn exactly_ten_predecessors() {
    for count in [0, 9, 11] {
        invalid(&header(32256),32256,work(32256),&vec![header(32255); count],Error::AncestorCount);
    }
}
#[test] fn every_predecessor_has_exact_length() {
    for i in 0..10 { for length in [79, 81] {
        let mut p = prior(32256); p[i].resize(length, 0);
        invalid(&header(32256),32256,work(32256),&p,Error::AncestorLength);
    } }
}
#[test] fn all_links_and_checkpoint_connection_are_required() {
    for i in 0..10 {
        let mut p = prior(32256); p[i][36] ^= 1;
        invalid(&header(32256),32256,work(32256),&p,Error::AncestorLink);
    }
    let mut p = prior(32256); p.reverse();
    invalid(&header(32256),32256,work(32256),&p,Error::AncestorLink);
    let p = (32245..32255).map(|h| if h==32245 { header(32246) } else { header(h) }).collect::<Vec<_>>();
    invalid(&header(32256),32256,work(32256),&p,Error::AncestorLink);
}
#[test] fn epoch_boundary_and_minimum_height() {
    for height in [0, 1, 9, 10, 2015, 2017, 32255, u64::MAX] {
        invalid(&header(32256),height,work(32256),&prior(32256),Error::Height);
    }
}
#[test] fn invalid_compact_and_mainnet_pow_limit_are_rejected() {
    for bits in [0u32, 0x01003456, 0x1d80ffff, 0x23000001, 0x22000100, 0x21010000, 0x1d010000, 0x207fffff] {
        let mut raw = header(32256); raw[72..76].copy_from_slice(&bits.to_le_bytes());
        invalid(&raw,32256,work(32256),&prior(32256),Error::Target);
    }
}
#[test] fn valid_target_still_requires_real_pow() {
    let mut raw = header(32256); raw[76] ^= 1;
    invalid(&raw,32256,work(32256),&prior(32256),Error::ProofOfWork);
    // Target 1 is positive and representable; no hash here meets it.
    raw[72..76].copy_from_slice(&0x03000001u32.to_le_bytes());
    invalid(&raw,32256,work(32256),&prior(32256),Error::ProofOfWork);
}
#[test] fn work_includes_anchor_once_but_earlier_work_is_trusted() {
    let raw = header(32256); let parsed: Header = deserialize(&raw).unwrap();
    let own = parsed.work().to_be_bytes();
    let mut insufficient = own; for byte in insufficient.iter_mut().rev() { if *byte>0 { *byte-=1; break; } *byte=255; }
    for value in [[0;32], insufficient] { invalid(&raw,32256,value,&prior(32256),Error::ChainWork); }
    for value in [own, work(32256), [255;32]] {
        let cp = validate(&raw,32256,value,&prior(32256)).unwrap();
        assert_eq!(cp.initialize(Clone::clone).unwrap().view().work, value);
    }
}
#[test] fn ring_uses_absolute_heights_for_all_eleven_positions() {
    for height in [32256, 34272, 36288] {
        let c = checkpoint(height).context().clone();
        for h in height-10..=height {
            let raw = header(h); let timestamp = u32::from_le_bytes(raw[68..72].try_into().unwrap());
            assert_eq!(c.prev_11_timestamps[(h%11) as usize], timestamp);
        }
        let chronological: Vec<u32> = (height-10..=height).map(|h| u32::from_le_bytes(header(h)[68..72].try_into().unwrap())).collect();
        if (height-10)%11!=0 { assert_ne!(c.prev_11_timestamps.as_slice(),chronological); }
    }
}
#[test] fn binding_rejects_changes_to_every_context_field() {
    let cp = checkpoint(32256);
    for field in 0..16 {
        let result = cp.initialize(|c| {
            let mut c = c.clone();
            match field { 0=>c.block_hash[0]^=1,1=>c.block_height+=1,2=>c.total_work[31]^=1,
                3=>c.current_target_bits^=1,4=>c.epoch_start_time^=1,i=>c.prev_11_timestamps[i-5]^=1 }
            c
        });
        assert!(matches!(result,Err(Error::ContextBinding)));
    }
}
#[test] fn predecessors_never_become_members_or_confirmations() {
    let cp = checkpoint(32256); let state = cp.initialize(Clone::clone).unwrap();
    for raw in prior(32256) {
        let h: Header = deserialize(&raw).unwrap(); let hash = h.block_hash().to_byte_array();
        assert_eq!(state.confirmations(hash),None); assert!(state.membership(hash).is_err());
        assert!(state.context(hash).is_none());
    }
    assert_eq!(state.ancestor(state.view().head,32255),None);
}
#[test] fn descriptor_and_state_bind_height_and_trusted_work() {
    let cp = checkpoint(32256);
    let shifted = validate(&header(32256),34272,work(32256),&prior(32256)).unwrap();
    let extra_work = validate(&header(32256),32256,[255;32],&prior(32256)).unwrap();
    for other in [shifted,extra_work] {
        assert_ne!(cp.descriptor_commitment(),other.descriptor_commitment());
        assert_ne!(cp.context().commitment(),other.context().commitment());
        assert_ne!(cp.initialize(Clone::clone).unwrap().view().state_commitment,
            other.initialize(Clone::clone).unwrap().view().state_commitment);
    }
}
#[test] fn returned_context_cannot_change_validated_anchor() {
    let cp = checkpoint(32256); let before = cp.descriptor_commitment();
    let mut copy = cp.context().clone(); copy.total_work = [0;32];
    assert_ne!(copy.commitment(),cp.context().commitment());
    assert_eq!(cp.descriptor_commitment(),before);
    assert_eq!(cp.initialize(Clone::clone).unwrap().view().work,work(32256));
}
