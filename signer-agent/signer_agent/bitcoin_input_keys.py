"""Public per-input BTC keys and generation fences. No separate nonce namespace."""
from hashlib import sha256
from eth_utils import keccak
from web3 import Web3

from . import crypto
from .bootstrap import artifact
from .deposit_intent import DepositIntentV1, WALLET_DOMAIN, PROFILE as DERIVATION_PROFILE

PROFILE = "BTC_INPUT_KEYS_V1"
PLAN_FIELDS = {"address_registry", "custody_generation", "derivation_generation", "input_keys", "input_provenance"}


def versioned(plan):
    return plan.get("version") in ("3","4")


def validate_schema(plan):
    """Only public canonical descriptors; never accept arbitrary tweaks/packages."""
    from .bitcoin_onchain_plan import address, decimal, hex_value
    address(plan["address_registry"])
    decimal(plan["custody_generation"], 2**256-1, positive=True)
    decimal(plan["derivation_generation"], 2**256-1)
    if (not isinstance(plan["input_keys"], list) or not isinstance(plan["input_provenance"], list)
            or len(plan["input_keys"]) != len(plan["inputs"]) or len(plan["input_provenance"]) != len(plan["inputs"])):
        raise ValueError("BTC_INPUT_KEY_SCHEMA")
    derived = False
    family = None
    for item, desc, provenance in zip(plan["inputs"], plan["input_keys"], plan["input_provenance"]):
        if not isinstance(desc, dict) or type(desc.get("version")) is not int or desc["version"] != 1:
            raise ValueError("BTC_INPUT_KEY_SCHEMA")
        base_fields = {"version", "mode", "base_group", "wallet_context"}
        if desc.get("mode") not in ("BASE", "DERIVED", "WITHDRAWAL_FEE") or set(desc) != (
                base_fields if desc["mode"] == "BASE" else base_fields | {"intent", "binding_commitment"}):
            raise ValueError("BTC_INPUT_KEY_SCHEMA")
        group = hex_value(desc["base_group"], 33)
        context = hex_value(desc["wallet_context"], 32)
        if group[0] != 2 or context == bytes(32) or plan["wallet_script"] != "5120"+group[1:].hex():
            raise ValueError("BTC_INPUT_KEY_BINDING")
        if family is not None and family != (group, context): raise ValueError("BTC_INPUT_KEY_BINDING")
        family = (group, context)
        proof = hex_value(provenance, 32)
        if desc["mode"] == "BASE":
            if item["script"] != plan["wallet_script"] or proof != bytes(32): raise ValueError("BTC_INPUT_KEY_BINDING")
        else:
            derived = True
            if desc['mode']=='WITHDRAWAL_FEE':
                from .withdrawal_fee_intent import WithdrawalFeeIntentV1,DOMAIN
                if plan['version']!='4':raise ValueError('WITHDRAWAL_FEE_PLAN_REQUIRED')
                fee_intent=WithdrawalFeeIntentV1.decode(hex_value(desc['intent'],len(DOMAIN)+478));intent=fee_intent.parent
                calculated=fee_intent.derive(group)
            else:
                intent = DepositIntentV1.decode(hex_value(desc["intent"], 382));calculated=intent.derive(group)
            if (Web3.to_checksum_address(intent.deposit_registry) != plan["address_registry"]
                    or intent.wallet_context != context or proof == bytes(32)):
                raise ValueError("BTC_INPUT_KEY_BINDING")
            if calculated["input_key"] != desc or calculated["script_pubkey"] != item["script"]:
                raise ValueError("BTC_INPUT_KEY_BINDING")
    if (int(plan["derivation_generation"]) > 0) != derived:
        raise ValueError("BTC_INPUT_GENERATION_CHANGED")


