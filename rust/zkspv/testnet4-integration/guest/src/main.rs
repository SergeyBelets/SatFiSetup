// SPDX-License-Identifier: MIT
#![no_main]
use risc0_zkvm::guest::env;
use satoshifi_testnet4_guest_kernel::{execute, read_request};
risc0_zkvm::guest::entry!(main);
pub fn main() {
    let bytes = read_request(env::stdin()).expect("invalid bounded guest transport");
    let result = execute(&bytes).expect("invalid testnet4 transition");
    env::commit_slice(&result.journal.encode_abi());
}
