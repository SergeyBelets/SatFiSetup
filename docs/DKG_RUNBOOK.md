# Local DKG runbook

Для первого запуска с адресами друзей используйте [on-chain runbook](FIRST_DKG_ONCHAIN.md). Он не требует общего mailbox или внешнего канала. Новый V6 bootstrap создаёт три независимые DKG сразу, а `frost-signer flow dkg` продвигает все три роли с bounded batches и проверкой подтверждённых раундов; см. [автоматизацию](NETWORKS_AND_AUTOMATION.md). Ниже сохранён низкоуровневый local-mailbox путь для изолированных тестов.

This runbook covers the implemented DKG/signing subsystem. It does not deploy the full DAO.
Never use the artificial EVM identities from tests outside an isolated local node.

## Build

```
python3.11 -m venv .venv
.venv/bin/pip install -r signer-agent/requirements.lock
.venv/bin/pip install --no-deps -e './signer-agent[test]'
source .venv/bin/activate
cargo build --locked --manifest-path frost-crypto/Cargo.toml
forge build --root web3
python ops/generate_abis.py --check
```

Install Foundry 1.8.5 and Bitcoin Core 30.3 on PATH. To run all implemented integration
checks with disposable Anvil/regtest nodes:

```
.venv/bin/python ops/local_acceptance.py
```

Each participant runs a separate process and encrypted state file. The shared SQLite
mailbox contains public packages, identity signatures and HPKE ciphertext only. Local
co-location is an integration test, not proof of independent operator isolation.

## Individual ceremony

Create separate private participant directories and a mode-0600 password file containing
at least 32 bytes of random entropy encoded as hex. EVM identities belong in mode-0600
local JSON files with a `private_key` field, not command arguments. They sign EVM messages;
they are not FROST signing shares. Fund them only on the chosen test chain.

```
frost-signer dkg transport-init --state participant/state.enc --password-file participant/password
```

Collect the **public** output keys and create a manifest matching
`signer_agent.dkg_protocol.Manifest.to_dict()`. Generate a fresh random session_id, use the
actual deployed DKGCoordinator address, original ordered participants and their transport
keys. Do not edit the manifest after signatures/publication. The e2e harness supplies a
complete executable example in `tests/e2e/test_dkg_processes.py`.

For a new registry, use its `ceremonyRegistrar()` as the creator or have that
account assign the manifest ID to the chosen creator before `dkg create`.
[Ceremony creation and delegation](DKG_PROTOCOL_V1.md#ceremony-creation).

```
frost-signer dkg create --manifest manifest.json --rpc http://127.0.0.1:8545 --identity-file participant/identity.json
frost-signer dkg run-once --manifest manifest.json --rpc http://127.0.0.1:8545 --identity-file participant/identity.json --state participant/state.enc --password-file participant/password --mailbox public-mailbox.sqlite
frost-signer dkg status --manifest manifest.json --rpc http://127.0.0.1:8545
```

Run `run-once` on each participant until all report ACTIVE. WAITING states require another
participant/message; retry does not regenerate a round or ciphertext. A cryptographic
validation failure or transcript mismatch aborts. A timeout needs a new session ID and
new randomness. Never publish plaintext round 2, SecretPackage or the encrypted-store password.

## Export / import

```
frost-signer dkg export --manifest manifest.json --rpc http://127.0.0.1:8545 --state participant/state.enc --password-file participant/password --format encrypted --output participant/key.enc
frost-signer dkg export --manifest manifest.json --rpc http://127.0.0.1:8545 --state participant/state.enc --password-file participant/password --format public --output public-certificate.json
frost-signer dkg export --manifest manifest.json --rpc http://127.0.0.1:8545 --state participant/state.enc --password-file participant/password --format scalar --output participant/signing-share.txt
frost-signer dkg import --manifest manifest.json --rpc http://127.0.0.1:8545 --identity-file participant/identity.json --state participant/imported.enc --password-file participant/password --file participant/key.enc
frost-signer dkg verify --manifest manifest.json --rpc http://127.0.0.1:8545 --state participant/imported.enc --password-file participant/password
```

Scalar export writes precisely 64 hex characters plus newline with mode 0600, and never
prints the scalar. It is the compatibility `FROST_SIGNING_SHARE_HEX` value, not an EVM key
or serialized KeyPackage. Encrypted export uses the same local storage key; no broad key
export HTTP endpoint exists. Import verifies participant/index, manifest, group/result,
and secret-to-public consistency against the completed registry.

The Rust process uses inherited local stdin/stdout pipes (`--ipc`), rejecting terminal
input/output. Secrets never appear in argv. Python memory and JSON secret buffers do not
provide guaranteed zeroization or constant-time behavior; only scalar arithmetic is moved
to k256. State encryption expects a random high-entropy key, not a human password.
