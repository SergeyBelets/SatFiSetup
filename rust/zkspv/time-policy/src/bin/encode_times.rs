// Test-vector utility only: scalar times here are NOT authenticated headers.
use satoshifi_header_time_policy::verify_batch;
use std::io::{self, BufRead};

fn main() {
    for line in io::stdin().lock().lines() {
        let line = line.unwrap();
        let times: Vec<u32> = line.split(',').map(|word| word.parse().unwrap()).collect();
        let (_, summary) = verify_batch((), times, |_, time| Ok::<_, ()>(((), time))).unwrap();
        for byte in summary.encode_abi() { print!("{byte:02x}"); }
        println!();
    }
}
