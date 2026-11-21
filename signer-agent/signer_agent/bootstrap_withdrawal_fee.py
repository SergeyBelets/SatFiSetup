"""Opt-in public fee policy and its ordinary, journalled bootstrap steps."""
import re
from web3 import Web3

FIELDS = {"version", "derivation_source_hash", "qualification_commitment", "service_policy_hash",
          "protocol_fee_cap_sats", "max_vbytes", "max_packet_bytes"}


def validate(policy):
    if not isinstance(policy, dict) or policy.get('version') not in ('1','2') or set(policy) != (FIELDS | ({'family_root','family_enrollment'} if policy.get('version')=='2' else set())):
        raise ValueError("WITHDRAWAL_BOOTSTRAP_POLICY")
    if policy['version']=='2':
        from .family_registry import validate_enrollment
        validate_enrollment(policy['family_root'])
        if policy['family_enrollment'] not in ('ORIGINAL_AUTHORITY','PRE_ENROLLED'):raise ValueError('WITHDRAWAL_BOOTSTRAP_POLICY')
        if policy['family_root']['version']=='create-1' and policy['family_enrollment']!='ORIGINAL_AUTHORITY':raise ValueError('WITHDRAWAL_BOOTSTRAP_POLICY')
    for field in ("derivation_source_hash", "qualification_commitment", "service_policy_hash"):
        value = policy[field]
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-f]{64}", value) or int(value, 16) == 0:
            raise ValueError("WITHDRAWAL_BOOTSTRAP_POLICY")
    from .btc_fee_reserve import number
    number(policy["protocol_fee_cap_sats"], 1, 2100000000000000)
    number(policy["max_vbytes"], 100, 100000)
    number(policy["max_packet_bytes"], 512, 4096)
    return policy


def genesis(network):
    from .bitcoin_network import PUBLIC_NETWORKS
    return "0x" + (PUBLIC_NETWORKS[network][1] if network in PUBLIC_NETWORKS else
                   "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206")


def extend_address(p, deploy, approve):
    policy = validate(p["withdrawal_fee"])
    from .bootstrap import artifact
    steps = p["steps"]
    deploy("address_derivation", "BitcoinDepositDerivation")
    runtime = Web3.keccak(hexstr=artifact("BitcoinDepositDerivation")["deployedBytecode"]["object"]).hex()
    runtime = "0x" + runtime.removeprefix("0x")
    deploy("address_registry", "BitcoinAddressRegistry", "$deposits", genesis(p["bitcoin_network"]),
           "$address_derivation", runtime, policy["derivation_source_hash"],
           policy["qualification_commitment"], policy["service_policy_hash"])
    steps[-1]['outputs']=[dict(name='recipient_policy',contract='address_registry',method='recipientPolicy',args=[],index=[],artifact='RecipientPolicy')]
    steps[-1]["checks"].extend([
        dict(contract="address_registry", method=m, args=[], equals=v) for m, v in (
            ("deposits", "$deposits"), ("verifier", "$address_derivation"),
            ("bitcoinGenesis", genesis(p["bitcoin_network"])), ("verifierRuntimeHash", runtime),
            ("verifierSourceHash", policy["derivation_source_hash"]),
            ("qualificationCommitment", policy["qualification_commitment"]),
            ("servicePolicyHash", policy["service_policy_hash"]))])
    if policy['version']=='2':
        root=policy['family_root']
        if root['version']=='create-1':
            deploy('family_root','BitcoinKeyFamilyRegistry','$conf_auth')
            steps[-1]['checks'].extend([dict(contract='family_root',method=m,args=[],equals=v) for m,v in
                (('governance','$conf_auth'),('dkg','$dkg'),('coordinator','$coordinator'),('chainId',p['chain_id']))])
        else:
            if root['chain_id']!=p['chain_id']:raise ValueError('FAMILY_ROOT_CHAIN')
            p.setdefault('contracts',{})['family_root']=dict(address=root['address'],artifact='BitcoinKeyFamilyRegistry',runtime_hash=root['runtime_hash'])
        if policy['family_enrollment']=='ORIGINAL_AUTHORITY':
            steps.append(dict(id='family_authority',kind='read',outputs=[],checks=[
                dict(contract='family_root',method='governance',args=[],equals='$conf_auth'),
                dict(contract='family_root',method='dkg',args=[],equals='$dkg')]))
            approve('enroll_key_family','family_root','getEnrollOperationHash',['$address_registry'],'enroll',['$address_registry'])
            for s in steps:
                if s.get('id')=='enroll_key_family_publish_intent':
                    s['args'][1]['$json_hex']['artifact']='BitcoinKeyFamilyRegistry'
        else:
            # The original root authority enrolled this new independent graph
            # separately. Never substitute this graph's EVM_AUTH key for it.
            steps.append(dict(id='family_base',kind='read',outputs=[dict(name='family_base',contract='address_registry',method='baseX',args=[],index=[])]))
            steps.append(dict(id='family_id',kind='read',outputs=[dict(name='family_id',contract='family_root',method='familyId',args=[genesis(p['bitcoin_network']),'$family_base'],index=[])]))
            steps.append(dict(id='family_enrollment',kind='wait',contract='family_root',method='registryFamily',
                args=['$address_registry'],equals='$family_id',pending_status='FAMILY_ENROLLMENT_REQUIRED'))
        approve('bind_key_family','address_registry','getBindFamilyRegistryOperationHash',['$family_root'],'bindFamilyRegistry',['$family_root'])
    approve("bind_address_registry", "deposits", "getSetAddressRegistryOperationHash",
            ["$address_registry"], "setAddressRegistry", ["$address_registry"])
    if "system_bootstrap" in p:
        deploy('address_message_board', 'BitcoinAddressMessageBoard', '$address_registry')
        steps[-1]['checks'].extend([dict(contract='address_message_board', method=m, args=[], equals=v)
            for m, v in (('registry', '$address_registry'), ('authorization', '$conf_auth'))])
    deploy('incident_board','BitcoinIncidentMessageBoard','$address_registry')
    steps[-1]['checks'].extend([dict(contract='incident_board',method=m,args=[],equals=v)
        for m,v in (('registry','$address_registry'),('authorization','$conf_auth'))])


