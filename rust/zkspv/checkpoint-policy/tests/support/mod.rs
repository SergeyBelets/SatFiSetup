#![allow(dead_code)]
use satoshifi_checkpoint_policy::ValidatedCheckpoint;
pub const FIRST: u64 = 32246;
pub const HEADERS: &[u8] = include_bytes!("../../../../../web3/test/fixtures/mainnet/headers-32246-36288.bin");
pub const EXPECTED: &[u8] = include_bytes!("../../../../../web3/test/fixtures/mainnet/expected-32246-36288.bin");
pub fn header(height: u64) -> Vec<u8> {
    let offset = (height - FIRST) as usize * 80;
    HEADERS[offset..offset + 80].to_vec()
}
pub fn work(height: u64) -> [u8; 32] {
    let offset = (height - FIRST) as usize * 68 + 32;
    EXPECTED[offset..offset + 32].try_into().unwrap()
}
pub fn prior(height: u64) -> Vec<Vec<u8>> { (height - 10..height).map(header).collect() }
pub fn checkpoint(height: u64) -> ValidatedCheckpoint {
    ValidatedCheckpoint::validate(&header(height), height, work(height), &prior(height)).unwrap()
}
