"""Local-only DKG/key CLI. No environment file is read by this interface."""

import json
import os
from pathlib import Path
from .dkg import Participant, Registry, atomic_private
from .dkg_protocol import Manifest
from .dkg_transport import Mailbox
from .secure_state import SecureState, requires_independent_nonce_ledger
from .safe_errors import DiagnosticArgumentParser, emit_debug
from . import crypto


def signing_confirmations(enrolled, *, nft_config=None, ulr_config=None, fee_sale_config=None):
    """Select the strongest local/enrolled depth before any signing reads."""
    depth = int(enrolled)
    for path, module in ((nft_config, 'nft_governance'), (ulr_config, 'ulr_treasury'),
                         (fee_sale_config, 'fee_sale_governance')):
        if path:
            from importlib import import_module
            pin = import_module('.' + module, __package__).load_pin(path)
            depth = max(depth, int(pin['confirmations']))
    return depth


def _check_local_share(key):
    local_point = crypto.call("public_share", share=key["result"]["signing_share"])["public"]
    if local_point != key["result"]["public_share"]:
        raise ValueError("SHARE_MISMATCH")


def _review_bitcoin_approval(intent, manifest, key, registry, state, args, *, enrolled):
    from .bitcoin_onchain_payment import APPROVAL_OPERATIONS, CANCEL_HELPER, review_intent

    pair = (intent.get("artifact"), intent.get("hash_method"))
    cancelling = pair == ("BitcoinBatchRegistry", CANCEL_HELPER)
    legacy_file = args.package and manifest.chain_id == 31337 and not enrolled
    if "bitcoin_plan" not in intent:
        if pair in APPROVAL_OPERATIONS and (cancelling or not legacy_file):
            raise ValueError("BTC_PUBLIC_PLAN_REQUIRED")
        return None
    if cancelling and (args.package or not args.board):
        raise ValueError("BTC_CANCEL_REQUIRES_BOARD_INTENT")

    def review():
        review_intent(intent, manifest, key, registry, state, board_address=args.board,
                      bitcoin_rpc=args.bitcoin_rpc, bitcoin_cookie=args.bitcoin_cookie)

    review()
    return intent["operation_hash"].removeprefix("0x"), review


