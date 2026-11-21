"""Proofs derived from Bitcoin Core raw blocks, with explicit wire byte order."""

import hashlib


def sha256d(raw):
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def inclusion(bitcoin, txid):
    bitcoin.check_network()
    transaction = bitcoin.call("getrawtransaction", txid, True)
    if "blockhash" not in transaction:
        raise ValueError("PAYMENT_NOT_CONFIRMED")
    block = bitcoin.call("getblock", transaction["blockhash"], 1)
    index = block["tx"].index(txid)
    position = index
    nodes = [bytes.fromhex(t)[::-1] for t in block["tx"]]
    siblings = []
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        siblings.append(nodes[index ^ 1].hex())
        index //= 2
        nodes = [sha256d(nodes[i] + nodes[i + 1]) for i in range(0, len(nodes), 2)]
    if nodes[0] != bytes.fromhex(block["merkleroot"])[::-1]:
        raise ValueError("MERKLE_ROOT_MISMATCH")
    return dict(
        raw=transaction["hex"],
        txid=bytes.fromhex(txid)[::-1].hex(),
        block_hash=bytes.fromhex(transaction["blockhash"])[::-1].hex(),
        siblings=siblings,
        index=str(position),
        confirmations=str(block["confirmations"]),
    )
