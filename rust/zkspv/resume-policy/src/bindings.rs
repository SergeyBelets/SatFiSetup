// SPDX-License-Identifier: MIT
// Generated from the qualified V1 source; function bodies are unchanged.
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _};
use super::history::{digest, Hash};
fn parse(raw: &[u8]) -> Option<(Header, [u8; 80])> {
    let bytes: [u8; 80] = raw.try_into().ok()?;
    Some((deserialize(&bytes).ok()?, bytes))
}

fn scope(anchor: Hash, height: u64) -> Hash {
    digest(b"mainnet-anchor", &[b"bitcoin-mainnet", &anchor, &height.to_be_bytes()])
}

fn leaf(scope: Hash, height: u64, header: &Header) -> Hash {
    digest(b"canonical-header", &[&scope, &height.to_be_bytes(), &header.block_hash().to_byte_array(),
        &header.merkle_root.to_byte_array(), &header.time.to_be_bytes()])
}

fn canonical_root(scope: Hash, head: Hash, height: u64, tree: Hash) -> Hash {
    digest(b"canonical-root", &[&scope, &head, &height.to_be_bytes(), &tree])
}
pub(crate) fn header(raw: &[u8]) -> Option<(Header, [u8; 80])> { parse(raw) }
pub(crate) fn scoped(anchor: Hash, height: u64) -> Hash { scope(anchor, height) }
pub(crate) fn history_leaf(scope: Hash, height: u64, header: &Header) -> Hash { leaf(scope, height, header) }
pub(crate) fn canonical(scope: Hash, head: Hash, height: u64, tree: Hash) -> Hash { canonical_root(scope, head, height, tree) }
