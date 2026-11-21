// SPDX-License-Identifier: MIT
//! Strict bounded wire format. Counts/flags precede allocation, path lengths are
//! implicit fixed protocol constants, and trailing bytes are forbidden.
use super::{Entry, Opening, Witness, LcaWitness, checkpoint::InitialContext, fork::{Hash, Membership}, resume};
pub const MAX_WITNESS_BYTES: usize = 34 + 64 * 35_555;
pub const MAX_PATH_HASHES_PER_STEP: usize = 1088;
const MAGIC: &[u8] = b"SATOSHIFI_RESUME_WITNESS_V2";
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub enum CodecError { Limit, Format, Truncated, Trailing }

fn entry(out:&mut Vec<u8>,e:&Entry) {
    out.extend(e.raw);let c=&e.context;
    out.extend(c.block_hash);out.extend(c.block_height.to_be_bytes());out.extend(c.total_work);
    out.extend(c.current_target_bits.to_be_bytes());out.extend(c.epoch_start_time.to_be_bytes());
    for time in c.prev_11_timestamps {out.extend(time.to_be_bytes());}out.extend(e.history_root);
}
fn path(out:&mut Vec<u8>,hashes:&[Hash]) {for h in hashes {out.extend(h);}}
fn opening(out:&mut Vec<u8>,p:&Opening) {entry(out,&p.entry);path(out,&p.siblings);}
fn member(out:&mut Vec<u8>,p:&Membership) {out.extend(p.height.to_be_bytes());out.extend(p.raw_header);path(out,&p.siblings);}
pub fn encode_witnesses(witnesses:&[Witness])->Result<Vec<u8>,CodecError> {
    if witnesses.is_empty() || witnesses.len()>64 || !witnesses.iter().all(resume::bounded) {return Err(CodecError::Limit);}
    let mut out=vec![0;32];out[..MAGIC.len()].copy_from_slice(MAGIC);out.extend((witnesses.len() as u16).to_be_bytes());
    for w in witnesses {
        match w {
            Witness::Known(p)=>{out.push(0);opening(&mut out,p);}
            Witness::New {absence,parent,head,empty_history_path,lca}=>{
                out.push(1);path(&mut out,absence);opening(&mut out,parent);opening(&mut out,head);path(&mut out,empty_history_path);
                out.push(u8::from(lca.is_some()));
                if let Some(p)=lca {
                    member(&mut out,&p.common_on_head);path(&mut out,&p.common_on_candidate);out.push(u8::from(p.children.is_some()));
                    if let Some((a,b))=&p.children {member(&mut out,a);member(&mut out,b);}
                }
            }
        }
    }
    if out.len()>MAX_WITNESS_BYTES {return Err(CodecError::Limit);}Ok(out)
}
struct Reader<'a> {bytes:&'a [u8],at:usize}
impl Reader<'_> {
    fn take<const N:usize>(&mut self)->Result<[u8;N],CodecError> {
        let end=self.at.checked_add(N).ok_or(CodecError::Truncated)?;
        let value=self.bytes.get(self.at..end).ok_or(CodecError::Truncated)?.try_into().unwrap();self.at=end;Ok(value)
    }
    fn flag(&mut self)->Result<bool,CodecError> {
        match self.take::<1>()?[0] {0=>Ok(false),1=>Ok(true),_=>Err(CodecError::Format)}
    }
    fn path(&mut self,count:usize)->Result<Vec<Hash>,CodecError> {
        let end=self.at.checked_add(count*32).ok_or(CodecError::Truncated)?;
        let bytes=self.bytes.get(self.at..end).ok_or(CodecError::Truncated)?;self.at=end;
        Ok(bytes.chunks_exact(32).map(|h|h.try_into().unwrap()).collect())
    }
    fn entry(&mut self)->Result<Entry,CodecError> {
        let raw=self.take()?;let block_hash=self.take()?;let block_height=u64::from_be_bytes(self.take()?);let total_work=self.take()?;
        let current_target_bits=u32::from_be_bytes(self.take()?);let epoch_start_time=u32::from_be_bytes(self.take()?);
        let mut prev_11_timestamps=[0;11];for time in &mut prev_11_timestamps {*time=u32::from_be_bytes(self.take()?);}
        let history_root=self.take()?;
        Ok(Entry {raw,context:InitialContext {block_hash,block_height,total_work,current_target_bits,epoch_start_time,prev_11_timestamps},history_root})
    }
    fn opening(&mut self)->Result<Opening,CodecError> {Ok(Opening {entry:self.entry()?,siblings:self.path(256)?})}
    fn member(&mut self)->Result<Membership,CodecError> {
        Ok(Membership {height:u64::from_be_bytes(self.take()?),raw_header:self.take()?,siblings:self.path(64)?})
    }
}
pub fn decode_witnesses(bytes:&[u8])->Result<Vec<Witness>,CodecError> {
    if bytes.len()>MAX_WITNESS_BYTES {return Err(CodecError::Limit);}
    let mut reader=Reader {bytes,at:0};let domain=reader.take::<32>()?;
    let mut expected=[0;32];expected[..MAGIC.len()].copy_from_slice(MAGIC);
    if domain!=expected {return Err(CodecError::Format);}
    let count=u16::from_be_bytes(reader.take()?) as usize;
    if count==0 || count>64 {return Err(CodecError::Limit);}
    let mut result=Vec::with_capacity(count);
    for _ in 0..count {
        result.push(if !reader.flag()? {Witness::Known(reader.opening()?)} else {
            let absence=reader.path(256)?;let parent=reader.opening()?;let head=reader.opening()?;let empty_history_path=reader.path(64)?;
            let lca=if reader.flag()? {
                let common_on_head=reader.member()?;let common_on_candidate=reader.path(64)?;
                let children=if reader.flag()? {Some((reader.member()?,reader.member()?))} else {None};
                Some(LcaWitness {common_on_head,common_on_candidate,children})
            } else {None};
            Witness::New {absence,parent,head,empty_history_path,lca}
        });
    }
    if reader.at!=bytes.len() {return Err(CodecError::Trailing);}Ok(result)
}
