"""Locally approve an independently reproduced on-chain bootstrap intent."""

import json
from eth_account import Account
from web3 import Web3
from .bootstrap import Bootstrap, artifact, encode_args
from .onboarding import read_private
from .onchain_transport import board_at, assembled_profile


ULR_METHODS = {
    'ULRCustodianSnapshots': {'getEnrollTreasuryOperationHash'},
    'ULRConfederationTreasury': {'getSpendOperationHash'},
    'CustodianValidator': {'getBindCandidateStakingOperationHash','getAddCustodianOperationHash','getRemoveCustodianOperationHash'},
    'CustodianDAODecisionBoard': {'getCreateOperationHash'},
}


def ulr_context(w3, intent, board_address, at, pin_file=None):
    """The custodian repeats the SAME typed/provenance review as FROST signers."""
    from . import ulr_treasury as policy
    from .onchain_transport import ceremony_bundle
    kind=intent['artifact']
    if intent.get('hash_method') not in ULR_METHODS[kind]:raise ValueError('CUSTODIAN_TARGET_UNSUPPORTED')
    bundle=ceremony_bundle(w3,board_address,'conf_ceremony')
    if 'ulr_bootstrap' in intent:
        from .bootstrap_ulr import enrolled_pin
        pin=enrolled_pin(w3,intent,bundle,at)
        if pin_file and policy.load_pin(pin_file)!=pin:raise ValueError('ULR_BOOTSTRAP_PIN_MISMATCH')
    elif pin_file:pin=policy.load_pin(pin_file)
    else:raise ValueError('ULR_PUBLIC_INTENT_AND_PIN_REQUIRED')
    reviewed=policy.review(w3,intent,bundle,at,pin)
    treasury=(reviewed['arguments'][2] if kind=='ULRCustodianSnapshots' else intent['target']
        if kind=='ULRConfederationTreasury' else intent['ulr_treasury'])
    *_,conf=policy.treasury_context(w3,pin,treasury,at)
    if reviewed['authority']!=conf[7]:raise ValueError('ULR_SIGNING_AUTHORITY')
    return conf[7],conf[8],reviewed


