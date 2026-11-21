# Signing and nonce persistence

[FROST → ZK → settlement](FROST_ZK_LIFECYCLE.md) joins this nonce protocol to
the DKG equations, target-bound EVM approval, separate Bitcoin input signing,
local proof generation and consumer checks. It also documents concurrent proof
publication and the difference between local provenance and public verification.

EVM retains `SATOHSI_FROST_NONCE_TRANSCRIPT_V1` and
`SATOHSI_FROST_BINDING_FACTOR_V1`. It does not substitute library-standard binding factors.
The Rust `evm_share_v1` adapter computes secret scalar arithmetic with k256; Python computes
public transcript/rho/challenge/Lagrange values. The common `tests/vectors/signing-v1.json` fixtures freeze the transcript, public
commitments, binding factors, original-index Lagrange coefficients, challenge,
partial scalars and final signature for both nonce parities. Python recomputes the
public values; Rust checks its scalar adapter and k256 BIP340 verification;
`SigningGolden.t.sol` submits the exact commitments/scalars to the actual
coordinator and canonical verifier. Coincurve supplies another independent final
BIP340 check. These artificial fixtures complement the real subprocess DKG tests.

`createSessionFromDKG` loads threshold, full roster and public shares automatically.
Commitments freeze at threshold, and original DKG indexes are retained. `DKGAuthorization`
rejects a legacy session or another DKG result even when its group key matches. Final
aggregation is checked independently with coincurve BIP340 before EVM submission.

`frost-signer dkg sign-evm --board ADDRESS --role conf_ceremony --session ID`
reads the typed intent and coordinator from the canonical DKG board. Each signer
uses its own RPC, identity and encrypted state. The lower-level compatibility path
accepts a manifest, `--coordinator`, `--session`, and `--package intent.json`.
Both paths require explicit local approval.
The intent contains chain_id (string), target, operation_hash, an allowlisted artifact name,
hash_method and hash_args. The participant calls the target hash helper itself and checks
its result against the signing session. This is not an automated spending-limit policy;
automated spending-limit policies remain an operator concern. The operations API
and resumable factory workflow prepare intents but do not approve them for participants.
The legacy CLI's approve-all option is not used by this profile.

Before nonce generation, scalar-share computation and every cached-share retry,
the same signing guards recheck the coordinator's canonical `FROSTVerifier`,
its exact constructor binding, the typed treasury policy and the public intent.
Verifier checks run at the confirmed read snapshot and at the current block;
session-specific verifier overrides are rejected. ABI compatibility alone is
insufficient, including for the lower-level signer: its coordinator and verifier
must match the locally reviewed artifacts and creation provenance.

ULR and gas treasury intents keep their reviewed context throughout these
checks. A runtime mismatch for that context is fatal and cannot be classified
as an unrelated target and skipped. Treasury addresses remain bound to the
participant's ULR pin or the enrolled gas registry/epoch/roster. ULR bootstrap
confederation fee-router/board review is mandatory from the signed enrollment;
removing the `ulr_treasury` label cannot route an intent around those checks.
ULR bootstrap fee-pipeline approval also reviews its enrolled ZK consumer path, including the
Groth16 verifier's exact constructor-child address. A different contract with
the same verifier code is not a substitute for that child.

Managed NFT create/mint requires an independently enrolled `--nft-config` pin
and a public-board intent. The signer binds the factory record, manager,
EVM_AUTH epoch, authorization and validator, then checks the exact collection,
recipient, expiry, volume and discount. Confirmed/latest review repeats before
every nonce/share/cached delivery boundary. Removing the typed context or
switching to a raw hash cannot bypass it. See [NFT commands and quota boundaries](NFT_CONFIGURATION.md).

These checks preserve nonce/spent journals. A rejected retry does not generate
replacement nonces or authorize a new operation. Regression coverage includes
`test_signing_target_substitution.py`, real local-EVM verifier substitutions,
and rejection of a foreign gas treasury before FROST commitments are submitted.
Initial Bitcoin address-registry binding also authenticates the derivation
verifier's runtime, canonical constructor and declared runtime hash at the
confirmed and current blocks before private work or cached-share export.
This identity check does not grant derivation qualification.
For on-chain Bitcoin reserve/bind, the attached strict public plan is mandatory
and must reproduce the exact operation arguments, target and hash. Showing an
unrelated plan or omitting it cannot bypass this check.

Nonce sequence:

1. Persist encrypted nonce pair before exposing commitments.
2. Compute/verify z locally while holding an exclusive OS file lock.
3. Persist a spent marker in a FULL-synchronous SQLite journal.
4. Persist exact transcript, z and spent state atomically; erase stored nonce fields.
5. Only then transmit z. Retry checks the independent journal identity and exact
   spent transcript before transmitting the saved z again.

A crash between steps 3 and 4 blocks the session. A reverted/dropped transaction never
makes the nonce reusable. Restoring an old encrypted state with the current spent journal
is detected. Restoring **both** state and journal from an old snapshot is not solved:
production recovery needs an independent monotonic journal or rotation. Do not restore
signing-state backups and restart without that procedure. No production recovery readiness
is claimed. OS locks prevent concurrent use of the same state path, not cloned files on
independent hosts.

BTC uses ZF standard signing with a separate BTC_SPEND KeyPackage, BIP341 SIGHASH_DEFAULT,
no annex and no second tweak. A persisted batch/transcript binds each input's nonce.
Before any BTC commitment, the participant must explicitly [inspect and pin the
canonical registry graph](BITCOIN_SIGNER_TRUST.md). Payment plans cannot select a
different approval authority. V2 plans bind chain, Bitcoin board, wallet result,
batch and a fixed threshold subset. The public plan, commitments and signature
shares travel through `BitcoinSigningBoard`; private DKG packets retain HPKE.
Completely cached Bitcoin packets can be delivered again after the deadline or
spending the inputs, with unchanged transcript/journal checks and no new nonce or
signature calculation. Regtest permits that exact cached delivery without Core.
Mainnet retains the own-Core and pinned relay health gates even for cached delivery.
Incomplete caches retain all first-sign checks.

The network is committed by the accepted invitation and pinned graph: regtest V3
and mainnet V4 are distinct deployments. Mainnet requires a fully validated,
unpruned Core with synced txindex, exact genesis/checkpoint/chainwork agreement,
a fresh canonical relay at the selected EVM confirmation depth and the correct
`MainnetSPVGuard`. A stale or halted relay does not authorize a new signature.
`dkg btc-network-status` reads readiness and `dkg btc-relay` performs bounded,
journaled synchronization without creating private nonce material. The
[mainnet runbook](BITCOIN_MAINNET_RUNBOOK.md) gives the full commands. See the [payment runbook](BITCOIN_ONCHAIN_RUNBOOK.md),
[Bitcoin withdrawals](BITCOIN_WITHDRAWALS.md) and
[separate security review](BITCOIN_ONCHAIN_SECURITY_REVIEW.md).