def build(plan):
    args = {k: plan[k] for k in ("inputs", "outputs", "wallet_script", "fee", "max_fee")}
    if versioned(plan): args.update(input_key_version=1, input_keys=plan["input_keys"])
    return crypto.call("bitcoin_build_v1" if versioned(plan) else "bitcoin_build", **args)


def key_args(plan, index, result):
    return dict(input_key=plan["input_keys"][index], public_package=result["public_package"])


def _read(node, n, method, *args):
    return getattr(node.functions, method)(*args).call(block_identifier=n)


def _contract(w3, address, kind):
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def wallet(manifest, key, registry, graph, number):
    """Registry identity/runtime/constructor were authenticated by fresh graph inspection."""
    if graph.get("usage") != PROFILE:
        raise ValueError("BTC_ADDRESS_SIGNING_PROFILE_REQUIRED")
    r = _contract(registry.w3, graph["contracts"]["address_registry"]["address"], "BitcoinAddressRegistry")
    common = _contract(registry.w3, graph["contracts"]["deposits"]["address"], "BitcoinDepositRegistry")
    read = lambda method: _read(r, number, method)
    if (bytes(read("walletId")) != manifest.session_id or bytes(read("walletResult")).hex() != key["result_hash"]
            or "02"+bytes(read("baseX")).hex() != key["result"]["group"]
            or read("dkg") != manifest.registry or read("deposits") != common.address
            or _read(common, number, "addressRegistry") != r.address):
        raise ValueError("BTC_INPUT_KEY_BINDING")
    context = sha256(WALLET_DOMAIN + bytes(read("bitcoinGenesis")) + manifest.chain_id.to_bytes(32,"big")
        + bytes.fromhex(r.address[2:]) + bytes(read("graphId")) + bytes.fromhex(manifest.registry[2:])
        + manifest.session_id + bytes(read("walletResult")) + int(read("keyEpoch")).to_bytes(8,"big")).digest()
    return r, common, context


def resolve(manifest, key, registry, graph, item, number):
    """Resolve only canonical registrations, never an operator-supplied output key."""
    from .address_mint import configuration, binding
    r, common, context = wallet(manifest, key, registry, graph, number)
    base = dict(version=1, mode="BASE", base_group=key["result"]["group"], wallet_context=context.hex())
    script_hash = keccak(bytes.fromhex(item["script"]))
    identifier = bytes(_read(common, number, "addressIntents", script_hash))
    if item["script"] == "5120"+key["result"]["group"][2:]:
        if identifier != bytes(32) or int(_read(common, number, "scriptPools", script_hash),16) == 0:
            raise ValueError("BTC_INPUT_KEY_BINDING")
        return base, bytes(32).hex()
    if graph.get('withdrawal_fees'):
        fee_id=bytes(_read(common,number,'feeScripts',script_hash))
        if fee_id!=bytes(32):
            from .withdrawal_fee import for_graph,funding_binding
            ctx=for_graph(registry,graph,number);intent,descriptor,provenance=funding_binding(ctx,fee_id,number)
            if descriptor['script_pubkey']!=item['script']:raise ValueError('WITHDRAWAL_FEE_BINDING')
            return descriptor['input_key'],provenance.hex()
    if identifier == bytes(32): raise ValueError("BTC_INPUT_KEY_BINDING")
    row = _read(r, number, "getBinding", identifier)
    pool = _contract(registry.w3, row[1], "MiningPoolV1")
    c = configuration(registry.w3, pool, number)
    if c["registry"].address != r.address or c["common"].address != common.address:
        raise ValueError("BTC_INPUT_KEY_BINDING")
    b = binding(c, identifier)
    derived = b["intent"].derive(bytes.fromhex(base["base_group"]))
    if b["script"].hex() != item["script"] or derived["wallet_context"] != context.hex():
        raise ValueError("BTC_INPUT_KEY_BINDING")
    return derived["input_key"], b["provenance"].hex()


