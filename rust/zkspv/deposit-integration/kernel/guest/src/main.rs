//! TEST ONLY: genuine regtest PoW; cannot produce the mainnet journal domain.
#![no_main]
use risc0_zkvm::guest::env;
use satoshifi_regtest_deposit_kernel::{execute,read_request};
risc0_zkvm::guest::entry!(main);
pub fn main(){let input=read_request(env::stdin()).expect("framing");let (journal,_)=execute(&input).expect("regtest headers");env::commit_slice(&journal);}
