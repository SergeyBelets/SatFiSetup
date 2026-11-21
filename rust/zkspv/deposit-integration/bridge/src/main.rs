// SPDX-License-Identifier: MIT
//! Compress an existing authenticated receipt; never re-execute or replace its journal.
use bincode::Options;
use risc0_binfmt::compute_image_id;
use risc0_zkvm::{
    sha::{Digestible, Sha256}, ExternalProver, Groth16ReceiptVerifierParameters,
    InnerReceipt, Prover, ProverOpts, Receipt,
};
use std::{fs, path::Path, time::Instant};

fn read_bounded(path: &Path, limit: u64) -> Vec<u8> {
    assert!(fs::metadata(path).expect("missing input").len() <= limit, "input size limit");
    let data = fs::read(path).unwrap();
    assert!(data.len() as u64 <= limit, "input changed size");
    data
}
fn sha(data: &[u8]) -> String {
    risc0_zkvm::sha::Impl::hash_bytes(data).to_string()
}
fn kind(receipt: &Receipt) -> &'static str {
    match &receipt.inner {
        InnerReceipt::Composite(_) => "composite",
        InnerReceipt::Succinct(_) => "succinct",
        InnerReceipt::Groth16(_) => "groth16",
        _ => panic!("development or unsupported receipt"),
    }
}
fn main() {
    let a: Vec<_> = std::env::args_os().collect();
    assert!(a.len() == 6 || a.len() == 8,
        "usage: bridge inspect PROGRAM JOURNAL RECEIPT OUT | bridge compress KIND R0VM PROGRAM JOURNAL RECEIPT OUT");
    let inspect = a[1] == "inspect";
    assert!((inspect && a.len() == 6) || (a[1] == "compress" && a.len() == 8));
    let start = if inspect { 2 } else { 4 };
    let program = read_bounded(Path::new(&a[start]), 4 * 1024 * 1024);
    let journal = read_bounded(Path::new(&a[start + 1]), 512);
    assert_eq!(journal.len(), 512, "exact TEST_ONLY regtest journal required");
    let mut domain = [0; 32];
    domain[..b"SATOSHIFI_REGTEST_DEPOSIT_V1".len()].copy_from_slice(b"SATOSHIFI_REGTEST_DEPOSIT_V1");
    assert_eq!(&journal[..32], &domain, "regtest journal domain required");
    let input = read_bounded(Path::new(&a[start + 2]), 64 * 1024 * 1024);
    let receipt: Receipt = bincode::DefaultOptions::new().with_fixint_encoding()
        .reject_trailing_bytes().with_limit(64 * 1024 * 1024)
        .deserialize(&input).expect("invalid receipt encoding");
    let input_kind = kind(&receipt);
    let image = compute_image_id(&program).unwrap();
    receipt.verify(image).expect("input receipt verification failed");
    assert_eq!(receipt.journal.bytes, journal, "input journal mismatch");
    let output = Path::new(&a[start + 3]);
    assert!(!output.exists(), "output must be new");
    let started = Instant::now();
    let result = if inspect { receipt } else {
        let (opts, expected) = if a[2] == "succinct" {
            (ProverOpts::succinct(), "succinct")
        } else {
            assert_eq!(a[2], "groth16", "kind must be succinct or groth16");
            assert_eq!(std::env::consts::ARCH, "x86_64", "Groth16 requires native x86_64");
            (ProverOpts::groth16(), "groth16")
        };
        let result = ExternalProver::new("explicit-local-r0vm", &a[3])
            .compress(&opts, &receipt).expect("local compression failed");
        assert_eq!(kind(&result), expected, "unexpected compressed receipt kind");
        result
    };
    let elapsed = started.elapsed().as_secs_f64();
    result.verify(image).expect("output receipt verification failed");
    assert_eq!(result.journal.bytes, journal, "compressed journal changed");
    let mut wrong_image = image;
    wrong_image.as_mut_words()[0] ^= 1;
    assert!(result.verify(wrong_image).is_err(), "wrong image accepted");
    let mut changed = result.clone();
    changed.journal.bytes[0] ^= 1;
    assert!(changed.verify(image).is_err(), "modified journal accepted");
    let params = Groth16ReceiptVerifierParameters::default();
    let params_digest = params.digest();
    let bytes = bincode::serialize(&result).unwrap();
    fs::create_dir_all(output).unwrap();
    fs::write(output.join("receipt.bin"), &bytes).unwrap();
    fs::write(output.join("journal.bin"), &journal).unwrap();
    let seal = if let InnerReceipt::Groth16(inner) = &result.inner {
        assert_eq!(inner.verifier_parameters, params_digest, "unexpected verifier parameters");
        assert_eq!(inner.seal.len(), 256, "unexpected Groth16 seal length");
        let mut seal = params_digest.as_bytes()[..4].to_vec();
        seal.extend_from_slice(&inner.seal);
        fs::write(output.join("seal.bin"), &seal).unwrap();
        Some(hex::encode(&seal))
    } else { None };
    let summary = serde_json::json!({
        "schema": "satoshifi.receipt-bridge.v1", "status": "VERIFIED",
        "production_ready": false, "input_kind": input_kind, "receipt_kind": kind(&result),
        "image_id": image.to_string(), "program_sha256": sha(&program),
        "input_receipt_sha256": sha(&input), "receipt_sha256": sha(&bytes),
        "journal_sha256": sha(&journal), "journal_bytes": journal.len(),
        "receipt_bytes": bytes.len(), "compression_seconds": elapsed,
        "groth16_parameters_digest": params_digest.to_string(),
        "groth16_selector": hex::encode(&params_digest.as_bytes()[..4]),
        "control_root": params.control_root.to_string(),
        "bn254_control_id_sdk_order": params.bn254_control_id.to_string(),
        "seal_hex": seal, "wrong_image_rejected": true, "modified_journal_rejected": true,
        "dev_mode_disabled": true, "public_transactions": 0
    });
    fs::write(output.join("receipt.json"), serde_json::to_string_pretty(&summary).unwrap() + "\n").unwrap();
    println!("{summary}");
}
