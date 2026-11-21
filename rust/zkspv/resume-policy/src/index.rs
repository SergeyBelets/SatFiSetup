// SPDX-License-Identifier: MIT
// NEW_INTEGRATION: sparse 256-bit index. Hash primitive is the pinned V1 digest.
use super::history::{digest, Hash};
pub(crate) fn empty_leaf() -> Hash { digest(b"resume-index-empty-v2", &[]) }
fn parent(depth: u16, left: Hash, right: Hash) -> Hash {
    digest(b"resume-index-node-v2", &[&depth.to_be_bytes(), &left, &right])
}
fn bit(key: Hash, depth: usize) -> bool { (key[31 - depth / 8] >> (depth % 8)) & 1 != 0 }
pub(crate) fn reconstruct(key: Hash, leaf: Hash, siblings: &[Hash]) -> Option<Hash> {
    if siblings.len() != 256 { return None; }
    let mut node = leaf;
    for (depth, sibling) in siblings.iter().enumerate() {
        node = if bit(key, depth) { parent((depth+1) as u16, *sibling, node) }
            else { parent((depth+1) as u16, node, *sibling) };
    }
    Some(node)
}
#[derive(Clone)]
struct Node { hash: Hash, left: usize, right: usize }
/// Archive-only persistent store. The bounded verifier never constructs this.
pub(crate) struct Index { nodes: Vec<Node>, empty: [Hash; 257] }
impl Index {
    pub fn new() -> Self {
        let mut empty = [[0;32];257]; empty[0]=empty_leaf();
        for d in 1..=256 { empty[d]=parent(d as u16,empty[d-1],empty[d-1]); }
        Self { nodes:vec![Node {hash:[0;32],left:0,right:0}],empty }
    }
    pub fn len(&self)->usize { self.nodes.len() }
    pub fn truncate(&mut self,length:usize) { self.nodes.truncate(length); }
    pub fn hash(&self,root:usize,depth:usize)->Hash { if root==0 {self.empty[depth]} else {self.nodes[root].hash} }
    pub fn insert(&mut self,root:usize,key:Hash,value:Hash)->usize { self.set(root,key,value,256) }
    fn set(&mut self,root:usize,key:Hash,value:Hash,depth:usize)->usize {
        let node=if depth==0 { Node {hash:value,left:0,right:0} } else {
            let (mut left,mut right)=if root==0 {(0,0)} else {(self.nodes[root].left,self.nodes[root].right)};
            if bit(key,depth-1) {right=self.set(right,key,value,depth-1);} else {left=self.set(left,key,value,depth-1);}
            Node {hash:parent(depth as u16,self.hash(left,depth-1),self.hash(right,depth-1)),left,right}
        };
        self.nodes.push(node); self.nodes.len()-1
    }
    pub fn proof(&self,mut root:usize,key:Hash)->Vec<Hash> {
        let mut result=Vec::with_capacity(256);
        for depth in (1..=256).rev() {
            let (left,right)=if root==0 {(0,0)} else {(self.nodes[root].left,self.nodes[root].right)};
            let right_bit=bit(key,depth-1);
            result.push(self.hash(if right_bit {left} else {right},depth-1));
            root=if right_bit {right} else {left};
        }
        result.reverse(); result
    }
}
