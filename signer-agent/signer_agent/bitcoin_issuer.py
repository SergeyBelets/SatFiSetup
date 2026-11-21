"""Identify proof issuers while preserving the federation MP and confederation S boundaries."""
def kind(node):
    return ("ConfederationBitcoinReserve" if any(
        item.get("name") == "confederationPool" for item in getattr(node, "abi", ())) else "MiningPoolV1")
