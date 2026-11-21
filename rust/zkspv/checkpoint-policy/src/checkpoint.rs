// SPDX-License-Identifier: MIT OR Apache-2.0
use super::{compact, fork::{self, Context, ForkState, Hash}};
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _, Target};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Height, HeaderLength, AncestorCount, AncestorLength, AncestorLink,
    Target, ProofOfWork, ChainWork, ContextBinding, ForkInitialization,
}

/// Fields required to resume the pinned Citrea mainnet verifier. This is derived
/// from the validated anchor, not accepted as user input. Timestamps are a ring
/// indexed by absolute block height modulo 11, not chronological array order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InitialContext {
    pub block_hash: Hash,
    pub block_height: u64,
    pub total_work: Hash,
    pub current_target_bits: u32,
    pub epoch_start_time: u32,
    pub prev_11_timestamps: [u32; 11],
}

impl Context for InitialContext {
    fn work(&self) -> Hash { self.total_work }
    fn commitment(&self) -> Hash {
        let times: Vec<u8> = self.prev_11_timestamps.iter().flat_map(|t| t.to_be_bytes()).collect();
        fork::digest(b"citrea-LatestDaState-v1", &[&self.block_hash, &self.block_height.to_be_bytes(),
            &self.total_work, &self.current_target_bits.to_be_bytes(), &self.epoch_start_time.to_be_bytes(), &times])
    }
}

/// Constructible only through validation. Does not prove historic chainwork,
/// the anchor's earlier retarget, or its own MTP (that needs eleven predecessors).
#[derive(Clone, Debug)]
pub struct ValidatedCheckpoint {
    raw: [u8; 80],
    prior: [[u8; 80]; 10],
    context: InitialContext,
}

impl ValidatedCheckpoint {
    /// Exactly ten oldest-to-newest linked predecessors, a mainnet epoch boundary,
    /// valid anchor target/PoW, and trusted cumulative work INCLUDING this block.
    /// Predecessors provide MTP context only; their PoW is not revalidated here.
    /// Acceptance-time policy for NEW headers is a separate EVM check.
    pub fn validate(raw: &[u8], height: u64, chain_work: Hash, prior: &[Vec<u8>]) -> Result<Self, Error> {
        if height < 10 || height % 2016 != 0 { return Err(Error::Height); }
        let raw: [u8; 80] = raw.try_into().map_err(|_| Error::HeaderLength)?;
        if prior.len() != 10 { return Err(Error::AncestorCount); }
        let header: Header = deserialize(&raw).map_err(|_| Error::HeaderLength)?;
        // Verbatim, hash-pinned corrected upstream compact decoder; no new target arithmetic.
        let target = compact::mainnet_target(header.bits.to_consensus()).map_err(|_| Error::Target)?;
        let hash = header.validate_pow(Target::from_be_bytes(target)).map_err(|_| Error::ProofOfWork)?;
        if chain_work < header.work().to_be_bytes() { return Err(Error::ChainWork); }
        let mut stored = [[0; 80]; 10];
        let mut previous = None;
        let mut times = [0; 11];
        for (i, bytes) in prior.iter().enumerate() {
            stored[i] = bytes.as_slice().try_into().map_err(|_| Error::AncestorLength)?;
            let ancestor: Header = deserialize(&stored[i]).map_err(|_| Error::AncestorLength)?;
            if previous.is_some_and(|hash| ancestor.prev_blockhash != hash) { return Err(Error::AncestorLink); }
            previous = Some(ancestor.block_hash());
            times[((height - 10 + i as u64) % 11) as usize] = ancestor.time;
        }
        if previous != Some(header.prev_blockhash) { return Err(Error::AncestorLink); }
        times[(height % 11) as usize] = header.time;
        Ok(Self { raw, prior: stored, context: InitialContext {
            block_hash: hash.to_byte_array(), block_height: height, total_work: chain_work,
            current_target_bits: header.bits.to_consensus(), epoch_start_time: header.time,
            prev_11_timestamps: times,
        } })
    }

    pub fn context(&self) -> &InitialContext { &self.context }
    pub fn raw_header(&self) -> &[u8; 80] { &self.raw }

    /// Deployment descriptor commitment, distinct from the initial fork-state
    /// commitment. An external authenticated deployment/proof must bind it.
    pub fn descriptor_commitment(&self) -> Hash {
        let prior: Vec<u8> = self.prior.iter().flat_map(|h| h.iter().copied()).collect();
        fork::digest(b"mainnet-checkpoint-v1", &[b"bitcoin-mainnet", &self.raw,
            &self.context.block_height.to_be_bytes(), &self.context.total_work, &prior])
    }

    /// `bind` adapts the derived fields to the trusted native verifier type.
    /// Its Context implementation must honestly commit ALL verifier fields using
    /// the InitialContext encoding. This is not a caller-supplied validation hook.
    pub fn initialize<C: Context>(&self, bind: impl FnOnce(&InitialContext) -> C) -> Result<ForkState<C>, Error> {
        let context = bind(&self.context);
        if context.work() != self.context.total_work || context.commitment() != self.context.commitment() {
            return Err(Error::ContextBinding);
        }
        ForkState::from_validated_anchor(&self.raw, self.context.block_height, context)
            .map_err(|_| Error::ForkInitialization)
    }
}