def extend(p, deploy, approve, output):
    policy = validate(p["withdrawal_fee"])
    from .btc_fee_client import bootstrap_plan as reserve_plan
    steps = p["steps"]
    deploy("signing_board", "BitcoinSigningBoard", "$batches", policy["max_packet_bytes"])
    steps[-1]["checks"].extend([dict(contract="signing_board", method=m, args=[], equals=v)
        for m, v in (("batchRegistry", "$batches"), ("dkg", "$dkg"),
                     ("walletId", p["btc_ceremony"]["id"]), ("maxPacketBytes", policy["max_packet_bytes"]))])
    # Use the existing public reserve recipe, with graph references resolved by
    # the main bootstrap instead of manually supplied deployment addresses.
    placeholder = Web3.to_checksum_address("0x" + "11" * 20)
    for s in reserve_plan(placeholder, placeholder, policy["protocol_fee_cap_sats"], policy["max_vbytes"])["steps"]:
        args = s["args"]
        if s["id"] == "fee_reserve":
            args = ["$address_registry", "$signing_board", *args[2:]]
        deploy(s["id"], s["artifact"], *args)
    steps[-1]["outputs"] = [output("fee_user_module", "fee_reserve", "userModule", art="BitcoinFeeReserveUser")]
    steps[-1]["checks"].extend([dict(contract="fee_reserve", method=m, args=[], equals=v)
        for m, v in (("addresses", "$address_registry"), ("signingBoard", "$signing_board"),
                     ("protocolFeeCap", policy["protocol_fee_cap_sats"]), ("maxVbytes", policy["max_vbytes"]))])
    approve("bind_fee_reserve", "batches", "getSetFeeReserveOperationHash",
            ["$fee_reserve"], "setFeeReserve", ["$fee_reserve"])
    deploy("withdrawal_plan_verifier", "WithdrawalFeePlanVerifier")
    deploy("withdrawal_derivation", "BitcoinWithdrawalFeeDerivation")
    for name, art, args in (
        ("withdrawal_payments", "WithdrawalFeePayments", ["$address_registry", "$withdrawal_credits", "$withdrawal_plan_verifier"]),
        ("withdrawal_funding", "WithdrawalFeeFunding", ["$address_registry", "$withdrawal_credits", "$withdrawal_derivation"]),
        ("withdrawal_credits", "WithdrawalFeeCredits", ["$address_registry", "$withdrawal_payments", "$withdrawal_funding"])):
        deploy(name, art, *args)
        steps[-1].update(create_group="withdrawal-fee-constructors", gas="8000000")
        steps[-1]["checks"] = [dict(contract=name, method=m, args=[], equals=v) for m, v in
            ([("addresses", args[0]), ("credits", args[1]), ("verifier", args[2])] if name != "withdrawal_credits" else
             [("addresses", args[0]), ("payments", args[1]), ("funding", args[2])])]
    approve("bind_withdrawal_fees", "escrow", "getSetWithdrawalFeesOperationHash",
            ["$withdrawal_credits"], "setWithdrawalFees", ["$withdrawal_credits"])
    steps[-1]["checks"] = [dict(contract="escrow", method="withdrawalFees", args=[], equals="$withdrawal_credits")]


