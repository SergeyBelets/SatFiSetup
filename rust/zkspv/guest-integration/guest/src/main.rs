// SPDX-License-Identifier: MIT
#![no_main]
use risc0_zkvm::guest::env;
use satoshifi_mainnet_guest_kernel::{execute, read_request};
risc0_zkvm::guest::entry!(main);

pub fn main() {
    let bytes = read_request(env::stdin()).expect("invalid bounded guest transport");
    let result = execute(&bytes).expect("invalid mainnet transition");
    // The EVM authenticates sha256 of these exact 1216 bytes, without serde framing.
    env::commit_slice(&result.journal.encode_abi());
}

