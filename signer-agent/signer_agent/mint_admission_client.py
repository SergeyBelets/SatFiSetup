"""Mint SDK operations using the canonical admission's immutable typed records."""
from .mint_admission import configuration, session_record, claim_id, _read
from .bitcoin_trust import confirmed_block
from .bootstrap import Bootstrap


def remember(state, registry, record):
    """Persist a separate receipt/intent per canonical session, never rewrite it."""
    intent = record["intent"]
    reference = record["claim_id"] or "operator-transfer:" + intent["session"]
    root = "btc-mint-admission:" + intent["chain_id"] + ":" + intent["mint_admission"] + ":" + reference
    key = root + ":session:" + intent["session"]
    cached = state.get(key)
    if cached is not None:
        if cached["intent"] != intent:
            raise ValueError("BTC_MINT_ADMISSION_HISTORY_CONFLICT")
        if registry.w3.eth.get_block(int(cached["anchor"]["number"])).hash.hex() != cached["anchor"]["hash"]:
            raise ValueError("BTC_MINT_ADMISSION_REORG")
        return key
    economic = {name: intent[name] for name in ("target", "artifact", "hash_method", "hash_args", "deadline")}
    if record.get("generation_mode"):
        economic["hash_args"] = Bootstrap.wire(record["economic_arguments"])
    index = state.get(root) or dict(economic=economic, sessions=[])
    if index["economic"] != economic:
        raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
    if len(index["sessions"]) >= 8:
        raise ValueError("BTC_MINT_ATTEMPT_BUDGET_EXHAUSTED")
    anchor = record["context"]["anchor"]
    state.put(key, dict(intent=intent, session=intent["session"], anchor=dict(
        number=str(anchor.number), hash=anchor.hash.hex())))
    index["sessions"].append(intent["session"])
    state.put(root, index)
    return key


def _result(record, status="AWAITING_PARTICIPANTS"):
    intent = record["intent"]
    return dict(status=status, session=intent["session"], intent=intent,
        target=intent["target"],
        operation_hash=intent["operation_hash"], mint_admission=intent["mint_admission"],
        role=intent.get("role"), operation="mint" if record["claim_id"] else "operator-transfer")


def _authorize(registry, graph, record):
    from .bitcoin_onchain import send
    context = record["context"]
    auth, coordinator = context["authorization"], context["coordinator"]
    session, digest, target = int(record["intent"]["session"]), record["operation_hash"], record["intent"]["target"]
    if not auth.functions.isOperationApproved(digest, target).call():
        finalized = coordinator.functions.getAuthorizationResult(session).call()
        if bytes(finalized[1]) != digest or finalized[4] != target:
            raise ValueError("BTC_OPERATION_SESSION_MISMATCH")
        send(registry, auth.address, "DKGAuthorization", "authorizeOperation", [session, digest, target], graph=graph)


def _approved_current(record):
    """An exact legacy approval can outlive an aborted admission session."""
    if not record.get("fence_valid", True):
        return False
    context = record["context"]
    number = context["anchor"].number
    target = context["pool"] if record["claim_id"] else context["board"]
    nonce_method = "nonce" if record["claim_id"] else "operatorNonce"
    return (int(_read(target, number, nonce_method)) == record["nonce"]
        and bytes(_read(target, number, record["intent"]["hash_method"], *record["arguments"])) == record["operation_hash"]
        and bool(_read(context["authorization"], number, "isOperationApproved",
            record["operation_hash"], target.address)))


def _execution_record(registry, address, session, role):
    record = session_record(registry.w3, address, session, "latest", role=role, require_active=False)
    if not _approved_current(record):
        if not record["active"]:
            raise ValueError("BTC_MINT_ADMISSION_NOT_ACTIVE")
        if record["state"] != 2:
            raise ValueError("BTC_MINT_AUTHORIZATION_NOT_FINALIZED")
    return record