def manifest(bootstrap):
    names = ("address_registry", "recipient_policy", "incident_board", "signing_board", "fee_reserve", "fee_user_module",
             "withdrawal_payments", "withdrawal_funding", "withdrawal_credits")
    policy=bootstrap.profile['withdrawal_fee']
    if policy['version']=='2':names=(*names,'family_root')
    if 'system_bootstrap' in bootstrap.profile:names=(*names,'address_message_board')
    contracts = bootstrap.contracts
    if not all(n in contracts for n in (*names, "mining")):
        result=dict(version="1",status="BOOTSTRAP_PENDING",ready=False)
        if policy['version']=='2' and all(k in contracts for k in ('family_root','conf_auth','dkg','coordinator')):
            address=contracts['family_root']['address']
            result['family_root_policy']=dict(version='1',chain_id=bootstrap.profile['chain_id'],address=address,
                initial_authority=contracts['conf_auth']['address'],dkg=contracts['dkg']['address'],coordinator=contracts['coordinator']['address'],
                runtime_hash='0x'+Web3.keccak(bootstrap.w3.eth.get_code(address,block_identifier=bootstrap.read_number())).hex().removeprefix('0x'))
            if policy['family_root']['version']!='create-1':
                result['family_root_policy']=dict(policy['family_root'])
            result['status']='CANONICAL_FAMILY_ROOT_PIN_REQUIRED'
        return result
    n = bootstrap.read_number()
    family_bound=False;root_policy=None
    if policy['version']=='2':
        r=bootstrap.contract('address_registry');root=bootstrap.contract('family_root')
        family_bound=(r.functions.familyRegistry().call(block_identifier=n)==root.address
            and bytes(root.functions.registryFamily(r.address).call(block_identifier=n))!=bytes(32))
        root_policy=policy['family_root'] if policy['family_root']['version']!='create-1' else dict(version='1',chain_id=bootstrap.profile['chain_id'],
            address=root.address,initial_authority=bootstrap.contracts['conf_auth']['address'],dkg=bootstrap.contracts['dkg']['address'],
            coordinator=bootstrap.contracts['coordinator']['address'],runtime_hash='0x'+Web3.keccak(bootstrap.w3.eth.get_code(root.address,block_identifier=n)).hex().removeprefix('0x'))
    bound = (bootstrap.contract("escrow").functions.withdrawalFees().call(block_identifier=n) == contracts["withdrawal_credits"]["address"]
             and bootstrap.contract("batches").functions.feeReserve().call(block_identifier=n) == contracts["fee_reserve"]["address"]
             and bootstrap.contract("deposits").functions.addressRegistry().call(block_identifier=n) == contracts["address_registry"]["address"]
             and bootstrap.contract("address_registry").functions.recipientPolicy().call(block_identifier=n) == contracts["recipient_policy"]["address"])
    bound=bound and family_bound
    if 'system_bootstrap' in bootstrap.profile:
        board=bootstrap.contract('address_message_board')
        bound=bound and (board.functions.registry().call(block_identifier=n)==contracts['address_registry']['address']
            and board.functions.authorization().call(block_identifier=n)==bootstrap.contract('address_registry').functions.authorization().call(block_identifier=n))
    route = {contracts["mining"]["address"]: contracts["withdrawal_credits"]["address"]}
    return dict(version="1", bindings={n: contracts[n]["address"] for n in names},
                continuity=dict(profile='ORIGINAL_BTC_ACCOUNTING_GRAPH_V1',canonical_inventory_required=True,
                    cross_graph_migration_supported=False,global_key_family_qualification=False,canonical_family_root_bound=family_bound,
                    independent_pin_required=True,root_policy=root_policy),
                worker=dict(withdrawal_fees=route if bound else {}), transport=dict(withdrawal_fees=route if bound else {}),
                bindings_confirmed=bound, status="DEPLOYED_REQUIRES_QUALIFICATION_AND_E2E" if bound else "BINDING_PENDING", ready=False)


def review_enrolled(ctx, invite, number):
    """A canonical graph must also match the economic/provenance enrollment."""
    if "withdrawal_fee" not in invite:
        return  # Older separately reviewed profiles remain usable.
    p = validate(invite["withdrawal_fee"])
    if p['version']=='2':
        from .family_registry import inspect,enrolled_pin
        pin=enrolled_pin(ctx['registry'].w3,ctx['registry'],p['family_root'],number)
        root=inspect(ctx['registry'].w3,pin,number)
        if ctx['registry'].functions.familyRegistry().call(block_identifier=number)!=root.address:raise ValueError('FAMILY_ROOT_GRAPH')
    from .btc_fee_reserve import read
    r, f, b = ctx["registry"], ctx["fee"], ctx["board"]
    expected = ((r, "verifierSourceHash", bytes.fromhex(p["derivation_source_hash"][2:])),
                (r, "qualificationCommitment", bytes.fromhex(p["qualification_commitment"][2:])),
                (r, "servicePolicyHash", bytes.fromhex(p["service_policy_hash"][2:])),
                (r, "bitcoinGenesis", bytes.fromhex(genesis(invite["bitcoin_network"])[2:])),
                (f, "protocolFeeCap", int(p["protocol_fee_cap_sats"])),
                (f, "maxVbytes", int(p["max_vbytes"])), (b, "maxPacketBytes", int(p["max_packet_bytes"])))
    if any(read(node, number, method) != value for node, method, value in expected):
        raise ValueError("WITHDRAWAL_BOOTSTRAP_POLICY")
