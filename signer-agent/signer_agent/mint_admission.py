"""Canonical mint admission and public typed records, independent of board operator.

The pool operation nonce, EOA transaction nonce and private FROST nonces are
different sequences. This module never rewrites a previous signing transcript.
"""
from eth_abi import encode
from eth_utils import keccak
from eth_utils.abi import function_abi_to_4byte_selector
from web3 import Web3

from .bootstrap import artifact, Bootstrap
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation

ZERO = "0x" + "00" * 20


def _contract(w3, address, kind, number):
    checked_runtime(w3, address, kind, number)
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def _read(contract, number, method, *args):
    return getattr(contract.functions, method)(*args).call(block_identifier=number)


def claim_id(pool, txid, vout):
    return keccak(encode(["address", "bytes32", "uint32"], [pool, txid, vout]))


def configuration(w3, address, block, *, expected_pool=None, expected_operator=None, require_bound=True):
    anchor = w3.eth.get_block(block)
    number = anchor.number
    if not Web3.is_checksum_address(address) or address == ZERO:
        raise ValueError("BTC_MINT_ADMISSION_REQUIRED")
    board = _contract(w3, address, "BitcoinMintAdmission", number)
    pool_address = _read(board, number, "pool")
    if expected_pool is not None and pool_address != expected_pool:
        raise ValueError("BTC_MINT_ADMISSION_POOL_MISMATCH")
    arguments = [pool_address, expected_operator] if expected_operator is not None else None
    prove_creation(w3, address, "BitcoinMintAdmission", arguments, number)
    pool_kind = "MiningPoolV1"
    try:
        pool = _contract(w3, pool_address, pool_kind, number)
    except ValueError as error:
        if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
            raise
        pool_kind = "ConfederationBitcoinReserve"
        pool = _contract(w3, pool_address, pool_kind, number)
    prove_creation(w3, pool_address, pool_kind, None, number)
    auth = _contract(w3, _read(pool, number, "authorization"), "DKGAuthorization", number)
    prove_creation(w3, auth.address, "DKGAuthorization", None, number)
    coordinator = _contract(w3, _read(auth, number, "coordinator"), "FROSTCoordinator", number)
    prove_creation(w3, coordinator.address, "FROSTCoordinator", None, number)
    dkg_id = bytes(_read(auth, number, "dkgId"))
    result_hash = bytes(_read(auth, number, "resultHash"))
    if (_read(board, number, "authorization") != auth.address
            or _read(board, number, "coordinator") != coordinator.address
            or bytes(_read(board, number, "dkgId")) != dkg_id
            or bytes(_read(board, number, "resultHash")) != result_hash
            or (require_bound and _read(pool, number, "mintAdmission") != address)
            or (expected_operator is not None and _read(board, number, "operator") != expected_operator)):
        raise ValueError("BTC_MINT_ADMISSION_GRAPH_MISMATCH")
    if w3.eth.get_block(number).hash != anchor.hash:
        raise ValueError("BTC_MINT_ADMISSION_REORG")
    return dict(board=board, pool=pool, authorization=auth, coordinator=coordinator,
                dkg_id=dkg_id, result_hash=result_hash, anchor=anchor, pool_kind=pool_kind)


def _operation_hash(context, kind, method, arguments, nonce):
    function = next(row for row in artifact(kind)["abi"]
                    if row.get("type") == "function" and row.get("name") == method)
    mint = method in ("mintFromProof", "mintFromProofV2", "mintFromAddressProof")
    target = context["pool"] if mint else context["board"]
    if method in ("mintFromAddressProof", "mintFromProofV2"):
        from .address_mint import CLAIM_TYPE
        r = context["address_context"]["registry"]
        prefix = [keccak(text="SATOHSI_ADDRESS_MINT_V1" if method == "mintFromAddressProof" else "SATOHSI_CUSTODY_MINT_V2"),
                  r.address, bytes(_read(r, context["anchor"].number, "custodyScope"))]
        types = ["bytes32", "address", "bytes32"] + ([CLAIM_TYPE] if method == "mintFromAddressProof"
            else ["bytes32", "uint32", "address[]", "uint256[]", "uint256"])
        arguments = prefix + arguments
    else:
        types = ["bytes32", "uint32", "address[]", "uint256[]"] if mint else ["address"]
    return bytes(_read(context["authorization"], context["anchor"].number, "hashOperation",
        target.address, function_abi_to_4byte_selector(function), keccak(encode(types, arguments)), nonce))