def approve(a):
    w3 = Web3(Web3.HTTPProvider(a.rpc))
    board = board_at(w3, a.board)
    profile = assembled_profile(w3, a.board)
    from .evm_network import read_block
    from . import fee_sale_governance as fee_sale
    sale_pin = fee_sale.load_pin(a.fee_sale_config) if getattr(a, "fee_sale_config", None) else None
    confirmations = max(int(profile.get("evm_confirmations", "1")), int(sale_pin["confirmations"]) if sale_pin else 1)
    block = read_block(w3, confirmations)
    read_number = block.number

    def check_snapshot():
        if bytes(w3.eth.get_block(read_number).hash) != bytes(block.hash):
            raise ValueError("EVM_SIGNING_SNAPSHOT_REORG")
    session = int(a.session)
    intent = json.loads(bytes(board.functions.intents(session).call(block_identifier=read_number)).decode())
    op = bytes.fromhex(a.operation_hash.removeprefix("0x"))
    if intent["operation_hash"].removeprefix("0x") != op.hex() or intent[
        "chain_id"
    ] != str(w3.eth.chain_id):
        raise ValueError("INTENT_MISMATCH")
    kind = intent["artifact"]
    if kind not in ULR_METHODS and kind not in (
        "ConfederationMultiPool",
        "ConfederationPoolV2",
        "ConfederationDAOIncome",
        "ConfederationNFTPass",
        "FederationFeeManager",
        "ConfederationFactory",
        "FundedFeeSale",
    ):
        raise ValueError("CUSTODIAN_TARGET_UNSUPPORTED")
    target = w3.eth.contract(address=intent["target"], abi=artifact(kind)["abi"])
    from .onchain_transport import ceremony_bundle
    from .bootstrap_confederation_dao import enabled as dao_enabled, review as review_dao

    dao_scope = None
    sale_review = None
    if fee_sale.matches(intent):
        if sale_pin is None or sale_pin["role"] != "conf_ceremony":
            raise ValueError("FEE_SALE_PUBLIC_INTENT_AND_PIN_REQUIRED")
        sale_bundle = ceremony_bundle(w3, a.board, "conf_ceremony")
        sale_review = fee_sale.review(w3, intent, sale_bundle, read_number, sale_pin)
        dao_scope = sale_review["authority"].address, sale_review["validator"]
    elif dao_enabled(profile):
        dao_scope = review_dao(w3, intent, ceremony_bundle(w3, a.board, "conf_ceremony"), read_number)
    ulr_review = None
    if dao_scope is not None:
        authorization, validator = dao_scope
    elif kind in ULR_METHODS:
        authorization,validator,ulr_review=ulr_context(w3,intent,a.board,read_number,getattr(a,'ulr_config',None))
    elif kind == "ConfederationFactory":
        conf = target.functions.getConfederation(intent["hash_args"][0]).call(block_identifier=read_number)
        authorization, validator = conf[7:9]
    else:
        validator = target.functions.custodianValidator().call(block_identifier=read_number)
        authorization = target.functions.frostMultisig().call(block_identifier=read_number)
    auth = w3.eth.contract(
        address=authorization, abi=artifact("DKGAuthorization")["abi"]
    )
    if auth.functions.coordinator().call(block_identifier=read_number) != intent["coordinator"]:
        raise ValueError("COORDINATOR_MISMATCH")
    coordinator = w3.eth.contract(
        address=intent["coordinator"], abi=artifact("FROSTCoordinator")["abi"]
    )
    if (
        coordinator.functions.dkgRegistry().call(block_identifier=read_number) != board.functions.registry().call(block_identifier=read_number)
        or bytes(coordinator.functions.sessionDKGId(session).call(block_identifier=read_number)).hex()
        != profile["conf_ceremony"]["id"][2:]
    ):
        raise ValueError("SESSION_DKG_MISMATCH")
    from .operation_status import executed_operation, verify_executed_session
    if any(item is not None and item["complete"] for item in (ulr_review, sale_review)):
        verify_executed_session(auth, coordinator, target.address, op, session, read_number)
        check_snapshot()
        return dict(status="ALREADY_EXECUTED", session=str(session),
                    operation_hash=op.hex(), target=target.address)
    if ulr_review is None and sale_review is None and executed_operation(w3, target, kind, intent, read_number, session=session,
                                                operator=profile.get("operator")):
        check_snapshot()
        return dict(status="ALREADY_EXECUTED", session=str(session),
                    operation_hash=op.hex(), target=target.address)
    if dao_scope is not None and not coordinator.functions.isFinalized(session).call(block_identifier=read_number):
        check_snapshot()
        return dict(status="AWAITING_FROST_FINALIZATION", session=str(session), operation_hash=op.hex(),
                    target=target.address)
    result = coordinator.functions.getAuthorizationResult(session).call(block_identifier=read_number)
    if (
        bytes(result[1]) != op
        or not result[2]
        or result[4] != target.address
        or result[6] < result[5]
    ):
        raise ValueError("FROST_APPROVAL_REQUIRED")
    name = intent["hash_method"]
    if not name.startswith("get") or not name.endswith("OperationHash"):
        raise ValueError("POLICY_HASH_HELPER")
    method = next(
        x for x in target.abi if x.get("type") == "function" and x.get("name") == name
    )
    args = encode_args(method["inputs"], intent["hash_args"])
    if bytes(getattr(target.functions, name)(*args).call(block_identifier=read_number)) != op:
        raise ValueError("OPERATION_PARAMETERS_MISMATCH")
    check_snapshot()
    account = Account.from_key(json.loads(read_private(a.identity_file))["private_key"])
    if "bitcoin_custody" in profile:
        from .onchain_transport import ceremony_bundle
        from .bootstrap_custody import review_intent
        review_intent(w3, intent, ceremony_bundle(w3, a.board, "conf_ceremony"), read_number)
        check_snapshot()
    if sale_review is not None and fee_sale.review(w3, intent, sale_bundle, "latest",
            sale_pin, require_finalized=False)["complete"]:
        raise ValueError("FEE_SALE_OPERATION_COMPLETE")
    c = w3.eth.contract(address=validator, abi=artifact("CustodianValidator")["abi"])
    if (
        account.address not in c.functions.getCustodians().call(block_identifier=read_number)
        or c.functions.frostMultisig().call(block_identifier=read_number) != authorization
    ):
        raise ValueError("CUSTODIAN_BINDING_MISMATCH")
    if not c.functions.custodianSigned(op, account.address).call(block_identifier=read_number):
        config = dict(
            deployment_id="custodian-" + account.address + "-" + op.hex(),
            mode=profile["mode"],
            chain_id=str(w3.eth.chain_id),
            bitcoin_network=profile["bitcoin_network"],
            evm_confirmations=str(confirmations),
            evm_rpc=a.rpc,
            contracts={
                "validator": dict(
                    address=validator,
                    artifact="CustodianValidator",
                    runtime_hash=Web3.keccak(w3.eth.get_code(validator, block_identifier=read_number)).hex(),
                )
            },
            steps=[
                dict(
                    id="approve",
                    kind="call",
                    contract="validator",
                    method="registerCustodianSignature",
                    args=["0x" + op.hex()],
                )
            ],
        )
        check_snapshot()
        if Bootstrap(config, a.state).apply_confirmed(a.identity_file)["status"] != "VERIFIED":
            raise ValueError("CUSTODIAN_TRANSACTION_UNCONFIRMED")
    check_snapshot()
    return dict(
        status="CUSTODIAN_APPROVED",
        operation_hash=op.hex(),
        participant=account.address,
        validator=validator,
    )