def fence(manifest, key, plan, registry, graph, number):
    r, _, _ = wallet(manifest, key, registry, graph, number)
    if (r.address != plan["address_registry"]
            or int(_read(r, number, "custodyGeneration")) != int(plan["custody_generation"])
            or int(_read(r, number, "custodyState")) not in (0,1)):
        raise ValueError("BTC_INPUT_GENERATION_CHANGED")
    if int(plan["derivation_generation"]):
        if (int(_read(r, number, "derivationGeneration")) != int(plan["derivation_generation"])
                or int(_read(r, number, "derivationState")) != 0):
            raise ValueError("BTC_INPUT_GENERATION_CHANGED")
        if any(d['mode']=='DERIVED' for d in plan['input_keys']):
            verifier = _contract(registry.w3, _read(r, number, "verifier"), "BitcoinDepositDerivation")
            if (keccak(bytes(registry.w3.eth.get_code(verifier.address,block_identifier=number))) != bytes(_read(r,number,"verifierRuntimeHash"))
                    or bytes(_read(verifier,number,"PROFILE")) != keccak(text=DERIVATION_PROFILE)
                    or _read(verifier, number, "QUALIFIED") is not True):
                raise ValueError("BTC_ADDRESS_VERIFIER_UNQUALIFIED")
        if any(d['mode']=='WITHDRAWAL_FEE' for d in plan['input_keys']):
            from .withdrawal_fee import for_graph
            from .withdrawal_fee_intent import PROFILE as FEE_PROFILE
            fee=for_graph(registry,graph,number)['withdrawal_fees'];v=fee['verifier'];f=fee['funding']
            if (_read(v,number,'FEE_QUALIFIED') is not True or bytes(_read(v,number,'FEE_PROFILE'))!=keccak(text=FEE_PROFILE)
                    or keccak(bytes(registry.w3.eth.get_code(v.address,block_identifier=number)))!=bytes(_read(f,number,'verifierRuntimeHash'))):
                raise ValueError('WITHDRAWAL_FEE_VERIFIER_UNQUALIFIED')


def review(manifest, key, plan, registry, graph, number):
    """Immutable input/binding review also applies to cached public retransmissions."""
    from .bitcoin_onchain_plan import validate_plan
    validate_plan(plan, manifest, allow_commitments=True)
    if not versioned(plan): raise ValueError("BTC_ADDRESS_SIGNING_PLAN_REQUIRED")
    fence(manifest, key, plan, registry, graph, number)
    for i, item in enumerate(plan["inputs"]):
        descriptor, provenance = resolve(manifest, key, registry, graph, item, number)
        if descriptor != plan["input_keys"][i] or provenance != plan["input_provenance"][i]:
            raise ValueError("BTC_INPUT_KEY_BINDING")


def guard(manifest, key, plan, registry, graph, state=None):
    """Check confirmed and latest status; retain monotonic local incident evidence."""
    if not versioned(plan):
        if graph.get("usage") == PROFILE: raise ValueError("BTC_ADDRESS_SIGNING_PLAN_REQUIRED")
        return
    from .epoch_service import signing_guard
    signing_guard(manifest,registry,state,graph,plan)
    from .bitcoin_trust import confirmed_block
    block = confirmed_block(registry.w3, graph)
    review(manifest, key, plan, registry, graph, block.number)
    latest = registry.w3.eth.get_block("latest")
    fence(manifest, key, plan, registry, graph, latest.number)
    if (registry.w3.eth.get_block(block.number).hash != block.hash
            or registry.w3.eth.get_block(latest.number).hash != latest.hash):
        raise ValueError("BTC_INPUT_SNAPSHOT_REORG")
    from .bitcoin_continuity import signing_guard as continuity_guard
    continuity_guard(manifest,registry,state,graph,plan)
    if state is not None:
        name = "btc-input-generation:"+manifest.context.hex()
        old = state.get(name)
        cg, dg = int(plan["custody_generation"]), int(plan["derivation_generation"])
        if old and (old["registry"] != plan["address_registry"]
                or cg < int(old["custody"]) or (dg and dg < int(old["derivation"]))
                or registry.w3.eth.get_block(int(old["number"])).hash.hex() != old["hash"]):
            raise ValueError("BTC_INPUT_RECOVERY_REQUIRED")
        # BASE-only plans do not erase a previously observed DERIVATION generation.
        state.put(name, dict(registry=plan["address_registry"], custody=str(cg),
            derivation=str(max(dg, int(old["derivation"]) if old else 0)),
            number=str(latest.number), hash=latest.hash.hex()))


