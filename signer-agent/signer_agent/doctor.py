"""Read-only chain diagnostics; an invitation board alone is never ready."""

import json
import shutil
from pathlib import Path

from .dkg_protocol import PROTOCOL_NAME, PROTOCOL_ID
from .participant_errors import participant_exception_code
from .safe_errors import DiagnosticArgumentParser, emit_debug
from .bitcoin_network import is_public_network


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--state", default="ops/deployments.sqlite")
    p.add_argument("--bitcoin-rpc")
    p.add_argument("--bitcoin-cookie")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    path = Path(a.profile)
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "config" / (a.profile + ".json")
    checks = []
    mainnet = False
    production_network = False

    def add(name, ok, reason):
        checks.append(dict(check=name, ok=bool(ok), reason=None if ok else reason))

    try:
        config = json.loads(path.read_text())
        network = config.get("bitcoin_network")
        mainnet = is_public_network(network)
        backed = mainnet or config.get("chain_id") == "1"
        production_network = network == "mainnet" or config.get("chain_id") == "1"
        invite = None
        if "onboarding" in config:
            from .onboarding import verify_profile

            invite = verify_profile(config)
            add("protocol", invite["protocol_id"] == PROTOCOL_ID.hex(), "UNSUPPORTED_PROTOCOL")
        else:
            add("protocol", config.get("protocol") == PROTOCOL_NAME, "UNSUPPORTED_PROTOCOL")
        add("mode", config.get("mode") == ("backed" if backed else "integration"), "EXPLICIT_MODE_REQUIRED")
        add("bitcoin_network", network in ("regtest", "mainnet", "testnet4"), "BITCOIN_NETWORK_UNSUPPORTED")
        if mainnet:
            if not invite or invite.get("version") not in ("4", "5", "6"):
                raise ValueError("BTC_NETWORK_INVITATION_REQUIRED")
            from .bitcoin_network import validate_policy

            validate_policy(config.get("bitcoin_policy"), expected_network=network)
        for name in ("forge", "anvil", "bitcoind", "cargo"):
            add(name, shutil.which(name) is not None, "MISSING_TOOL")
        bitcoin = None
        if a.bitcoin_rpc and a.bitcoin_cookie:
            try:
                from .bitcoin_rpc import BitcoinRPC

                bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network=network,
                                     policy=config.get("bitcoin_policy") if mainnet else None)
                info = bitcoin.check_network()
                core_ready = not info.get("initialblockdownload", True)
                add("bitcoin_rpc", core_ready, "BITCOIN_NOT_SYNCED")
            except Exception as exc:
                code = participant_exception_code(exc)
                emit_debug(exc, code=code)
                add("bitcoin_rpc", False, code)
                core_ready = False
            if mainnet and not core_ready:
                raise ValueError("MAINNET_CORE_REQUIRED")
        else:
            add("bitcoin_rpc", False, "BITCOIN_RPC_NOT_CONFIGURED")
            if mainnet:
                raise ValueError("MAINNET_CORE_REQUIRED")
        from .bootstrap import Bootstrap
        from .bootstrap_plan import build

        # A caller-supplied steps list must not remove the V4 readiness barrier.
        # Rebuild mainnet steps from the verified, enrolled public inputs.
        if backed or "steps" not in config:
            config = build(config)
        bootstrap = Bootstrap(config, a.state, bitcoin=bitcoin if mainnet else None)
        verified = bootstrap.apply(verify=True)
        add("bootstrap", verified["status"] == "VERIFIED", "DEPLOYMENT_INCOMPLETE")
        required = {
            "dkg", "coordinator", "conf_pool", "batches", "escrow", "deposits",
            "mining", "mp", "fee_manager", "staking", "fee_router",
        }
        if 'ulr' in config:
            from .bootstrap_ulr import ROOTS
            required.update(ROOTS)
            required.update(('ulr_treasury','ulr_voting','decisions','fee_pipeline','validator'))
            add('ulr_graph',verified.get('ulr',{}).get('ready') is True,'ULR_GRAPH_INCOMPLETE')
        else:
            required.add('settlement')
        if config.get('system_bootstrap')=='1':
            from .bootstrap_system import SYSTEM_BINDINGS
            required.update(SYSTEM_BINDINGS)
            system=verified.get('system',{})
            add('system_graph',system.get('graph_configured') is True,'SYSTEM_GRAPH_INCOMPLETE')
            add('system_qualification',system.get('ready') is True,'SYSTEM_QUALIFICATION_PENDING')
        if mainnet:
            required.update(("relay", "spv_guard"))
            if config.get("chain_id") == "1":
                required.update(("project_auth", "project_vault", "project_router", "federations", "confederations"))
        if config.get("chain_id") == "1":
            required.update(("project_auth", "project_vault", "project_router", "federations", "confederations"))
        full_graph = required.issubset(bootstrap.contracts)
        add("full_graph", full_graph, "FULL_GRAPH_NOT_DEPLOYED")
        for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
            try:
                result = bootstrap.contract("dkg").functions.getCompletedResult(
                    bytes.fromhex(config[role]["id"].removeprefix("0x"))
                ).call(block_identifier=bootstrap.read_number())
                result_hash = bytes(result[1])
                add(role, len(result_hash) == 32 and result_hash != bytes(32), "DKG_INCOMPLETE")
            except Exception as exc:
                emit_debug(exc, code="DKG_INCOMPLETE")
                add(role, False, "DKG_INCOMPLETE")
        if mainnet and full_graph:
            current = bootstrap.bitcoin_ready(None, readonly=True)
            add("mainnet_readiness", current["status"] == "MAINNET_RELAY_READY", "MAINNET_RELAY_STALE")
    except Exception as exc:
        # No heuristic uppercase exception passthrough: even an uppercase value
        # can contain a bearer token, cookie, password or private key.
        code = participant_exception_code(exc)
        emit_debug(exc, code=code)
        add("config_or_deployment", False, code)
    if not any(item["check"] == "bitcoin_rpc" for item in checks):
        add("bitcoin_rpc", False, "BITCOIN_RPC_NOT_READY" if a.bitcoin_rpc and a.bitcoin_cookie else "BITCOIN_RPC_NOT_CONFIGURED")
    ready = bool(checks) and all(item["ok"] for item in checks)
    readiness = "READY_MAINNET_PROTOCOL" if production_network else "READY_TESTNET_PROTOCOL" if mainnet else "READY_INTEGRATION"
    result = dict(status=readiness if ready else "NOT_READY", checks=checks)
    if ready:
        result.update(chain_id=config["chain_id"], bitcoin_network=network)
    if production_network:
        result["scope"] = "Mainnet protocol readiness only; not production custody approval."
    print(json.dumps(result, indent=2))
    return 0 if ready else 2
