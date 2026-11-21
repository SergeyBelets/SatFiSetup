# Fork choice and canonical history component

Experimental application policy for specification 2.16 §§5.6–5.11 and canonical
history membership under §6. This is new critical code, not an audited Bitcoin
core, an activated ZK guest, or a production state/adapter.

## Selection and batch semantics

The state retains the exact raw header, verified full branch context, cumulative
work and history root for every admitted block. New headers use their parent's
context through a trusted binding to the pinned existing Bitcoin verifier. The
native tests bind directly to the five-patch Citrea mainnet transition. This
module implements no PoW, target, retarget or MTP arithmetic. A permissive callback
does not authenticate headers. `Context::commitment` must bind every field needed
to resume verification; the native binding includes all `LatestDaState` fields.

Only strictly greater uint256 big-endian chainwork changes the head. Ties retain
the current head, a shorter heavier branch may win, and losing branches remain
available for continuation. LCA/ancestor lookup ports the repository relay's
checkpoint-relative Core skip algorithm; exact source pins are in `source-map.json`.
The new profile fixes `MAX_REORG_DEPTH=100` without changing legacy deployments.

Ordered batches contain 1–64 inputs. Exact known headers are no-op retries.
Malformed or invalid unknown inputs roll back the whole batch, including new
entries, history allocations, selected head and commitments. A verified heavier
candidate with rollback depth >100 produces a **successful permanent halt**.
The safe-prefix head is preserved, the triggering candidate is retained, and the
suffix is not parsed. The output contains the processed count and zero-based halt
position. Later calls fail; no reset API exists. Confirmations and membership are
disabled after halt. Diagnostic head and canonical root remain available.

`maximum_timestamp` covers all newly verified headers, including losing branches
and a halt candidate, but excludes duplicates and the ignored suffix. A batch of
known headers has no new timestamp and changes no state commitment. The canonical
head timestamp is recorded separately. A future guest/state must bind the exact
batch outcome and combine this maximum with EVM acceptance-time validation;
neither that proof binding nor full Fresh policy is supplied here.

## History and witnesses

Each branch has a persistent binary tree of depth **64**, keyed by absolute uint64
height. Adding a header inherits its parent's root and allocates 65 nodes. Head
selection uses the winner's existing root; a shorter winner has no old canonical
tail. Known-history retention is separate from canonical membership. No global
history rewrite or deletion occurs on reorg. Memory retention/proving costs are
not yet qualified; there is no pruning or succinct incremental resume witness.

Hashing uses pinned rust-bitcoin SHA-256, with no new cryptographic primitive.
For domain `d` and byte strings `p_i`, define:

```
H(d, p...) = SHA256("SATOSHIFI_FORK_V1\0" || u32be(len(d)) || d
                    || concat(u64be(len(p_i)) || p_i))
scope = H("mainnet-anchor", "bitcoin-mainnet", checkpointHash, u64be(checkpointHeight))
leaf  = H("canonical-header", scope, u64be(height), rawSHA256d(header),
          header.merkleRoot[wire order], u32be(header.timestamp))
empty[0] = H("history-empty")
node[level] = H("history-node", u8(level), left, right)  // levels 1..64
empty[level] = node[level](empty[level-1], empty[level-1])
canonicalRoot = H("canonical-root", scope, headHash, u64be(headHeight), treeRoot)
```

Wire headers are exactly 80 bytes and SHA256d hashes remain in raw digest order.
Membership contains the height, complete header and exactly 64 sibling hashes,
leaf to root. Height bit 0 selects the first pair's orientation. Verifiers reject
out-of-range heights, changed header fields/roots and incorrect witness length.
`BitcoinCanonicalHistory.sol` implements the same verification for a future EVM
adapter. Its `View` must come from authenticated accepted state. It does not
authenticate arbitrary roots, enforce Fresh, or prove transaction inclusion.

The state commitment binds mainnet/anchor, ordered known-entry commitment,
selected canonical root/head/height/work/timestamp and halt evidence. Each known
entry binds parent, height, work, context commitment, skip pointer and branch root.
Side-branch changes therefore affect state even without changing the head.
There is no current on-chain cache: old witnesses require bounded reproof against
the current root; a future cache must implement its own authenticated validity
rules without mass deletion.

## Recovery and checkpoint boundary

`snapshot_headers` exports the complete admitted insertion log. `restore` starts
from a fresh validated immutable anchor, reruns the actual verifier, rebuilds all
branch contexts/roots and requires an externally authenticated expected state
commitment. It rejects missing, reordered, duplicated or altered records and any
unprocessed suffix after halt. It does not trust serialized work, contexts or
skip links. This is full replay, not a bounded succinct proof or an availability
service. Unknown or missing data never becomes successful recovery.

`from_validated_anchor` checks shape/epoch height/nonzero work only. The caller
must validate checkpoint PoW/target, ten linked predecessor headers, checkpoint-
inclusive work and exact MTP/difficulty context before using it. A production
checkpoint factory remains open. Native tests derive epoch checkpoints 40320 and
872928 through real historical transitions; fixture work is relative to the
upstream synthetic zero-work state, not absolute canonical Bitcoin chainwork.

## Qualification

- 18 Rust tests in each of debug/release on Rust/Cargo 1.88.0, including a losing
  branch with 8,192 blocks, independent parent-walk LCA, rollback and replay.
- 505 synthetic graph transitions compared with the actual Solidity relay's
  `_selectHead`/ancestor methods. Synthetic graphs do not qualify PoW branches.
- Nine shared Rust membership cases checked by Solidity and independent Python
  hashing; ten Solidity tests also exercise actual public `addBlockHeaders`
  overlap, atomic failure and halt-before-malformed-suffix with real input headers.
- 19 actual native verifier tests: 15 retained verifier/time tests plus four new
  integrations. 4,074 historical headers prepare/check the anchors and paths;
  4,052 descendants pass through the fork/history component and replay recovery.
- All 14 external dependency records match the original upstream Cargo.lock.

Read `report.json` and its source/log hashes. Original vendor and previous
qualification evidence remain unchanged. Native tests use a dedicated archive
checkout and restore/check all 887 files. Solidity uses a temporary project and
unchanged compiler configuration. Synthetic graph setup resets test gas; those
runs are not production transaction/proving cost measurements.

```sh
python3.11 rust/zkspv/fork-policy/qualify.py \
  --cargo /path/to/rust-1.88.0/bin/cargo --rustc /path/to/rust-1.88.0/bin/rustc \
  --rustdoc /path/to/rust-1.88.0/bin/rustdoc --cargo-home /path/to/prepared-cache \
  --target-dir /path/to/fork-target --forge /path/to/forge \
  --source /path/to/unchanged-citrea-tree --archive /path/to/pinned-citrea.tar.gz \
  --native-target-dir /path/to/native-target
```

The vector utility can regenerate the checked-in fixture with
`generate_vectors.py --binary /path/to/fork-target/debug/policy_vectors`.
The qualifier uses `--check` and rejects stale vectors. Preparation is separate;
qualification is offline, without RPC, deployment or real proof generation.
Production guest/state/adapter, authenticated public outputs, efficient proving
witnesses, availability and independent review remain open. Production readiness
stays false. New code is MIT; the Core skip attribution is retained, and the
combined temporary native build retains upstream licenses including GPL parts.
