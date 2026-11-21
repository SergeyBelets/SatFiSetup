"""Authenticate native confederation S reserve bindings before signing."""
from web3 import Web3

from .bootstrap import artifact, encode_args
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .dkg_protocol import Manifest


def review(w3, intent, bundle, at):
    from .bootstrap_custody import review_publishers, sources_args
    from .ethereum_intents import _prove_pool
    from .zk_spv import review_consumer

    kind, method = intent.get("artifact"), intent.get("hash_method")
    relevant = (kind == "ConfederationBitcoinReserve" or (kind, method) in {
        ("ConfederationPoolV2", "getBindBitcoinReserveOperationHash"),
        ("ConfederationPoolV2", "getSetWithdrawalEscrowOperationHash"),
        ("BitcoinBatchRegistry", "getSetWithdrawalEscrowOperationHash"),
        ("BitcoinWithdrawalEscrowV2", "getSetDepositRegistryOperationHash"),
        ("BitcoinWithdrawalEscrowV2", "getRequireLocalCustodyOperationHash"),
        ("BitcoinDepositRegistry", "getRegisterOperationHash"),
    })
    if not relevant:
        return
    invite = bundle["onboarding"]["invitation"]
    manifest = Manifest.from_dict(bundle)
    anchor = w3.eth.get_block(at)
    at = anchor.number
    review_publishers(w3, bundle, at, intent["coordinator"])

    def node(address, name):
        checked_runtime(w3, address, name, at)
        return w3.eth.contract(address=address, abi=artifact(name)["abi"])

    def get(contract, name, *args):
        return getattr(contract.functions, name)(*args).call(block_identifier=at)

    def direct(contract, name, args):
        proof = prove_creation(w3, contract.address, name, args, at)
        if proof.get("method") != "direct" or proof.get("sender") != invite["operator"]:
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")

    args = intent["hash_args"]
    target = node(intent["target"], kind)
    reserve = None
    pool = target if kind == "ConfederationPoolV2" else None
    if kind == "ConfederationBitcoinReserve":
        reserve = target
        pool = node(get(reserve, "confederationPool"), "ConfederationPoolV2")
    elif kind == "BitcoinDepositRegistry":
        reserve = node(args[0], "ConfederationBitcoinReserve")
        pool = node(get(reserve, "confederationPool"), "ConfederationPoolV2")
        if args[1].removeprefix("0x") != bytes(get(reserve, "payoutScript")).hex():
            raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")
    elif kind == "ConfederationPoolV2" and method == "getBindBitcoinReserveOperationHash":
        reserve = node(args[0], "ConfederationBitcoinReserve")
    elif kind == "BitcoinWithdrawalEscrowV2" and method == "getRequireLocalCustodyOperationHash":
        pool = node(args[0], "ConfederationPoolV2")

    if kind == "BitcoinBatchRegistry":
        escrow = node(args[0], "BitcoinWithdrawalEscrowV2")
        if get(escrow, "batches") != target.address:
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
    elif kind == "BitcoinWithdrawalEscrowV2":
        escrow = target
    elif kind == "BitcoinDepositRegistry":
        escrow = node(get(target, "spendRecorder"), "BitcoinWithdrawalEscrowV2")
    elif kind == "ConfederationPoolV2" and method == "getSetWithdrawalEscrowOperationHash":
        escrow = node(args[0], "BitcoinWithdrawalEscrowV2")
    else:
        escrow = node(get(pool, "withdrawalEscrow"), "BitcoinWithdrawalEscrowV2")
    batches = node(get(escrow, "batches"), "BitcoinBatchRegistry")
    auth = node(get(batches, "authorization"), "DKGAuthorization")
    if (bundle.get("role") != "conf_ceremony" or manifest.purpose != "EVM_AUTH"
            or manifest.chain_id != w3.eth.chain_id
            or manifest.session_id.hex() != invite["sessions"]["conf_ceremony"]
            or get(auth, "registry") != manifest.registry or get(auth, "coordinator") != intent["coordinator"]
            or bytes(get(auth, "dkgId")) != manifest.session_id
            or list(get(auth, "getParticipants")) != list(manifest.participants)
            or list(manifest.participants) != invite["participants"][1:]
            or get(auth, "threshold") != manifest.threshold):
        raise ValueError("BITCOIN_CUSTODY_AUTHORIZATION")
    dkg = node(manifest.registry, "DKGCoordinator")
    direct(dkg, "DKGCoordinator", [])
    coordinator = node(intent["coordinator"], "FROSTCoordinator")
    verifier = node(get(coordinator, "defaultVerifier"), "FROSTVerifier")
    direct(verifier, "FROSTVerifier", [])
    direct(coordinator, "FROSTCoordinator", [verifier.address])
    if (get(coordinator, "dkgRegistry") != dkg.address
            or get(coordinator, "hasRole", Web3.keccak(text="ADMIN_ROLE"), invite["operator"])):
        raise ValueError("BITCOIN_CUSTODY_AUTHORIZATION")
    completed = get(dkg, "getCompletedResult", manifest.session_id)
    if bytes(completed[0]) != manifest.context or bytes(get(auth, "resultHash")) != bytes(completed[1]):
        raise ValueError("BITCOIN_CUSTODY_AUTHORIZATION")
    prove_creation(w3, auth.address, "DKGAuthorization", [intent["coordinator"], manifest.session_id,
        bytes(completed[4]), list(manifest.participants), manifest.threshold], at)
    direct(batches, "BitcoinBatchRegistry", [auth.address, manifest.registry,
        bytes.fromhex(invite["sessions"]["btc_ceremony"])])
    direct(escrow, "BitcoinWithdrawalEscrowV2", [batches.address, get(escrow, "spv"),
        int(invite["bitcoin_confirmations"]), int(invite["min_p2wpkh_sats"]),
        int(invite["min_p2tr_sats"]), int(invite["ulr"]["default_miner_fee_sats"])])
    review_consumer(w3, invite, escrow, "BitcoinWithdrawalEscrowV2", intent, at)

    if pool is not None:
        _prove_pool(w3, pool, "ConfederationPoolV2", invite["operator"], at)
        if get(pool, "confederationId") != invite["confederation_id"] or get(pool, "frostMultisig") != auth.address:
            raise ValueError("BITCOIN_CUSTODY_POOL_BINDING")
    deposit_address = args[0] if kind == "BitcoinWithdrawalEscrowV2" and method == "getSetDepositRegistryOperationHash" else get(escrow, "depositRegistry")
    if int(deposit_address, 16):
        deposits = node(deposit_address, "BitcoinDepositRegistry")
        direct(deposits, "BitcoinDepositRegistry", [get(escrow, "spv"), auth.address,
            int(invite["bitcoin_confirmations"]), escrow.address])
        if kind == "BitcoinDepositRegistry" and target.address != deposits.address:
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
    if reserve is not None:
        if (get(reserve, "confederationPool") != pool.address or get(reserve, "deposits") != deposits.address
                or get(reserve, "escrow") != escrow.address or get(reserve, "authorization") != auth.address):
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
        direct(reserve, "ConfederationBitcoinReserve", [get(reserve, "poolId"), deposits.address, escrow.address, pool.address])
        if method == "getSetCustodyTransferOperationHash":
            gateway = node(args[0], "BitcoinCustodyTransferV2")
            constructor = next(row for row in artifact("BitcoinCustodyTransferV2")["abi"] if row["type"] == "constructor")
            direct(gateway, "BitcoinCustodyTransferV2", encode_args(constructor["inputs"],
                [reserve.address, pool.address, sources_args(invite["bitcoin_custody"])]))
        if method == "getSetMintAdmissionOperationHash":
            from .mint_admission import configuration
            configuration(w3, args[0], at, expected_pool=reserve.address,
                          expected_operator=invite["participants"][1], require_bound=False)
    if w3.eth.get_block(at).hash != anchor.hash:
        raise ValueError("BITCOIN_CUSTODY_REORG")
