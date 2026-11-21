// SPDX-License-Identifier: MIT
//! Diagnostic native execution only; stdout is a journal, never a receipt.
use std::io::{self, Write};
fn main() {
    let bytes = satoshifi_testnet4_guest_kernel::read_request(io::stdin()).expect("invalid transport");
    let result = satoshifi_testnet4_guest_kernel::execute(&bytes).expect("invalid transition");
    io::stdout().write_all(&result.journal.encode_abi()).unwrap();
}
