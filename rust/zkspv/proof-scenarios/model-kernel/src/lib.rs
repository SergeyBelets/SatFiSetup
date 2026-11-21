//! TEST ONLY: synthetic work from nonce, no Bitcoin PoW qualification.
//! Uses the unchanged bounded fork/history transition and a distinct journal domain.
pub use satoshifi_mainnet_guest_kernel::{read_request, transition};
use transition::bounded::checkpoint::InitialContext;
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _};
pub const DOMAIN: &[u8] = b"SATOSHIFI_TEST_POLICY_V1";
pub const NETWORK: &[u8] = b"synthetic-no-pow";

/// Test model derived from resume-policy/tests/support/mod.rs::verify.
/// Nonce is a synthetic work increment. This never verifies mainnet consensus.
pub fn verify(c:&InitialContext,raw:&[u8;80],height:u64)->Result<InitialContext,&'static str>{
    let h:Header=deserialize(raw).map_err(|_|"model header")?;
    if h.nonce==0 || c.total_work[..16]!=[0;16]{return Err("model work");}
    let work=u128::from_be_bytes(c.total_work[16..].try_into().unwrap())
        .checked_add(u128::from(h.nonce)).ok_or("model overflow")?;
    let mut next=c.clone();next.block_hash=h.block_hash().to_byte_array();next.block_height=height;
    next.total_work=[0;32];next.total_work[16..].copy_from_slice(&work.to_be_bytes());
    next.prev_11_timestamps[(height%11) as usize]=h.time;Ok(next)
}

pub fn execute(bytes:&[u8])->Result<(transition::Execution,[u8;1216]),transition::RunError<&'static str>>{
    let out=transition::execute_encoded(bytes,verify)?;
    // Prove terminal-state rejection as part of a successful halt-model execution.
    if out.state.view.halted {
        assert!(matches!(transition::bounded::resume(out.state.view.state_commitment,&out.state,
            &[vec![0;80]],&[],verify),Err(transition::bounded::Error::Halted)));
    }
    let mut journal=out.journal.encode_abi();journal[..64].fill(0);
    journal[..DOMAIN.len()].copy_from_slice(DOMAIN);
    journal[32..32+NETWORK.len()].copy_from_slice(NETWORK);
    Ok((out,journal))
}
