// SPDX-License-Identifier: MIT
//! Static mainnet adapter. Build only in the source-verified prepared workspace.
use bitcoin::consensus::deserialize;
use bitcoin_da::{spec::{header::HeaderWrapper, RollupParams}, verifier::{BitcoinVerifier, ValidationError}};
use sov_rollup_interface::{da::{DaVerifier, LatestDaState}, Network};
use std::io::{self, Read};
pub use satoshifi_resume_transition as transition;
use transition::bounded::checkpoint::InitialContext;

/// All consensus rules are delegated to the pinned, corrected upstream crate.
/// The caller cannot choose a network or supply a precomputed block hash.
pub fn verify_mainnet(parent: &InitialContext, raw: &[u8; 80], height: u64)
    -> Result<InitialContext, ValidationError>
{
    let header = deserialize(raw).map_err(|_| ValidationError::InvalidBlock)?;
    let header = HeaderWrapper::new(header, 0, height, [0; 32]);
    let previous = LatestDaState {
        block_hash: parent.block_hash, block_height: parent.block_height,
        total_work: parent.total_work, current_target_bits: parent.current_target_bits,
        epoch_start_time: parent.epoch_start_time, prev_11_timestamps: parent.prev_11_timestamps,
    };
    let verifier = BitcoinVerifier::new(RollupParams { reveal_tx_prefix: vec![], network: Network::Mainnet });
    let next = verifier.verify_header_chain(Some(&previous), &header, Network::Mainnet)?;
    Ok(InitialContext {
        block_hash: next.block_hash, block_height: next.block_height, total_work: next.total_work,
        current_target_bits: next.current_target_bits, epoch_start_time: next.epoch_start_time,
        prev_11_timestamps: next.prev_11_timestamps,
    })
}

/// Raw transport: u32 little-endian byte length, then exactly that many bytes.
/// Bound the outer allocation, use no serde vector decoding, require EOF.
pub fn read_request(mut input: impl Read) -> io::Result<Vec<u8>> {
    let mut length = [0; 4];
    input.read_exact(&mut length)?;
    let length = u32::from_le_bytes(length) as usize;
    if length == 0 || length > transition::MAX_INPUT_BYTES {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "guest input length"));
    }
    let mut bytes = vec![0; length];
    input.read_exact(&mut bytes)?;
    let mut trailing = [0];
    if input.read(&mut trailing)? != 0 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "trailing guest input"));
    }
    Ok(bytes)
}

/// No verifier callback, network flag, development proof, or host-supplied journal.
pub fn execute(bytes: &[u8]) -> Result<transition::Execution, transition::RunError<ValidationError>> {
    transition::execute_encoded(bytes, verify_mainnet)
}

