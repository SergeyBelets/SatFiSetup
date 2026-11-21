// SPDX-License-Identifier: MIT
//! Application policy, not a Bitcoin header verifier or a proof verifier.
//!
//! A trusted guest must bind `advance` to its pinned header verifier and return
//! the timestamp of that same verified header. Commit the resulting ABI bytes
//! inside the authenticated transition journal. The EVM checks its own clock at
//! acceptance; no host/prover clock is accepted here. Authentication and fork
//! choice are the caller's obligations and are not supplied by this module.
#![no_std]

pub const ABI_LEN: usize = 128;
pub const DOMAIN: [u8; 32] = {
    let name = b"SATOSHIFI_HEADER_TIME_V1";
    let mut result = [0; 32];
    let mut index = 0;
    while index < name.len() {
        result[index] = name[index];
        index += 1;
    }
    result
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SummaryError {
    EmptyBatch,
    CountOverflow,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BatchError<E> {
    Header(E),
    Summary(SummaryError),
}

/// Summary of a nonempty processed sequence. The last timestamp is not a
/// substitute for a canonical-head timestamp selected by future fork policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimeSummary {
    count: u64,
    maximum: u32,
    last: u32,
}

impl TimeSummary {
    pub fn count(self) -> u64 { self.count }
    pub fn maximum(self) -> u32 { self.maximum }
    pub fn last(self) -> u32 { self.last }

    /// Compose consecutive, already-verified chunks. This merges time metadata
    /// only; their state/root continuity must also be authenticated by the guest.
    pub fn then(self, next: Self) -> Result<Self, SummaryError> {
        Ok(Self {
            count: self.count.checked_add(next.count).ok_or(SummaryError::CountOverflow)?,
            maximum: self.maximum.max(next.maximum),
            last: next.last,
        })
    }

    /// Solidity abi.encode(bytes32 domain, uint64 count, uint32 max, uint32 last).
    /// This is a versioned journal component, not a complete transition journal.
    pub fn encode_abi(self) -> [u8; ABI_LEN] {
        let mut result = [0; ABI_LEN];
        result[..32].copy_from_slice(&DOMAIN);
        result[56..64].copy_from_slice(&self.count.to_be_bytes());
        result[92..96].copy_from_slice(&self.maximum.to_be_bytes());
        result[124..128].copy_from_slice(&self.last.to_be_bytes());
        result
    }
}

/// Fold a nonempty linear sequence without returning partial state on error.
/// `advance` must be a side-effect-free binding to the actual consensus verifier;
/// a permissive callback does not qualify headers. Invalid input is an error,
/// never a permanent-halt result. Prior context timestamps are not new headers.
pub fn verify_batch<S, H, E>(
    initial: S,
    headers: impl IntoIterator<Item = H>,
    mut advance: impl FnMut(&S, H) -> Result<(S, u32), E>,
) -> Result<(S, TimeSummary), BatchError<E>> {
    let mut state = initial;
    let mut summary: Option<TimeSummary> = None;
    for header in headers {
        let (next, timestamp) = advance(&state, header).map_err(BatchError::Header)?;
        let item = TimeSummary { count: 1, maximum: timestamp, last: timestamp };
        summary = Some(match summary {
            None => item,
            Some(previous) => previous.then(item).map_err(BatchError::Summary)?,
        });
        state = next;
    }
    let summary = summary.ok_or(BatchError::Summary(SummaryError::EmptyBatch))?;
    Ok((state, summary))
}
