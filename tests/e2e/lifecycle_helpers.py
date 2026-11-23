"""Real factory graph and Bitcoin proof helpers; no signature/proof mocks."""

import hashlib
import pytest
from web3 import Web3
from web3.exceptions import ContractCustomError
from signer_agent.bootstrap_formation import federation_formation_hash, share_token_hash
from test_dkg_processes import deploy, tx, artifact


def contract(w3, name, address):
    return w3.eth.contract(address=address, abi=artifact(name)["abi"])


def committed_deposit(wallet, address, bitcoin_amount, commitment):
    """Preserve the wallet output at vout 0 when funding an allocation-bound deposit."""
    raw = wallet.call("createrawtransaction", [], [{address: bitcoin_amount}, {"data": commitment}])
    funded = wallet.call("fundrawtransaction", raw, {"changePosition": 2, "fee_rate": 2})
    signed = wallet.call("signrawtransactionwithwallet", funded["hex"])
    assert signed["complete"]
    return wallet.call("sendrawtransaction", signed["hex"])


def tx_from(w3, function, sender):
    """Submit as an explicit unlocked test identity, preserving contract caller checks."""
    receipt = w3.eth.wait_for_transaction_receipt(function.transact({"from": sender}))
    assert receipt.status == 1
    return receipt


def federation(w3, registry, coordinator, m):
    leaves = {
        name: deploy(w3, name)
        for name in (
            "FROSTAuthorizationDeployer",
            "MultiPoolShareTokenDeployer",
            "FederationFeeManagerDeployer",
            "FeeDistributionCalculatorDeployer",
            "FederationNFTModifierDeployer",
            "ProjectWalletRegistryDeployer",
        )
    }
    fees = deploy(
        w3,
        "FeeSystemDeployer",
        *[
            leaves[x].address
            for x in (
                "FederationFeeManagerDeployer",
                "FeeDistributionCalculatorDeployer",
                "FederationNFTModifierDeployer",
                "ProjectWalletRegistryDeployer",
            )
        ]
    )
    simple = deploy(
        w3,
        "SimpleMultiPoolDeployer",
        leaves["FROSTAuthorizationDeployer"].address,
        leaves["MultiPoolShareTokenDeployer"].address,
    )
    for name in ("FROSTAuthorizationDeployer", "MultiPoolShareTokenDeployer"):
        tx(w3, leaves[name].functions.setAuthorizedDeployer(simple.address, True))
    orchestrator = deploy(w3, "FederationDeployer", simple.address, fees.address)
    tx(w3, simple.functions.setAuthorizedDeployer(orchestrator.address, True))
    factory = deploy(
        w3,
        "FederationFactory",
        registry.address,
        orchestrator.address,
        coordinator.address,
    )
    tx(w3, orchestrator.functions.setAuthorizedDeployer(factory.address, True))
    tx(w3, factory.functions.configureDeploymentFee("0x" + "00" * 20, 0, True))
    token_metadata = ("Integration Share", "IS")
    tx(w3, factory.functions.reserveFederationId(m.session_id, "integration-fed",
        federation_formation_hash(m.participants, m.threshold), share_token_hash(*token_metadata)))
    w3.provider.make_request("anvil_impersonateAccount", [m.participants[0]])
    receipt = w3.eth.wait_for_transaction_receipt(
        factory.functions.adoptFederationDKG(m.session_id, "integration-fed").transact(
            {"from": m.participants[0]}
        )
    )
    assert receipt.status == 1
    assert (
        factory.functions.deploymentInitiator(m.session_id).call() == m.participants[0]
    )
    with pytest.raises(ContractCustomError, match="ea8e4eb5"):
        factory.functions.deployFederation(m.session_id, "Attacker Share", "BAD").call(
            {"from": w3.eth.accounts[0]}
        )
    tx_from(
        w3,
        factory.functions.deployFederation(m.session_id, *token_metadata),
        m.participants[0],
    )
    config = factory.functions.getFederation("integration-fed").call()
    # Named ABI components, not hand-maintained tuple offsets.
    fields = next(x for x in factory.abi if x.get("name") == "getFederation")[
        "outputs"
    ][0]["components"]
    data = dict(zip([x["name"] for x in fields], config))
    auth = contract(w3, "DKGAuthorization", data["frostMultisig"])
    pool = contract(w3, "SimpleMultiPool", data["multiPool"])
    assert auth.functions.dkgId().call() == m.session_id
    assert bytes(auth.functions.resultHash().call()) == bytes(
        registry.functions.ceremonies(m.session_id).call()[3]
    )
    return factory, auth, pool


def relay_headers(w3, relay, btc):
    count = btc.call("getblockcount")
    start = (
        0
        if relay.functions.getMainchainHead().call() == bytes(32)
        else relay.functions.getMainchainHeight().call() + 1
    )
    for h in range(start, count + 1):
        block = btc.call("getblockhash", h)
        tx(
            w3,
            relay.functions.addBlockHeader(
                bytes.fromhex(btc.call("getblockheader", block, False))
            ),
        )


def proof(btc, txid):
    decoded = btc.call("getrawtransaction", txid, True)
    block = btc.call("getblock", decoded["blockhash"], 1)
    position = block["tx"].index(txid)
    index = position
    nodes = [bytes.fromhex(x)[::-1] for x in block["tx"]]
    siblings = []
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        siblings.append(nodes[index ^ 1])
        index //= 2
        nodes = [
            hashlib.sha256(hashlib.sha256(nodes[i] + nodes[i + 1]).digest()).digest()
            for i in range(0, len(nodes), 2)
        ]
    return (
        bytes.fromhex(decoded["hex"]),
        bytes.fromhex(decoded["blockhash"])[::-1],
        siblings,
        position,
    )
