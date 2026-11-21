use satoshifi_mainnet_guest_kernel::{execute, read_request, verify_mainnet, transition};
use transition::{bounded::{archive::Archive, checkpoint::{InitialContext, ValidatedCheckpoint}}, Deployment, Request};
use std::io::{self, Read};

const HEADERS: &[u8] = include_bytes!("../fixtures/headers-32246-36288.bin");
const EXPECTED: &[u8] = include_bytes!("../fixtures/expected-32246-36288.bin");
fn header(h: u64) -> Vec<u8> { let i = (h - 32246) as usize * 80; HEADERS[i..i+80].to_vec() }
fn work(h: u64) -> [u8;32] { let i = (h - 32246) as usize * 68 + 32; EXPECTED[i..i+32].try_into().unwrap() }
fn checkpoint() -> ValidatedCheckpoint {
    ValidatedCheckpoint::validate(&header(32256), 32256, work(32256), &(32246..32256).map(header).collect::<Vec<_>>()).unwrap()
}
fn request(a: &mut Archive, input: Vec<Vec<u8>>) -> Request {
    let before = a.state().clone();
    let witnesses = a.prepare_batch(&input, verify_mainnet).unwrap();
    let mut chain = [0;32]; chain[30..].copy_from_slice(&31337u16.to_be_bytes());
    Request { deployment: Deployment { evm_chain_id: chain, state_address: [0x11;20], checkpoint_descriptor: before.checkpoint_descriptor },
        expected: before.view.state_commitment, before, headers: input, witnesses }
}
fn frame(bytes: &[u8]) -> Vec<u8> {
    let mut framed = (bytes.len() as u32).to_le_bytes().to_vec(); framed.extend(bytes); framed
}
fn record(height: u64, input: &[u8], journal: &[u8]) {
    if let Some(path) = std::env::var_os("SATOSHIFI_GUEST_VECTORS") {
        let path = std::path::PathBuf::from(path);
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join(format!("mainnet-{height}.input")), frame(input)).unwrap();
        std::fs::write(path.join(format!("mainnet-{height}.journal")), journal).unwrap();
    }
}

#[test]
fn public_mainnet_dispatch_matches_every_context_field_across_two_retargets() {
    let cp = checkpoint(); let mut context = cp.context().clone();
    for h in 32257..=36288 {
        context = verify_mainnet(&context, &header(h).try_into().unwrap(), h).unwrap();
        let i = (h - 32246) as usize * 68;
        let mut expected_hash: [u8;32] = EXPECTED[i..i+32].try_into().unwrap(); expected_hash.reverse();
        assert_eq!(context.block_hash, expected_hash); assert_eq!(context.total_work, work(h));
        assert_eq!(context.block_height, h);
        let time = |height| u32::from_le_bytes(header(height)[68..72].try_into().unwrap());
        let next = if h % 2016 == 2015 { h+1 } else { h };
        assert_eq!(context.current_target_bits, u32::from_le_bytes(header(next)[72..76].try_into().unwrap()));
        assert_eq!(context.epoch_start_time, time(h - h % 2016));
        for prev in h-10..=h { assert_eq!(context.prev_11_timestamps[(prev % 11) as usize], time(prev)); }
    }
}

#[test]
fn static_kernel_exact_journal_and_bounded_resume_at_both_retargets() {
    let mut archive = Archive::new(&checkpoint()); let mut next = 32257;
    for boundary in [32257, 34272, 36288] {
        let history: Vec<_> = (next..boundary).map(header).collect();
        for chunk in history.chunks(64) { archive.prepare_batch(chunk, verify_mainnet).unwrap(); }
        let r = request(&mut archive, vec![header(boundary)]); let input = r.encode().unwrap();
        assert_eq!(input.len(), 31805);
        let decoded = read_request(frame(&input).as_slice()).unwrap();
        let output = execute(&decoded).unwrap(); let journal = output.journal.encode_abi();
        assert_eq!(&output.state, archive.state()); assert_eq!(output.state.view.work, work(boundary));
        assert_eq!(journal.len(), 1216); assert_eq!(&journal[8*32..9*32], output.state.commitment());
        assert_eq!(output.journal.time_summary().count(), 1);
        record(boundary, &input, &journal); next = boundary + 1;
    }
}

