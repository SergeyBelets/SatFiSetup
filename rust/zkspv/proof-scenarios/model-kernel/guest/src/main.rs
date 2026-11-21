//! TEST ONLY. Distinct image and journal domains; never a mainnet proof.
#![no_main]
use risc0_zkvm::guest::env;
use satoshifi_test_policy_kernel::{execute,read_request};
risc0_zkvm::guest::entry!(main);
pub fn main(){
    let bytes=read_request(env::stdin()).expect("test model framing");
    let (_,journal)=execute(&bytes).expect("test model transition");
    env::commit_slice(&journal);
}
