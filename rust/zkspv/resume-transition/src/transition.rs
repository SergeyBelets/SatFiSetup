// SPDX-License-Identifier: MIT
use super::bounded::{self, State, Witness, checkpoint::InitialContext, fork::{self, Hash}, time};
pub const ABI_LEN: usize = 38 * 32;
pub const DOMAIN: &[u8] = b"SATOSHIFI_TRANSITION_V2";
pub const NETWORK: &[u8] = b"bitcoin-mainnet";
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Deployment {
    pub evm_chain_id: Hash,
    pub state_address: [u8; 20],
    pub checkpoint_descriptor: Hash,
}
#[derive(Debug)]
pub enum Error<E> { CheckpointDescriptor, Resume(bounded::Error<E>) }
/// Constructed only after the entire accepted prefix has passed verification.
/// Full old/new context is transitively bound by the fork state commitments.
/// Counts describe submitted items, processed items and UNIQUE new headers.
#[derive(Clone, Debug)]
pub struct Journal {
    deployment: Deployment,
    before: State,
    after: State,
    batch_commitment: Hash,
    submitted: u64,
    processed: u64,
    summary: time::TimeSummary,
    halt_position: u64,
    halt: Option<fork::Halt>,
}

impl Journal {
    pub fn deployment(&self) -> &Deployment { &self.deployment }
    pub fn before(&self) -> &State { &self.before }
    pub fn after(&self) -> &State { &self.after }
    pub fn submitted(&self) -> u64 { self.submitted }
    pub fn processed(&self) -> u64 { self.processed }
    pub fn time_summary(&self) -> time::TimeSummary { self.summary }
    /// One-based position of the halt trigger, or zero if no halt occurred.
    pub fn halt_position(&self) -> u64 { self.halt_position }
    pub fn halt(&self) -> Option<&fork::Halt> { self.halt.as_ref() }
    pub fn batch_commitment(&self) -> Hash { self.batch_commitment }
    /// Side-branch accumulation alone must not refresh lastUsefulAcceptedAt.
    pub fn useful(&self) -> bool { self.after.view.work > self.before.view.work }

    /// Exactly 38 ABI words. Integers are big endian and left-zero-padded;
    /// bytes32 ASCII domains are right-zero-padded. See README for the word map.
    pub fn encode_abi(&self) -> [u8; ABI_LEN] {
        let mut out = [0; ABI_LEN];
        out[..DOMAIN.len()].copy_from_slice(DOMAIN);
        out[32..32 + NETWORK.len()].copy_from_slice(NETWORK);
        fn put(out: &mut [u8; ABI_LEN], index: usize, value: &[u8]) {
            out[(index + 1) * 32 - value.len()..(index + 1) * 32].copy_from_slice(value);
        }
        put(&mut out, 2, &self.deployment.evm_chain_id);
        put(&mut out, 3, &self.deployment.state_address);
        put(&mut out, 4, &self.deployment.checkpoint_descriptor);
        put(&mut out, 5, &self.before.view.anchor);
        put(&mut out, 6, &self.before.view.anchor_height.to_be_bytes());
        put(&mut out, 7, &self.before.view.state_commitment);
        put(&mut out, 8, &self.after.view.state_commitment);
        put(&mut out, 9, &self.before.view.canonical_root);
        put(&mut out, 10, &self.after.view.canonical_root);
        for (at, view) in [(11, &self.before.view), (15, &self.after.view)] {
            put(&mut out, at, &view.head);
            put(&mut out, at + 1, &view.height.to_be_bytes());
            put(&mut out, at + 2, &view.work);
            put(&mut out, at + 3, &view.timestamp.to_be_bytes());
        }
        put(&mut out, 19, &self.batch_commitment);
        put(&mut out, 20, &self.submitted.to_be_bytes());
        put(&mut out, 21, &self.processed.to_be_bytes());
        out[22 * 32..26 * 32].copy_from_slice(&self.summary.encode_abi());
        put(&mut out, 26, &[u8::from(self.after.view.halted)]);
        put(&mut out, 27, &self.halt_position.to_be_bytes());
        if let Some(halt) = &self.halt {
            put(&mut out, 28, &halt.old_head);
            put(&mut out, 29, &halt.candidate);
            put(&mut out, 30, &halt.common_ancestor);
            put(&mut out, 31, &halt.depth.to_be_bytes());
        }
        for (at, state) in [(32, &self.before), (35, &self.after)] {
            put(&mut out, at, &state.known_root);
            put(&mut out, at + 1, &state.known_log);
            put(&mut out, at + 2, &state.known_count.to_be_bytes());
        }
        out
    }
}

pub struct Execution { pub state: State, pub journal: Journal }

/// The accepted commitment is an externally authenticated value. Passing the
/// supplied state's own commitment is only appropriate inside a guest whose
/// resulting journal binds it for comparison with EVM storage.
/// `verify` is trusted static code, never a selectable witness field.
pub fn execute<E>(deployment: Deployment, expected: Hash, accepted: &State,
    headers: &[Vec<u8>], witnesses: &[Witness],
    verify: impl FnMut(&InitialContext, &[u8;80], u64) -> Result<InitialContext,E>,
) -> Result<Execution,Error<E>> {
    if deployment.checkpoint_descriptor != accepted.checkpoint_descriptor {
        return Err(Error::CheckpointDescriptor);
    }
    let outcome = bounded::resume(expected, accepted, headers, witnesses, verify).map_err(Error::Resume)?;
    let journal = Journal {
        deployment, before: accepted.clone(), after: outcome.state.clone(),
        batch_commitment: outcome.batch_commitment, submitted: headers.len() as u64,
        processed: outcome.processed as u64, summary: outcome.summary,
        halt_position: outcome.halt_at.map_or(0, |i| i as u64 + 1), halt: outcome.state.halt.clone(),
    };
    Ok(Execution {state: outcome.state, journal})
}
