// SPDX-License-Identifier: MIT
//! Experimental application policy. This module does not verify Bitcoin PoW,
//! difficulty, MTP or checkpoints and is not an activated guest/state contract.
//! A trusted integration must supply the pinned verifier and a fully validated
//! immutable mainnet checkpoint/context. All context fields must be committed.
mod history;
pub use history::{digest, Hash};
use history::History;
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _};
use std::collections::BTreeMap;

pub const MAX_REORG_DEPTH: u64 = 100;
pub const MAX_BATCH: usize = 64;

/// The integration owns this binding: work is uint256 BIG endian, and context
/// commits every field required to resume the pinned verifier on this branch.
pub trait Context: Clone {
    fn work(&self) -> Hash;
    fn commitment(&self) -> Hash;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error<E> {
    InvalidAnchor, InvalidBatch, MalformedHeader, UnknownParent, HeightOverflow,
    NonIncreasingWork, ConflictingKnownHeader, Halted, NotCanonical, InvalidSnapshot,
    Header(E),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Halt {
    pub old_head: Hash, pub candidate: Hash, pub common_ancestor: Hash, pub depth: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct View {
    pub anchor: Hash, pub anchor_height: u64,
    pub head: Hash, pub height: u64, pub work: Hash, pub timestamp: u32,
    pub canonical_root: Hash, pub state_commitment: Hash, pub halted: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BatchOutcome {
    pub previous_commitment: Hash, pub view: View,
    /// Includes exact duplicates and the halt-triggering header; excludes suffix after halt.
    pub processed: usize, pub new_headers: usize, pub maximum_timestamp: Option<u32>,
    /// Zero-based index in this batch; absence distinguishes ordinary success.
    pub halt_at: Option<usize>, pub halt: Option<Halt>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Membership {
    pub height: u64, pub raw_header: [u8; 80],
    /// Exactly 64 hashes in leaf-to-root order; absolute height bits are the index.
    pub siblings: Vec<Hash>,
}

struct Entry<C> {
    raw: [u8; 80], parent: Hash, height: u64, timestamp: u32,
    context: C, skip: Hash, history: usize,
}

pub struct ForkState<C> {
    anchor: Hash, anchor_height: u64, scope: Hash,
    entries: BTreeMap<Hash, Entry<C>>, order: Vec<Hash>, history: History,
    head: Hash, log_commitment: Hash, halt: Option<Halt>,
}

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

/// Verify against an authenticated accepted View; a caller-supplied View is not
/// authentication. Fresh and transaction-Merkle inclusion are separate checks.
pub fn verify_membership(view: &View, proof: &Membership) -> bool {
    if view.halted || proof.height < view.anchor_height || proof.height > view.height { return false; }
    let Some((header, _)) = parse(&proof.raw_header) else { return false; };
    let scoped = scope(view.anchor, view.anchor_height);
    let Some(root) = history::reconstruct(proof.height, leaf(scoped, proof.height, &header), &proof.siblings)
        else { return false; };
    canonical_root(scoped, view.head, view.height, root) == view.canonical_root
}

// Port of the repository MainnetHeaderRelay's checkpoint-relative Core skip
// algorithm (MIT, Bitcoin Core developers). Exact source hash is in source-map.json.
fn invert_lowest(value: u64) -> u64 { if value == 0 { 0 } else { value & (value - 1) } }
fn skip_height(height: u64) -> u64 {
    if height < 2 { 0 } else if height & 1 != 0 { invert_lowest(invert_lowest(height - 1)) + 1 }
    else { invert_lowest(height) }
}

impl<C: Context> ForkState<C> {
    /// Preconditions outside this component: 80-byte mainnet checkpoint PoW,
    /// target, ten linked prior headers and exact initial difficulty/MTP context
    /// are validated; work includes the checkpoint and trusted earlier history.
    pub fn from_validated_anchor(raw: &[u8], height: u64, context: C) -> Result<Self, Error<()>> {
        let (header, raw) = parse(raw).ok_or(Error::InvalidAnchor)?;
        if height < 10 || height % 2016 != 0 || context.work() == [0; 32] { return Err(Error::InvalidAnchor); }
        let anchor = header.block_hash().to_byte_array();
        let scoped = scope(anchor, height);
        let mut history = History::new();
        let tree = history.insert(0, height, leaf(scoped, height, &header));
        let log = digest(b"anchor-context", &[&scoped, &context.work(), &context.commitment()]);
        let entry = Entry { raw, parent: header.prev_blockhash.to_byte_array(), height,
            timestamp: header.time, context, skip: anchor, history: tree };
        Ok(Self { anchor, anchor_height: height, scope: scoped,
            entries: BTreeMap::from([(anchor, entry)]), order: vec![], history,
            head: anchor, log_commitment: log, halt: None })
    }

    pub fn view(&self) -> View {
        let head = &self.entries[&self.head];
        let canonical = canonical_root(self.scope, self.head, head.height, self.history.hash(head.history, 64));
        let halt = match &self.halt {
            None => digest(b"running", &[]),
            Some(h) => digest(b"halt", &[&h.old_head, &h.candidate, &h.common_ancestor, &h.depth.to_be_bytes()]),
        };
        let commitment = digest(b"state", &[&self.scope, &self.log_commitment, &canonical,
            &self.head, &head.height.to_be_bytes(), &head.context.work(), &head.timestamp.to_be_bytes(), &halt]);
        View { anchor: self.anchor, anchor_height: self.anchor_height, head: self.head, height: head.height,
            work: head.context.work(), timestamp: head.timestamp, canonical_root: canonical,
            state_commitment: commitment, halted: self.halt.is_some() }
    }

    pub fn context(&self, hash: Hash) -> Option<&C> { self.entries.get(&hash).map(|node| &node.context) }
    pub fn known_count(&self) -> usize { self.entries.len() }
    pub fn retained_history_nodes(&self) -> usize { self.history.len() }

    pub fn ancestor(&self, mut cursor: Hash, height: u64) -> Option<Hash> {
        let node = self.entries.get(&cursor)?;
        if height < self.anchor_height || height > node.height { return None; }
        let mut walk = node.height - self.anchor_height;
        let target = height - self.anchor_height;
        while walk > target {
            let skip = skip_height(walk);
            let previous = skip_height(walk - 1);
            if skip == target || (skip > target && !(skip > 2 && previous < skip - 2 && previous >= target)) {
                cursor = self.entries[&cursor].skip; walk = skip;
            } else { cursor = self.entries[&cursor].parent; walk -= 1; }
        }
        Some(cursor)
    }

    pub fn common_ancestor(&self, mut a: Hash, mut b: Hash) -> Option<Hash> {
        let ah = self.entries.get(&a)?.height;
        let bh = self.entries.get(&b)?.height;
        if ah > bh { a = self.ancestor(a, bh)?; } else if bh > ah { b = self.ancestor(b, ah)?; }
        while a != b {
            let ap = self.entries[&a].skip;
            let bp = self.entries[&b].skip;
            if ap != bp { a = ap; b = bp; }
            else { a = self.entries[&a].parent; b = self.entries[&b].parent; }
        }
        Some(a)
    }

    pub fn confirmations(&self, hash: Hash) -> Option<u64> {
        if self.halt.is_some() { return None; }
        let height = self.entries.get(&hash)?.height;
        if self.ancestor(self.head, height)? != hash { return None; }
        Some(self.entries[&self.head].height - height + 1)
    }

    pub fn membership(&self, hash: Hash) -> Result<Membership, Error<()>> {
        if self.halt.is_some() { return Err(Error::Halted); }
        self.confirmations(hash).ok_or(Error::NotCanonical)?;
        let entry = &self.entries[&hash];
        Ok(Membership { height: entry.height, raw_header: entry.raw,
            siblings: self.history.proof(self.entries[&self.head].history, entry.height) })
    }

    /// Atomic errors restore entries, roots, head, log and arena allocations.
    /// A verified deep-reorg halt is successful, keeps the safe-prefix head and
    /// stops before parsing later inputs. Exact known bytes are no-op retries.
    pub fn apply_batch<E>(&mut self, headers: &[Vec<u8>],
        mut verify: impl FnMut(&C, &[u8; 80], u64) -> Result<C, E>) -> Result<BatchOutcome, Error<E>> {
        if self.halt.is_some() { return Err(Error::Halted); }
        if headers.is_empty() || headers.len() > MAX_BATCH { return Err(Error::InvalidBatch); }
        let previous_commitment = self.view().state_commitment;
        let old_head = self.head;
        let old_log = self.log_commitment;
        let old_order = self.order.len();
        let old_arena = self.history.len();
        let result = self.process(headers, &mut verify, previous_commitment);
        if result.is_err() {
            for hash in self.order.drain(old_order..) { self.entries.remove(&hash); }
            self.history.truncate(old_arena);
            self.head = old_head; self.log_commitment = old_log; self.halt = None;
        }
        result
    }

    fn process<E>(&mut self, headers: &[Vec<u8>], verify: &mut impl FnMut(&C, &[u8; 80], u64) -> Result<C, E>,
        previous_commitment: Hash) -> Result<BatchOutcome, Error<E>> {
        let mut processed = 0;
        let mut new_headers = 0;
        let mut maximum_timestamp: Option<u32> = None;
        let mut halt_at = None;
        for (index, bytes) in headers.iter().enumerate() {
            let (header, raw) = parse(bytes).ok_or(Error::MalformedHeader)?;
            let hash = header.block_hash().to_byte_array();
            processed += 1;
            if let Some(known) = self.entries.get(&hash) {
                if known.raw != raw { return Err(Error::ConflictingKnownHeader); }
                continue;
            }
            let parent_hash = header.prev_blockhash.to_byte_array();
            let parent = self.entries.get(&parent_hash).ok_or(Error::UnknownParent)?;
            let height = parent.height.checked_add(1).ok_or(Error::HeightOverflow)?;
            let context = verify(&parent.context, &raw, height).map_err(Error::Header)?;
            if context.work() <= parent.context.work() { return Err(Error::NonIncreasingWork); }
            let skip = self.ancestor(parent_hash, self.anchor_height + skip_height(height - self.anchor_height)).unwrap();
            let tree = self.history.insert(parent.history, height, leaf(self.scope, height, &header));
            self.log_commitment = digest(b"verified-entry", &[&self.log_commitment, &hash, &parent_hash,
                &height.to_be_bytes(), &context.work(), &context.commitment(), &skip, &self.history.hash(tree, 64)]);
            self.entries.insert(hash, Entry { raw, parent: parent_hash, height, timestamp: header.time, context, skip, history: tree });
            self.order.push(hash);
            new_headers += 1;
            maximum_timestamp = Some(maximum_timestamp.map_or(header.time, |old| old.max(header.time)));
            if self.entries[&hash].context.work() <= self.entries[&self.head].context.work() { continue; }
            let common = self.common_ancestor(self.head, hash).unwrap();
            let depth = self.entries[&self.head].height - self.entries[&common].height;
            if depth > MAX_REORG_DEPTH {
                self.halt = Some(Halt { old_head: self.head, candidate: hash, common_ancestor: common, depth });
                halt_at = Some(index);
                break;
            }
            self.head = hash;
        }
        Ok(BatchOutcome { previous_commitment, view: self.view(), processed, new_headers,
            maximum_timestamp, halt_at, halt: self.halt.clone() })
    }

    /// Complete accepted insertion log, including side branches and any halt
    /// candidate. No private material; omit duplicates and ignored batch suffixes.
    pub fn snapshot_headers(&self) -> Vec<Vec<u8>> {
        self.order.iter().map(|hash| self.entries[hash].raw.to_vec()).collect()
    }

    /// Replay data into a fresh validated anchor using the real verifier. Never
    /// trust serialized context, skip links, roots or claimed chainwork. The
    /// expected commitment must come from authenticated accepted state.
    pub fn restore<E>(mut anchor: Self, records: &[Vec<u8>], expected: Hash,
        mut verify: impl FnMut(&C, &[u8; 80], u64) -> Result<C, E>) -> Result<Self, Error<E>> {
        if anchor.entries.len() != 1 || anchor.halt.is_some() { return Err(Error::InvalidSnapshot); }
        for chunk in records.chunks(MAX_BATCH) {
            let result = anchor.apply_batch(chunk, &mut verify)?;
            if result.processed != chunk.len() || result.new_headers != chunk.len() { return Err(Error::InvalidSnapshot); }
        }
        if anchor.view().state_commitment != expected { return Err(Error::InvalidSnapshot); }
        Ok(anchor)
    }
}
