"""One enrolled Bitcoin + ULR deployment recipe; operational gates stay explicit."""

SYSTEM_BINDINGS = ("address_registry", "address_message_board", "incident_board", "signing_board", "family_root",
    "fee_reserve", "withdrawal_credits", "withdrawal_payments", "withdrawal_funding",
    "deposits", "escrow", "batches", "mining", "ulr_treasury", "staking", "snapshots")


def bundle(value):
    if (not isinstance(value, dict) or not {"version", "ulr", "withdrawal_fee"} <= set(value)
            or set(value) - {"version", "ulr", "withdrawal_fee", "token_metadata", "bitcoin_custody"}
            or value["version"] != "1"):
        raise ValueError("SYSTEM_BOOTSTRAP_POLICY")
    from .bootstrap_withdrawal_fee import validate as validate_fee
    fee = validate_fee(value["withdrawal_fee"])
    if fee["version"] != "2" or not isinstance(value["ulr"], dict):
        raise ValueError("SYSTEM_BOOTSTRAP_REQUIRES_FAMILY_ROOT")
    from .bootstrap_tokens import resolve
    resolve(value)
    if "bitcoin_custody" in value:
        from .bootstrap_custody import validate
        validate(value["bitcoin_custody"])
    return value


def validate(profile):
    if profile.get("system_bootstrap") != "1" or "ulr" not in profile or "withdrawal_fee" not in profile:
        raise ValueError("SYSTEM_BOOTSTRAP_POLICY")
    bundle(dict(version="1", ulr=profile["ulr"], withdrawal_fee=profile["withdrawal_fee"],
                **{k: profile[k] for k in ("token_metadata", "bitcoin_custody") if k in profile}))
    from .bootstrap_ulr import validate as validate_ulr
    validate_ulr(profile)


def manifest(bootstrap, economic, bitcoin):
    """Graph verification does not attest qualification, local pins or running services."""
    validate(bootstrap.profile)
    names = SYSTEM_BINDINGS + (("custody_transfer",) if "bitcoin_custody" in bootstrap.profile else ())
    from .launch_economics import economics
    if economics(bootstrap.profile["ulr"])["startup"]["enabled"]:names += ("startup_rewards",)
    contracts = bootstrap.contracts
    configured = (all(name in contracts for name in names) and economic.get("ready") is True
                  and bitcoin.get("bindings_confirmed") is True)
    metadata = {}
    if configured and "token_metadata" in bootstrap.profile:
        from .bootstrap_tokens import check
        at = bootstrap.read_number()
        for role, name in (("governance", "dao_token"), ("mining", "mp"),
                           ("federation_share", "fed_shares"), ("confederation_share", "conf_shares")):
            metadata[role] = check(bootstrap.w3, contracts[name]["address"], role, bootstrap.profile, at)
    result = dict(version="1", bitcoin_network=bootstrap.profile["bitcoin_network"],
        chain_id=bootstrap.profile["chain_id"], bitcoin_backend=bootstrap.profile["ulr"]["bitcoin_backend"],
        graph_configured=bool(configured), ready=False,
        status="SYSTEM_GRAPH_CONFIGURED_QUALIFICATION_PENDING" if configured else "BOOTSTRAP_PENDING",
        bindings={name: contracts[name]["address"] for name in names if name in contracts},
        required_actions=["independently pin canonical Bitcoin family and registry on every signer",
            "qualify deposit and fee derivation before address activation",
            "configure and run Core/relay or ZK worker, address request/watch service and daily payout keeper",
            "complete combined Core/FROST/SPV/EVM acceptance"])
    if "address_message_board" in contracts:
        result["address_transport"] = dict(board=contracts["address_message_board"]["address"],
            registry=contracts["address_registry"]["address"], pool=contracts["mining"]["address"] if "mining" in contracts else None)
    if "family_root_policy" in bitcoin:
        result["family_root_policy"] = bitcoin["family_root_policy"]
    elif bitcoin.get("continuity", {}).get("root_policy"):
        result["family_root_policy"] = bitcoin["continuity"]["root_policy"]
    if metadata:
        result["token_metadata"] = metadata
    if "bitcoin_custody" in bootstrap.profile:
        result["bitcoin_custody"] = dict(bootstrap.profile["bitcoin_custody"],
            bitcoin_participants=bootstrap.profile["btc_ceremony"]["participants"],
            authorization_participants=bootstrap.profile["conf_ceremony"]["participants"])
    return result


def main(argv):
    """Prepare public commitments from the local build; never activates a verifier."""
    import hashlib
    import json
    from pathlib import Path
    from web3 import Web3
    from .safe_errors import DiagnosticArgumentParser
    from .bootstrap import artifact
    from .onboarding import read_public
    from .operations import canonical

    parser = DiagnosticArgumentParser(description=__doc__)
    parser.add_argument("--token-metadata", help="JSON names and symbols for the four issued token roles")
    parser.add_argument("--bitcoin-custody-policy", required=True, help="custodian-only monetary keys and immutable transfer sources")
    parser.add_argument("--ulr-policy", required=True)
    parser.add_argument("--qualification-report", required=True)
    parser.add_argument("--service-policy", required=True)
    parser.add_argument("--protocol-fee-cap-sats", required=True)
    parser.add_argument("--max-vbytes", required=True)
    parser.add_argument("--max-packet-bytes", required=True)
    parser.add_argument("--family-root-policy")
    parser.add_argument("--family-enrollment", choices=["ORIGINAL_AUTHORITY", "PRE_ENROLLED"], default="PRE_ENROLLED")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    ulr = read_public(args.ulr_policy)
    if ulr.get("mode") == "reuse" and not args.family_root_policy:
        raise ValueError("SYSTEM_BOOTSTRAP_REUSE_REQUIRES_FAMILY_ROOT")
    candidate = artifact("BitcoinDepositDerivation")
    metadata = candidate["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    sources = dict(compiler=metadata["compiler"], settings=metadata["settings"], sources=metadata["sources"])
    source_hash = "0x" + hashlib.sha256(canonical(sources).encode()).hexdigest()
    # These commit exact public documents, including a report explicitly saying
    # qualification is pending. A hash never overrides on-chain qualification.
    def document_hash(path):
        document = read_public(path)
        return "0x" + hashlib.sha256(canonical(document).encode()).hexdigest()
    root = read_public(args.family_root_policy) if args.family_root_policy else dict(version="create-1",
        creation_code_hash=Web3.to_hex(Web3.keccak(hexstr=artifact("BitcoinKeyFamilyRegistry")["bytecode"]["object"])))
    from .bootstrap_tokens import resolve
    names = resolve(dict(ulr=ulr, **({"token_metadata": read_public(args.token_metadata)} if args.token_metadata else {})))
    result = bundle(dict(version="1", ulr=ulr, token_metadata=names,
        **({"bitcoin_custody": read_public(args.bitcoin_custody_policy)} if args.bitcoin_custody_policy else {}),
        withdrawal_fee=dict(version="2", family_root=root,
        family_enrollment=args.family_enrollment if args.family_root_policy else "ORIGINAL_AUTHORITY",
        derivation_source_hash=source_hash, qualification_commitment=document_hash(args.qualification_report),
        service_policy_hash=document_hash(args.service_policy), protocol_fee_cap_sats=args.protocol_fee_cap_sats,
        max_vbytes=args.max_vbytes, max_packet_bytes=args.max_packet_bytes)))
    with Path(args.output).open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(status="SYSTEM_PROFILE_PREPARED", output=args.output, ready=False,
        qualification="must satisfy independent review and on-chain gates")))
    return 0
