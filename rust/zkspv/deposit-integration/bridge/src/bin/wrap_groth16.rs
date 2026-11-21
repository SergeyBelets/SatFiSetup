// SPDX-License-Identifier: MIT
//! Wrap an already verified succinct receipt using the SDK's local Docker backend.
//! The launcher pins a native-platform image and enforces --network none.
use bincode::Options;
use risc0_binfmt::compute_image_id;
use risc0_zkvm::{ExternalProver, InnerReceipt, Prover, ProverOpts, Receipt};
use std::{fs, io::Write, path::Path};

fn read(path: &Path, limit: u64) -> Vec<u8> {
    assert!(fs::metadata(path).unwrap().len() <= limit, "input size limit");
    let bytes = fs::read(path).unwrap();
    assert!(bytes.len() as u64 <= limit);
    bytes
}

fn main() {
    let a: Vec<_> = std::env::args_os().collect();
    assert_eq!(a.len(), 6, "usage: wrap_groth16 R0VM PROGRAM JOURNAL SUCCINCT_RECEIPT OUTPUT");
    let program = read(Path::new(&a[2]), 4 * 1024 * 1024);
    let journal = read(Path::new(&a[3]), 512);
    assert_eq!(journal.len(), 512);
    let mut domain = [0; 32];
    domain[..b"SATOSHIFI_REGTEST_DEPOSIT_V1".len()].copy_from_slice(b"SATOSHIFI_REGTEST_DEPOSIT_V1");
    assert_eq!(&journal[..32], &domain, "regtest journal domain required");
    let input = read(Path::new(&a[4]), 64 * 1024 * 1024);
    let receipt: Receipt = bincode::DefaultOptions::new().with_fixint_encoding()
        .reject_trailing_bytes().with_limit(64 * 1024 * 1024).deserialize(&input).unwrap();
    assert!(matches!(&receipt.inner, InnerReceipt::Succinct(_)), "verified succinct input required");
    let image = compute_image_id(&program).unwrap();
    receipt.verify(image).expect("input receipt failed verification");
    assert_eq!(receipt.journal.bytes, journal);
    assert!(!Path::new(&a[5]).exists(), "output must be new");
    let result = ExternalProver::new("explicit-local-r0vm", &a[1])
        .compress(&ProverOpts::groth16(), &receipt).expect("local Groth16 wrapping failed");
    match &result.inner {
        InnerReceipt::Groth16(inner) => assert_eq!(inner.seal.len(), 256),
        _ => panic!("genuine Groth16 output required"),
    }
    result.verify(image).expect("Groth16 verification failed");
    assert_eq!(result.journal.bytes, journal, "journal changed");
    let bytes = bincode::serialize(&result).unwrap();
    let mut output = fs::OpenOptions::new().write(true).create_new(true).open(&a[5]).unwrap();
    output.write_all(&bytes).unwrap(); output.sync_all().unwrap();
    println!("verified Groth16 image_id={image} receipt_bytes={} journal_bytes=512", bytes.len());
}