def dispatch_funding(a, manifest, key, registry, state, graph, contracts, target, arguments,
                     bitcoin, block, address):
    from .bitcoin_funding import _mint_complete, _mint_proof
    from .bitcoin_onchain import send, snapshot_check
    from .bitcoin_onchain_payment import _auth_board
    from .bitcoin_onchain_plan import decimal
    _, role = _auth_board(a, manifest, registry, graph)
    deadline = decimal(str(a.deadline), positive=True)
    context = configuration(registry.w3, address, block.number, expected_pool=target.address)
    board = context["board"]
    reference = claim_id(target.address, arguments[0], arguments[1])
    claim = _read(board, block.number, "getClaim", reference)
    if claim[4] and (int(claim[4]) != deadline or Bootstrap.wire(list(claim[:4])) != Bootstrap.wire(arguments)):
        raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
    with state.exclusive():
        if a.action == "btc-mint-retry":
            if not claim[4]:
                raise ValueError("BTC_MINT_ADMISSION_CLAIM_REQUIRED")
            previous = session_record(registry.w3, address, int(claim[6]), block.number,
                                      role=role, require_active=False)
            remember(state, registry, previous)
            if _approved_current(previous):
                raise ValueError("BTC_MINT_OPERATION_ALREADY_APPROVED_EXECUTE_REQUIRED")
            if deadline <= registry.w3.eth.get_block("latest").timestamp:
                raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            receipt = send(registry, address, "BitcoinMintAdmission", "retryMint",
                [reference, target.functions.nonce().call(block_identifier=block.number), int(claim[6])], graph=graph)
            block = registry.w3.eth.get_block(receipt["blockNumber"])
            claim = _read(board, block.number, "getClaim", reference)
        elif not claim[4]:
            if a.action != "btc-mint-propose":
                raise ValueError("BTC_MINT_ADMISSION_CLAIM_REQUIRED")
            if deadline <= registry.w3.eth.get_block("latest").timestamp:
                raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            nonce = target.functions.nonce().call(block_identifier=block.number)
            digest = _read(target, block.number, "getMintOperationHash", *arguments)
            if _read(context["authorization"], block.number, "isOperationApproved", digest, target.address):
                raise ValueError("BTC_MINT_OPERATION_ALREADY_APPROVED_LEGACY_RECOVERY_REQUIRED")
            # The atomic contract check is authoritative; this latest read only
            # avoids knowingly sending a transaction for an already stale slot.
            if target.functions.nonce().call() != nonce:
                raise ValueError("BTC_MINT_STALE_CONTRACT_NONCE")
            receipt = send(registry, address, "BitcoinMintAdmission", "openMint",
                [*arguments, deadline, nonce], graph=graph)
            block = registry.w3.eth.get_block(receipt["blockNumber"])
            claim = _read(board, block.number, "getClaim", reference)
        record = session_record(registry.w3, address, int(claim[6]), block.number, role=role,
                                require_active=a.action != "btc-mint-execute", signing=a.action != "btc-mint-execute")
        if record["arguments"] != arguments or record["deadline"] != deadline:
            raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
        remember(state, registry, record)
        snapshot_check(registry.w3, block)
        if a.action != "btc-mint-execute":
            return _result(record)
        # Exact completion was checked before UTXO observation in the caller.
        # A finalized authorization can still execute after its original deadline;
        # never open another session or silently extend that deadline to do so.
        record = _execution_record(registry, address, int(claim[6]), role)
        current = confirmed_block(registry.w3, graph)
        if _mint_complete(target, arguments, current):
            snapshot_check(registry.w3, current)
            return _result(record, "BTC_FUNDING_EXECUTED")
        execute_args = _mint_proof(a, bitcoin, contracts, target, arguments, current)
        snapshot_check(registry.w3, current)
        _authorize(registry, graph, record)
        record = _execution_record(registry, address, int(claim[6]), role)
        if not _approved_current(record):
            raise ValueError("BTC_MINT_AUTHORIZATION_NOT_APPROVED")
        send(registry, target.address, "MiningPoolV1", "mintFromProof",
            [*execute_args, record["operation_hash"]], graph=graph)
    current = confirmed_block(registry.w3, graph)
    if not _mint_complete(target, arguments, current):
        raise ValueError("BTC_OPERATION_POSTCONDITION")
    snapshot_check(registry.w3, current)
    return _result(record, "BTC_FUNDING_EXECUTED")


