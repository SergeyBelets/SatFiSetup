# Migration to the proof-settlement lifecycle

No external deployment or key has been migrated by this change. New immutable
contracts require new addresses; replacing an ABI does not update existing code.
The acceptance suite creates fresh isolated deployments.

1. Inventory old DKG/signing sessions, approvals, MP/S balances, pending
   withdrawals, collateral UTXOs, accrued/pending fee reports and unsettled DAO
   days. Save the old manifests, receipt hashes and participant nonce journals.
2. Stop admitting new obligations to the old deployment through its existing
   authorized controls. Complete its funded DAO days and claims, sync pending
   fee reports and transfer claimable fees using its existing authorization.
   Reconcile tokens, UTXOs and recipients before moving assets.
3. Resolve each old withdrawal under the code actually deployed. Cancel only
   where that version permits cancellation and no Bitcoin signature/payment
   exists. A legacy `processWithdrawal` event is not proof of payment. If that
   version cannot safely settle an obligation, preserve its keys and funds and
   handle that obligation separately; do not mark it migrated or mint replacement
   claims from a spreadsheet.
4. Create fresh V1 EVM_AUTH and BTC_SPEND ceremonies with new IDs. Do not import
   an old scalar as a fabricated completed ceremony. Bind the new coordinator
   to its registry once, then use the resumable bootstrap to adopt completed
   results and deploy federation/confederation, pools, escrow, governance and DAO.
   Both factories and the Bitcoin batch registry reject a different DKG registry.
5. Keep the old signing threshold available while old UTXOs or obligations remain.
   Moving UTXOs requires an actual transaction signed by the old group and verified
   confirmations. A lost threshold cannot be recovered by an operator key.
6. Migrate economically by closing old positions and depositing into the new
   proof-backed pool. Do not copy supply, TVL or fee counters. The new common
   outpoint registry permits one issuance per proven deposit and quarantines wallet
   change. Old liabilities must be extinguished before treating transferred Bitcoin
   as backing for new MP. There is no privileged balance-copy migration endpoint.
7. The new DAO has its own ordered day ledger. Finish old funded claims separately;
   do not replay old reports or count already allocated fees as new income.
8. Refresh generated ABIs and client state handling. `processWithdrawal` prepares
   a request; only successful `BitcoinWithdrawalEscrow.settle` emits final pool
   processing and burns S/MP. Requests escrow real tokens. Cancellation passes the
   shared signing boundary. Use `depositFeeV1` with an explicit stream: legacy
   `isWrap=false` must never be interpreted as a deployment fee.
9. Preserve current nonce journals during backup/restore. This implementation
   detects stale encrypted state against its current local journal, but cannot
   detect simultaneous rollback or cloning of both stores across machines. Stop
   signing and rotate with a reviewed procedure if that integrity is uncertain.

The reproducible local path is documented in [FIRST_DKG_ONCHAIN](FIRST_DKG_ONCHAIN.md),
[BOOTSTRAP](BOOTSTRAP.md) and [BITCOIN_WITHDRAWALS](BITCOIN_WITHDRAWALS.md).
The operational agent now supports Bitcoin mainnet through the [V4 workflow](BITCOIN_MAINNET_RUNBOOK.md),
with a pinned policy, `MainnetHeaderRelay` and `MainnetSPVGuard`. This requires a
fresh deployment and DKG; an existing regtest pin or key context is not renamed
to mainnet. A live migration, production EVM custody, external liquidity/price
policy and a funded Bitcoin mainnet payment have not been validated by the local
acceptance suite.


## 17 October participant-state journal binding

New nonce states bind a random journal identity into the encrypted state before
nonce creation. A missing/replaced journal, or a legacy nonce-state without that
binding, fails closed. Do not delete the journal to get past this error. Preserve
old pending signatures/commitments and restore an independently current journal;
otherwise retire the affected signing attempt and follow the key rotation and
obligation reconciliation process above. Exporting a key package does not export
or revive pending nonce state. Cloning both old files is not detectable locally.

On-chain onboarding uses a new `DKGOnchainBoard`. Replacement-aware batch registry
and escrow are new immutable deployments. Old addresses cannot gain these methods
through an ABI update. Keep old signed Bitcoin versions and outstanding escrow
under the old contracts until resolved; never copy obligations into a new system
while an old payable version remains live.

## 18 October immutable policy changes

The current graph also binds explicit P2WPKH/P2TR withdrawal minimums, Bitcoin
confirmation depth and DAO settlement treasury to the invitation and verified
constructors. Existing escrow/settlement contracts do not acquire these values
through a client update. Preserve their obligations under their actual deployed
code and apply the reconciliation steps above before adopting fresh contracts.

Use [staged deployment](STAGED_DEPLOYMENT.md) for the separate project
deployment-fee DKG, authorization, vault and router. Its explicit
`deployStakerBps=0` routes the fee entirely to project treasury. Each factory's
fee configuration is one-time: an existing exempt factory cannot be converted
into a paid factory. Do not reuse the user-confederation DAO router for this step.

For existing infrastructure use the Python runner's canonical-creation checks;
direct Forge runtime/getter comparisons do not establish constructor provenance.
See [CONTRACT_PROVENANCE](CONTRACT_PROVENANCE.md) for historical-RPC requirements.

## Independent networks and batching in V6

New invitations select the two networks independently and require the current
batch-capable contract runtimes. Use the [V6 migration boundary](NETWORKS_AND_AUTOMATION.md#обновление-существующего-развёртывания).
Existing immutable boards, DKG registries, authorizations and pending transaction
journals are not upgraded by installing the new CLI. Finish or explicitly migrate
their obligations using their original build; never edit a signed journal or the
enrolled network policy to make it appear compatible.

## Historical Ethereum mainnet V5 migration

V5 requires a new invitation, fresh participant enrollments and the chain-1
profile in [the Ethereum runbook](ETHEREUM_MAINNET_RUNBOOK.md). It binds positive
deployment fee and project treasury, and requires finalized Ethereum state.
Do not change a V3/V4 profile's `chain_id` or reuse an exempt factory as a paid
one. Moving from Sepolia to Ethereum mainnet also changes all EVM addresses,
receipts, authorization domains and journals; it is not a client-only upgrade.
Reconcile old obligations and migrate assets under their existing keys before
admitting new backing in the new system.
