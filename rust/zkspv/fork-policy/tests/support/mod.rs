// Test-only synthetic graph binding. No PoW/MTP/difficulty acceptance is claimed.
use bitcoin::{block::{Header, Version}, BlockHash, TxMerkleNode, CompactTarget,
    consensus::{serialize, deserialize}, hashes::Hash as _};
use satoshifi_fork_policy::{Context, ForkState, Hash, digest};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TestContext { pub work: Hash, pub path: Hash }
impl Context for TestContext {
    fn work(&self) -> Hash { self.work }
    fn commitment(&self) -> Hash { digest(b"TEST-ONLY-context", &[&self.work, &self.path]) }
}
pub fn word(value: u128) -> Hash { let mut out=[0;32]; out[16..].copy_from_slice(&value.to_be_bytes()); out }
pub fn value(work: Hash) -> u128 { assert_eq!(&work[..16], &[0;16]); u128::from_be_bytes(work[16..].try_into().unwrap()) }
pub fn anchor_raw() -> Vec<u8> {
    let file = include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../web3/test/fixtures/mainnet/headers-32246-36288.bin"));
    file[800..880].to_vec()
}
pub fn initial_context() -> TestContext {
    let file = include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../web3/test/fixtures/mainnet/expected-32246-36288.bin"));
    TestContext { work: file[712..744].try_into().unwrap(), path: [0;32] }
}
pub fn state() -> ForkState<TestContext> {
    ForkState::from_validated_anchor(&anchor_raw(), 32256, initial_context()).unwrap()
}
pub fn raw(parent: Hash, serial: u32, delta: u32, time: u32) -> Vec<u8> {
    let mut root=[0;32]; root[28..].copy_from_slice(&serial.to_be_bytes());
    serialize(&Header { version: Version::from_consensus(1), prev_blockhash: BlockHash::from_byte_array(parent),
        merkle_root: TxMerkleNode::from_byte_array(root), time, bits: CompactTarget::from_consensus(0x1d00ffff), nonce: delta })
}
pub fn hash(raw: &[u8]) -> Hash { deserialize::<Header>(raw).unwrap().block_hash().to_byte_array() }
pub fn verify(parent: &TestContext, raw: &[u8;80], _: u64) -> Result<TestContext, &'static str> {
    let h: Header = deserialize(raw).unwrap();
    if h.nonce == 0 { return Err("synthetic invalid transition"); }
    Ok(TestContext { work: word(value(parent.work) + u128::from(h.nonce)),
        path: digest(b"TEST-ONLY-path", &[&parent.path, raw]) })
}
pub fn add(state: &mut ForkState<TestContext>, parent: Hash, serial: u32, delta: u32) -> Hash {
    let raw = raw(parent, serial, delta, 1300000000 + serial);
    let hash = hash(&raw);
    state.apply_batch(&[raw], verify).unwrap(); hash
}
pub fn chain(state: &mut ForkState<TestContext>, mut parent: Hash, count: u32, serial: &mut u32, delta: u32) -> Hash {
    for _ in 0..count { *serial+=1; parent=add(state,parent,*serial,delta); } parent
}
