// SPDX-License-Identifier: MIT
// New application commitment code; SHA-256 itself comes from pinned rust-bitcoin.
use bitcoin::hashes::{sha256, Hash as _, HashEngine as _};
pub type Hash = [u8; 32];

pub fn digest(domain: &[u8], parts: &[&[u8]]) -> Hash {
    let mut engine = sha256::Hash::engine();
    engine.input(b"SATOSHIFI_FORK_V1\0");
    engine.input(&(domain.len() as u32).to_be_bytes());
    engine.input(domain);
    for part in parts {
        engine.input(&(part.len() as u64).to_be_bytes());
        engine.input(part);
    }
    sha256::Hash::from_engine(engine).to_byte_array()
}

fn parent(depth: u8, left: Hash, right: Hash) -> Hash {
    digest(b"history-node", &[&[depth], &left, &right])
}

#[derive(Clone)]
struct Node { hash: Hash, left: usize, right: usize }

/// Persistent 64-level binary tree keyed by absolute uint64 height. Each child
/// inherits only its parent's branch root. Switching heads selects an existing
/// root, so a shorter winner cannot retain the former canonical tail.
pub(crate) struct History {
    nodes: Vec<Node>,
    empty: [Hash; 65],
}

impl History {
    pub fn new() -> Self {
        let mut empty = [[0; 32]; 65];
        empty[0] = digest(b"history-empty", &[]);
        for depth in 1..=64 { empty[depth] = parent(depth as u8, empty[depth - 1], empty[depth - 1]); }
        Self { nodes: vec![Node { hash: [0; 32], left: 0, right: 0 }], empty }
    }
    pub fn len(&self) -> usize { self.nodes.len() }
    pub fn truncate(&mut self, length: usize) { self.nodes.truncate(length); }
    pub fn hash(&self, index: usize, depth: u8) -> Hash {
        if index == 0 { self.empty[depth as usize] } else { self.nodes[index].hash }
    }
    pub fn insert(&mut self, root: usize, key: u64, value: Hash) -> usize { self.set(root, key, value, 64) }
    fn set(&mut self, root: usize, key: u64, value: Hash, depth: u8) -> usize {
        let node = if depth == 0 {
            Node { hash: value, left: 0, right: 0 }
        } else {
            let (mut left, mut right) = if root == 0 { (0, 0) }
                else { (self.nodes[root].left, self.nodes[root].right) };
            if (key >> (depth - 1)) & 1 == 0 { left = self.set(left, key, value, depth - 1); }
            else { right = self.set(right, key, value, depth - 1); }
            Node { hash: parent(depth, self.hash(left, depth - 1), self.hash(right, depth - 1)), left, right }
        };
        self.nodes.push(node);
        self.nodes.len() - 1
    }
    pub fn proof(&self, mut root: usize, key: u64) -> Vec<Hash> {
        let mut siblings = Vec::with_capacity(64);
        for depth in (1..=64).rev() {
            let (left, right) = if root == 0 { (0, 0) }
                else { (self.nodes[root].left, self.nodes[root].right) };
            let bit = (key >> (depth - 1)) & 1;
            siblings.push(self.hash(if bit == 0 { right } else { left }, depth - 1));
            root = if bit == 0 { left } else { right };
        }
        siblings.reverse(); // leaf-to-root; bit 0 of height is consumed first.
        siblings
    }
}

pub(crate) fn reconstruct(key: u64, leaf: Hash, siblings: &[Hash]) -> Option<Hash> {
    if siblings.len() != 64 { return None; }
    let mut result = leaf;
    for (bit, sibling) in siblings.iter().enumerate() {
        result = if (key >> bit) & 1 == 0 { parent((bit + 1) as u8, result, *sibling) }
            else { parent((bit + 1) as u8, *sibling, result) };
    }
    Some(result)
}
