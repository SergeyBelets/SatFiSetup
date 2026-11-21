// SPDX-License-Identifier: MIT
use super::{bounded::{self, State, Witness, fork::View}, Deployment, Execution};

pub const INPUT_DOMAIN: &[u8] = b"SATOSHIFI_TRANSITION_INPUT_V2";
pub const MAX_INPUT_BYTES: usize = 402 + 128 + 5120 + 4 + bounded::MAX_WITNESS_BYTES;

/// No clock, network selector, verifier selector, dev flag, or executable code.
/// Only an unhalted state can be resumed. Its descriptor is shared with deployment.
#[derive(Clone, Debug)]
pub struct Request {
    pub deployment: Deployment,
    pub expected: [u8;32],
    pub before: State,
    pub headers: Vec<Vec<u8>>,
    pub witnesses: Vec<Witness>,
}
#[derive(Debug, PartialEq, Eq)]
pub enum InputError { Limit, Format, Truncated, Trailing, Halted, Checkpoint, Witness(bounded::CodecError) }
#[derive(Debug)]
pub enum RunError<E> { Input(InputError), Transition(super::Error<E>) }

struct Reader<'a> { bytes: &'a [u8], at: usize }
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8],InputError> {
        let end=self.at.checked_add(n).ok_or(InputError::Limit)?;
        let result=self.bytes.get(self.at..end).ok_or(InputError::Truncated)?;
        self.at=end; Ok(result)
    }
    fn array<const N:usize>(&mut self)->Result<[u8;N],InputError> { Ok(self.take(N)?.try_into().unwrap()) }
    fn u64(&mut self)->Result<u64,InputError> { Ok(u64::from_be_bytes(self.array()?)) }
}
impl Request {
    pub fn encode(&self)->Result<Vec<u8>,InputError> {
        if self.before.view.halted || self.before.halt.is_some() { return Err(InputError::Halted); }
        if self.before.checkpoint_descriptor!=self.deployment.checkpoint_descriptor { return Err(InputError::Checkpoint); }
        if self.headers.is_empty() || self.headers.len()>64 || self.witnesses.len()>self.headers.len()
            || self.headers.iter().try_fold(0usize,|n,h|n.checked_add(h.len())).is_none_or(|n|n>5120) {
            return Err(InputError::Limit);
        }
        let witness=bounded::encode_witnesses(&self.witnesses).map_err(InputError::Witness)?;
        let mut out=vec![0;32]; out[..INPUT_DOMAIN.len()].copy_from_slice(INPUT_DOMAIN);
        out.extend(self.deployment.evm_chain_id); out.extend(self.deployment.state_address);
        out.extend(self.deployment.checkpoint_descriptor); out.extend(self.expected);
        let v=&self.before.view;
        out.extend(v.anchor); out.extend(v.anchor_height.to_be_bytes()); out.extend(v.head);
        out.extend(v.height.to_be_bytes()); out.extend(v.work); out.extend(v.timestamp.to_be_bytes());
        out.extend(v.canonical_root); out.extend(v.state_commitment);
        out.extend(self.before.known_root); out.extend(self.before.known_log); out.extend(self.before.known_count.to_be_bytes());
        out.extend((self.headers.len() as u16).to_be_bytes());
        for h in &self.headers { out.extend((h.len() as u16).to_be_bytes()); out.extend(h); }
        out.extend((witness.len() as u32).to_be_bytes()); out.extend(witness);
        debug_assert!(out.len()<=MAX_INPUT_BYTES); Ok(out)
    }

    /// The transport must also enforce MAX_INPUT_BYTES before allocating the
    /// enclosing input buffer. Decoding here checks all nested lengths before
    /// copying/allocating nested vectors and accepts no trailing bytes.
    pub fn decode(bytes:&[u8])->Result<Self,InputError> {
        if bytes.len()>MAX_INPUT_BYTES { return Err(InputError::Limit); }
        let mut r=Reader {bytes,at:0}; let mut domain=[0;32]; domain[..INPUT_DOMAIN.len()].copy_from_slice(INPUT_DOMAIN);
        if r.array::<32>()?!=domain { return Err(InputError::Format); }
        let deployment=Deployment {evm_chain_id:r.array()?,state_address:r.array()?,checkpoint_descriptor:r.array()?};
        let expected=r.array()?;
        let view=View {anchor:r.array()?,anchor_height:r.u64()?,head:r.array()?,height:r.u64()?,work:r.array()?,
            timestamp:u32::from_be_bytes(r.array()?),canonical_root:r.array()?,state_commitment:r.array()?,halted:false};
        let before=State {view,checkpoint_descriptor:deployment.checkpoint_descriptor,known_root:r.array()?,known_log:r.array()?,known_count:r.u64()?,halt:None};
        let count=usize::from(u16::from_be_bytes(r.array()?));
        if count==0 || count>64 { return Err(InputError::Limit); }
        let mut headers=Vec::with_capacity(count); let mut total=0usize;
        for _ in 0..count {
            let len=usize::from(u16::from_be_bytes(r.array()?)); total+=len;
            if total>5120 { return Err(InputError::Limit); }
            headers.push(r.take(len)?.to_vec());
        }
        let length=u32::from_be_bytes(r.array()?) as usize;
        if length>bounded::MAX_WITNESS_BYTES { return Err(InputError::Limit); }
        let wire=r.take(length)?;
        if r.at!=bytes.len() { return Err(InputError::Trailing); }
        let witnesses=bounded::decode_witnesses(wire).map_err(InputError::Witness)?;
        if witnesses.len()>count { return Err(InputError::Limit); }
        Ok(Self {deployment,expected,before,headers,witnesses})
    }
}

/// Runtime-neutral entry boundary. It does not read RISC Zero env or create a
/// receipt. The real guest must call it with a statically linked native adapter,
/// then commit_slice the exact journal.encode_abi() bytes (not serde framing).
pub fn execute_encoded<E>(bytes:&[u8],
    verify:impl FnMut(&bounded::checkpoint::InitialContext,&[u8;80],u64)->Result<bounded::checkpoint::InitialContext,E>,
)->Result<Execution,RunError<E>> {
    let r=Request::decode(bytes).map_err(RunError::Input)?;
    super::execute(r.deployment,r.expected,&r.before,&r.headers,&r.witnesses,verify).map_err(RunError::Transition)
}