def session_record(w3, address, session, block, *, role=None, require_active=True, signing=False):
    context = configuration(w3, address, block)
    board, pool, coordinator = (context[key] for key in ("board", "pool", "coordinator"))
    number = context["anchor"].number
    attempt = _read(board, number, "attempts", session)
    mode, fence_valid, economic_arguments = 0, True, None
    if bytes(attempt[0]) != bytes(32):
        claim = _read(board, number, "getClaim", attempt[0])
        txid, vout, recipients, amounts, deadline = claim[:5]
        arguments = [bytes(txid), int(vout), list(recipients), list(amounts)]
        if (claim_id(pool.address, arguments[0], arguments[1]) != bytes(attempt[0])
                or not 1 <= len(recipients) <= 64 or len(recipients) != len(amounts)
                or any(item == ZERO for item in recipients) or any(int(item) < 0 for item in amounts)
                or int(attempt[3]) != int(deadline)):
            raise ValueError("BTC_MINT_ADMISSION_CLAIM_MISMATCH")
        nonce, digest = int(attempt[2]), bytes(attempt[1])
        mode = int(_read(board, number, "claimModes", bytes(attempt[0])))
        execution = "mintFromProof"
        if mode in (1, 2):
            from . import address_mint
            context["address_context"] = address_mint.configuration(w3, pool, number)
            r = context["address_context"]["registry"]
            cg, dg = [int(x) for x in _read(board, number, "attemptFences", session)]
            if mode == 2:
                proposed = list(_read(board, number, "getAttemptAddressClaim", session))
                original = list(_read(board, number, "getAddressClaim", bytes(attempt[0])))
                if (address_mint.economics(proposed) != address_mint.economics(original)
                        or [int(proposed[6]), int(proposed[7])] != [cg, dg]
                        or bytes(proposed[2]) != arguments[0] or int(proposed[3]) != arguments[1]
                        or [proposed[4]] != list(recipients) or [int(proposed[5]) * 10**10] != list(amounts)):
                    raise ValueError("BTC_ADDRESS_CLAIM_MISMATCH")
                # Immutable binding/provenance is verified even for a stale historical attempt.
                b = address_mint.binding(context["address_context"], bytes(proposed[0]))
                if (b["binding"] != bytes(proposed[1]) or b["recipient"] != proposed[4]
                        or b["provenance"] != bytes(proposed[8])):
                    raise ValueError("BTC_ADDRESS_CLAIM_MISMATCH")
                try:
                    address_mint.validate_claim(context["address_context"], proposed)
                except ValueError as exc:
                    if str(exc) not in ("BTC_MINT_GENERATION_CHANGED", "BTC_ADDRESS_VERIFIER_UNQUALIFIED"):
                        raise
                    fence_valid = False
                arguments = [proposed]
                economic_arguments = [address_mint.economics(proposed)]
                execution = "mintFromAddressProof"
            else:
                fence_valid = cg == int(_read(r, number, "custodyGeneration")) and int(_read(r, number, "custodyState")) in (0, 1) and dg == 0
                economic_arguments = list(arguments)
                arguments = list(arguments) + [cg]
                execution = "mintFromProofV2"
        elif mode != 0:
            raise ValueError("BTC_MINT_ADMISSION_CLAIM_MISMATCH")
        else:
            fence_valid = int(_read(pool, number, "mintProofVersion")) == 1
        if digest != _operation_hash(context, context.get("pool_kind", "MiningPoolV1"), execution, arguments, nonce):
            raise ValueError("BTC_MINT_ADMISSION_HASH_MISMATCH")
        active = (fence_valid and not attempt[4] and int(claim[6]) == session
                  and int(_read(pool, number, "nonce")) == nonce
                  and int(_read(board, number, "slotSession", nonce)) == session
                  and bool(_read(board, number, "active", session)))
        kind, method, target = context.get("pool_kind", "MiningPoolV1"), "getMintOperationHash", pool.address
        if mode == 2: method = "getAddressMintOperationHash"
        elif mode == 1: method = "getCustodyMintOperationHash"
        reference = bytes(attempt[0]).hex()
    else:
        transfer = _read(board, number, "operatorTransfers", session)
        if transfer[0] == ZERO:
            raise ValueError("BTC_MINT_ADMISSION_SESSION_REQUIRED")
        arguments = [transfer[0]]
        nonce, deadline, digest = int(transfer[1]), int(transfer[2]), bytes(transfer[3])
        if digest != _operation_hash(context, "BitcoinMintAdmission", "transferOperator", arguments, nonce):
            raise ValueError("BTC_MINT_ADMISSION_HASH_MISMATCH")
        active = (int(_read(board, number, "operatorNonce")) == nonce
                  and int(_read(board, number, "operatorTransferSession", nonce)) == session
                  and not _read(board, number, "operatorTransferRetired", session)
                  and bool(_read(board, number, "activeOperatorTransfer", session)))
        kind, method, target = "BitcoinMintAdmission", "getTransferOperatorOperationHash", board.address
        reference = None
    info = _read(coordinator, number, "getSession", session)
    if (int(info[0]) != session or info[1] != board.address or bytes(info[3]) != digest or not info[4]
            or not info[8] or info[9] != ZERO or info[15] != target
            or bytes(_read(coordinator, number, "sessionDKGId", session)) != context["dkg_id"]
            or bytes(_read(coordinator, number, "sessionDKGResult", session)) != context["result_hash"]):
        raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
    active = active and int(info[10]) in (1, 2)
    if require_active and not active:
        raise ValueError("BTC_MINT_ADMISSION_NOT_ACTIVE")
    if signing and not fence_valid:
        raise ValueError("BTC_MINT_GENERATION_CHANGED")
    # FINALIZED authorization remains potentially executable after its original
    # deadline. An OPENED session may not get another nonce/share after that
    # deadline, including when a coordinator administrator extended its field.
    if signing and int(info[10]) == 1 and (int(info[7]) != int(deadline)
            or int(context["anchor"].timestamp) >= int(deadline)):
        raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
    intent = dict(chain_id=str(w3.eth.chain_id), coordinator=coordinator.address,
        target=target, artifact=kind, hash_method=method, hash_args=Bootstrap.wire(arguments),
        operation_hash=digest.hex(), session=str(session), mint_admission=board.address,
        admission_nonce=str(nonce), deadline=str(deadline))
    if role is not None:
        intent["role"] = role
    if w3.eth.get_block(number).hash != context["anchor"].hash:
        raise ValueError("BTC_MINT_ADMISSION_REORG")
    return dict(context=context, intent=intent, claim_id=reference, arguments=arguments,
                generation_mode=mode, fence_valid=fence_valid,
                economic_arguments=economic_arguments,
                operation_hash=digest, nonce=nonce, deadline=int(deadline), state=int(info[10]), active=active)


