//! TEST ONLY. Fixed regtest consensus, bounded linear history, one checkpoint.
//! Consensus is the pinned corrected Citrea regtest verifier, not a PoW model.
use bitcoin::{block::Header, consensus::deserialize, hashes::{sha256, Hash as _}, Target, Work};
use bitcoin_da::{spec::{header::HeaderWrapper, RollupParams}, verifier::BitcoinVerifier};
use sov_rollup_interface::{da::{DaVerifier, LatestDaState}, Network};
pub use satoshifi_mainnet_guest_kernel::read_request;
use satoshifi_mainnet_guest_kernel::transition::bounded::{checkpoint::InitialContext, fork::{ForkState, Membership}};

pub const DOMAIN: &[u8] = b"SATOSHIFI_REGTEST_DEPOSIT_V1";
pub const NETWORK: &[u8] = b"bitcoin-regtest";
pub const PREFIX: usize = 32 + 32 + 20;
pub const CHECKPOINT_BYTES: usize = 8 + 32 + 11 * 80;
pub const HEADER_OFFSET: usize = PREFIX + CHECKPOINT_BYTES + 1;
pub const JOURNAL_BYTES: usize = 16 * 32;

fn header(raw: &[u8]) -> Result<Header, &'static str> { deserialize(raw).map_err(|_| "header encoding") }
fn word(n: u64) -> [u8;32] { let mut w=[0;32]; w[24..].copy_from_slice(&n.to_be_bytes()); w }

/// Regtest upstream deliberately returns zero chainwork. Restore cumulative work
/// using rust-bitcoin's Work arithmetic after all upstream consensus checks pass.
fn verify(c: &InitialContext, raw: &[u8;80], height: u64) -> Result<InitialContext, &'static str> {
    let h=header(raw)?;
    let previous=LatestDaState { block_hash:c.block_hash,block_height:c.block_height,total_work:c.total_work,
        current_target_bits:c.current_target_bits,epoch_start_time:c.epoch_start_time,prev_11_timestamps:c.prev_11_timestamps };
    let verifier=BitcoinVerifier::new(RollupParams {reveal_tx_prefix:vec![],network:Network::Nightly});
    let next=verifier.verify_header_chain(Some(&previous),&HeaderWrapper::new(h,0,height,[0;32]),Network::Nightly)
        .map_err(|_| "regtest consensus")?;
    // Input work is constrained below to u64 and at most 64 regtest blocks are
    // appended. Each has work 2, so this addition cannot overflow uint256.
    let work=(Work::from_be_bytes(c.total_work)+h.work()).to_be_bytes();
    Ok(InitialContext {block_hash:next.block_hash,block_height:next.block_height,total_work:work,
        current_target_bits:next.current_target_bits,epoch_start_time:next.epoch_start_time,prev_11_timestamps:next.prev_11_timestamps})
}

