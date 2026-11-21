"""Registered-address SPV needs. Transport hints never authorize mint or value."""
import re
from web3 import Web3
from .bootstrap import artifact
from . import address_mint

KIND="address_deposit"


def validate_profile(profile,allowed=None):
    if (not isinstance(profile,dict) or set(profile)!={"version","registries"} or profile["version"]!="1"
            or not isinstance(profile["registries"],dict) or not 1<=len(profile["registries"])<=4096):
        raise ValueError("SPV_ADDRESS_PROFILE")
    for pool,registry in profile["registries"].items():
        if (not Web3.is_checksum_address(pool) or not Web3.is_checksum_address(registry) or int(registry,16)==0
                or (allowed is not None and allowed.get(pool)!="MiningPoolV1")):raise ValueError("SPV_ADDRESS_PROFILE")


def scope(policy,need):
    if (not isinstance(need,dict) or set(need)!={"kind","consumer","intent_id","txid","vout"}
            or need["kind"]!=KIND
            or any(not isinstance(need[k],str) or not re.fullmatch(r"[0-9a-f]{64}",need[k]) for k in ("txid","intent_id"))
            or not isinstance(need["vout"],str) or not re.fullmatch(r"0|[1-9][0-9]*",need["vout"])
            or int(need["vout"])>2**32-1):raise ValueError("SPV_ADDRESS_NEED_SCHEMA")
    profile=policy.get("address_deposits")
    if profile is None:raise ValueError("BTC_ADDRESS_ZK_WORKER_PROFILE_REQUIRED")
    validate_profile(profile,policy["allowed_consumers"])
    pinned=profile["registries"].get(need["consumer"])
    if pinned is None:raise ValueError("SPV_ADDRESS_CONSUMER_NOT_ALLOWED")
    return pinned


def view(w3,policy,need,number):
    pinned=scope(policy,need);block=w3.eth.get_block(number)
    pool=w3.eth.contract(address=need["consumer"],abi=artifact("MiningPoolV1")["abi"])
    ctx=address_mint.configuration(w3,pool,block.number)
    if ctx["registry"].address!=pinned:raise ValueError("SPV_ADDRESS_REGISTRY_MISMATCH")
    identifier=bytes.fromhex(need["intent_id"])
    b=address_mint.binding(ctx,identifier)
    claim=address_mint.receipt_claim(ctx,identifier,bytes.fromhex(need["txid"])[::-1],int(need["vout"]))
    done=address_mint.complete(ctx,claim)
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError("ADDRESS_EVM_REORG")
    return ctx,b,claim,done


def inspect(worker,need,block):
    """Exact completion first; pending work checks native funding and both fences."""
    ctx,b,_,done=view(worker.w3,worker.c,need,block.number)
    if done:return dict(status="COMPLETED")
    try:claim=address_mint.funding(ctx,worker.bitcoin,need["txid"],int(need["vout"]),bytes.fromhex(need["intent_id"]),prove=False)
    except ValueError as exc:
        if str(exc)=="BTC_MINT_PROOF_OR_ALLOCATION":return dict(status="WAITING_BITCOIN")
        raise
    latest,_,_,done=view(worker.w3,worker.c,need,"latest")
    if done:return dict(status="COMPLETED")
    address_mint.validate_claim(latest,claim)
    return dict(context=ctx,script=b["script"],claim=claim,
        depth=int(address_mint.read(ctx["common"],ctx["number"],"depth")))


def before_spend(w3,policy,state,need,bitcoin,allowed):
    """Guard new unsigned admission/payment; exact signed recovery stays separate."""
    from .spv_automation import observation
    result,_=observation(w3,policy,state,need,bitcoin,allowed)
    if result["status"]!="NEEDS_STATE":raise ValueError("SPV_ADDRESS_WORK_NOT_REQUIRED")


def guard(w3,policy,state,need,bitcoin,allowed):
    if need.get("kind")!=KIND:return None
    return lambda:before_spend(w3,policy,state,need,bitcoin,allowed)


def check_graph(policy,graph,need):
    pinned=scope(policy,need)
    if (graph.get("bitcoin_network")!=policy['bitcoin_policy']['network'] or (graph.get("bitcoin_policy") or {}).get("version")!="2"
            or graph["contracts"].get("address_registry",{}).get("address")!=pinned
            or graph["contracts"].get("deposits",{}).get("address") is None):
        raise ValueError("SPV_ADDRESS_GRAPH_MISMATCH")
