// SPDX-License-Identifier: MIT
//! Offline verification; reject development receipts and require the exact journal.
use risc0_binfmt::compute_image_id;
use risc0_zkvm::{InnerReceipt, Receipt};
use std::fs;
use bincode::Options;
fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    assert_eq!(args.len(), 4, "usage: verify_receipt program.r0bf expected.journal receipt");
    let program = fs::read(&args[1]).unwrap();
    let expected = fs::read(&args[2]).unwrap();
    assert_eq!(expected.len(), 1216);
    assert!(fs::metadata(&args[3]).unwrap().len() <= 64 * 1024 * 1024, "receipt size limit");
    let data = fs::read(&args[3]).unwrap();
    let receipt: Receipt = bincode::DefaultOptions::new().with_fixint_encoding()
        .reject_trailing_bytes().with_limit(64 * 1024 * 1024).deserialize(&data).expect("invalid receipt encoding");
    let kind = match &receipt.inner {
        InnerReceipt::Composite(_) => "composite",
        InnerReceipt::Succinct(_) => "succinct",
        InnerReceipt::Groth16(_) => "groth16",
        _ => panic!("development or unsupported receipt"),
    };
    let image_id = compute_image_id(&program).unwrap();
    receipt.verify(image_id).expect("receipt verification failed");
    assert_eq!(receipt.journal.bytes, expected, "journal differs from native result");
    let mut wrong_image = image_id;
    wrong_image.as_mut_words()[0] ^= 1;
    assert!(receipt.verify(wrong_image).is_err(), "wrong image accepted");
    let mut tampered = receipt.clone(); tampered.journal.bytes[0] ^= 1;
    assert!(tampered.verify(image_id).is_err(), "modified journal accepted");
    println!("verified {kind} image_id={image_id} journal_bytes={}", receipt.journal.bytes.len());
    println!("wrong_image_rejected=true modified_journal_rejected=true dev_mode_disabled=true");
}
