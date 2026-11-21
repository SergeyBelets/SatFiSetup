# SatFiSetup

SatoshiFi custodian SDK and the operator's monitoring backend.

| Path | What it is |
| --- | --- |
| `signer-agent/` | `frost-signer` CLI: on-chain DKG, FROST signing, bootstrap, ZK-SPV worker |
| `rust/zkspv/` | ZK-SPV guest, worker host and state adapter sources with pins |
| `web3/`, `frost-crypto/`, `js/` | Solidity contracts, native FROST, JS helpers (built on each host) |
| `ops/` | Host setup (`bootstrap_host.py`), packaging, ZK build and contour scripts |
| `docs/`, `config/`, `tests/` | Runbooks, example configs, integration tests |
| `SDK_README.md`, `distribution-manifest.json` | SDK overview and SHA256 of every SDK file |
| `backend/` | AWS SAM stack behind `api.btcyield.online`: monitoring, Telegram bot, alarms |
| `tools/satoshifi_api.py` | Client for the monitoring API (participant reports, operator calls) |
| `site/` | Browser-side signed-text formats for `btcyield.online` |

Licensed under Apache 2.0 (`LICENSE`); third-party notices in `rust/zkspv/NOTICE.md`.

## Install (Linux or Windows WSL2)

```bash
git clone https://github.com/SergeyBelets/SatFiSetup.git ~/satfi-sdk
cd ~/satfi-sdk/signer-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e '.[evm-test]'
cd .. && python ops/bootstrap_host.py build     # native FROST and both Solidity projects
python -m pytest signer-agent/tests -q
```

The SDK part of this repository is the participant source distribution
(`python -m ops.package_distribution`): source and pins only, no proofs,
receipts, caches or private state. `distribution-manifest.json` lists SHA256
of every SDK file. ZK worker modes (prepare, export, verify-bundle, publish)
are described in `docs/ZK_SPV_WORKER.md` and `docs/DISTRIBUTION.md`.

Then follow, in order:

1. `docs/CROSS_PLATFORM_QUICKSTART.md` and `docs/COLD_START.md`: host check and your identity.
2. `docs/FIRST_DKG_ONCHAIN.md`: invitation, discovery, bootstrap, DKG.
3. `docs/ETHEREUM_MAINNET_RUNBOOK.md` and `docs/BITCOIN_MAINNET_RUNBOOK.md`: mainnet specifics.

Keys, identity files, journals and `.env` never go into this repository.

## Monitoring reporter (each custodian)

The reporter sends a signed heartbeat every 5 minutes. The operator's bot posts
verdict changes to the ceremony chat and raises an alarm after 15 minutes of silence.
It signs with your work identity; the operator registers your work address from the
invitation.

```bash
mkdir -p ~/.satoshifi && chmod 700 ~/.satoshifi
cat > ~/.satoshifi/report.json <<'JSON'
{"api_base": "https://api.btcyield.online",
 "deployment": "<deployment id from the operator>",
 "identity_file": "/home/<you>/<path to your identity.json>",
 "rpc_url": "https://ethereum-rpc.publicnode.com",
 "min_balance_wei": "50000000000000000",
 "report_period_s": 300}
JSON
~/satfi-sdk/signer-agent/.venv/bin/python ~/satfi-sdk/tools/satoshifi_api.py report --config ~/.satoshifi/report.json --once
```

To keep it running, install `tools/satoshifi-report.service` as a systemd user unit
(instructions inside the file). To flag a problem to the chat yourself:

```bash
python ~/satfi-sdk/tools/satoshifi_api.py event --config ~/.satoshifi/report.json \
  --kind manual --verdict NEEDS_HELP --reason "what happened" --action "what you need"
```

## Operator

The operator signs `/v1/operator/*` calls with a dedicated API key that holds no funds.

```bash
T=~/satfi-sdk/tools/satoshifi_api.py
python $T op-deployment --config op.json --status ACTIVE --chat-id <chat> --members members.json
python $T op-event --config op.json --kind stage --verdict NEEDS_HUMAN --reason "..." --action "..."
python $T op-status --config op.json
```

`members.json` lists `{"work_address", "label"}` for each custodian from the invitation.
`NEEDS_HUMAN` events also go to the operator's e-mail. The backend is deployed with
`sam build && sam deploy --config-env prod` from `backend/` (see `backend/README.md`).