pub fn execute(bytes: &[u8]) -> Result<([u8;JOURNAL_BYTES],Vec<Membership>), &'static str> {
    if bytes.len()<HEADER_OFFSET || bytes[..32]!=padded(DOMAIN) {return Err("regtest input domain");}
    let chain: [u8;32]=bytes[32..64].try_into().unwrap();
    let address=&bytes[64..84];
    if chain==[0;32] || address==[0;20] {return Err("deployment binding");}
    let height=u64::from_be_bytes(bytes[PREFIX..PREFIX+8].try_into().unwrap());
    let work: [u8;32]=bytes[PREFIX+8..PREFIX+40].try_into().unwrap();
    if height<10 || height%2016!=0 || height>u32::MAX as u64 || work[..24]!=[0;24] || work==[0;32] {return Err("checkpoint bounds");}
    let prior=&bytes[PREFIX+40..PREFIX+CHECKPOINT_BYTES];
    let mut timestamps=[0;11]; let mut previous=None;
    for (i,raw) in prior.chunks_exact(80).enumerate() {
        let h=header(raw)?;
        if h.bits.to_consensus()!=0x207fffff || h.validate_pow(Target::MAX_ATTAINABLE_REGTEST).is_err() {return Err("checkpoint pow");}
        if previous.is_some_and(|hash| h.prev_blockhash!=hash) {return Err("checkpoint link");}
        previous=Some(h.block_hash());timestamps[((height-10+i as u64)%11) as usize]=h.time;
    }
    let raw=&prior[800..880];let anchor=header(raw)?;
    if work<anchor.work().to_be_bytes() {return Err("checkpoint work");}
    let context=InitialContext {block_hash:anchor.block_hash().to_byte_array(),block_height:height,total_work:work,
        current_target_bits:0x207fffff,epoch_start_time:0,prev_11_timestamps:timestamps};
    let mut state=ForkState::from_validated_anchor(raw,height,context).map_err(|_| "checkpoint")?;
    let before=state.view();
    let count=bytes[HEADER_OFFSET-1] as usize;
    if !(1..=64).contains(&count) || bytes.len()!=HEADER_OFFSET+count*80 {return Err("header count");}
    let headers: Vec<Vec<u8>>=bytes[HEADER_OFFSET..].chunks_exact(80).map(<[u8]>::to_vec).collect();
    // This guest proves one linear extension. It does not claim fork/halt coverage.
    let mut parent=anchor.block_hash();
    for raw in &headers {let h=header(raw)?;if h.prev_blockhash!=parent {return Err("linear parent");}parent=h.block_hash();}
    let result=state.apply_batch(&headers,verify).map_err(|_| "regtest transition")?;
    let after=state.view();
    if after.halted || result.new_headers!=count || after.height!=height+count as u64 {return Err("linear result");}
    let mut journal=[0;JOURNAL_BYTES];
    let mut address_word=[0;32];address_word[12..].copy_from_slice(address);
    let words=[padded(DOMAIN),padded(NETWORK),chain,address_word,
        sha256::Hash::hash(&bytes[PREFIX..PREFIX+CHECKPOINT_BYTES]).to_byte_array(),
        after.anchor,word(height),after.head,word(after.height),after.work,word(after.timestamp as u64),
        after.canonical_root,after.state_commitment,word(result.maximum_timestamp.unwrap() as u64),word(count as u64),before.state_commitment];
    for (i,w) in words.iter().enumerate(){journal[i*32..(i+1)*32].copy_from_slice(w);}
    let mut paths=vec![];
    for raw in &headers {paths.push(state.membership(header(raw)?.block_hash().to_byte_array()).map_err(|_| "membership")?);}
    Ok((journal,paths))
}
fn padded(bytes:&[u8])->[u8;32]{let mut out=[0;32];out[..bytes.len()].copy_from_slice(bytes);out}

#[cfg(test)]
mod tests {
    use super::*;
    fn input()->Vec<u8>{let file=std::env::var_os("SATOSHIFI_REGTEST_INPUT").expect("real Core input required");read_request(std::fs::File::open(file).unwrap()).unwrap()}
    #[test] fn real_core_headers_and_paths(){let b=input();let (j,p)=execute(&b).unwrap();assert_eq!(p.len(),6);assert_eq!(&j[..32],&padded(DOMAIN));assert!(p.iter().all(|m|m.siblings.len()==64));}
    #[test] fn tampered_pow_bits_parent_and_mtp_rejected(){
        let b=input();
        for offset in [4,68,72] {let mut bad=b.clone();bad[HEADER_OFFSET+offset..HEADER_OFFSET+offset+4].fill(0);assert!(execute(&bad).is_err());}
        let mut bad=b.clone();let mut h=header(&bad[HEADER_OFFSET..HEADER_OFFSET+80]).unwrap();
        while h.validate_pow(Target::MAX_ATTAINABLE_REGTEST).is_ok(){h.nonce=h.nonce.wrapping_add(1);}
        bad[HEADER_OFFSET+76..HEADER_OFFSET+80].copy_from_slice(&h.nonce.to_le_bytes());assert!(execute(&bad).is_err());
    }
    #[test] fn deployment_checkpoint_and_format_bound(){
        let b=input();let (j,_)=execute(&b).unwrap();let mut other=b.clone();other[64]^=1;
        let (changed,_)=execute(&other).unwrap();assert_ne!(j,changed);
        for offset in [0,HEADER_OFFSET-1] {let mut bad=b.clone();bad[offset]=0;assert!(execute(&bad).is_err());}
        let mut bad=b.clone();bad[PREFIX+40+4]^=1;assert!(execute(&bad).is_err());
        let mut bad=b.clone();bad.push(0);assert!(execute(&bad).is_err());assert!(execute(&b[..b.len()-1]).is_err());
    }
}
