"""Pin both Bitcoin custody legs to authentic payment SPV deployments."""

from web3 import Web3
import rlp

from .bitcoin_trust import _check_runtime, checked_runtime
from .bootstrap import artifact
from .bootstrap_withdrawal_fee import genesis
from .contract_provenance import prove_creation
from .zk_profiles import MAINNET_IMAGE, TESTNET4_IMAGE
from .zk_spv import VERIFIER_SELECTOR, VERIFIER_VERSION


_LEGACY_GATEWAYS = {
    "BitcoinCustodyTransfer": (12967, "9d9c6e86af63162b5ea16baf4eb31e084dac1f0892477057921c2c8f2ea1df2d",
                                ((1238, 3724, 4523, 9410), (1494, 5862, 6275))),
    "BitcoinCustodyTransferV2": (13270, "3bc5dab013133e0b8b490a02d67a6388b45c63ae0c061f9cb5a51ec650fb0796",
                                  ((1717, 4012, 4827, 9877), (6325, 6754))),
}


def known_legacy_gateway_runtime(code, kind):
    """Recognize the reviewed pre-network-binding build for enrolled recovery."""
    length, expected, groups = _LEGACY_GATEWAYS[kind]
    if len(code) != length:
        return False
    masked = bytearray(code)
    for group in groups:
        if len({bytes(code[offset:offset + 32]) for offset in group}) != 1:
            return False
        for offset in group:
            masked[offset:offset + 32] = bytes(32)
    return Web3.keccak(masked).hex() == expected


def _node(w3, address, kind, block, *, previous_build=False):
    checked_runtime(w3, address, kind, block, previous_build=previous_build)
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def _read(contract, method, block, *args):
    return getattr(contract.functions, method)(*args).call(block_identifier=block)


def _runtime_kind(w3, address, kinds, block):
    actual = bytes(w3.eth.get_code(address, block_identifier=block))
    for kind in kinds:
        try:
            code = artifact(kind)["deployedBytecode"]
            _check_runtime(actual, code)
        except ValueError as error:
            if str(error) not in ("BTC_CONTRACT_BUILD_MISMATCH", "ARTIFACT_NOT_UNIQUE"):
                raise
        else:
            return kind
    raise ValueError("BITCOIN_CUSTODY_SPV_UNSUPPORTED")


def _direct(w3, address, kind, block):
    if prove_creation(w3, address, kind, None, block).get("method") != "direct":
        raise ValueError("BITCOIN_CUSTODY_SPV_UNSUPPORTED")


def _child(parent, nonce):
    return Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(parent[2:]), nonce]))[-20:])


def spv_network(w3, endpoint, block):
    """Resolve the network from reviewed bytecode and canonical constructor evidence."""
    kind = _runtime_kind(w3, endpoint, ("RegtestHeaderRelay", "MainnetSPVGuard", "ZkSpvAdapter",
                                          "ZkTestnet4SpvAdapter"), block)
    spv = w3.eth.contract(address=endpoint, abi=artifact(kind)["abi"])
    if kind == "RegtestHeaderRelay":
        _direct(w3, endpoint, kind, block)
        if _read(spv, "testHelpersEnabled", block):
            raise ValueError("BITCOIN_CUSTODY_SPV_UNSUPPORTED")
        return "regtest"
    if kind == "MainnetSPVGuard":
        _direct(w3, endpoint, kind, block)
        relay = _read(spv, "relay", block)
        relay_kind = _runtime_kind(w3, relay, ("MainnetHeaderRelay", "Testnet4HeaderRelay"), block)
        _direct(w3, relay, relay_kind, block)
        return "mainnet" if relay_kind == "MainnetHeaderRelay" else "testnet4"

    network, state_kind, image = ("mainnet", "ZkBitcoinStateV2", MAINNET_IMAGE) if kind == "ZkSpvAdapter" else (
        "testnet4", "ZkBitcoinTestnet4StateV2", TESTNET4_IMAGE)
    parent = _read(spv, "state", block)
    state = _node(w3, parent, state_kind, block)
    _direct(w3, parent, state_kind, block)
    verifier = _node(w3, _read(state, "verifier", block), "RiscZeroGroth16Verifier", block)
    if (_child(parent, 2) != endpoint or _child(parent, 1) != verifier.address
            or _read(state, "adapter", block) != endpoint or bytes(_read(state, "programImageId", block)).hex() != image
            or bytes(_read(state, "verifierCodeHash", block)) != Web3.keccak(w3.eth.get_code(verifier.address, block_identifier=block))
            or _read(verifier, "VERSION", block) != VERIFIER_VERSION
            or bytes(_read(verifier, "SELECTOR", block)) != VERIFIER_SELECTOR):
        raise ValueError("BITCOIN_CUSTODY_SPV_UNSUPPORTED")
    return network