def guard_signing_session(client, session):
    """Shared guard for both enrolled and generic signer command paths."""
    if session.origin_contract == ZERO:
        return
    anchor = getattr(client, "_read_snapshot", None) or client.w3.eth.get_block("latest")
    kind = None
    for candidate in ("MiningPoolV1", "ConfederationBitcoinReserve", "BitcoinMintAdmission"):
        try:
            checked_runtime(client.w3, session.origin_contract, candidate, anchor.number)
        except ValueError as exc:
            if str(exc) != "BTC_CONTRACT_BUILD_MISMATCH":
                raise
        else:
            kind = candidate
            break
    if kind is None:
        return  # Other typed monetary operations keep their existing policy.
    if kind in ("MiningPoolV1", "ConfederationBitcoinReserve"):
        pool = client.w3.eth.contract(address=session.origin_contract, abi=artifact(kind)["abi"])
        context = getattr(client, "custody_context", None)
        if context:
            intent, bundle, _ = context
            if (intent.get("artifact") == kind
                    and intent.get("hash_method") == "getSetCustodyTransferOperationHash"):
                if ("bitcoin_custody" not in bundle.get("onboarding", {}).get("invitation", {})
                        or intent.get("target", "").lower() != pool.address.lower()
                        or not isinstance(intent.get("hash_args"), list) or len(intent["hash_args"]) != 1):
                    raise ValueError("BTC_MINT_CONFIGURATION_INTENT")
                # This one-time configuration is not an incoming mint claim.
                # It receives its own enrolled source/provenance/epoch guard,
                # including recomputation of the exact configuration operation.
                from .bootstrap_custody import guard_signing_session
                guard_signing_session(client, session)
                expected = _read(pool, anchor.number, "getSetCustodyTransferOperationHash", intent["hash_args"][0])
                if bytes(expected) != session.message_hash:
                    raise ValueError("BTC_MINT_CONFIGURATION_INTENT")
                return
        address = _read(pool, anchor.number, "mintAdmission")
        if address == ZERO:
            if int(_read(pool, anchor.number, "mintProofVersion")) != 1:
                raise ValueError("BTC_MINT_ADMISSION_REQUIRED")
            return  # Explicit legacy deployment, with no claimed admission.
    else:
        address = session.origin_contract
    record = session_record(client.w3, address, session.session_id, anchor.number, signing=True)
    if (record["context"]["coordinator"].address != client.address
            or record["operation_hash"] != session.message_hash):
        raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
    # A confirmed slot may already lag an observed competing permitted legacy
    # mint. Recheck current membership immediately before private work as well.
    latest = session_record(client.w3, address, session.session_id, "latest", signing=True)
    if (latest["context"]["coordinator"].address != client.address
            or latest["operation_hash"] != session.message_hash):
        raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
    if latest.get('generation_mode')==1:
        client.continuity_context=(latest['context']['address_context'],'BASE_MINT',session.session_id)
    if latest.get("generation_mode") == 2:
        client.continuity_context=(latest['context']['address_context'],'ADDRESS_MINT',session.session_id)
        from .address_mint import review_record
        bitcoin = getattr(client, "address_mint_bitcoin", None)
        if bitcoin is None or getattr(client, "bitcoin_graph", {}).get("usage") != "MINT_ONLY_V1":
            raise ValueError("BTC_ADDRESS_SIGNING_REVIEW_REQUIRED")
        review_record(latest, bitcoin, client.bitcoin_graph)
        # Core/proof review performs external I/O. A suspension/resume during
        # that review must be observed before generating or reusing a share.
        after_review = session_record(client.w3, address, session.session_id, "latest", signing=True)
        if (after_review["context"]["coordinator"].address != client.address
                or after_review["operation_hash"] != session.message_hash):
            raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
    if client.w3.eth.get_block(anchor.number).hash != anchor.hash:
        raise ValueError("BTC_MINT_ADMISSION_REORG")
    client.check_read_snapshot()