def prepare(manifest, key, registry, graph, plan, number):
    r, _, _ = wallet(manifest, key, registry, graph, number)
    pairs = [resolve(manifest,key,registry,graph,item,number) for item in plan["inputs"]]
    plan.update(version="4" if plan.get('version')=='4' else "3", address_registry=r.address, input_keys=[x[0] for x in pairs],
        input_provenance=[x[1] for x in pairs], custody_generation=str(_read(r,number,"custodyGeneration")),
        derivation_generation=str(_read(r,number,"derivationGeneration")) if any(x[0]["mode"]!='BASE' for x in pairs) else "0")
    guard(manifest,key,plan,registry,graph)
    return plan


def recover(manifest, key, plan, registry, graph, state):
    """Explicitly re-anchor public generation observations; never reset signing state."""
    if not versioned(plan): raise ValueError("BTC_ADDRESS_SIGNING_PLAN_REQUIRED")
    with state.exclusive():
        if state.get("btc-payments-recovery:"+manifest.context.hex()):
            raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
        guard(manifest,key,plan,registry,graph)
        name="btc-input-generation:"+manifest.context.hex();old=state.get(name)
        latest=registry.w3.eth.get_block("latest")
        r,_,_=wallet(manifest,key,registry,graph,latest.number)
        cg=int(_read(r,latest.number,"custodyGeneration"));dg=int(_read(r,latest.number,"derivationGeneration"))
        if old and (old["registry"]!=r.address or cg<int(old["custody"]) or dg<int(old["derivation"])):
            raise ValueError("BTC_INPUT_RECOVERY_REQUIRED")
        # Validate the common journal identity even when this plan has no local
        # nonce yet. An interrupted signing record is never cleared here.
        with state._journal(): pass
        for i in range(len(plan["inputs"])):
            sid="btc:"+manifest.context.hex()+":"+plan["batch_id"]+":"+str(i)
            saved=state.get(sid)
            if saved:
                from .bitcoin_onchain_plan import onchain_plan_hash
                if saved.get("plan_hash")!=onchain_plan_hash(manifest,plan).hex():
                    raise ValueError("NONCE_PLAN_MISMATCH")
                if saved.get("status")=="share-prepared":state.assert_spent(sid,saved["transcript"])
                elif saved.get("status")=="nonce-created":state.assert_unspent(sid)
                else:raise ValueError("BTC_CACHED_SIGNING_STATE_INVALID")
        fence(manifest,key,plan,registry,graph,latest.number)
        if registry.w3.eth.get_block(latest.number).hash!=latest.hash:raise ValueError("BTC_INPUT_SNAPSHOT_REORG")
        current=dict(registry=r.address,custody=str(cg),derivation=str(dg),number=str(latest.number),hash=latest.hash.hex())
        receipt_key="btc-input-generation-recovered:"+manifest.context.hex()+":"+latest.hash.hex()
        receipt=state.get(receipt_key)
        if receipt and receipt["current"]!=current:raise ValueError("BTC_INPUT_RECOVERY_REQUIRED")
        if not receipt:state.put(receipt_key,dict(previous=old,current=current))
        state.put(name,current)
        return dict(status="BTC_INPUT_GENERATIONS_RECOVERED",batch=plan["batch_id"],custody_generation=str(cg),derivation_generation=str(dg))
