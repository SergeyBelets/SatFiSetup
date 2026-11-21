# Operational API v1

The API is a separate loopback process, without a participant keystore or EVM identity. Start with:

```sh
frost-signer api --policy config/api-policy.json --state ops/deployments.sqlite --port 8787
```

The policy contains `principals` (`sha256` of a high-entropy bearer token; `role`: reader/operator/admin), `deployments` (journal IDs), `actions`, and `views`. Keep the policy locally controlled. The service binds only 127.0.0.1; remote access requires an authenticated TLS reverse proxy or SSH tunnel. Do not expose the HTTP listener directly.

`GET /health` is public. Other paths require `Authorization: Bearer …`. `GET /readiness` verifies recorded deployment receipts, their canonical block hashes, runtime code hashes and configured binding/role checks. `GET /metrics` returns operation counts by status. `/v1/operations`, `/v1/deployments`, and named `/v1/views/{name}` expose public state. Configure named views for DKG results, signing status, pool information, fee balances, withdrawal states, DAO reports, proposals and claimable balances. Each view includes EVM chain and Bitcoin network context; configure `units` with token address and decimals for financial getters.

A write request only prepares an operation. An allowlisted action maps to a deployment, contract, artifact, and method. All ABI integer arguments, including array/tuple entries, are decimal strings. Example:

```json
{"action":"claim_dao","args":[["20742"]]}
```

`POST /v1/operations` requires operator/admin and an `Idempotency-Key`. Keys are scoped to the action's deployment. Identical retries in that deployment return the same operation. A changed payload with the same key in that deployment returns 409. Unknown actions and reader writes are rejected. The body limit is 64 KiB. Credentials and request bodies are excluded from logs.

When the same key exists in multiple visible deployments, append `?deployment=DEPLOYMENT_ID` to `GET /v1/operations/ID`. Use `--deployment DEPLOYMENT_ID` for worker `status` and `execute` in that case.

A separate local operator executes a prepared operation:

```sh
frost-signer ops execute --id CLAIM_ID --state ops/deployments.sqlite --identity-file /private/operator.json
frost-signer ops status --id CLAIM_ID --state ops/deployments.sqlite
```

The 0600 identity contains an **EVM account key**, never a FROST share. Execution first verifies the deployment and uses the same durable transaction engine as bootstrap. Target-bound FROST authorization and custodian signatures must already exist where required by the contract. Failure records `RECOVERY_REQUIRED`; rerun reconciles the same signed transaction rather than allocating a replacement deployment.

The machine-readable contract is [openapi.json](openapi.json). API roles authorize preparation/execution intent; they never replace on-chain thresholds. Bearer secrets are not stored in the operations journal.

API request IDs, status and events live in dedicated SQLite tables. A key that
matches a deployment or bootstrap step ID can coexist with it in the same
`--state` file. New executions use the sibling
`<state filename>.api-execution.sqlite` for transaction recovery and share the
identity's existing nonce lock. Include both databases in operator backups.

Before upgrading, stop processes using the old SDK and back up the journal.
Starting the upgraded API or running `frost-signer ops status --state JOURNAL --id ID`
moves old API requests and their events atomically. Client IDs and statuses stay
the same. A previously started execution resumes from its original transaction
journal and keeps the original nonce binding.

The journal is **private recovery state**, not a public export: bootstrap profiles
can contain RPC access credentials, and pending steps contain signed transactions
that can be broadcast. Keep the database and backups local. The client creates
the database and SQLite sidecars with mode `0600`, creates new containing
directories with mode `0700`, and rejects symlinks, hard links and directories
writable by other users. Existing database permissions are tightened on open.
Do not store journals directly in shared directories such as `/tmp`.

HTTP responses use a public projection. Operation listings and status reads
include only actions whose saved specification still matches the policy and whose
deployment is allowlisted. Bootstrap internals are not operation endpoints;
deployment manifests expose step status and transaction/block hashes, without
profiles, raw signed transactions or internal requests. Actions and named views
must also belong to an allowlisted deployment. All authenticated readers of one
API policy share this public scope; use separate instances/policies for separate
reader groups.

`frost-signer index --rpc URL --chain-id ID --state JOURNAL --addresses ADDRESS ...
--from-block HEIGHT --confirmations DEPTH` advances up to 100 confirmed blocks per
run. Its SQLite checkpoints compare canonical block hashes, retain orphaned logs
with `canonical=0`, and rewind only index state on a reorg. A detected reorg returns
RECOVERY_REQUIRED and never triggers refunds or reminting. `GET /v1/events` exposes
the latest 200 public log records for the chain/address pairs in the allowlisted
deployment manifests; `topics`/`data` decode with the generated ABI.

The deployment `READY` response describes contract/journal integrity. Financial
operations still have their own guards: funded converter inventory, finalized
historical price evidence, a closed UTC day, participant/custodian approval, and
Bitcoin proof depth. Configure corresponding named views so operators can see
which prerequisite blocks the next action. A zero-fee integration bootstrap is
explicitly visible in both factory getters and its manifest steps.

## CLI preparation, simulation and public reads

```sh
frost-signer ops prepare --policy config/api-policy.json --state ops/deployments.sqlite --id UNIQUE_ID --params request.json
frost-signer ops execute --state ops/deployments.sqlite --id UNIQUE_ID --dry-run --sender OPERATOR_ADDRESS
frost-signer ops execute --state ops/deployments.sqlite --id UNIQUE_ID --identity-file /private/operator.json
frost-signer ops view --policy config/api-policy.json --state ops/deployments.sqlite --view withdrawal --params args.json
```

`request.json` is the same `{action,args}` body accepted by HTTP; `args.json` is an
ABI argument array, for example `["1"]` for withdrawal ID 1. Simulation uses
`eth_call` and gas estimation with the supplied public sender, sends no transaction
and does not consume the operation. A failed simulation must be resolved before
execution. `ops status` reads the same journal from CLI.

`GET /v1/actions` returns the allowlisted action names, ABI input schemas and
named reads. `GET /v1/deployments/{id}` returns the recorded public manifest,
including an incomplete bootstrap's waiting state. For a view explicitly marked
`parameters: true`, append URL-encoded `?args=["1"]`; other views keep fixed
arguments. Reads include the observed EVM block number/hash and check that it is
still canonical. Methods must be `view` or `pure`. `units` can contain token and
decimals metadata, with manifest references such as `$mp`.

The example policy covers DKG results, signatures, pool lists/details, withdrawals,
fee counters and helper hashes, Bitcoin batches/payments, DAO days and proposals.
It exposes preparation actions for issuance, withdrawal, reservation, payment,
fee sync/routing and DAO settlement/claim. Each caller still needs the respective
on-chain permissions; the example has no enabled bearer principal.

`/metrics` also evaluates policy `metrics` entries through allowlisted reads. The
example exposes converter inventory, the next DAO day and pending wrap/unwrap fee
reports. Unavailable reads return `METRIC_UNAVAILABLE`; they are never reported
as zero. The index CLI returns confirmed checkpoint lag on each pass.
