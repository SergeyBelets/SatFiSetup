// SPDX-License-Identifier: MIT
use super::{checkpoint::{self, InitialContext, ValidatedCheckpoint}, fork::{self, Context, ForkState, Hash, View}, time};

pub const ABI_LEN: usize = 32 * 32;
pub const DOMAIN: &[u8] = b"SATOSHIFI_TRANSITION_V1";
pub const NETWORK: &[u8] = b"bitcoin-mainnet";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Deployment {
    /// Unsigned 256-bit EVM chain ID, big endian. Not Bitcoin network selection.
    pub evm_chain_id: Hash,
    pub state_address: [u8; 20],
    pub checkpoint_descriptor: Hash,
}

#[derive(Debug)]
pub enum StepError<E> { Header(E), Time(time::SummaryError) }
#[derive(Debug)]
pub enum Error<E> {
    Checkpoint(checkpoint::Error), CheckpointDescriptor, InvalidBatch, Halted,
    Restore(fork::Error<E>), AcceptedStateMismatch, Batch(fork::Error<StepError<E>>),
    NoChange, TimeBinding,
}

/// Constructed only after the entire accepted prefix has passed verification.
/// Full old/new context is transitively bound by the fork state commitments.
/// Counts describe submitted items, processed items and UNIQUE new headers.
#[derive(Clone, Debug)]
pub struct Journal {
    deployment: Deployment,
    before: View,
    after: View,
    batch_commitment: Hash,
    submitted: u64,
    processed: u64,
    summary: time::TimeSummary,
    halt_position: u64,
    halt: Option<fork::Halt>,
}

impl Journal {
    pub fn deployment(&self) -> &Deployment { &self.deployment }
    pub fn before(&self) -> &View { &self.before }
    pub fn after(&self) -> &View { &self.after }
    pub fn submitted(&self) -> u64 { self.submitted }
    pub fn processed(&self) -> u64 { self.processed }
    pub fn time_summary(&self) -> time::TimeSummary { self.summary }
    /// One-based position of the halt trigger, or zero if no halt occurred.
    pub fn halt_position(&self) -> u64 { self.halt_position }
    pub fn halt(&self) -> Option<&fork::Halt> { self.halt.as_ref() }
    pub fn batch_commitment(&self) -> Hash { self.batch_commitment }
    /// Side-branch accumulation alone must not refresh lastUsefulAcceptedAt.
    pub fn useful(&self) -> bool { self.after.work > self.before.work }

    /// Exactly 32 ABI words. Integers are big endian and left-zero-padded;
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
        put(&mut out, 5, &self.before.anchor);
        put(&mut out, 6, &self.before.anchor_height.to_be_bytes());
        put(&mut out, 7, &self.before.state_commitment);
        put(&mut out, 8, &self.after.state_commitment);
        put(&mut out, 9, &self.before.canonical_root);
        put(&mut out, 10, &self.after.canonical_root);
        for (at, view) in [(11, &self.before), (15, &self.after)] {
            put(&mut out, at, &view.head);
            put(&mut out, at + 1, &view.height.to_be_bytes());
            put(&mut out, at + 2, &view.work);
            put(&mut out, at + 3, &view.timestamp.to_be_bytes());
        }
        put(&mut out, 19, &self.batch_commitment);
        put(&mut out, 20, &self.submitted.to_be_bytes());
        put(&mut out, 21, &self.processed.to_be_bytes());
        out[22 * 32..26 * 32].copy_from_slice(&self.summary.encode_abi());
        put(&mut out, 26, &[u8::from(self.after.halted)]);
        put(&mut out, 27, &self.halt_position.to_be_bytes());
        if let Some(halt) = &self.halt {
            put(&mut out, 28, &halt.old_head);
            put(&mut out, 29, &halt.candidate);
            put(&mut out, 30, &halt.common_ancestor);
            put(&mut out, 31, &halt.depth.to_be_bytes());
        }
        out
    }
}

pub struct Execution<C> {
    pub state: ForkState<C>,
    pub journal: Journal,
}

/// Restore the COMPLETE admitted history, match every externally accepted field,
/// then execute one ordered 1..64-item batch. The input state is never mutated.
/// A malformed/invalid unknown item returns no journal; a verified deep reorg
/// returns the safe prefix plus permanent halt. Ignored suffix bytes are hashed
/// into the batch commitment but are not interpreted as validated headers.
///
/// `bind` and `verify` are trusted native/guest type plumbing, not proof inputs.
/// Snapshot replay is currently unbounded and is NOT a succinct resume witness.
pub fn execute<C: Context, E>(
    checkpoint: &ValidatedCheckpoint,
    deployment: Deployment,
    accepted: &View,
    snapshot: &[Vec<u8>],
    headers: &[Vec<u8>],
    bind: impl FnOnce(&InitialContext) -> C,
    mut verify: impl FnMut(&C, &[u8; 80], u64) -> Result<C, E>,
) -> Result<Execution<C>, Error<E>> {
    if deployment.checkpoint_descriptor != checkpoint.descriptor_commitment() { return Err(Error::CheckpointDescriptor); }
    if headers.is_empty() || headers.len() > fork::MAX_BATCH { return Err(Error::InvalidBatch); }
    if accepted.halted { return Err(Error::Halted); }
    let anchor = checkpoint.initialize(bind).map_err(Error::Checkpoint)?;
    let mut state = ForkState::restore(anchor, snapshot, accepted.state_commitment, &mut verify).map_err(Error::Restore)?;
    if state.view() != *accepted { return Err(Error::AcceptedStateMismatch); }
    let mut summary: Option<time::TimeSummary> = None;
    let outcome = state.apply_batch(headers, |parent, raw, height| {
        // Time is taken from the exact bytes passed to the actual verifier.
        let (next, part) = time::verify_batch(parent.clone(), [raw], |context, raw| {
            verify(context, raw, height).map(|next| (next, u32::from_le_bytes(raw[68..72].try_into().unwrap())))
        }).map_err(|error| match error {
            time::BatchError::Header(error) => StepError::Header(error),
            time::BatchError::Summary(error) => StepError::Time(error),
        })?;
        summary = Some(match summary {
            None => part,
            Some(previous) => previous.then(part).map_err(StepError::Time)?,
        });
        Ok(next)
    }).map_err(Error::Batch)?;
    if outcome.new_headers == 0 { return Err(Error::NoChange); }
    let summary = summary.ok_or(Error::TimeBinding)?;
    if summary.count() != outcome.new_headers as u64 || Some(summary.maximum()) != outcome.maximum_timestamp {
        return Err(Error::TimeBinding);
    }
    let parts: Vec<&[u8]> = headers.iter().map(Vec::as_slice).collect();
    let journal = Journal {
        deployment, before: accepted.clone(), after: outcome.view,
        batch_commitment: fork::digest(b"submitted-batch-v1", &parts),
        submitted: headers.len() as u64, processed: outcome.processed as u64, summary,
        halt_position: outcome.halt_at.map_or(0, |index| index as u64 + 1), halt: outcome.halt,
    };
    Ok(Execution { state, journal })
}