def main(argv):
    p = DiagnosticArgumentParser(prog="frost-signer dkg")
    sub = p.add_subparsers(dest="action", required=True)
    from .bitcoin_successor_ceremony import add_parser as add_successor_ceremony
    add_successor_ceremony(sub)
    ledger = sub.add_parser("nonce-ledger-init", help="Bind a fresh signing state to an independent spent ledger")
    ledger.add_argument("--state", required=True)
    ledger.add_argument("--password-file", required=True)
    ledger.add_argument("--nonce-ledger", required=True, help="Absolute file path on independent storage")
    ledger.add_argument("--nonce-anchor-config", help="Private client configuration for the off-host nonce anchor")
    ledger.add_argument("--evm-auth", action="store_true",
                        help="Bind a fresh EVM_AUTH role and its .signing state to the same ledger")
    for action in (
        "transport-init",
        "create",
        "run-once",
        "status",
        "export",
        "import",
        "verify",
        "sign-evm",
        "gas-prepare-configure",
        "gas-prepare-pause",
        "gas-propose",
        "gas-execute",
        "gas-status",
        "btc-commit",
        "btc-sign",
        "btc-deploy-board",
        "btc-prepare",
        "btc-import-plan",
        "btc-propose",
        "btc-execute",
        "btc-publish-plan",
        "btc-pending",
        "btc-status",
        "btc-broadcast",
        "btc-settle",
        "btc-relay",
        "btc-network-status",
        "btc-recover",
        "btc-recover-inputs",
        "btc-mint-propose",
        "btc-mint-execute",
        "btc-mint-retry",
        "btc-address-mint-propose",
        "btc-address-mint-execute",
        "btc-address-mint-retry",
        "btc-mint-status",
        "btc-mint-retire",
        "btc-mint-renew",
        "btc-mint-transfer-propose",
        "btc-mint-transfer-execute",
        "btc-mint-transfer-retire",
        "btc-treasury-propose",
        "btc-treasury-execute",
        "inspect-bitcoin-registry",
        "trust-bitcoin-registry",
    ):
        c = sub.add_parser(action)
        c.add_argument("--board")
        if action.startswith("btc-") or action in ("inspect-bitcoin-registry", "trust-bitcoin-registry"):
            c.add_argument("--recovery-board", help="Original CLTV deployment board for a rotated Bitcoin DKG")
        c.add_argument(
            "--role", choices=["evm_ceremony", "conf_ceremony", "btc_ceremony"]
        )
        c.add_argument("--manifest")
        c.add_argument("--rpc")
        c.add_argument("--identity-file")
        c.add_argument("--state")
        c.add_argument("--password-file")
        c.add_argument("--nonce-ledger", help="Assert the independent spent ledger bound to this state")
        c.add_argument("--nonce-anchor-config", help="Use the off-host nonce anchor pinned to this state")
        c.add_argument("--mailbox")
        if action == "run-once":
            c.add_argument("--confirm-abort", help="Confirm the reviewed DKG abort fingerprint")
        c.add_argument("--file")
        c.add_argument("--output")
        c.add_argument(
            "--format", choices=["encrypted", "scalar", "public"], default="encrypted"
        )
        c.add_argument("--bitcoin-rpc")
        c.add_argument("--bitcoin-cookie")
        c.add_argument("--fee-reserve-config", help="Private local BTC fee reserve and reconciliation policy")
        c.add_argument('--incident-config',help='Private incident inbox, evidence and explicit local status approval')
        c.add_argument('--ulr-config', help='Reviewed common ULR staking, snapshot registry and treasury addresses')
        c.add_argument('--ulr-intent', help='Public ULR snapshot intent file when the board publishes its SHA-256')
        c.add_argument('--nft-config', help='Independently reviewed NFT entity/factory/fee-manager pin')
        c.add_argument('--fee-sale-config', help='Independently reviewed fee sale and treasury pin')
        c.add_argument('--epoch-service-config',help='Private enrolled old-BTC-epoch service policy')
        c.add_argument("--zk-worker-config", help="Private local self-hosted SPV worker configuration")
        c.add_argument("--zk-transport-config", help="Private encrypted on-chain SPV transport configuration")
        c.add_argument("--zk-service-recipient", help="Registered service transport identity")
        c.add_argument("--zk-request-federation", help="Federation on whose behalf this request is sent")
        c.add_argument("--coordinator")
        c.add_argument("--session", type=int)
        c.add_argument("--package")
        c.add_argument("--mint-admission")
        if action in ("inspect-bitcoin-registry", "trust-bitcoin-registry", "btc-mint-status", "btc-mint-retire", "btc-mint-renew",
                      "btc-mint-transfer-propose", "btc-mint-transfer-execute", "btc-mint-transfer-retire"):
            c.add_argument("--mint-only", action="store_true", help="Separate graph trust for mint; never permits Bitcoin signing")
        if action in ("inspect-bitcoin-registry", "trust-bitcoin-registry"):
            c.add_argument("--address-signing", action="store_true", help="Explicit generation-aware BASE/DERIVED Bitcoin signing profile")
        if action.startswith("gas-"):
            c.add_argument("--gas-registry")
        if action == "gas-prepare-configure":
            c.add_argument("--configuration", required=True)
        if action.startswith("gas-prepare-"):
            c.add_argument("--deadline", required=True)
        c.add_argument("--batch-registry")
        c.add_argument("--accept-fingerprint")
        c.add_argument("--signing-board")
        c.add_argument("--batch")
        if action in ("btc-commit", "btc-sign"):
            c.add_argument("--resume-expired", action="store_true",
                           help="Resume the exact expired S to BTC batch already in Signing")
        if action == "btc-broadcast":
            c.add_argument("--signed-out", help="Export the exact bound V5 Bitcoin payment without broadcasting")
        c.set_defaults(operation=None, inputs=None, withdrawals=None, fee=None,
                       max_fee=None, deadline=None, signers=None, max_packet_bytes=None)
        if action in ("btc-propose", "btc-execute"):
            c.add_argument("--operation", choices=["reserve", "bind", "cancel"])
            c.add_argument("--deadline", help="Fresh EVM authorization deadline for a Reserved batch cancellation")
        if action == "btc-prepare":
            c.add_argument("--input", dest="inputs", action="append")
            c.add_argument("--withdrawal", dest="withdrawals", action="append")
            c.add_argument("--fee")
            c.add_argument("--max-fee")
            c.add_argument("--previous", help="Previous user-funded batch for an RBF replacement")
            c.add_argument("--deadline")
            c.add_argument("--signers", nargs="+")
        if action=='btc-import-plan':c.add_argument('--plan-file',required=True)
        if action in ("btc-mint-propose", "btc-mint-execute", "btc-mint-retry"):
            c.add_argument("--txid", required=True)
            c.add_argument("--vout", type=int, required=True)
            c.add_argument("--recipient", dest="recipients", action="append", required=True)
            c.add_argument("--amount", dest="amounts", action="append", required=True)
            c.add_argument("--deadline", required=True)
        if action.startswith("btc-address-mint-"):
            c.add_argument("--intent-id", required=True)
            c.add_argument("--txid", required=True)
            c.add_argument("--vout", type=int, required=True)
            c.add_argument("--deadline", required=True)
        if action == "btc-mint-transfer-propose":
            c.add_argument("--new-operator", required=True)
            c.add_argument("--deadline", required=True)
        if action == "btc-mint-renew":
            c.add_argument("--deadline", required=True, help="Fresh Unix deadline within seven days for an expired mint")
        if action.startswith("btc-treasury-"):
            c.add_argument("--request-id", required=True)
            c.add_argument("--satoshis", required=True)
            c.add_argument("--destination-script", required=True)
            c.add_argument("--treasury-owner")
            c.add_argument("--deadline", required=True)
        if action == "btc-deploy-board":
            c.add_argument("--max-packet-bytes", type=int)
    a = p.parse_args(argv)
    from .verified_cache import cache_state
    cache_token = cache_state.set(None)
    pin_token = crypto.pin_required.set(crypto.pin_required.get())
    try:
        if a.action == "successor-create":
            from .bitcoin_successor_ceremony import run

            print(json.dumps(run(a), sort_keys=True))
            return 0
        if getattr(a, "resume_expired", False) and (a.package or not a.signing_board or not a.batch):
            raise ValueError("BTC_EXPIRED_SIGNING_REQUIRES_IDS")
        if (a.action in ("btc-propose", "btc-execute") and a.operation == "cancel"
                and (not a.signing_board or not a.batch)):
            raise ValueError("BTC_CANCEL_REQUIRES_IDS")
        if a.action.startswith("btc-") and a.package and (
            a.action not in ("btc-commit", "btc-sign") or a.signing_board or a.batch
        ):
            raise ValueError("BTC_FILE_AND_ONCHAIN_MODES_CONFLICT")
        from .onboarding import read_private, password_file, validate_bundle

        nonce_anchor = None
        if a.nonce_anchor_config:
            from .nonce_anchor import NonceAnchor
            nonce_anchor = NonceAnchor.from_file(a.nonce_anchor_config)
        if a.action == "nonce-ledger-init":
            password = password_file(a.password_file)
            role_state = SecureState(a.state, password, nonce_ledger=a.nonce_ledger, nonce_anchor=nonce_anchor)
            signing_state = SecureState(a.state + ".signing", password, nonce_anchor=nonce_anchor) if a.evm_auth else None
            role_state.initialize_nonce_ledger(evm_signing_state=signing_state)
            print(json.dumps(dict(status="NONCE_LEDGER_READY")))
            return 0
        state = signing_state = None
        evm_nonce_ledger = None
        if a.state:
            password = password_file(a.password_file)
            state = SecureState(a.state, password, nonce_ledger=a.nonce_ledger, nonce_anchor=nonce_anchor)
            if a.action == "sign-evm":
                signing_state = SecureState(a.state + ".signing", password, nonce_anchor=nonce_anchor)
                evm_nonce_ledger = state.evm_nonce_ledger(signing_state)
            elif a.nonce_ledger is not None or nonce_anchor is not None:
                state.require_nonce_ledger()
            cache_state.set(SecureState(a.state + ".public-cache", password))
        elif a.nonce_ledger is not None or nonce_anchor is not None:
            raise ValueError("NONCE_LEDGER_STATE_REQUIRED")
        if a.action == "transport-init":
            with state.exclusive():
                if state.get("transport"):
                    raise ValueError("TRANSPORT_ALREADY_EXISTS")
                key = crypto.call("transport_key")
                state.put("transport", key)
            print(json.dumps(dict(public=key["public"])))
            return 0
        if a.board:
            from .onchain_transport import ceremony_bundle
            from .evm_rpc import evm_web3

            bundle = ceremony_bundle(evm_web3(a.rpc), a.board, a.role)
        else:
            bundle = json.loads(Path(a.manifest).read_text())
        m = Manifest.from_dict(bundle)
        if getattr(a, "recovery_board", None) and (m.purpose != "BTC_SPEND" or not a.board
                                                  or a.recovery_board == a.board):
            raise ValueError("BTC_RECOVERY_BINDING")
        enrolled = "onboarding" in bundle
        invite = bundle.get("onboarding", {}).get("invitation", {})
        signing = a.action in ("sign-evm", "btc-commit", "btc-sign")
        if signing and not enrolled and state is not None and state.get("enrollment"):
            raise ValueError("ENROLLED_MANIFEST_REQUIRED")
        if requires_independent_nonce_ledger(m.chain_id, invite.get("bitcoin_network")):
            crypto.pin_required.set(True)
            if a.action == "import":
                raise ValueError("MAINNET_KEY_IMPORT_REQUIRES_ORIGINAL_STATE")
            if signing:
                if state is None:
                    raise ValueError("NONCE_LEDGER_STATE_REQUIRED")
                state.require_nonce_ledger()
                if a.action == "sign-evm" and evm_nonce_ledger is None:
                    raise ValueError("EVM_NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED")
        identity = (
            json.loads(read_private(a.identity_file))["private_key"]
            if a.identity_file
            else None
        )
        registry = Registry(a.rpc, m.registry, identity)
        registry.fee_reserve_config = a.fee_reserve_config
        registry.epoch_service_config = a.epoch_service_config
        if a.identity_file:
            registry.identity_file = a.identity_file
            registry.transaction_journal = (
                str(Path(a.identity_file).resolve()) + ".transactions.sqlite"
            )
        if bundle.get("onboarding") and (a.action.startswith(("btc-", "gas-")) or a.action in (
                "run-once", "sign-evm", "inspect-bitcoin-registry", "trust-bitcoin-registry")):
            validate_bundle(bundle, state, registry)
            from .bitcoin_trust import invitation_options

            options = invitation_options(bundle["onboarding"]["invitation"])
            registry.bitcoin_graph = dict(version="2", chain_id=str(m.chain_id), **options) if options else {}
        if a.action == "create":
            if registry.w3.eth.chain_id != m.chain_id:
                raise ValueError("CHAIN_MISMATCH")
            tx = registry.send(
                "createCeremony",
                m.session_id,
                list(m.participants),
                m.threshold,
                list(m.transport_keys),
                m.epoch,
                __import__("signer_agent.dkg_protocol", fromlist=["PURPOSES"]).PURPOSES[
                    m.purpose
                ],
                m.protocol_id,
            )
            result = dict(status="CREATED", tx=tx, context=m.context.hex())
        elif a.action == "import":
            registry.check(m)
            source = SecureState(a.file, password)
            key = source.get(m.context.hex())
            result_data = registry.read("getCompletedResult", m.session_id)
            if (
                not key
                or key.get("stage") != "active"
                or key["manifest"] != m.to_dict()
            ):
                raise ValueError("PACKAGE_CONTEXT_MISMATCH")
            if (
                bytes(result_data[1]).hex() != key["result_hash"]
                or bytes(result_data[4]).hex() != key["result"]["group"][2:]
            ):
                raise ValueError("PACKAGE_RESULT_MISMATCH")
            index = key["index"]
            address = m.participants[index - 1]
            if key["participant"] != address or registry.account.address != address:
                raise ValueError("PACKAGE_PARTICIPANT_MISMATCH")
            point = crypto.call("public_share", share=key["result"]["signing_share"])[
                "public"
            ]
            if (
                bytes(registry.read("getPublicKeyShare", m.session_id, address)).hex()
                != point
                or point != key["result"]["public_share"]
            ):
                raise ValueError("PACKAGE_SHARE_MISMATCH")
            with state.exclusive():
                if state.get(m.context.hex()):
                    raise ValueError("KEY_ALREADY_EXISTS")
                state.put(m.context.hex(), key)
            result = dict(status="IMPORTED", context=m.context.hex())
        elif a.action == "status":
            info, c = registry.check(m)
            result = dict(
                phase=info[4],
                completed=info[9],
                context=bytes(c[0]).hex(),
                result=bytes(c[3]).hex(),
            )
        elif a.action == "run-once":
            if bundle.get("onchain_board"):
                from .onchain_transport import OnchainMailbox

                mailbox = OnchainMailbox(
                    registry, bundle["onchain_board"], a.identity_file, a.state + ".transactions.sqlite",
                )
            else:
                mailbox = Mailbox(a.mailbox)
            recovery = invite.get("bitcoin_recovery") if m.purpose == "BTC_SPEND" else None
            if recovery is None:
                participant = Participant(m, registry, state, mailbox)
            else:
                participant = Participant(m, registry, state, mailbox, bitcoin_recovery=recovery)
            result = participant.run_once(a.confirm_abort)
        else:
            registry.check(m)
            key = state.get(m.context.hex())
            if not key or key.get("stage") != "active":
                raise ValueError("KEY_NOT_ACTIVE")
            result_data = registry.read("getCompletedResult", m.session_id)
            if bytes(result_data[1]).hex() != key["result_hash"]:
                raise ValueError("TRANSCRIPT_MISMATCH")
            if a.action != "sign-evm":
                _check_local_share(key)
            if key["participant"] != m.participants[key["index"] - 1]:
                raise ValueError("PARTICIPANT_MISMATCH")
            if a.action.startswith("gas-"):
                from .gas_governance import management
                result = management(a, bundle, registry, state)
            elif a.action.startswith("btc-") and not a.package:
                from .bitcoin_onchain import dispatch

                result = dispatch(a, m, key, registry, state)
            elif a.action in ("inspect-bitcoin-registry", "trust-bitcoin-registry"):
                from .bitcoin_trust import (inspect_registry, pin_registry, discover_registry,
                    board_options, invitation_options, bitcoin_for_graph, check_bitcoin_graph)

                recovery_board = getattr(a, "recovery_board", None)
                primary = recovery_board or a.board
                options = (board_options(m, registry, primary) if primary else
                           invitation_options(bundle["onboarding"]["invitation"])
                           if bundle.get("onboarding") else {})
                if a.mint_only:
                    options["mint_only"] = True
                    if not a.batch_registry: raise ValueError("BTC_REGISTRY_ADDRESS_REQUIRED")
                if a.address_signing:
                    if a.mint_only: raise ValueError("BTC_REGISTRY_PROFILE_CONFLICT")
                    options["address_signing"] = True
                    if not a.batch_registry: raise ValueError("BTC_REGISTRY_ADDRESS_REQUIRED")

                if primary and (not a.batch_registry or recovery_board):
                    reviewed_address = discover_registry(m, key, registry, primary,
                                                         address_signing=a.address_signing)
                    if a.batch_registry and a.batch_registry != reviewed_address:
                        raise ValueError("BTC_REGISTRY_NOT_TRUSTED")
                    a.batch_registry = reviewed_address
                if not a.batch_registry:
                    raise ValueError("BTC_REGISTRY_ADDRESS_REQUIRED")
                if a.action == "inspect-bitcoin-registry":
                    result = dict(status="REVIEW_REQUIRED", **inspect_registry(
                        m, key, registry, a.batch_registry, **options))
                    if options.get("bitcoin_network", "regtest") != "regtest":
                        result["network_observation"] = check_bitcoin_graph(registry, result["graph"],
                            bitcoin_for_graph(result["graph"], a.bitcoin_rpc, a.bitcoin_cookie))
                else:
                    result = pin_registry(m, key, registry, state,
                                          a.batch_registry, a.accept_fingerprint, **options,
                                          bitcoin=bitcoin_for_graph(dict(version="2", **options),
                                              a.bitcoin_rpc, a.bitcoin_cookie)
                                              if options.get("bitcoin_network", "regtest") != "regtest" else None)
            elif a.action == "verify":
                result = dict(
                    status="VERIFIED",
                    context=m.context.hex(),
                    participant=key["participant"],
                )
            elif a.action == "export":
                if not a.output:
                    raise ValueError("OUTPUT_REQUIRED")
                if a.format == "scalar":
                    atomic_private(
                        a.output, (key["result"]["signing_share"] + "\n").encode()
                    )
                elif a.format == "encrypted":
                    # Export only this package, not unrelated keys or nonce history.
                    temp = SecureState(a.output, password)
                    payload = json.dumps(
                        dict(version=1, sessions={m.context.hex(): key}), sort_keys=True
                    ).encode()
                    atomic_private(a.output, temp._fernet.encrypt(payload))
                else:
                    public = dict(
                        manifest=m.to_dict(),
                        result_hash=key["result_hash"],
                        public_package=key["result"]["public_package"],
                        group=key["result"]["group"],
                        public_shares=key["result"]["public_shares"],
                        normalization_negated=key["result"]["normalization_negated"],
                        original_group=key["result"]["original_group"],
                        raw_dkg_group=key["result"]["raw_dkg_group"],
                        internal_key=key["result"]["internal_key"],
                        taproot_tweak=key["result"]["taproot_tweak"],
                    )
                    if key["result"].get("recovery_descriptor") is not None:
                        public["recovery_descriptor"] = key["result"]["recovery_descriptor"]
                    atomic_private(
                        a.output, (json.dumps(public, indent=2) + "\n").encode()
                    )
                result = dict(status="EXPORTED", format=a.format)
            elif a.action == "sign-evm":
                if m.purpose != "EVM_AUTH":
                    raise ValueError("WRONG_KEY_PURPOSE")
                from .ethereum_intents import require_mainnet_signing_enrollment
                require_mainnet_signing_enrollment(bundle, m.chain_id)
                from .config import Config
                from .coordinator import CoordinatorClient, SESSION_NONE
                from .evm_network import read_block
                from .service import SigningService
                from .dkg import ROOT

                if a.mint_admission and a.package:
                    raise ValueError("BTC_MINT_INTENT_SOURCES_CONFLICT")
                if a.mint_admission and not a.coordinator:
                    from .mint_admission import configuration
                    a.coordinator = configuration(registry.w3, a.mint_admission, "latest")["coordinator"].address

                if a.mint_admission:
                    from .mint_admission import configuration
                    from .address_mint import signing_graph
                    mint_context = configuration(registry.w3, a.mint_admission, "latest")
                    if int(mint_context["pool"].functions.mintProofVersion().call()) == 2:
                        registry.bitcoin_graph = signing_graph(bundle, mint_context)

                depth = signing_confirmations(
                    getattr(registry, "bitcoin_graph", {}).get("evm_confirmations", "1"),
                    nft_config=a.nft_config, ulr_config=a.ulr_config, fee_sale_config=a.fee_sale_config)
                anchor = read_block(registry.w3, depth)
                read_number = anchor.number
                board_intent = None
                if a.board and (not a.coordinator or not (a.mint_admission or a.package)):
                    from .onchain_transport import board_at

                    published = board_at(registry.w3, a.board, anchor).functions.intents(a.session).call(
                        block_identifier=read_number)
                    if not published:
                        raise ValueError("EVM_CONFIRMATIONS_PENDING")
                    board_intent = json.loads(bytes(published).decode())
                    if not a.coordinator:
                        a.coordinator = board_intent["coordinator"]
                cfg = Config(
                    rpc_url=a.rpc,
                    sender=key["participant"],
                    private_key=identity,
                    coordinator_address=a.coordinator,
                    coordinator_abi_path=str(
                        ROOT / "signer-agent/contracts/FROSTCoordinator.json"
                    ),
                    signing_share_hex=key["result"]["signing_share"],
                    state_key=password,
                    state_path=a.state + ".signing",
                    deployment_block=0,
                    scan_chunk_size=100,
                    confirmations=depth,
                    poll_interval=1,
                    receipt_timeout=120,
                    gas_limit=0,
                    max_fee_gwei=None,
                    priority_fee_gwei=1,
                    auto_sign=False,
                    auto_finalize=True,
                    nonce_ledger=evm_nonce_ledger,
                )
                client = CoordinatorClient(cfg)
                client.continuity_identity_file=a.identity_file
                client.bitcoin_graph = getattr(registry, "bitcoin_graph", {})
                client.pin_read_snapshot(anchor)
                session = client.session(a.session)
                if session.state == SESSION_NONE:
                    raise ValueError("EVM_CONFIRMATIONS_PENDING")
                contract = client.contract
                if (
                    contract.functions.dkgRegistry().call(block_identifier=read_number).lower()
                    != m.registry.lower()
                    or bytes(contract.functions.sessionDKGId(a.session).call(block_identifier=read_number))
                    != m.session_id
                    or bytes(
                        contract.functions.sessionDKGResult(a.session).call(block_identifier=read_number)
                    ).hex()
                    != key["result_hash"]
                ):
                    raise ValueError("SESSION_DKG_MISMATCH")
                # Explicit local approval binds exact typed intent, not merely a session number.
                if a.mint_admission:
                    from .mint_admission import session_record
                    intent = session_record(registry.w3, a.mint_admission, a.session, read_number,
                        role=bundle.get("role", a.role), require_active=False)["intent"]
                elif a.package:
                    intent = json.loads(Path(a.package).read_text())
                elif board_intent is not None:
                    intent = board_intent
                else:
                    raise ValueError("PUBLIC_INTENT_REQUIRED")
                from .ulr_treasury import resolve_board_intent
                intent = resolve_board_intent(intent, a.ulr_intent, session=a.session)
                from .ethereum_intents import validate_signing_binding
                validate_signing_binding(intent, bundle, client.contract.address,
                    allow_legacy_file=bool(a.package and not a.board and a.coordinator
                        and "onboarding" not in bundle and m.chain_id != 1))
                allowed_artifacts = {
                    "DKGAuthorization",
                    "SimpleMultiPool",
                    "ConfederationMultiPool",
                    "ConfederationPoolV2",
                    "BitcoinWithdrawalEscrowV2",
                    "BitcoinWithdrawalRecovery",
                    "ConfederationDAOIncome",
                    "ConfederationNFTPass",
                    "FederationFactory",
                    "ConfederationFactory",
                    "FederationFeeManager",
                    "CustodianDAOIncomeVault",
                    "CustodianDAODailySettlement",
                    "BitcoinBatchRegistry",
                    "BitcoinWithdrawalEscrow",
                    "CustodianDAOFeeRouter",
                    "PoolFeePipeline",
                    "GovernanceRegistry",
                    "BitcoinDepositRegistry",
                    "BitcoinAddressRegistry",
                    "BitcoinKeyFamilyRegistry",
                    "BitcoinFeeReserve",
                    "WithdrawalFeePayments",
                    "WithdrawalFeeFunding",
                    "MiningPoolV1",
                    "ConfederationBitcoinReserve",
                    "BitcoinMintAdmission",
                    "SPVServicePaymentRegistry",
                    "CustodianDAOStaking",
                    "CustodianDAORateController",
                    "CustodianDAODecisionBoard",
                    "ULRCustodianSnapshots",
                    "ULRConfederationTreasury",
                    "CustodianValidator",
                    "FundedFeeSale",
                }
                artifact = intent.get("artifact")
                if artifact not in allowed_artifacts:
                    raise ValueError("POLICY_TARGET_TYPE")
                abi = json.loads(
                    (
                        ROOT
                        / "web3/contracts/out"
                        / f"{artifact}.sol"
                        / f"{artifact}.json"
                    ).read_text()
                )["abi"]
                target_contract = client.w3.eth.contract(
                    address=intent["target"], abi=abi
                )
                if (session.message_hash.hex() != intent["operation_hash"].removeprefix("0x")
                        or session.origin_contract.lower() != intent["target"].lower()
                        or intent["chain_id"] != str(m.chain_id)):
                    raise ValueError("POLICY_MISMATCH")
                if artifact=='BitcoinDepositRegistry' and intent.get('hash_method')=='getSetAddressRegistryOperationHash':
                    client.continuity_binding=intent
                family_operation=(artifact=='BitcoinKeyFamilyRegistry' or artifact=='BitcoinAddressRegistry' and intent.get('hash_method')=='getBindFamilyRegistryOperationHash')
                if family_operation:
                    from .family_registry import load_pin,review as review_family
                    from .bitcoin_continuity import Catalog
                    pin=load_pin(Catalog(identity_file=a.identity_file,state=state),str(client.w3.eth.chain_id))
                    reviewed_family=review_family(client.w3,intent,bundle,read_number,pin)
                    if reviewed_family['complete']:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status='ALREADY_EXECUTED',session=str(a.session),operation_hash=intent['operation_hash'])))
                        return 0
                    client.family_registry_context=(intent,bundle)
                    client.family_registry_state=state
                from .bootstrap_tokens import review_intent as review_token_metadata
                formation_checked = review_token_metadata(client.w3, intent, bundle, read_number)
                if formation_checked or "token_metadata" in bundle.get("onboarding", {}).get("invitation", {}):
                    client.token_metadata_context = (intent, bundle)
                from . import fee_sale_governance as fee_sale
                if fee_sale.matches(intent):
                    if a.package or not a.board or not a.fee_sale_config:
                        raise ValueError("FEE_SALE_PUBLIC_INTENT_AND_PIN_REQUIRED")
                    sale_pin = fee_sale.load_pin(a.fee_sale_config)
                    dao_scope = fee_sale.review(client.w3, intent, bundle, read_number, sale_pin)
                    if dao_scope["complete"]:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status="ALREADY_EXECUTED", session=str(a.session),
                                              operation_hash=intent["operation_hash"])))
                        return 0
                    client.fee_sale_context = (intent, bundle, sale_pin)
                else:
                    from .bootstrap_confederation_dao import review as review_dao
                    dao_scope = review_dao(client.w3, intent, bundle, read_number)
                from .bootstrap_custody import review_intent as review_custody
                review_custody(client.w3, intent, bundle, read_number)
                if "bitcoin_custody" in bundle.get("onboarding", {}).get("invitation", {}):
                    client.custody_context = (intent, bundle, bundle["onboarding"]["invitation"]["evm_confirmations"])
                from .operation_status import executed_operation
                if artifact == 'FederationFeeManager':
                    from . import nft_governance
                    nft_pin = None
                    if intent.get('hash_method') in nft_governance.METHODS:
                        if a.package or not a.board or not a.nft_config:
                            raise ValueError('NFT_PUBLIC_INTENT_AND_PIN_REQUIRED')
                        nft_pin = nft_governance.load_pin(a.nft_config)
                        nft_review = nft_governance.review(client.w3,intent,bundle,read_number,nft_pin)
                        if nft_review['complete']:
                            client.check_read_snapshot()
                            print(json.dumps(dict(status='ALREADY_EXECUTED',operation_hash=intent['operation_hash'])))
                            return 0
                    client.nft_intent_context = (intent,bundle,nft_pin)
                from .ulr_treasury import requires_review as requires_ulr_review
                if dao_scope is None and requires_ulr_review(intent, bundle):
                    if a.package or not a.board or bundle.get("onchain_board") != a.board:
                        raise ValueError("ULR_PUBLIC_INTENT_AND_PIN_REQUIRED")
                    from .ulr_treasury import load_pin as load_ulr_pin, review as review_ulr
                    if "ulr_bootstrap" in intent:
                        from .bootstrap_ulr import enrolled_pin
                        ulr_pin = enrolled_pin(client.w3, intent, bundle, read_number)
                        if a.ulr_config and load_ulr_pin(a.ulr_config) != ulr_pin:
                            raise ValueError("ULR_BOOTSTRAP_PIN_MISMATCH")
                    elif a.ulr_config:
                        ulr_pin = load_ulr_pin(a.ulr_config)
                    else:
                        raise ValueError("ULR_PUBLIC_INTENT_AND_PIN_REQUIRED")
                    ulr_review = review_ulr(client.w3, intent, bundle, read_number, ulr_pin)
                    if ulr_review["complete"]:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status="ALREADY_EXECUTED", operation_hash=intent["operation_hash"])))
                        return 0
                    client.ulr_intent_context = (intent, bundle, ulr_pin)
                if artifact in ("WithdrawalFeePayments","WithdrawalFeeFunding"):
                    if a.package or not a.board or bundle.get('onchain_board')!=a.board:
                        raise ValueError('WITHDRAWAL_FEE_PUBLIC_INTENT_REQUIRED')
                    from .btc_fee_reserve import configured
                    from .withdrawal_fee_policy import review as review_personal_fee
                    inspector=configured(registry,intent.get('withdrawal_fee_request',{}).get('reserve'))
                    reviewed=review_personal_fee(client.w3,intent,bundle,read_number,inspector=inspector,session=a.session,state=state)
                    client.withdrawal_fee_context=(intent,bundle,inspector,state)
                    if reviewed['complete']:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status='ALREADY_EXECUTED',session=str(a.session),operation_hash=intent['operation_hash'])))
                        return 0
                if artifact == "BitcoinFeeReserve":
                    if a.package or not a.board or bundle.get("onchain_board") != a.board:
                        raise ValueError("BTC_FEE_PUBLIC_INTENT_REQUIRED")
                    from .btc_fee_reserve import configured
                    from .btc_fee_policy import review as review_fee
                    inspector = configured(registry,intent["target"])
                    reviewed_fee = review_fee(client.w3,intent,bundle,read_number,inspector=inspector,session=a.session,state=state)
                    client.btc_fee_context = (intent,bundle,inspector,state)
                    if reviewed_fee["complete"]:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status="ALREADY_EXECUTED",session=str(a.session),operation_hash=intent["operation_hash"])))
                        return 0
                if artifact == "BitcoinAddressRegistry" and not family_operation:
                    if a.package or not a.board or bundle.get("onchain_board") != a.board:
                        raise ValueError("ADDRESS_REGISTRATION_PUBLIC_INTENT_REQUIRED")
                    if intent.get('hash_method')=='getStatusOperationHash':
                        if not a.incident_config:raise ValueError('INCIDENT_CONFIG_REQUIRED')
                        from .incident_transport import Client as IncidentClient,load_config as incident_config
                        from .address_status import private_review
                        incident=IncidentClient(incident_config(a.incident_config),client.w3)
                        if incident.account.address!=client.sender:raise ValueError('INCIDENT_SIGNER_IDENTITY')
                        registered=private_review(incident,intent,bundle,read_number,session=a.session,approval=True)
                        client.incident_status_context=(intent,bundle,incident)
                    else:
                        from .address_registration import review
                        registered = review(client.w3, intent, bundle, read_number, session=a.session)
                        if not registered['complete']:
                            client.continuity_context=(registered['context'],'ISSUE',a.session)
                        client.address_registration_context = (intent, bundle)
                    if registered["complete"]:
                        client.check_read_snapshot()
                        print(json.dumps(dict(status="ALREADY_EXECUTED" if intent.get('hash_method')=='getStatusOperationHash' else "ALREADY_REGISTERED", session=str(a.session),
                            operation_hash=intent["operation_hash"], target=target_contract.address)))
                        return 0
                if executed_operation(client.w3, target_contract, artifact, intent, read_number, session=a.session,
                        coordinator=client.contract.address,
                        operator=bundle.get("onboarding", {}).get("invitation", {}).get("operator")):
                    client.check_read_snapshot()
                    print(json.dumps(dict(status="ALREADY_EXECUTED", session=str(a.session),
                        operation_hash=intent["operation_hash"], target=target_contract.address)))
                    return 0
                if a.mint_admission:
                    session_record(registry.w3, a.mint_admission, a.session, read_number,
                        role=bundle.get("role", a.role), signing=True)
                if artifact == "SPVServicePaymentRegistry":
                    if a.package or not a.board:
                        raise ValueError("GAS_TYPED_INTENT_REQUIRED")
                    from .gas_governance import review
                    review(registry.w3, intent, bundle, read_number, session=a.session)
                    client.gas_intent_context = (intent, bundle)
                helper = intent.get("hash_method", "")
                rotation = artifact == "BitcoinWithdrawalRecovery"
                if rotation:
                    if (a.package or not a.board or bundle.get("onchain_board") != a.board
                            or bundle.get("role") != "conf_ceremony"
                            or helper != "getRotateOperationHash" or intent.get("bitcoin_rotation") != "1"):
                        raise ValueError("BTC_RECOVERY_ROTATION_INTENT")
                    from .bitcoin_recovery_cli import review_rotation_intent
                    from .evm_network import verify_anchor

                    def review_rotation():
                        confirmed = read_block(client.w3, depth)
                        verify_anchor(client.w3, read_number, anchor.hash, safe=confirmed)
                        reviewed = review_rotation_intent(client.w3, intent, bundle, confirmed.number)
                        latest = client.w3.eth.get_block("latest")
                        if latest.number != confirmed.number:
                            live = review_rotation_intent(client.w3, intent, bundle, latest.number, finalized=False)
                            if live.complete != reviewed.complete:
                                raise ValueError("EVM_CONFIRMATIONS_PENDING")
                        verify_anchor(client.w3, latest.number, latest.hash)
                        return reviewed, confirmed

                    reviewed, rotation_anchor = review_rotation()
                    if reviewed.complete:
                        from .operation_status import verify_executed_session

                        verify_executed_session(reviewed.auth, client.contract, target_contract.address, reviewed.op,
                                                a.session, rotation_anchor.number)
                        verify_anchor(client.w3, rotation_anchor.number, rotation_anchor.hash)
                        print(json.dumps(dict(status="ALREADY_EXECUTED", session=str(a.session),
                            operation_hash=intent["operation_hash"], target=target_contract.address)))
                        return 0
                if helper not in ("hashOperation", "operationHash") and not (
                    helper.startswith("get") and helper.endswith("OperationHash")
                ):
                    raise ValueError("POLICY_HASH_HELPER")
                from .bootstrap import encode_args
                from .bitcoin_onchain_payment import CANCEL_HELPER

                def integer_strings(value):
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, int):
                        return str(value)
                    if isinstance(value, list):
                        return [integer_strings(x) for x in value]
                    return value

                cancelling = (artifact, helper) == ("BitcoinBatchRegistry", CANCEL_HELPER)
                if cancelling:
                    if str(session.deadline) != intent.get("cancel_deadline"):
                        raise ValueError("BTC_CANCEL_DEADLINE_MISMATCH")
                    # This deployed registry commits to cancel(bytes32,bytes32)
                    # inside the mutating entry point, with no public hash getter.
                    # The strict public-plan reviewer reads its exact on-chain
                    # batch, reservation and nonce before supplying the hash.
                    bitcoin_review = _review_bitcoin_approval(
                        intent, m, key, registry, state, a, enrolled=enrolled,
                    )
                    computed = bytes.fromhex(bitcoin_review[0])
                    typed_args = []
                else:
                    helper_abi = next(
                        x for x in abi
                        if x.get("type") == "function" and x.get("name") == helper
                    )
                    typed_args = encode_args(helper_abi["inputs"], integer_strings(intent["hash_args"]))
                    computed = getattr(target_contract.functions, helper)(
                        *typed_args
                    ).call(block_identifier=read_number)
                if bytes(computed) != session.message_hash:
                    raise ValueError("OPERATION_PARAMETERS_MISMATCH")
                if (artifact,helper)==("BitcoinBatchRegistry","getSetFeeReserveOperationHash"):
                    from .btc_fee_policy import review_binding
                    review_binding(client.w3,intent,read_number)
                    client.btc_fee_binding=intent
                if (artifact,helper)==('BitcoinWithdrawalEscrow','getSetWithdrawalFeesOperationHash'):
                    from .withdrawal_fee_policy import review_binding
                    review_binding(client.w3,intent,read_number)
                    client.withdrawal_fee_binding=intent
                client.bitcoin_plan_review = bitcoin_review if cancelling else _review_bitcoin_approval(
                    intent, m, key, registry, state, a, enrolled=enrolled,
                )
                if rotation:
                    def guard_rotation():
                        current, _ = review_rotation()
                        if current.complete:
                            raise ValueError("BTC_OPERATION_CHANGED")

                    client.bitcoin_plan_review = (session.message_hash.hex(), guard_rotation)
                if m.chain_id == 1 and dao_scope is None and not hasattr(client, "ulr_intent_context") and not rotation:
                    from .ethereum_intents import review_bootstrap_intent
                    review_bootstrap_intent(registry.w3, intent, bundle, read_number)
                elif artifact in ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2") and helper == "getSetCallbackPoolRegistryOperationHash":
                    from .callback_admission import review_callback_configuration
                    if len(typed_args) != 1:
                        raise ValueError("BTC_CALLBACK_CONFIGURATION_INTENT")
                    review_callback_configuration(target_contract, typed_args[0], read_number)
                elif artifact in ("MiningPoolV1", "ConfederationBitcoinReserve") and helper == "getSetMintAdmissionOperationHash":
                    from .mint_admission import configuration
                    if len(typed_args) != 1:
                        raise ValueError("BTC_MINT_CONFIGURATION_INTENT")
                    configuration(registry.w3, typed_args[0], read_number,
                        expected_pool=target_contract.address, require_bound=False)
                client.bitcoin_graph = getattr(registry, "bitcoin_graph", {})
                client.identity_file = a.identity_file
                client.base_mint_funding = (bundle, a.bitcoin_rpc, a.bitcoin_cookie)
                if client.bitcoin_graph.get("usage") == "MINT_ONLY_V1":
                    from .bitcoin_trust import bitcoin_for_graph
                    client.address_mint_bitcoin = bitcoin_for_graph(client.bitcoin_graph, a.bitcoin_rpc, a.bitcoin_cookie)
                client.transaction_journal = registry.transaction_journal
                _check_local_share(key)
                result = SigningService(cfg, client, state=signing_state,
                                        reviewed_session_id=session.session_id).approve(session).to_dict()
            else:
                from .bitcoin_worker import sign_step
                from .bitcoin_trust import require_registry, bitcoin_for_graph

                plan = json.loads(Path(a.package).read_text())
                pinned = require_registry(m, key, registry, state, plan["batch_registry"])
                registry.bitcoin_graph = pinned["graph"]
                result = sign_step(
                    a.action,
                    m,
                    key,
                    plan,
                    registry,
                    state,
                    bitcoin_for_graph(pinned["graph"], a.bitcoin_rpc, a.bitcoin_cookie),
                )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        from .transaction_wait import TransactionPending, TransactionRecoveryRequired
        if isinstance(exc, TransactionPending):
            print(json.dumps(exc.result, sort_keys=True))
            return 0
        if isinstance(exc, TransactionRecoveryRequired):
            emit_debug(exc, code="RECOVERY_REQUIRED")
            print(json.dumps(exc.result, sort_keys=True))
            return 2
        if a.action in ("run-once", "sign-evm"):
            from .transaction_wait import observation_pending
            if observation_pending(exc) or (type(exc) is ValueError and str(exc) in (
                    "DKG_ANCHOR_UNAVAILABLE", "EVM_CONFIRMATIONS_PENDING", "ETHEREUM_FINALITY_UNAVAILABLE")):
                status = "WAITING_DKG_RPC" if a.action == "run-once" else "AWAITING_EVM_RPC"
                print(json.dumps(dict(status=status)))
                return 0
        # Exceptions from RPC may echo calldata, but never print identity/state/crypto request payloads.
        from .participant_errors import participant_failure

        failure = participant_failure(exc)
        emit_debug(exc, code=failure["code"])
        print(json.dumps(failure))
        return 2
    finally:
        cache_state.reset(cache_token)
        crypto.pin_required.reset(pin_token)