#[test]
fn overlapping_headers_use_authenticated_context_and_count_unique_work() {
    let mut a = Archive::new(&checkpoint()); a.prepare_batch(&[header(32257)], verify_mainnet).unwrap();
    let r = request(&mut a, vec![header(32257), header(32258), header(32258)]);
    let out = execute(&r.encode().unwrap()).unwrap();
    assert_eq!(out.journal.processed(), 3); assert_eq!(out.journal.time_summary().count(), 1);
    assert_eq!(out.state.view.work, work(32258));
}

#[test]
fn public_adapter_rejects_invalid_pow_parent_height_bits_mtp_and_overflow() {
    let cp = checkpoint(); let c = cp.context(); let raw: [u8;80] = header(32257).try_into().unwrap();
    let mut bad_pow = raw; bad_pow[76] ^= 1;
    assert!(verify_mainnet(c, &bad_pow, 32257).is_err());
    assert!(verify_mainnet(c, &raw, 32258).is_err());
    for field in 0..4 {
        let mut parent = c.clone();
        match field { 0 => parent.block_hash[0] ^= 1, 1 => parent.current_target_bits = 0x207fffff,
            2 => parent.prev_11_timestamps = [u32::MAX;11], _ => parent.total_work = [255;32] }
        assert!(verify_mainnet(&parent, &raw, 32257).is_err());
    }
}

#[test]
fn kernel_rejects_invalid_pow_even_with_a_structurally_valid_witness() {
    use bitcoin::hashes::{sha256d, Hash};
    let mut a = Archive::new(&checkpoint()); let before = a.state().clone();
    let mut bad = header(32257); bad[76] ^= 1;
    // Only witness generation is artificial: execution uses the static real core.
    let witnesses = a.prepare_batch(&[bad.clone()], |c, raw, h| {
        let mut next = verify_mainnet(c, &header(32257).try_into().unwrap(), h)?;
        next.block_hash = sha256d::Hash::hash(raw).to_byte_array(); Ok::<InitialContext, bitcoin_da::verifier::ValidationError>(next)
    }).unwrap();
    let mut r = request(&mut Archive::new(&checkpoint()), vec![header(32257)]);
    r.before = before; r.headers = vec![bad]; r.witnesses = witnesses;
    assert!(matches!(execute(&r.encode().unwrap()), Err(transition::RunError::Transition(transition::Error::Resume(transition::bounded::Error::Header(_))))));
}

#[test]
fn kernel_rejects_forged_context_and_stale_state() {
    let mut r = request(&mut Archive::new(&checkpoint()), vec![header(32257)]);
    let transition::bounded::Witness::New { parent, .. } = &mut r.witnesses[0] else { panic!() };
    parent.entry.context.prev_11_timestamps[0] ^= 1;
    assert!(execute(&r.encode().unwrap()).is_err());
    let mut r = request(&mut Archive::new(&checkpoint()), vec![header(32257)]);
    r.expected[0] ^= 1; assert!(execute(&r.encode().unwrap()).is_err());
}

#[test]
fn transport_rejects_length_before_reading_body() {
    struct PrefixOnly { prefix: io::Cursor<[u8;4]> }
    impl Read for PrefixOnly {
        fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
            assert!(self.prefix.position() < 4, "must reject before body read/allocation"); self.prefix.read(out)
        }
    }
    for n in [0, transition::MAX_INPUT_BYTES as u32 + 1, u32::MAX] {
        assert_eq!(read_request(PrefixOnly { prefix: io::Cursor::new(n.to_le_bytes()) }).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }
}

#[test]
fn transport_rejects_truncation_and_trailing_bytes() {
    for bytes in [vec![], vec![1,0,0], vec![2,0,0,0,7], vec![1,0,0,0,7,8]] {
        assert!(read_request(bytes.as_slice()).is_err());
    }
    assert_eq!(read_request([1,0,0,0,7].as_slice()).unwrap(), vec![7]);
    assert!(execute(&[7]).is_err());
}
