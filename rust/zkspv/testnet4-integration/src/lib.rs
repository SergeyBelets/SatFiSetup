// SPDX-License-Identifier: MIT
//! Fixed Bitcoin testnet4 consensus adapter. No witness-selectable network.
use bitcoin::{consensus::deserialize, block::Header};
use bitcoin_da::{spec::{header::HeaderWrapper, RollupParams}, verifier::{BitcoinVerifier, ValidationError}};
use sov_rollup_interface::{da::{DaVerifier, LatestDaState}, Network};
use std::io::{self, Read};
pub use satoshifi_resume_transition as transition;
use transition::bounded::checkpoint::InitialContext;

pub fn verify_testnet4(parent: &InitialContext, raw: &[u8; 80], height: u64)
    -> Result<InitialContext, ValidationError>
{
    let header: Header = deserialize(raw).map_err(|_| ValidationError::InvalidBlock)?;
    let previous_time = parent.prev_11_timestamps[(parent.block_height % 11) as usize];
    // BIP94: the first block in an epoch cannot go back over ten minutes.
    // Checked before upstream PoW; no signed result can omit this condition.
    if height % 2016 == 0 && u64::from(header.time) + 600 < u64::from(previous_time) {
        return Err(ValidationError::InvalidTimestamp);
    }
    let header = HeaderWrapper::new(header, 0, height, [0; 32]);
    let previous = LatestDaState { block_hash: parent.block_hash, block_height: parent.block_height,
        total_work: parent.total_work, current_target_bits: parent.current_target_bits,
        epoch_start_time: parent.epoch_start_time, prev_11_timestamps: parent.prev_11_timestamps };
    // Pinned Citrea names the testnet4 dispatch variant `Testnet`.
    let verifier = BitcoinVerifier::new(RollupParams { reveal_tx_prefix: vec![], network: Network::Testnet });
    let next = verifier.verify_header_chain(Some(&previous), &header, Network::Testnet)?;
    Ok(InitialContext { block_hash: next.block_hash, block_height: next.block_height,
        total_work: next.total_work, current_target_bits: next.current_target_bits,
        epoch_start_time: next.epoch_start_time, prev_11_timestamps: next.prev_11_timestamps })
}

pub fn read_request(mut input: impl Read) -> io::Result<Vec<u8>> {
    let mut length = [0; 4]; input.read_exact(&mut length)?;
    let length = u32::from_le_bytes(length) as usize;
    if length == 0 || length > transition::MAX_INPUT_BYTES {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "guest input length"));
    }
    let mut bytes = vec![0; length]; input.read_exact(&mut bytes)?;
    let mut trailing = [0];
    if input.read(&mut trailing)? != 0 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "trailing guest input"));
    }
    Ok(bytes)
}

pub fn execute(bytes: &[u8]) -> Result<transition::Execution, transition::RunError<ValidationError>> {
    transition::execute_encoded(bytes, verify_testnet4)
}
