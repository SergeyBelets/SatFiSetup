use satoshifi_testnet4_guest_kernel::{execute, read_request, verify_testnet4, transition};
use transition::{bounded::{archive::Archive, checkpoint::ValidatedCheckpoint}, Deployment, Request};

const HEADERS: &[u8] = include_bytes!("../fixtures/headers-2006-4050.bin");
const EXPECTED: &[u8] = include_bytes!("../fixtures/expected-2006-4050.bin");
fn header(h: u64) -> Vec<u8> { let i = (h - 2006) as usize * 80; HEADERS[i..i+80].to_vec() }
fn work(h: u64) -> [u8;32] { let i = (h - 2006) as usize * 68 + 32; EXPECTED[i..i+32].try_into().unwrap() }
fn checkpoint() -> ValidatedCheckpoint {
    ValidatedCheckpoint::validate(&header(2016), 2016, work(2016), &(2006..2016).map(header).collect::<Vec<_>>()).unwrap()
}
fn request(a: &mut Archive, headers: Vec<Vec<u8>>) -> Request {
    let before = a.state().clone(); let witnesses = a.prepare_batch(&headers, verify_testnet4).unwrap();
    let mut chain = [0;32]; chain[24..].copy_from_slice(&31337u64.to_be_bytes());
    Request { deployment: Deployment { evm_chain_id: chain, state_address: [0x11;20], checkpoint_descriptor: before.checkpoint_descriptor },
        expected: before.view.state_commitment, before, headers, witnesses }
}

#[test]
fn historic_testnet4_pow_work_and_bip94_retarget_match_core() {
    let mut context = checkpoint().context().clone();
    for h in 2017..=4050 {
        context = verify_testnet4(&context, &header(h).try_into().unwrap(), h).unwrap();
        let i = (h - 2006) as usize * 68;
        // Testnet4 fixture records store hashes in internal byte order.
        assert_eq!(context.block_hash, EXPECTED[i..i+32], "height={h}");
        assert_eq!(context.total_work, work(h), "height={h}");
        let next_epoch = if h % 2016 == 2015 { h + 1 } else { h - h % 2016 };
        assert_eq!(context.current_target_bits, u32::from_le_bytes(header(next_epoch)[72..76].try_into().unwrap()));
    }
}

#[test]
fn exact_testnet4_journal_and_resume_cross_retarget() {
    let mut archive = Archive::new(&checkpoint()); let mut next = 2017;
    for boundary in [2017, 4032, 4050] {
        for chunk in (next..boundary).map(header).collect::<Vec<_>>().chunks(64) {
            archive.prepare_batch(chunk, verify_testnet4).unwrap();
        }
        let r = request(&mut archive, vec![header(boundary)]); let input = r.encode().unwrap();
        let out = execute(&input).unwrap(); let journal = out.journal.encode_abi();
        assert_eq!(journal.len(),1216); assert_eq!(&journal[32..48], b"bitcoin-testnet4");
        assert_eq!(&out.state,archive.state()); assert_eq!(out.state.view.work,work(boundary));
        if let Some(path)=std::env::var_os("SATOSHIFI_TESTNET4_VECTORS") {
            let path=std::path::PathBuf::from(path);std::fs::create_dir_all(&path).unwrap();
            let mut frame=(input.len() as u32).to_le_bytes().to_vec();frame.extend(input);
            std::fs::write(path.join(format!("testnet4-{boundary}.input")),frame).unwrap();
            std::fs::write(path.join(format!("testnet4-{boundary}.journal")),journal).unwrap();
        }
        next = boundary + 1;
    }
}

#[test]
fn bip94_epoch_timestamp_guard_inclusive_boundary() {
    let mut c=checkpoint().context().clone();
    for h in 2017..4032 { c=verify_testnet4(&c,&header(h).try_into().unwrap(),h).unwrap(); }
    let raw: [u8;80]=header(4032).try_into().unwrap();
    let time=u32::from_le_bytes(raw[68..72].try_into().unwrap());
    c.prev_11_timestamps[(c.block_height%11) as usize]=time+601;
    assert!(verify_testnet4(&c,&raw,4032).is_err());
    c.prev_11_timestamps[(c.block_height%11) as usize]=time+600;
    assert!(verify_testnet4(&c,&raw,4032).is_ok());
}

#[test]
fn malformed_pow_parent_height_and_context_cannot_advance() {
    let c=checkpoint().context().clone();let raw: [u8;80]=header(2017).try_into().unwrap();
    let mut bad=raw;bad[76]^=1;assert!(verify_testnet4(&c,&bad,2017).is_err());
    assert!(verify_testnet4(&c,&raw,2018).is_err());
    let mut p=c.clone();p.block_hash[0]^=1;assert!(verify_testnet4(&p,&raw,2017).is_err());
    p=c.clone();p.current_target_bits=0x207fffff;assert!(verify_testnet4(&p,&raw,2017).is_err());
    p=c.clone();p.total_work=[255;32];assert!(verify_testnet4(&p,&raw,2017).is_err());
}

#[test]
fn twenty_minute_comparison_does_not_overflow() {
    let mut c=checkpoint().context().clone();
    c.prev_11_timestamps[(c.block_height%11) as usize]=u32::MAX;
    let _=verify_testnet4(&c,&header(2017).try_into().unwrap(),2017);
}

#[test]
fn bounded_transport_and_previous_state_are_authenticated() {
    assert!(read_request(&[0,0,0,0][..]).is_err());
    assert!(read_request(&[1,0,0,0,42,43][..]).is_err());
    let mut archive=Archive::new(&checkpoint());let mut r=request(&mut archive,vec![header(2017)]);
    r.expected[0]^=1;assert!(execute(&r.encode().unwrap()).is_err());
    r.expected[0]^=1;r.deployment.checkpoint_descriptor[0]^=1;assert!(r.encode().is_err());
}