def management(a, manifest, key, registry, state, graph):
    from .bitcoin_onchain import send, snapshot_check
    from .bitcoin_onchain_payment import _auth_board
    from .bitcoin_onchain_plan import decimal
    if not a.mint_admission:
        raise ValueError("BTC_MINT_ADMISSION_REQUIRED")
    block = confirmed_block(registry.w3, graph)
    context = configuration(registry.w3, a.mint_admission, block.number)
    board, pool = context["board"], context["pool"]
    if (context["authorization"].address != graph["contracts"]["authorization"]["address"]
            or context["dkg_id"].hex() != graph["evm_dkg_id"]):
        raise ValueError("BTC_MINT_ADMISSION_GRAPH_MISMATCH")
    _, role = _auth_board(a, manifest, registry, graph)
    if a.action == "btc-mint-status":
        nonce = int(_read(pool, block.number, "nonce"))
        session = a.session or int(_read(board, block.number, "slotSession", nonce))
        result = dict(status="BTC_MINT_ADMISSION_STATUS", mint_admission=board.address,
            pool=pool.address, operation_nonce=str(nonce), operator=_read(board, block.number, "operator"),
            active_session=str(session))
        if session:
            record = session_record(registry.w3, board.address, session, block.number,
                                    role=role, require_active=False)
            result.update(intent=record["intent"], active=record["active"], session_state=record["state"])
        snapshot_check(registry.w3, block)
        return result
    with state.exclusive():
        if a.action == "btc-mint-transfer-propose":
            deadline = decimal(str(a.deadline), positive=True)
            nonce = _read(board, block.number, "operatorNonce")
            previous = _read(board, block.number, "latestOperatorTransferSession", nonce)
            receipt = send(registry, board.address, "BitcoinMintAdmission", "openOperatorTransfer",
                [a.new_operator, deadline, nonce, previous], graph=graph)
            block = registry.w3.eth.get_block(receipt["blockNumber"])
            nonce = _read(board, block.number, "operatorNonce")
            session = int(_read(board, block.number, "operatorTransferSession", nonce))
            record = session_record(registry.w3, board.address, session, block.number, role=role, signing=True)
            remember(state, registry, record)
            return _result(record)
        if not a.session:
            raise ValueError("BTC_MINT_ADMISSION_SESSION_REQUIRED")
        record = session_record(registry.w3, board.address, a.session, block.number,
                                role=role, require_active=False)
        remember(state, registry, record)
        if a.action in ("btc-mint-retire", "btc-mint-transfer-retire"):
            mint = a.action == "btc-mint-retire"
            if bool(record["claim_id"]) != mint:
                raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
            send(registry, board.address, "BitcoinMintAdmission", "retire" if mint else "retireOperatorTransfer",
                [a.session], graph=graph)
            return _result(record, "BTC_MINT_ADMISSION_RETIRED")
        if a.action != "btc-mint-transfer-execute" or record["claim_id"]:
            raise ValueError("BTC_MINT_ADMISSION_SESSION_MISMATCH")
        next_operator = record["arguments"][0]
        if (_read(board, block.number, "operatorNonce") > record["nonce"]
                and _read(board, block.number, "operator") == next_operator
                and _read(context["authorization"], block.number, "executedOperations", record["operation_hash"])):
            return _result(record, "BTC_MINT_OPERATOR_TRANSFERRED")
        record = _execution_record(registry, board.address, a.session, role)
        _authorize(registry, graph, record)
        send(registry, board.address, "BitcoinMintAdmission", "transferOperator",
            [next_operator, record["operation_hash"]], graph=graph)
    block = confirmed_block(registry.w3, graph)
    if _read(board, block.number, "operator") != next_operator:
        raise ValueError("BTC_OPERATION_POSTCONDITION")
    snapshot_check(registry.w3, block)
    return _result(record, "BTC_MINT_OPERATOR_TRANSFERRED")
