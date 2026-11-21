// Test adapters only. Header decoding and hashing use pinned rust-bitcoin.
// No PoW, target, retarget or timestamp validation is implemented here.
use bitcoin::hashes::Hash;

#[derive(Clone)]
pub struct HeaderWrapper {
    inner: bitcoin::block::Header,
    height: u64,
}

#[derive(Clone, Copy)]
pub struct HashBytes([u8; 32]);

impl HashBytes {
    pub fn to_byte_array(self) -> [u8; 32] { self.0 }
}

impl From<HashBytes> for [u8; 32] {
    fn from(value: HashBytes) -> Self { value.0 }
}

pub struct Seconds(u32);

impl Seconds {
    pub fn secs(self) -> i64 { i64::from(self.0) }
}

impl HeaderWrapper {
    pub fn decode(hex: &str, height: u64) -> Self {
        let bytes = hex::decode(hex).expect("fixture hex");
        assert_eq!(bytes.len(), 80, "complete canonical header");
        Self { inner: bitcoin::consensus::deserialize(&bytes).expect("fixture header"), height }
    }
    pub fn prev_hash(&self) -> HashBytes { HashBytes(self.inner.prev_blockhash.to_byte_array()) }
    pub fn hash(&self) -> HashBytes { HashBytes(self.inner.block_hash().to_byte_array()) }
    pub fn verify_hash(&self) -> bool {
        // This adapter computes every hash, with no caller-supplied hash cache.
        self.hash().0 == self.inner.block_hash().to_byte_array()
    }
    pub fn height(&self) -> u64 { self.height }
    pub fn time(&self) -> Seconds { Seconds(self.inner.time) }
    pub fn bits(&self) -> u32 { self.inner.bits.to_consensus() }
    pub fn inner(&self) -> &bitcoin::block::Header { &self.inner }
}
