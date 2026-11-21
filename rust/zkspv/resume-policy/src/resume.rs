// SPDX-License-Identifier: MIT
use super::{bindings, history, index, checkpoint::{InitialContext, ValidatedCheckpoint}, fork::{self, Context, Hash, View}, time};
use bitcoin::hashes::Hash as _;

pub const MAX_BATCH: usize = 64;
pub const MAX_BATCH_BYTES: usize = MAX_BATCH * 80;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub raw: [u8;80],
    pub context: InitialContext,
    pub history_root: Hash,
}
impl Entry {
    pub fn hash(&self)->Hash { self.context.block_hash }
    pub fn height(&self)->u64 { self.context.block_height }
    pub fn timestamp(&self)->u32 { u32::from_le_bytes(self.raw[68..72].try_into().unwrap()) }
    pub(crate) fn leaf(&self,scope:Hash)->Hash {
        history::digest(b"known-header-v2", &[&scope,&self.raw,&self.height().to_be_bytes(),
            &self.context.total_work,&self.context.commitment(),&self.history_root])
    }
}

/// The commitment must originate from the accepted V2 state/proof. V1 roots are
/// intentionally incompatible. Public fields are untrusted until authenticated.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    pub view: View,
    pub checkpoint_descriptor: Hash,
    pub known_root: Hash,
    /// Ordered admission commitment; the indexed map alone does not bind order.
    pub known_log: Hash,
    pub known_count: u64,
    pub halt: Option<fork::Halt>,
}
impl State {
    pub fn commitment(&self)->Hash {
        let v=&self.view;
        let halt=match &self.halt {
            None=>history::digest(b"running",&[]),
            Some(h)=>history::digest(b"halt",&[&h.old_head,&h.candidate,&h.common_ancestor,&h.depth.to_be_bytes()]),
        };
        history::digest(b"resume-state-v2", &[&self.checkpoint_descriptor,&v.anchor,&v.anchor_height.to_be_bytes(),
            &self.known_root,&self.known_log,&self.known_count.to_be_bytes(),&v.canonical_root,&v.head,&v.height.to_be_bytes(),
            &v.work,&v.timestamp.to_be_bytes(),&halt])
    }
    pub(crate) fn seal(&mut self) { self.view.state_commitment=self.commitment(); }
    pub(crate) fn scoped(&self)->Hash { bindings::scoped(self.view.anchor,self.view.anchor_height) }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Opening { pub entry: Entry, pub siblings: Vec<Hash> }
/// Exact LCA: equal header at the common height, and unequal immediate children
/// in the two authenticated histories (unless one tip is itself the LCA).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LcaWitness {
    pub common_on_head: fork::Membership,
    pub common_on_candidate: Vec<Hash>,
    pub children: Option<(fork::Membership,fork::Membership)>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Witness {
    Known(Opening),
    New {
        absence: Vec<Hash>, parent: Opening, head: Opening,
        empty_history_path: Vec<Hash>, lca: Option<LcaWitness>,
    },
}
#[derive(Debug)]
pub enum Error<E> {
    State, Halted, Limits, WitnessCount, MalformedHeader, IndexProof, Context,
    HistoryProof, Lca, Work, Height, NoChange, Header(E), Internal,
}
pub(crate) struct Step {
    pub state: State, pub inserted: Option<Entry>, pub summary: Option<time::TimeSummary>,
}
#[derive(Clone, Debug)]
pub struct Outcome {
    pub previous_commitment: Hash, pub state: State, pub processed: usize,
    pub summary: time::TimeSummary, pub halt_at: Option<usize>, pub batch_commitment: Hash,
}

/// Initial V2 state is constructed solely from the validated checkpoint.
pub fn initialize(checkpoint:&ValidatedCheckpoint)->(State,Entry) {
    let c=checkpoint.context().clone(); let scope=bindings::scoped(c.block_hash,c.block_height);
    let header=bindings::header(checkpoint.raw_header()).unwrap().0;
    let mut history=history::History::new();
    let root=history.insert(0,c.block_height,bindings::history_leaf(scope,c.block_height,&header));
    let entry=Entry {raw:*checkpoint.raw_header(),context:c.clone(),history_root:history.hash(root,64)};
    let mut map=index::Index::new(); let map_root=map.insert(0,c.block_hash,entry.leaf(scope));
    let mut state=State {
        view:View {anchor:c.block_hash,anchor_height:c.block_height,head:c.block_hash,height:c.block_height,
            work:c.total_work,timestamp:header.time,canonical_root:bindings::canonical(scope,c.block_hash,c.block_height,entry.history_root),
            state_commitment:[0;32],halted:false},
        checkpoint_descriptor:checkpoint.descriptor_commitment(),known_root:map.hash(map_root,256),
        known_log:history::digest(b"resume-anchor-v2",&[&checkpoint.descriptor_commitment(),&entry.leaf(scope)]),known_count:1,halt:None,
    };
    state.seal(); (state,entry)
}

fn authenticate<E>(expected:Hash,state:&State)->Result<(),Error<E>> {
    let v=&state.view;
    if v.state_commitment!=expected || state.commitment()!=expected || v.halted!=state.halt.is_some()
        || state.known_count==0 || v.anchor_height<10 || v.anchor_height%2016!=0 || v.height<v.anchor_height {
        return Err(Error::State);
    }
    if v.halted { return Err(Error::Halted); }
    Ok(())
}
fn entry_shape<E>(state:&State,entry:&Entry)->Result<(),Error<E>> {
    let h=bindings::header(&entry.raw).ok_or(Error::MalformedHeader)?.0;
    if h.block_hash().to_byte_array()!=entry.hash() || entry.height()<state.view.anchor_height
        || entry.context.total_work==[0;32] || entry.context.prev_11_timestamps[(entry.height()%11) as usize]!=h.time {
        return Err(Error::Context);
    }
    Ok(())
}
fn open<E>(state:&State,opening:&Opening,key:Hash)->Result<(),Error<E>> {
    if opening.entry.hash()!=key || index::reconstruct(key,opening.entry.leaf(state.scoped()),&opening.siblings)!=Some(state.known_root) {
        return Err(Error::IndexProof);
    }
    entry_shape(state,&opening.entry)
}
fn member<E>(state:&State,tip:&Entry,proof:&fork::Membership)->Result<Hash,Error<E>> {
    if proof.height<state.view.anchor_height || proof.height>tip.height() { return Err(Error::HistoryProof); }
    let h=bindings::header(&proof.raw_header).ok_or(Error::MalformedHeader)?.0;
    if history::reconstruct(proof.height,bindings::history_leaf(state.scoped(),proof.height,&h),&proof.siblings)!=Some(tip.history_root) {
        return Err(Error::HistoryProof);
    }
    Ok(h.block_hash().to_byte_array())
}
fn lca<E>(state:&State,head:&Entry,candidate:&Entry,witness:&LcaWitness)->Result<(Hash,u64),Error<E>> {
    let proof=&witness.common_on_head; let common=member(state,head,proof)?;
    let other=fork::Membership {height:proof.height,raw_header:proof.raw_header,siblings:witness.common_on_candidate.clone()};
    if member(state,candidate,&other)?!=common { return Err(Error::Lca); }
    let shorter=head.height().min(candidate.height());
    if proof.height==shorter {
        if witness.children.is_some() || (head.height()==shorter && common!=head.hash())
            || (candidate.height()==shorter && common!=candidate.hash()) { return Err(Error::Lca); }
    } else {
        let (a,b)=witness.children.as_ref().ok_or(Error::Lca)?;
        if a.height!=proof.height+1 || b.height!=proof.height+1 { return Err(Error::Lca); }
        let ah=member(state,head,a)?; let bh=member(state,candidate,b)?;
        if ah==bh || bindings::header(&a.raw_header).unwrap().0.prev_blockhash.to_byte_array()!=common
            || bindings::header(&b.raw_header).unwrap().0.prev_blockhash.to_byte_array()!=common { return Err(Error::Lca); }
    }
    Ok((common,head.height()-proof.height))
}

/// Verify a single step from authenticated roots, without a map/history archive.
pub(crate) fn step<E>(state:&State,raw:&[u8;80],witness:&Witness,
    verify:&mut impl FnMut(&InitialContext,&[u8;80],u64)->Result<InitialContext,E>)->Result<Step,Error<E>> {
    authenticate(state.view.state_commitment,state)?;
    let header=bindings::header(raw).ok_or(Error::MalformedHeader)?.0;
    let hash=header.block_hash().to_byte_array();
    if let Witness::Known(opening)=witness {
        open(state,opening,hash)?;
        if opening.entry.raw!=*raw { return Err(Error::Context); }
        return Ok(Step {state:state.clone(),inserted:None,summary:None});
    }
    let Witness::New {absence,parent,head,empty_history_path,lca:witness_lca}=witness else {unreachable!()};
    if index::reconstruct(hash,index::empty_leaf(),absence)!=Some(state.known_root) { return Err(Error::IndexProof); }
    open(state,parent,header.prev_blockhash.to_byte_array())?;
    open(state,head,state.view.head)?;
    let head=&head.entry; let parent=&parent.entry;
    if head.height()!=state.view.height || head.context.total_work!=state.view.work || head.timestamp()!=state.view.timestamp
        || bindings::canonical(state.scoped(),head.hash(),head.height(),head.history_root)!=state.view.canonical_root {
        return Err(Error::State);
    }
    let height=parent.height().checked_add(1).ok_or(Error::Height)?;
    if history::reconstruct(height,history::digest(b"history-empty",&[]),empty_history_path)!=Some(parent.history_root) {
        return Err(Error::HistoryProof);
    }
    let (context,summary)=time::verify_batch(parent.context.clone(),[raw],|context,raw| {
        verify(context,raw,height).map(|next|(next,header.time))
    }).map_err(|e|match e {time::BatchError::Header(e)=>Error::Header(e),_=>Error::Internal})?;
    let next=Entry {raw:*raw,context,history_root:history::reconstruct(height,
        bindings::history_leaf(state.scoped(),height,&header),empty_history_path).ok_or(Error::HistoryProof)?};
    entry_shape(state,&next)?;
    if next.height()!=height || next.hash()!=hash { return Err(Error::Context); }
    if next.context.total_work<=parent.context.total_work { return Err(Error::Work); }
    let mut after=state.clone();
    after.known_root=index::reconstruct(hash,next.leaf(state.scoped()),absence).ok_or(Error::IndexProof)?;
    after.known_log=history::digest(b"resume-entry-v2",&[&state.known_log,&hash,&next.leaf(state.scoped())]);
    after.known_count=after.known_count.checked_add(1).ok_or(Error::Limits)?;
    if next.context.total_work>head.context.total_work {
        let (common,depth)=lca(state,head,&next,witness_lca.as_ref().ok_or(Error::Lca)?)?;
        if depth>fork::MAX_REORG_DEPTH {
            after.halt=Some(fork::Halt {old_head:head.hash(),candidate:hash,common_ancestor:common,depth});
            after.view.halted=true;
        } else {
            after.view.head=hash; after.view.height=height; after.view.work=next.context.total_work; after.view.timestamp=header.time;
            after.view.canonical_root=bindings::canonical(state.scoped(),hash,height,next.history_root);
        }
    } else if witness_lca.is_some() { return Err(Error::Lca); }
    after.seal();
    Ok(Step {state:after,inserted:Some(next),summary:Some(summary)})
}

pub(crate) fn bounded(witness:&Witness)->bool {
    match witness {
        Witness::Known(p)=>p.siblings.len()==256,
        Witness::New {absence,parent,head,empty_history_path,lca}=>absence.len()==256 && parent.siblings.len()==256
            && head.siblings.len()==256 && empty_history_path.len()==64 && lca.as_ref().is_none_or(|p|
                p.common_on_head.siblings.len()==64 && p.common_on_candidate.len()==64
                && p.children.as_ref().is_none_or(|(a,b)|a.siblings.len()==64 && b.siblings.len()==64)),
    }
}
/// Bounded verifier input: <=64 items, <=5120 raw bytes, fixed-depth paths.
/// Caller authenticates `expected` from the accepted V2 state, not from the prover.
/// Invalid batches return no prefix. A valid halt succeeds and ignores suffix.
pub fn resume<E>(expected:Hash,before:&State,headers:&[Vec<u8>],witnesses:&[Witness],
    mut verify:impl FnMut(&InitialContext,&[u8;80],u64)->Result<InitialContext,E>)->Result<Outcome,Error<E>> {
    authenticate(expected,before)?;
    if headers.is_empty() || headers.len()>MAX_BATCH || witnesses.len()>headers.len()
        || headers.iter().try_fold(0usize,|n,h|n.checked_add(h.len())).is_none_or(|n|n>MAX_BATCH_BYTES)
        || !witnesses.iter().all(bounded) { return Err(Error::Limits); }
    let mut state=before.clone(); let mut summary:Option<time::TimeSummary>=None;
    let mut processed=0; let mut halt_at=None;
    for (i,raw) in headers.iter().enumerate() {
        let raw:&[u8;80]=raw.as_slice().try_into().map_err(|_|Error::MalformedHeader)?;
        let result=step(&state,raw,witnesses.get(i).ok_or(Error::WitnessCount)?,&mut verify)?;
        if let Some(part)=result.summary { summary=Some(match summary {None=>part,Some(prior)=>prior.then(part).map_err(|_|Error::Limits)?}); }
        state=result.state; processed+=1;
        if state.view.halted { halt_at=Some(i); break; }
    }
    if processed!=witnesses.len() { return Err(Error::WitnessCount); }
    let summary=summary.ok_or(Error::NoChange)?;
    let parts:Vec<&[u8]>=headers.iter().map(Vec::as_slice).collect();
    Ok(Outcome {previous_commitment:expected,state,processed,summary,halt_at,
        batch_commitment:history::digest(b"submitted-batch-v1",&parts)})
}
