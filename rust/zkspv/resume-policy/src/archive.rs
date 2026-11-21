// SPDX-License-Identifier: MIT
//! Local witness construction/data availability. This full archive is NEVER
//! passed to the bounded verifier. It is reconstructible from admitted headers.
use super::{bindings, history, index, resume::{self, Entry, Opening, Witness, LcaWitness, State, Error},
    checkpoint::{InitialContext, ValidatedCheckpoint}, fork::{Hash, Membership}};
use bitcoin::hashes::Hash as _;
use std::collections::BTreeMap;

pub struct Archive {
    state: State,
    entries: BTreeMap<Hash,(Entry,usize)>,
    order: Vec<Hash>,
    history: history::History,
    index: index::Index,
    index_root: usize,
}
impl Archive {
    pub fn new(checkpoint:&ValidatedCheckpoint)->Self {
        let (state,entry)=resume::initialize(checkpoint);
        let mut history=history::History::new();
        let header=bindings::header(&entry.raw).unwrap().0;
        let root=history.insert(0,entry.height(),bindings::history_leaf(state.scoped(),entry.height(),&header));
        let mut index=index::Index::new(); let index_root=index.insert(0,entry.hash(),entry.leaf(state.scoped()));
        Self {entries:BTreeMap::from([(entry.hash(),(entry,root))]),state,order:vec![],history,index,index_root}
    }
    pub fn state(&self)->&State { &self.state }
    pub fn entry(&self,hash:Hash)->Option<&Entry> { self.entries.get(&hash).map(|(entry,_)|entry) }
    pub fn snapshot_headers(&self)->Vec<Vec<u8>> { self.order.iter().map(|hash|self.entries[hash].0.raw.to_vec()).collect() }
    pub fn retained_nodes(&self)->usize { self.history.len()+self.index.len() }
    pub fn opening(&self,hash:Hash)->Option<Opening> {
        Some(Opening {entry:self.entry(hash)?.clone(),siblings:self.index.proof(self.index_root,hash)})
    }
    fn at<E>(&self,mut tip:Entry,height:u64)->Result<Entry,Error<E>> {
        if height<self.state.view.anchor_height || height>tip.height() { return Err(Error::HistoryProof); }
        while tip.height()>height {
            let parent=bindings::header(&tip.raw).unwrap().0.prev_blockhash.to_byte_array();
            tip=self.entry(parent).ok_or(Error::IndexProof)?.clone();
        }
        Ok(tip)
    }
    fn proof(&self,root:usize,entry:&Entry)->Membership {
        Membership {height:entry.height(),raw_header:entry.raw,siblings:self.history.proof(root,entry.height())}
    }
    pub fn membership(&self,tip:Hash,height:u64)->Option<Membership> {
        let (entry,root)=self.entries.get(&tip)?;
        let ancestor=self.at::<()>(entry.clone(),height).ok()?;
        Some(self.proof(*root,&ancestor))
    }
    fn common<E>(&self,a:&Entry,b:&Entry)->Result<Entry,Error<E>> {
        let height=a.height().min(b.height());
        let mut a=self.at(a.clone(),height)?; let mut b=self.at(b.clone(),height)?;
        while a.hash()!=b.hash() {
            let height=a.height().checked_sub(1).ok_or(Error::Lca)?;
            a=self.at(a,height)?; b=self.at(b,height)?;
        }
        Ok(a)
    }
    fn next<E>(&mut self,raw:&[u8;80],verify:&mut impl FnMut(&InitialContext,&[u8;80],u64)->Result<InitialContext,E>)
        ->Result<Witness,Error<E>> {
        let h=bindings::header(raw).ok_or(Error::MalformedHeader)?.0; let hash=h.block_hash().to_byte_array();
        let mut inserted_history=None;
        let witness=if let Some(known)=self.opening(hash) { Witness::Known(known) } else {
            let parent=self.opening(h.prev_blockhash.to_byte_array()).ok_or(Error::IndexProof)?;
            let head=self.opening(self.state.view.head).ok_or(Error::IndexProof)?;
            let height=parent.entry.height().checked_add(1).ok_or(Error::Height)?;
            let context=verify(&parent.entry.context,raw,height).map_err(Error::Header)?;
            let parent_root=self.entries[&parent.entry.hash()].1;
            let path=self.history.proof(parent_root,height);
            let root=self.history.insert(parent_root,height,bindings::history_leaf(self.state.scoped(),height,&h));
            inserted_history=Some(root);
            let candidate=Entry {raw:*raw,context,history_root:self.history.hash(root,64)};
            let lca=if candidate.context.total_work>head.entry.context.total_work {
                let common=self.common(&head.entry,&candidate)?; let head_root=self.entries[&head.entry.hash()].1;
                let children=if common.height()<head.entry.height().min(candidate.height()) {
                    let a=self.at(head.entry.clone(),common.height()+1)?;
                    let b=self.at(candidate.clone(),common.height()+1)?;
                    Some((self.proof(head_root,&a),self.proof(root,&b)))
                } else {None};
                Some(LcaWitness {common_on_head:self.proof(head_root,&common),common_on_candidate:self.history.proof(root,common.height()),children})
            } else {None};
            Witness::New {absence:self.index.proof(self.index_root,hash),parent,head,empty_history_path:path,lca}
        };
        let result=resume::step(&self.state,raw,&witness,verify)?;
        if let Some(entry)=result.inserted {
            let root=inserted_history.ok_or(Error::Internal)?;
            if entry.history_root!=self.history.hash(root,64) { return Err(Error::Internal); }
            self.index_root=self.index.insert(self.index_root,hash,entry.leaf(self.state.scoped()));
            if self.index.hash(self.index_root,256)!=result.state.known_root { return Err(Error::Internal); }
            self.entries.insert(hash,(entry,root)); self.order.push(hash);
        }
        self.state=result.state;
        Ok(witness)
    }
    /// Deterministic trusted verifier adapter is called while constructing and
    /// checking witnesses. This host cost is separate from bounded resume cost.
    /// Errors roll back archive entries, roots, logs and allocated nodes.
    pub fn prepare_batch<E>(&mut self,headers:&[Vec<u8>],mut verify:impl FnMut(&InitialContext,&[u8;80],u64)->Result<InitialContext,E>)
        ->Result<Vec<Witness>,Error<E>> {
        if self.state.view.halted { return Err(Error::Halted); }
        if headers.is_empty() || headers.len()>resume::MAX_BATCH
            || headers.iter().try_fold(0usize,|n,h|n.checked_add(h.len())).is_none_or(|n|n>resume::MAX_BATCH_BYTES) {return Err(Error::Limits);}
        let old=self.state.clone(); let index_root=self.index_root;
        let index_len=self.index.len(); let history_len=self.history.len(); let order_len=self.order.len();
        let result=(|| {
            let mut witnesses=vec![];
            for raw in headers {
                let raw=raw.as_slice().try_into().map_err(|_|Error::MalformedHeader)?;
                witnesses.push(self.next(raw,&mut verify)?);
                if self.state.view.halted {break;}
            }
            if self.order.len()==order_len {return Err(Error::NoChange);}
            Ok(witnesses)
        })();
        if result.is_err() {
            for hash in self.order.drain(order_len..) {self.entries.remove(&hash);}
            self.state=old; self.index_root=index_root; self.index.truncate(index_len); self.history.truncate(history_len);
        }
        result
    }
}