def source_graph(w3, mining_address, block, *, enrolled=False):
    """Return the stable source descriptor and its independent Bitcoin network."""
    mining = _node(w3, mining_address, "MiningPoolV1", block, previous_build=enrolled)
    token = _node(w3, _read(mining, "mpToken", block), "PoolMpToken", block)
    escrow = _node(w3, _read(mining, "escrow", block), "BitcoinWithdrawalEscrow", block)
    deposits = _node(w3, _read(mining, "deposits", block), "BitcoinDepositRegistry", block)
    batches = _node(w3, _read(escrow, "batches", block), "BitcoinBatchRegistry", block)
    payment_spv = _read(escrow, "spv", block)
    if (_read(token, "poolDAO", block) != mining.address or _read(escrow, "depositRegistry", block) != deposits.address
            or _read(deposits, "spendRecorder", block) != escrow.address or _read(batches, "withdrawalEscrow", block) != escrow.address
            or _read(deposits, "spv", block) != payment_spv
            or bytes(_read(deposits, "poolScripts", block, mining.address)) != Web3.keccak(_read(mining, "payoutScript", block))):
        raise ValueError("BITCOIN_CUSTODY_SOURCE_GRAPH")
    result = dict(escrow=escrow.address, token=token.address)
    for node, name, field in ((escrow, "BitcoinWithdrawalEscrow", "escrow_hash"), (token, "PoolMpToken", "token_hash"),
                              (mining, "MiningPoolV1", "mining_hash"), (deposits, "BitcoinDepositRegistry", "deposit_hash"),
                              (batches, "BitcoinBatchRegistry", "batch_hash")):
        if node is not token:
            options = {"previous_build": True} if enrolled and node is mining else {}
            prove_creation(w3, node.address, name, None, block, **options)
        result[field] = Web3.to_hex(Web3.keccak(w3.eth.get_code(node.address, block_identifier=block)))
    return result, spv_network(w3, payment_spv, block)


def review_source(w3, expected, network, block):
    token = _node(w3, expected["token"], "PoolMpToken", block)
    descriptor, actual_network = source_graph(w3, _read(token, "poolDAO", block), block, enrolled=True)
    if descriptor != expected:
        raise ValueError("BITCOIN_CUSTODY_SOURCE_GRAPH")
    if actual_network != network:
        raise ValueError("BITCOIN_CUSTODY_NETWORK_MISMATCH")


def review_gateway_network(w3, gateway, invitation, block):
    network = invitation["bitcoin_network"]
    if bytes(_read(gateway, "bitcoinGenesis", block)).hex() != genesis(network)[2:]:
        raise ValueError("BITCOIN_CUSTODY_NETWORK_MISMATCH")
    for source in invitation["bitcoin_custody"]["sources"]:
        review_source(w3, source, network, block)


def require_enrolled_legacy(w3, gateway, kind, issuer, op, block):
    """Allow only a historical gateway already bound by the issuer's consumed approval."""
    code = bytes(w3.eth.get_code(gateway.address, block_identifier=block))
    if not known_legacy_gateway_runtime(code, kind):
        raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
    auth = _node(w3, _read(issuer, "authorization", block), "DKGAuthorization", block)
    if (_read(issuer, "custodyTransfer", block) != gateway.address
            or bytes(_read(issuer, "getSetCustodyTransferOperationHash", block, gateway.address)) != op
            or not _read(auth, "executedOperations", block, op) or _read(auth, "operationTargets", block, op) != issuer.address):
        raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")
