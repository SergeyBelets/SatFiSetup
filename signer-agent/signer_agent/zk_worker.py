"""Demand-driven Bitcoin mainnet and testnet4 updater with a private local inbox.

Requests are SPV service needs, never permission to mint or settle. Public or
encrypted cross-host request transport must authenticate before using this API.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from types import SimpleNamespace

from web3 import Web3
from .bootstrap import Bootstrap, artifact
from .bitcoin_network import validate_policy, check_core, header_hash
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .evm_network import read_block
from .operations import canonical
from .zk_spv import inspect_binding, is_zk
from .zk_profiles import profile as zk_profile
from .zk_spv_cli import transition_step, materialization_step
from .zk_worker_prover import LocalProver, ROOT, read, write, sha


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def state_wire(s):
    return [bytes(x).hex() if i in (0, 2, 6, 7, 9, 10) else x if i == 8
            else f"{x:064x}" if i == 4 else str(x) for i, x in enumerate(s)]


def validate_chain_config(c):
    validate_policy(c["bitcoin_policy"])
    if not is_zk(c["bitcoin_policy"]):
        raise ValueError("ZK_POLICY_REQUIRED")
    from .bootstrap_policy import validate_confirmations
    validate_confirmations(c, "ZK_WORKER_")


def load_config(path, *, review=False):
    path = Path(path)
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError("ZK_WORKER_CONFIG_PERMISSIONS")
    c = read(path)
    if review and set(c) == {"version", "chain_id", "evm_rpc", "evm_confirmations", "bitcoin_policy", "zk_state"}:
        if (c["version"] != "1" or c["chain_id"] not in ("1", "11155111", "31337")
                or not isinstance(c["evm_rpc"], str) or not c["evm_rpc"]
                or not Web3.is_checksum_address(c["zk_state"])):
            raise ValueError("ZK_WORKER_CONFIG_SCHEMA")
        validate_chain_config(c)
        return c
    required = {"version", "chain_id", "evm_rpc", "evm_confirmations", "bitcoin_rpc", "bitcoin_cookie",
        "bitcoin_policy", "zk_state", "identity_file", "directory", "allowed_consumers", "host_binary", "host_sha256",
        "stage_timeout_seconds", "max_stage_retries", "max_pending", "max_attempts", "minimum_free_bytes", "max_tx_gas",
        "max_transaction_cost_wei", "max_total_cost_wei", "poll_seconds"}
    if c.get("version") in ("2","3","4","5","6","7"): required |= {"service", "heartbeat"}
    if c.get("version") in ("3","4","5","6","7"): required |= {"transport","batching"}
    if c.get("version") in ("4","5","6","7"): required |= {"archive","automation"}
    if c.get("version") in ("5","6","7"):required.add("address_deposits")
    if c.get("version") in ("6","7"):required.add("withdrawal_fees")
    if c.get("version")=="7": required.add("observations")
    if (not required <= set(c) <= required | {"headers_per_attempt", "cuda"}
            or c["version"] not in ("1", "2", "3", "4", "5", "6", "7")
            or c["chain_id"] not in ("1", "11155111", "31337")):
        raise ValueError("ZK_WORKER_CONFIG_SCHEMA")
    validate_chain_config(c)
    from .zk_worker_prover import MAX_STAGE_TIMEOUT_SECONDS, validate_cuda
    if c.get("cuda") is not None:
        validate_cuda(c["cuda"])
    batch = c.get("headers_per_attempt")
    if batch is not None and (not isinstance(batch, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", batch)
            or int(batch) > int(c["bitcoin_policy"]["max_headers_per_sync"])):
        raise ValueError("ZK_WORKER_CONFIG_RANGE:headers_per_attempt")
    for name, low, high in (("stage_timeout_seconds", 60, MAX_STAGE_TIMEOUT_SECONDS), ("max_pending", 1, 4096),
        ("max_stage_retries", 1, 8),
        ("max_attempts", 1, 128), ("minimum_free_bytes", 8*1024**3, 2**64-1),
        ("max_tx_gas", 21000, 16777216), ("max_transaction_cost_wei", 1, 2**256-1),
        ("max_total_cost_wei", 1, 2**256-1), ("poll_seconds", 1, 3600)):
        value = c[name]
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value) or not low <= int(value) <= high:
            raise ValueError("ZK_WORKER_CONFIG_RANGE:" + name)
    if (not isinstance(c["allowed_consumers"], dict) or not 1 <= len(c["allowed_consumers"]) <= 4096
            or any(not Web3.is_checksum_address(k) or v not in ("MiningPoolV1", "BitcoinWithdrawalEscrow",
                   "ConfederationBitcoinReserve", "BitcoinWithdrawalEscrowV2")
                   for k, v in c["allowed_consumers"].items())
            or not Web3.is_checksum_address(c["zk_state"])):
        raise ValueError("ZK_WORKER_CONSUMER_POLICY")
    if not re.fullmatch(r"[0-9a-f]{64}", c["host_sha256"]): raise ValueError("ZK_HOST_PIN")
    for name in ("directory", "identity_file", "host_binary", "bitcoin_cookie"):
        if not Path(c[name]).is_absolute(): raise ValueError("ZK_WORKER_ABSOLUTE_PATH_REQUIRED")
    if c["version"] in ("2","3","4","5","6","7"):
        from .zk_worker_service import validate_service
        from .zk_worker_heartbeat import validate_heartbeat
        validate_service(c); validate_heartbeat(c)
    if c["version"] in ("3","4","5","6","7"):
        b=c["batching"]
        if set(b)!={"local_service_seconds"} or not isinstance(b["local_service_seconds"],str) or not b["local_service_seconds"].isdecimal() or not 60<=int(b["local_service_seconds"])<=604800:
            raise ValueError("ZK_BATCHING_POLICY")
        t=c["transport"]
        if t is not None:
            if (set(t)!={"config_file","federation_consumers","status_interval_seconds"} or not Path(t["config_file"]).is_absolute()
                    or not isinstance(t["federation_consumers"],dict) or not 1<=len(t["federation_consumers"])<=256
                    or not isinstance(t["status_interval_seconds"],str) or not t["status_interval_seconds"].isdecimal()
                    or not 1<=int(t["status_interval_seconds"])<=86400): raise ValueError("ZK_TRANSPORT_POLICY")
            for fed,consumers in t["federation_consumers"].items():
                if (not isinstance(fed,str) or not 1<=len(fed.encode())<=128 or not isinstance(consumers,list) or not consumers
                        or any(a not in c["allowed_consumers"] for a in consumers)):raise ValueError("ZK_TRANSPORT_CONSUMERS")
    if c["version"] in ("4","5","6","7"):
        from .zk_service_issuer import validate
        validate(c)
        a=c['archive']
        if set(a)!={'directory','max_headers','max_bytes'} or not Path(a['directory']).is_absolute():raise ValueError('ZK_ARCHIVE_POLICY')
        from .spv_automation import integer
        integer(a['max_headers'],64,2000000);integer(a['max_bytes'],1048576,2**40)
    if c["version"] in ("5","6","7"):
        from .address_spv import validate_profile
        validate_profile(c["address_deposits"],c["allowed_consumers"])
    if c["version"] in ("6","7"):
        from .withdrawal_fee_spv import validate_profile
        validate_profile(c["withdrawal_fees"],c["allowed_consumers"])
    if c["version"]=="7":
        from .zk_observations import validate
        validate(c["observations"])
    return c


class Inbox:
    def __init__(self, config):
        self.config = config
        from .zk_observations import require_policy
        require_policy(config)
        self.path = Path(config["directory"])
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.stat().st_mode & 0o077 or self.path.stat().st_uid != os.getuid():
            raise ValueError("ZK_WORKER_DIRECTORY_PERMISSIONS")
        self.db = sqlite3.connect(self.path / "worker.sqlite", isolation_level=None, timeout=30)
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS needs(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, cost TEXT NOT NULL);
        """)
        scope = {k: config[k] for k in ("chain_id", "evm_confirmations", "bitcoin_policy", "zk_state", "allowed_consumers")}
        if config.get("version") in ("2","3","4","5","6","7"):
            scope["service"] = {k: v for k, v in config["service"].items() if not k.startswith("max_")}
            scope["heartbeat"] = config["heartbeat"]
        if config.get("version") in ("3","4","5","6","7"):
            scope["transport"]=config["transport"];scope["batching"]=config["batching"]
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('scope',?)", (canonical(scope),))
        if self.get("scope") != scope: raise ValueError("ZK_WORKER_SCOPE_CHANGED")
        personal=config.get("withdrawal_fees")
        if self.get("withdrawal_fees_config") not in (None,personal):raise ValueError("SPV_WITHDRAWAL_FEE_PROFILE_CHANGED")
        if personal is not None:self.put("withdrawal_fees_config",personal)
        profile=config.get('address_deposits')
        if self.get('address_deposits_config') not in (None,profile):raise ValueError('SPV_ADDRESS_PROFILE_CHANGED')
        if profile is not None:self.put('address_deposits_config',profile)
        if config.get('version') in ('4','5','6','7'):
            for key,value in [('archive_config',config['archive'])]+([('automation_config',config['automation'])] if config['automation'].get('enabled') else []):
                old=self.get(key)
                if old is not None and old!=value:raise ValueError('ZK_WORKER_RUNTIME_POLICY_CHANGED')
                self.put(key,value)

        observed=config.get('observations')
        if self.get('observations_policy') is not None and observed!=self.get('observations_policy'):
            raise ValueError('ZK_OBSERVATION_POLICY_CHANGED')
        if observed is not None:
            from .zk_observations import validate
            validate(observed);self.put('observations_policy',observed)

    def close(self): self.db.close()
    def get(self, key):
        row = self.db.execute("SELECT body FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body", (key, canonical(value)))
    def needs(self):
        return [(row[0], json.loads(row[1]), row[2]) for row in self.db.execute("SELECT id,body,status FROM needs ORDER BY rowid")]
    def status(self, key, status): self.db.execute("UPDATE needs SET status=? WHERE id=?", (status, key))
    def enqueue(self, request):
        key = digest(["satoshifi.local-spv-need.v1", self.get("scope"), request])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT status FROM needs WHERE id=?", (key,)).fetchone()
            if not row:
                if self.db.execute("SELECT COUNT(*) FROM needs").fetchone()[0] >= int(self.config["max_pending"]):
                    raise ValueError("ZK_WORKER_INBOX_FULL")
                self.db.execute("INSERT INTO needs VALUES(?,?,?)", (key, canonical(request), "QUEUED"))
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise
        return key
    def reserve(self, key, cost, *, transaction=True):
        if transaction: self.db.execute("BEGIN IMMEDIATE")
        try:
            costs = dict(self.db.execute("SELECT id,cost FROM reservations"))
            wanted = max(cost, int(costs.get(key, "0")))
            total = sum(int(v) for k, v in costs.items() if k != key) + wanted
            if wanted > int(self.config["max_transaction_cost_wei"]) or total > int(self.config["max_total_cost_wei"]):
                raise ValueError("ZK_WORKER_GAS_BUDGET")
            self.db.execute("INSERT INTO reservations VALUES(?,?) ON CONFLICT(id) DO UPDATE SET cost=excluded.cost", (key, str(wanted)))
            if transaction: self.db.commit()
        except BaseException:
            if transaction: self.db.rollback()
            raise


class Worker:
    def __init__(self, config):
        self.c = config; self.inbox = Inbox(config)
        self.w3 = Web3(Web3.HTTPProvider(config["evm_rpc"], request_kwargs={"timeout": 30}))
        self.bitcoin = BitcoinRPC(config["bitcoin_rpc"], config["bitcoin_cookie"], config['bitcoin_policy']['network'], config["bitcoin_policy"])
        self.lock_fd = None

    def archive_store(self):
        if not hasattr(self,'_archive'):
            from .zk_archive import ArchiveStore
            a=self.c['archive']
            scope={k:self.c[k] for k in ('chain_id','zk_state','bitcoin_policy')}
            self._archive=ArchiveStore(a['directory'],scope,int(a['max_headers']),int(a['max_bytes']))
        return self._archive

    def close(self):
        if hasattr(self,'_native_session'):self._native_session.close()
        if hasattr(self,'_archive'):self._archive.close()
        self.inbox.close()

    def archive_ref(self,current):
        store=self.archive_store()
        if store.version(current[7]):return store.ref_existing(current)
        legacy=self.inbox.get('archive:'+current[7])
        if legacy is not None:
            from .zk_archive_ops import migrate_legacy
            migrate_legacy(self,current,legacy)
            return store.ref_existing(current)
        return store.ref(current)

    @contextmanager
    def exclusive(self):
        with (self.inbox.path / "worker.lock").open("a+") as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError("ZK_WORKER_BUSY") from None
            self.lock_fd = lock.fileno()
            try: yield
            finally: self.lock_fd = None

    def context(self, *, allow_halt=False):
        if self.w3.eth.chain_id != int(self.c["chain_id"]): raise ValueError("CHAIN_MISMATCH")
        block = read_block(self.w3, int(self.c["evm_confirmations"]))
        state, adapter, records, _ = inspect_binding(self.w3, self.c["zk_state"], self.c["bitcoin_policy"], block.number)
        halted = state.functions.halted().call()
        if halted and not allow_halt: raise ValueError("MAINNET_RELAY_HALTED")
        core = None if halted else check_core(self.bitcoin, self.c['bitcoin_policy']['network'], self.c["bitcoin_policy"])
        from .zk_observations import observe
        observe(self,block,state,core)
        return block, state, adapter, records, core

    def new_work_guard(self):
        if self.c.get('observations'):
            self.context()
            from .zk_observations import observer
            observer(self).fence()

    def stable(self, block):
        if self.w3.eth.get_block(block.number).hash != block.hash: raise ValueError("BTC_REGISTRY_READ_REORG")

    def aligned(self, state, block, core):
        s = state.functions.getState().call(block_identifier=block.number)
        if s[3] > core["blocks"]: return False
        h = self.bitcoin.call("getblockhash", s[3])
        header = self.bitcoin.call("getblockheader", h, True)
        return bytes(s[2])[::-1].hex() == h and s[4] == int(header["chainwork"], 16) and s[5] == header["time"]

    def contract(self, address, kind, block):
        checked_runtime(self.w3, address, kind, block.number)
        prove_creation(self.w3, address, kind, None, block.number)
        return self.w3.eth.contract(address=address, abi=artifact(kind)["abi"])

    def observe_need(self, request, block, adapter, core):
        if request.get("kind")=="withdrawal_fee":
            from .withdrawal_fee_spv import inspect
            fee=inspect(self,request,block)
            if fee.get("status"):return fee
            ctx=fee["context"]
            if any(c.functions.spv().call(block_identifier=block.number)!=adapter.address for c in (ctx["common"],ctx["escrow"])):raise ValueError("ZK_CONSUMER_STATE_MISMATCH")
            return self._observe_transaction(request,block,adapter,core,fee["depth"],fee["script"])
        if request.get("kind")=="address_deposit":
            from .address_spv import inspect
            address=inspect(self,request,block)
            if address.get("status"):return address
            ctx=address["context"]
            escrow=self.contract(ctx["registry"].functions.escrow().call(block_identifier=block.number),"BitcoinWithdrawalEscrow",block)
            if any(c.functions.spv().call(block_identifier=block.number)!=adapter.address for c in (ctx["common"],escrow)):
                raise ValueError("ZK_CONSUMER_STATE_MISMATCH")
            return self._observe_transaction(request,block,adapter,core,address["depth"],address["script"])
        kind = request.get("kind")
        fields = {"consumer", "kind", "txid", "vout" if kind == "deposit" else "batch"}
        if set(request) != fields or kind not in ("deposit", "withdrawal") or not re.fullmatch(r"[0-9a-f]{64}", request["txid"]):
            raise ValueError("ZK_NEED_SCHEMA")
        wanted = self.c["allowed_consumers"].get(request["consumer"])
        permitted = (("MiningPoolV1", "ConfederationBitcoinReserve") if kind == "deposit"
                     else ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2"))
        if wanted not in permitted: raise ValueError("ZK_CONSUMER_NOT_ALLOWED")
        consumer = self.contract(request["consumer"], wanted, block)
        def call(c, name, *args): return getattr(c.functions, name)(*args).call(block_identifier=block.number)
        if kind == "deposit":
            vout = request["vout"]
            if not isinstance(vout, str) or not re.fullmatch(r"0|[1-9][0-9]*", vout) or int(vout) > 2**32-1:
                raise ValueError("ZK_NEED_VOUT")
            deposits = self.contract(call(consumer, "deposits"), "BitcoinDepositRegistry", block)
            escrow = self.contract(call(consumer, "escrow"), "BitcoinWithdrawalEscrowV2" if wanted == "ConfederationBitcoinReserve"
                                   else "BitcoinWithdrawalEscrow", block)
            if call(deposits, "spv") != adapter.address or call(escrow, "spv") != adapter.address:
                raise ValueError("ZK_CONSUMER_STATE_MISMATCH")
            from eth_abi import encode
            outpoint = Web3.keccak(encode(["bytes32", "uint32"], [bytes.fromhex(request["txid"])[::-1], int(vout)]))
            if call(deposits, "consumed", outpoint): return dict(status="COMPLETED")
            depth = int(call(deposits, "depth"))
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", request["batch"]): raise ValueError("ZK_NEED_BATCH")
            payment = call(consumer, "payment", bytes.fromhex(request["batch"]))
            if call(consumer, "spv") != adapter.address or bytes(payment[0])[::-1].hex() != request["txid"]:
                raise ValueError("ZK_WITHDRAWAL_BINDING")
            if payment[2]: return dict(status="COMPLETED")
            depth = int(call(consumer, "depth"))
        return self._observe_transaction(request,block,adapter,core,depth,
            bytes(call(consumer,"payoutScript")) if kind=="deposit" else None)

    def _observe_transaction(self,request,block,adapter,core,depth,script):
        tx = self.bitcoin.call("getrawtransaction", request["txid"], True)
        from . import crypto
        decoded = crypto.call("bitcoin_funding", raw=tx["hex"], txid=request["txid"])
        if script is not None:
            index = int(request["vout"])
            if index >= len(decoded["outputs"]) or decoded["outputs"][index]["script"] != script.hex():
                raise ValueError("ZK_DEPOSIT_BINDING")
            if decoded["coinbase"]: depth = max(depth,101) if request.get("kind")=="withdrawal_fee" else 100
        if depth < 6: raise ValueError("ZK_CONSUMER_DEPTH")
        if "blockhash" not in tx: return dict(status="WAITING_BITCOIN")
        header = self.bitcoin.call("getblockheader", tx["blockhash"], True)
        height = header["height"]
        if height < int(self.c["bitcoin_policy"]["checkpoint_height"]): raise ValueError("ZK_NEED_BEFORE_CHECKPOINT")
        if self.bitcoin.call("getblockhash", height) != tx["blockhash"]: return dict(status="WAITING_BITCOIN_REORG")
        if height + depth - 1 > core["blocks"]: return dict(status="WAITING_BITCOIN")
        # Derive and verify the Bitcoin Merkle proof independently of RPC's
        # confirmations claim. The guest later authenticates its header root.
        from .bitcoin_proofs import inclusion
        proof = inclusion(self.bitcoin, request["txid"])
        raw = bytes.fromhex(self.bitcoin.call("getblockheader", tx["blockhash"], False))
        node = bytes.fromhex(request["txid"])[::-1]; index = int(proof["index"])
        for sibling in proof["siblings"]:
            sibling = bytes.fromhex(sibling)
            node = header_hash(sibling + node if index & 1 else node + sibling); index >>= 1
        if index or len(raw) != 80 or node != raw[36:68] or header_hash(raw)[::-1].hex() != tx["blockhash"]:
            raise ValueError("ZK_NEED_INCLUSION")
        if (proof["block_hash"] != header_hash(raw).hex()
                or self.bitcoin.call("getblockhash", height) != tx["blockhash"]
                or self.bitcoin.call("getblockhash", core["blocks"]) != core["bestblockhash"]):
            raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
        canonical, count = adapter.functions.getBlockStatus(header_hash(raw)).call(block_identifier=block.number)
        return dict(status="READY" if canonical and count >= depth else "NEEDS_STATE", height=height,
                    block_hash=tx["blockhash"], depth=depth, header=raw.hex())

    def observe_queued_need(self, key, request, block, adapter, core):
        retry_key = "need-observation-retry:" + key
        retry = self.inbox.get(retry_key)
        now = int(time.time())
        if retry and now < retry["next_at"]:
            return dict(status="WAITING_BITCOIN_RPC")
        try:
            observation = self.observe_need(request, block, adapter, core)
        except BitcoinRPCError as error:
            if (error.rpc_code not in (-5, -8, -28) and str(error) != "BITCOIN_RPC_FAILED"
                    and error.http_status not in (429, 502, 503, 504)):
                raise
            attempts = min(8, 1 + (retry["attempts"] if retry else 0))
            delay = min(3600, int(self.c["poll_seconds"]) * 2 ** (attempts - 1))
            self.inbox.put(retry_key, dict(attempts=attempts, next_at=now + delay))
            return dict(status="WAITING_BITCOIN_RPC")
        if retry:
            self.inbox.put(retry_key, None)
        return observation

    def request(self, request, *, register_local=True):
        block, state, adapter, _, core = self.context()
        observation = self.observe_need(request, block, adapter, core); self.stable(block)
        if observation["status"] == "READY" and not self.aligned(state, block, core):
            observation["status"] = "NEEDS_STATE"
        if self.c.get('observations'):
            from .zk_observations import observer
            log=observer(self)
            if observation['status']=='COMPLETED':
                key=digest(['satoshifi.local-spv-need.v1',self.inbox.get('scope'),request]);linked=self.inbox.get('service:'+key)
                return dict(status='COMPLETED',service_job_id=key,local_need_id=key,
                    canonical_service_job_id=linked['service_job_id'] if linked else None)
            log.fence()
            local=bool(register_local and self.c.get('batching'))
            key=digest(['satoshifi.local-spv-need.v1',self.inbox.get('scope'),request]);prior=self.inbox.get('obligation:'+key)
            created=prior['created_at'] if local and prior and 'local' in prior['origins'] else block.timestamp
            deadline=prior['origins']['local'] if local and prior and 'local' in prior['origins'] else block.timestamp+int(self.c['batching']['local_service_seconds']) if local else None
            key=log.detect_need(request,observation['status'],created,deadline,local)
            log.drain();self.inbox.status(key,observation['status'])
        else:
            key = self.inbox.enqueue(request); self.inbox.status(key, observation["status"])
        if not self.c.get('observations') and register_local and self.c.get("batching") and self.inbox.get("obligation:"+key) is None:
            from .zk_worker_batching import register
            register(self,key,block.timestamp,block.timestamp+int(self.c["batching"]["local_service_seconds"]),"local")
        linked = self.inbox.get("service:" + key)
        return dict(status=observation["status"], service_job_id=key, local_need_id=key,
                    canonical_service_job_id=linked["service_job_id"] if linked else None)

    def native(self, archive, headers, heights, out):
        if sha(self.c["host_binary"]) != self.c["host_sha256"]: raise ValueError("ZK_HOST_BINARY_DRIFT")
        p = self.c["bitcoin_policy"]
        request = dict(checkpoint=p["checkpoint_header"], height=p["checkpoint_height"], work=p["checkpoint_chainwork"],
            prior=p["checkpoint_predecessors"], chain_id=self.c["chain_id"], state=self.c["zk_state"][2:],
            archive=archive, headers=headers, memberships=[str(h) for h in sorted(set(heights))])
        out.mkdir(parents=True, exist_ok=True)
        from .zk_archive import Ref
        if isinstance(archive,Ref):
            stream=archive.store.materialize(archive)
            request.pop('archive')
            request.update(archive_file=stream['path'],archive_count=str(stream['count']),expected_before=stream['state'])
            from .zk_archive_native import NativeSession
            if not hasattr(self,'_native_session') or self._native_session.process.poll() is not None:
                if hasattr(self,'_native_session'):self._native_session.close()
                self._native_session=NativeSession(self.c['host_binary'],self.inbox.path)
            self._native_session.prepare(request,out)
        else:
            with (out / "native.log").open("w") as log:
                subprocess.run([self.c["host_binary"], str(out)], input=canonical(request).encode(),
                               stdout=log, stderr=subprocess.STDOUT, timeout=600, check=True)
        for name in ("native.json", "state.input", "state.journal"):
            path = out / name
            if path.exists():
                with path.open("rb") as stream: os.fsync(stream.fileno())
        fd = os.open(out, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return read(out / "native.json")

    def publish(self, name, step, records, *, recover_only=False):
        inbox, config = self.inbox, self.c
        worker = self
        class BudgetedBootstrap(Bootstrap):
            def build_transaction(self, step, account, nonce):
                worker.new_work_guard()
                tx = super().build_transaction(step, account, nonce)
                if tx["gas"] > int(config["max_tx_gas"]): raise ValueError("ZK_WORKER_GAS_LIMIT")
                fee = tx.get("maxFeePerGas", tx.get("gasPrice"))
                from .zk_worker_service import reserve
                reserve(worker, name + ":" + step["id"], int(fee) * tx["gas"] + int(tx.get("value", 0)),
                        getattr(worker, "publication_needs", []))
                return tx
        profile = dict(deployment_id=name, mode="backed", bitcoin_network=config['bitcoin_policy']['network'], bitcoin_policy=config["bitcoin_policy"],
            chain_id=config["chain_id"], evm_rpc=config["evm_rpc"], evm_confirmations=config["evm_confirmations"],
            contracts=records, steps=[step])
        runner=BudgetedBootstrap(profile,self.inbox.path/'transactions.sqlite')
        if recover_only:return runner.apply(config['identity_file'],before_submit=lambda steps:True)
        return runner.apply(config['identity_file'])

    def retain_accepted_archive(self,active,native,current,block):
        """Retain only an already accepted exact native transition; no proving/send."""
        prepared=read(self.inbox.path/'attempts'/active['id']/'preparation.json')
        if (current!=native['after'] or prepared.get('native_sha256')!=sha(self.inbox.path/'attempts'/active['id']/'native.json')):
            raise ValueError('ZK_ARCHIVE_NATIVE_SUMMARY_DRIFT')
        self.stable(block)
        if self.c.get('archive'):
            if 'delta' not in native:
                from .zk_archive_ops import migrate_legacy
                migrate_legacy(self,current,native['archive'])
            else:
                self.archive_ref(native['before'])
                self.archive_store().promote(native['before'],current,native['delta'],dict(block=str(block.number),hash=block.hash.hex(),attempt=active['id']))
        else:self.inbox.put('archive:'+current[7],native['archive'])
        self.inbox.put('attempt',None)

    def tick(self):
        with self.exclusive():
            transport=None
            if self.c.get("transport"):
                try:
                    from .zk_worker_transport import WorkerTransport
                    transport=WorkerTransport(self);transport.ingest()
                    if self.c.get('automation',{}).get('enabled'):
                        from .zk_service_issuer import tick as admission_tick
                        self.inbox.put('automation_status',admission_tick(self,transport))
                    self.inbox.put("transport_error",None)
                except Exception as exc:
                    self.inbox.put("transport_error",str(exc) if type(exc) is ValueError and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}",str(exc)) else "SPV_TRANSPORT_UNAVAILABLE")
            try:
                result=self._tick()
            except Exception:
                if transport:
                    try:transport.updates(dict(status="BLOCKED"));transport.flush()
                    except Exception:self.inbox.put("transport_error","SPV_STATUS_DELIVERY_PENDING")
                    finally:transport.close()
                raise
            if transport:
                try:transport.updates(result);transport.flush()
                except Exception:self.inbox.put("transport_error","SPV_STATUS_DELIVERY_PENDING")
                finally:transport.close()
            return result

    def advance(self, boundary):
        """Contour boundary: no transport/admission and no implicit proving at publication."""
        if boundary=='prove':return self.compute_local()
        if boundary not in ('prepare','publish'):raise ValueError('ZK_WORKER_BOUNDARY')
        with self.exclusive():return self._tick(boundary=boundary)

    def prepare_service(self):
        """Prepare queued work and retain an exact accepted transition for the next poll."""
        with self.exclusive():
            return self._tick(boundary="prepare", retain_accepted=True)

    def revisit_preparation(self, active, native, current, block, core):
        attempt = active["id"]
        anchor = active.get("evm_prepare_anchor")
        if anchor and block.number < anchor["number"]:
            return dict(status="AWAITING_EVM_RPC", proving_attempt_id=attempt)
        if current == native["after"]:
            self.retain_accepted_archive(active, native, current, block)
            return dict(status="STATE_ALREADY_ACCEPTED", proving_attempt_id=attempt)
        if current != native["before"]:
            if int(current[11]) < int(native["before"][11]):
                if not anchor:
                    return dict(status="AWAITING_EVM_RPC", proving_attempt_id=attempt)
                from .evm_network import verify_anchor
                try:
                    verify_anchor(self.w3, anchor["number"], anchor["hash"], safe=block)
                except ValueError as error:
                    if str(error) == "EVM_RPC_BEHIND":
                        return dict(status="AWAITING_EVM_RPC", proving_attempt_id=attempt)
                    if str(error) != "EVM_REORG_RECOVERY_REQUIRED":
                        raise
                else:
                    return dict(status="AWAITING_EVM_RPC", proving_attempt_id=attempt)
            status = "REVALIDATE_STATE"
        else:
            height = active["target_height"]
            if core["blocks"] < height:
                return dict(status="WAITING_BITCOIN_RPC", proving_attempt_id=attempt)
            try:
                observed = self.bitcoin.call("getblockhash", height)
                if observed == active["target_hash"]:
                    return dict(status="PROOF_PREPARED", proving_attempt_id=attempt)
                tip = self.bitcoin.call("getblockhash", core["blocks"])
                if (tip != core["bestblockhash"]
                        or (tip if height == core["blocks"] else self.bitcoin.call("getblockhash", height)) != observed):
                    return dict(status="WAITING_BITCOIN_RPC", proving_attempt_id=attempt)
            except BitcoinRPCError:
                return dict(status="WAITING_BITCOIN_RPC", proving_attempt_id=attempt)
            status = "REVALIDATE_BITCOIN"
        self.stable(block)
        self.inbox.put("attempt", None)
        return dict(status=status, proving_attempt_id=attempt, retired=True)

    def accept_local_transition(self,active,native,current,block):
        """Retain an already accepted exact transition; never submit it again."""
        if current!=native['after']:raise ValueError('ZK_ACCEPTED_TRANSITION_MISMATCH')
        if self.c.get('archive'):
            if 'delta' not in native:
                from .zk_archive_ops import migrate_legacy
                migrate_legacy(self,current,native['archive'])
            else:
                self.archive_ref(native['before'])
                self.archive_store().promote(native['before'],current,native['delta'],dict(block=str(block.number),hash=block.hash.hex(),attempt=active['id']))
        else:self.inbox.put('archive:'+current[7],native['archive'])
        self.inbox.put('attempt',None)

    def compute_local(self):
        """Compute this participant's frozen transition without sending a transaction.

        Another publisher may already have advanced EVM state. That does not
        substitute for our computation of an existing, independently prepared
        attempt, and never authorizes publication of a stale result.
        """
        with self.exclusive():
            active=self.inbox.get('attempt')
            if not active:
                result=self._tick(boundary='prepare');active=self.inbox.get('attempt')
                if not active:return result
            if active['phase']=='SENDING':
                return dict(status='PUBLICATION_RECOVERY_REQUIRED',published=False)
            base=self.inbox.path/'attempts'/active['id']
            prepared=read(base/'preparation.json')
            if prepared.get('native_sha256') and sha(base/'native.json')!=prepared['native_sha256']:
                raise ValueError('ZK_ARCHIVE_NATIVE_SUMMARY_DRIFT')
            self.new_work_guard()
            block,state,_,_,_=self.context(allow_halt=True)
            current=state_wire(state.functions.getState().call(block_identifier=block.number))
            if state.functions.halted().call():
                return dict(status='HALTED' if current[8] else 'HALT_OBSERVED_UNCONFIRMED',published=False)
            if self.bitcoin.call('getblockhash',active['target_height'])!=active['target_hash']:
                return dict(status='REVALIDATE_BITCOIN',published=False)
            self.stable(block)
            LocalProver(self.c,self.lock_fd).prove(base)
            if self.bitcoin.call('getblockhash',active['target_height'])!=active['target_hash']:
                return dict(status='REVALIDATE_BITCOIN',published=False)
            result=dict(status='LOCAL_PROOF_COMPUTED',proving_attempt_id=active['id'],published=False,
                receipt_sha256=sha(base/'local-result/receipt.bin'),journal_sha256=sha(base/'state.journal'))
            newer,latest,_,_,_=self.context(allow_halt=True)
            now=state_wire(latest.functions.getState().call(block_identifier=newer.number))
            native=read(base/'native.json');self.stable(newer)
            result['accepted']=now==native['after']
            if result['accepted']:self.accept_local_transition(active,native,now,newer)
            elif now!=native['before']:
                result.update(status='LOCAL_PROOF_COMPUTED_REPLAY_REQUIRED',requires_replay=True)
            self.inbox.put('local-proof:'+active['id'],result)
            return result

    def _tick(self, *, boundary='all', retain_accepted=False):
        block, state, adapter, records, core = self.context(allow_halt=True)
        current = state_wire(state.functions.getState().call(block_identifier=block.number))
        self.stable(block)
        if state.functions.halted().call():
            if current[8]:
                self.inbox.put("terminal_halt", dict(state=current, block_number=block.number, block_hash=block.hash.hex()))
                return dict(status="HALTED", confirmed=True)
            return dict(status="HALT_OBSERVED_UNCONFIRMED", confirmed=False)
        from .zk_observations import observer
        observation_log=observer(self)
        if observation_log:observation_log.fence()
        heartbeat = None
        if self.c.get("heartbeat", {}).get("enabled"):
            from .zk_worker_heartbeat import observe
            heartbeat = observe(self, block, state, current, core, self.aligned(state, block, core))
        if observation_log:
            maintenance=observation_log.maintenance(block,current,self.aligned(state,block,core))
            if maintenance:
                if heartbeat:maintenance['deadline']=min(maintenance['deadline'],heartbeat['deadline'])
                heartbeat=maintenance
        active = self.inbox.get("attempt")
        if active:
            if retain_accepted and active["phase"] != "PREPARED":
                return dict(status="PUBLICATION_RECOVERY_REQUIRED", proving_attempt_id=active["id"])
            base = self.inbox.path / "attempts" / active["id"]
            native = read(base / "native.json")
            prepared=read(base/'preparation.json')
            if prepared.get('native_sha256') and sha(base/'native.json')!=prepared['native_sha256']:
                raise ValueError('ZK_ARCHIVE_NATIVE_SUMMARY_DRIFT')
            if retain_accepted:
                return self.revisit_preparation(active, native, current, block, core)
            if current == native["after"]:
                if boundary=='prepare':return dict(status='STATE_ALREADY_ACCEPTED',proving_attempt_id=active['id'])
                self.accept_local_transition(active,native,current,block)
                if boundary=='publish':return dict(status='STATE_ALREADY_ACCEPTED',proving_attempt_id=active['id'])
                active = None
            elif current != native["before"]:
                if boundary!='all':return dict(status='REVALIDATE_STATE',proving_attempt_id=active['id'])
                if active["phase"] == "SENDING": raise ValueError("ZK_TRANSACTION_RECOVERY_REQUIRED")
                self.inbox.put("attempt", None); active = None
            else:
                if boundary=='prepare':
                    if self.bitcoin.call('getblockhash',active['target_height'])!=active['target_hash']:
                        return dict(status='REVALIDATE_BITCOIN',proving_attempt_id=active['id'])
                    return dict(status='PROOF_PREPARED',proving_attempt_id=active['id'])
                if active.get("broadcast_not_before") and active["phase"] != "SENDING":
                    # A new mature consumer request may make prepared heartbeat
                    # work urgent. Never hold it until the heartbeat boundary.
                    for key, request, _ in self.inbox.needs():
                        observation = self.observe_queued_need(key, request, block, adapter, core)
                        if observation["status"] == "NEEDS_STATE":
                            if self.c.get("service", {}).get("mode") == "paid":
                                from .zk_worker_service import source
                                try: source(self, [key])
                                except ValueError as exc:
                                    if str(exc) == "ZK_SERVICE_AWAITING_PAYMENT": continue
                                    raise
                            active["broadcast_not_before"] = 0
                            active["need_ids"] = sorted(set(active.get("need_ids", []) + [key]))
                    self.inbox.put("attempt", active)
                if active["phase"] != "SENDING" and self.bitcoin.call("getblockhash", active["target_height"]) != active["target_hash"]:
                    if boundary!='all':return dict(status='REVALIDATE_BITCOIN',proving_attempt_id=active['id'])
                    self.inbox.put("attempt", None); active = None
                else:
                    self.publication_needs = active.get("funding_need_ids", active.get("need_ids", []))
                    if active["phase"] != "SENDING" and self.c.get("service"):
                        from .zk_worker_service import preflight
                        try: preflight(self, self.publication_needs)
                        except ValueError as exc:
                            if str(exc) != "ZK_SERVICE_AWAITING_PAYMENT" or not heartbeat: raise
                            # An unpaid/reorged consumer obligation must not veto
                            # explicitly funded security work on the same proof.
                            self.publication_needs = [heartbeat["service_job_id"]]
                            preflight(self, self.publication_needs)
                            active["funding_need_ids"] = self.publication_needs
                            active["broadcast_not_before"] = heartbeat["broadcast_not_before"]
                            self.inbox.put("attempt", active)
                    prover=LocalProver(self.c,self.lock_fd)
                    seal=(prover.verify_own(base,base/'publish-verified')
                          if boundary=='publish' else prover.prove(base))
                    # Re-read confirmed state/Core after the expensive proof.
                    newer, s, _, _, _ = self.context()
                    if state_wire(s.functions.getState().call(block_identifier=newer.number)) != current:
                        return dict(status="REVALIDATE_STATE")
                    if active["phase"] != "SENDING" and self.bitcoin.call("getblockhash", active["target_height"]) != active["target_hash"]:
                        return dict(status="REVALIDATE_BITCOIN")
                    self.stable(newer)
                    if active.get("broadcast_not_before") and active["broadcast_not_before"] > newer.timestamp:
                        return dict(status="WAITING_HEARTBEAT_BROADCAST", not_before=active["broadcast_not_before"])
                    step = transition_step(self.c["chain_id"], state.address, self.c["bitcoin_policy"],
                                           (base / "state.journal").read_bytes(), seal)
                    active["phase"] = "SENDING"; self.inbox.put("attempt", active)
                    result = self.publish("zk-worker-" + active["id"], step, records)
                    return dict(status="STATE_PROGRESS" if result["status"] == "VERIFIED" else result["status"],
                                proving_attempt_id=active["id"])
        if boundary=='publish':return dict(status='NO_PREPARED_ATTEMPT')
        observations = []
        waiting_for_core = False
        aligned = self.aligned(state, block, core)
        for key, request, _ in self.inbox.needs():
            observation = self.observe_queued_need(key, request, block, adapter, core)
            waiting_for_core |= observation["status"] == "WAITING_BITCOIN_RPC"
            if observation["status"] == "READY" and not aligned: observation["status"] = "NEEDS_STATE"
            self.inbox.status(key, observation["status"])
            if observation["status"] == "NEEDS_STATE":
                if self.c.get("service", {}).get("mode") == "paid":
                    from .zk_worker_service import source
                    try: source(self, [key])
                    except ValueError as exc:
                        if str(exc) != "ZK_SERVICE_AWAITING_PAYMENT": raise
                        # A sufficiently confirmed Fresh root does not need a
                        # second paid proving job merely to rebuild its cache.
                        if (aligned and state.functions.isFresh().call(block_identifier=block.number)
                                and observation["height"] + observation["depth"] - 1 <= int(current[3])):
                            observation["own_materialization"] = True
                            observations.append((key, observation)); continue
                        self.inbox.status(key, "AWAITING_SERVICE_PAYMENT"); continue
                observations.append((key, observation))
        self.stable(block)
        from .zk_worker_batching import select as select_group
        group=select_group(self,observations,block.timestamp) if self.c.get("batching") else None
        if not observations and not heartbeat:
            return dict(status="WAITING_BITCOIN_RPC" if waiting_for_core else "IDLE")
        archive = self.archive_ref(current) if self.c.get('archive') else self.inbox.get("archive:" + current[7])
        if archive is None:
            if current[11] != "1": raise ValueError("ZK_ARCHIVE_RECOVERY_REQUIRED")
            archive = []
        # Materialization is its own transaction and never changes Fresh.
        heights = [v["height"] for _, v in observations]
        if aligned and state.functions.isFresh().call(block_identifier=block.number):
            enough = [(key, o) for key, o in observations if o["height"] + o["depth"] - 1 <= int(current[3])]
            if enough:
                if boundary=='prepare':return dict(status='MATERIALIZATION_REQUIRED')
                out = self.inbox.path / "membership" / current[7]
                native = self.native(archive, [], heights, out)
                if native["before"] != current: raise ValueError("ZK_ARCHIVE_STATE_MISMATCH")
                key, need = enough[0]
                member = next((m for m in native["memberships"] if int(m["height"]) == need["height"]), None)
                if member is not None and member["header"] == need["header"]:
                    self.stable(block)
                    self.publication_needs = ["maintenance:"+key if need.get("own_materialization") else key]
                    if self.c.get("service"):
                        from .zk_worker_service import preflight
                        preflight(self, self.publication_needs)
                    result = self.publish("zk-member-" + digest([current[6], member]), materialization_step(member), records)
                    return dict(status="HEADER_PROGRESS" if result["status"] == "VERIFIED" else result["status"], service_job_id=key)
                raise ValueError("ZK_HEADER_MEMBERSHIP_UNAVAILABLE")
        # Find Core's common ancestor in the admitted archive. Unknown external
        # state updates cannot be reconstructed from a tip alone.
        from .zk_archive import Ref
        known = None if isinstance(archive,Ref) else {header_hash(bytes.fromhex(h))[::-1].hex() for h in archive}
        checkpoint_hash=header_hash(bytes.fromhex(self.c["bitcoin_policy"]["checkpoint_header"]))[::-1].hex()
        if known is not None:known.add(checkpoint_hash)
        height = min(int(current[3]), core["blocks"])
        if archive:
            last_hash = header_hash(bytes.fromhex(archive[-1]))[::-1].hex()
            last = self.bitcoin.call("getblockheader", last_hash, True)
            if 0 <= last["height"] <= core["blocks"] and self.bitcoin.call("getblockhash", last["height"]) == last_hash:
                height = max(height, last["height"])
        floor = max(int(self.c["bitcoin_policy"]["checkpoint_height"]), height - 101)
        while height >= floor:
            candidate=self.bitcoin.call('getblockhash',height)
            if candidate==checkpoint_hash or (archive.contains(candidate) if isinstance(archive,Ref) else candidate in known):break
            height-=1
        if height < floor: raise ValueError("ZK_FORK_ARCHIVE_REVIEW_REQUIRED")
        limit = int(self.c.get("headers_per_attempt") or self.c["bitcoin_policy"]["max_headers_per_sync"])
        end = min(core["blocks"], height + limit)
        if end == height: return dict(status="WAITING_USEFUL_PROGRESS")
        headers = [self.bitcoin.call("getblockheader", self.bitcoin.call("getblockhash", h), False) for h in range(height+1, end+1)]
        target_hash = self.bitcoin.call("getblockhash", end)
        if header_hash(bytes.fromhex(headers[-1]))[::-1].hex() != target_hash: raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
        attempt_id = digest([self.inbox.get("scope"), current, headers])
        need_ids = [key for key, _ in observations]
        if heartbeat and not observations: need_ids.append(heartbeat["service_job_id"])
        if boundary == "all" and self.c.get("service"):
            from .zk_worker_service import preflight
            preflight(self, need_ids)
        jobs = self.inbox.get("job_attempts") or {}
        for key in need_ids:
            attempts = jobs.get(key, [])
            if attempt_id not in attempts:
                if len(attempts) >= int(self.c["max_attempts"]): raise ValueError("ZK_WORKER_ATTEMPT_BUDGET")
                attempts.append(attempt_id)
            jobs[key] = attempts
        self.inbox.put("job_attempts", jobs)
        base = self.inbox.path / "attempts" / attempt_id
        native = self.native(archive, headers, heights, base)
        if native["before"] != current: raise ValueError("ZK_ARCHIVE_STATE_MISMATCH")
        guest = read(zk_profile(self.c['bitcoin_policy'])['report'])
        from .zk_worker_prover import preparation_sources
        sources = preparation_sources(guest)
        for p in [Path(__file__)] + [Path(__file__).with_name(name) for name in (
                "zk_worker_prover.py","zk_worker_service.py","zk_service_binding.py","zk_worker_batching.py",
                "zk_worker_heartbeat.py","zk_worker_transport.py","spv_transport.py","spv_automation.py",
                "zk_service_issuer.py","zk_archive.py","zk_archive_native.py","address_spv.py","withdrawal_fee_spv.py","withdrawal_fee.py","withdrawal_fee_intent.py",
                "address_mint.py","deposit_intent.py","funding_envelope.py","zk_observations.py")]:
            sources[str(p.relative_to(ROOT))] = sha(p)
        preparation = dict(source_sha256=sources, input_sha256=sha(base / "state.input"),
                           journal_sha256=sha(base / "state.journal"),native_sha256=sha(base/'native.json'), program_image_id=guest["program_image_id"])
        preparation['historical_qualification_source_sha256'] = dict(guest['source_sha256'])
        if observation_log:preparation['processed_observation_checkpoint']=observation_log.rows[-1][1]
        write(base / "preparation.json", preparation)
        if self.bitcoin.call("getblockhash", end) != target_hash: raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
        self.stable(block)
        from .zk_worker_batching import attempt as record_group_attempt
        self.inbox.db.execute("BEGIN IMMEDIATE")
        try:
            self.inbox.put("attempt", dict(id=attempt_id, phase="PREPARED", target_height=end, target_hash=target_hash,
                evm_prepare_anchor=dict(number=block.number, hash=block.hash.hex()),
                need_ids=need_ids, execution_group_id=group["id"] if group else None,
                service_deadline=group["deadline"] if group else heartbeat["deadline"] if heartbeat else None,
                broadcast_not_before=heartbeat["broadcast_not_before"] if heartbeat and not observations else 0))
            record_group_attempt(self,group,attempt_id);self.inbox.db.commit()
        except BaseException:self.inbox.db.rollback();raise
        return dict(status="PROOF_PREPARED", proving_attempt_id=attempt_id)


def request_from_consumer(config_path, graph, request):
    """Local participant hook. All money/signature checks still run on retry."""
    config = load_config(config_path)
    if (config["chain_id"] != graph["chain_id"] or config["bitcoin_policy"] != graph.get("bitcoin_policy")
            or config["zk_state"] != graph["contracts"]["relay"]["address"]):
        raise ValueError("ZK_WORKER_CONSUMER_SCOPE")
    if request.get("kind")=="withdrawal_fee":
        from .withdrawal_fee_spv import check_graph
        check_graph(config,graph,request)
    if request.get('kind')=='address_deposit':
        from .address_spv import check_graph
        check_graph(config,graph,request)
    worker = Worker(config)
    try: return worker.request(request)
    finally: worker.inbox.close()


def worker_failure(exc):
    code = str(exc).split(":", 1)[0] if type(exc) is ValueError else "ZK_WORKER_LOCAL_FAILURE"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code):
        code = "ZK_WORKER_OBSERVATION_UNAVAILABLE"
    return dict(status="BLOCKED", reason=code)


def bundle_action(operation, *args, **options):
    try:
        result = operation(*args, **options)
    except Exception as exc:
        result = worker_failure(exc)
    print(json.dumps(result), flush=True)
    return 0 if result["status"] in ("ZK_BUNDLE_EXPORTED", "ZK_BUNDLE_PREPARATION_REVIEWED",
                                     "ZK_BUNDLE_PROOF_VERIFIED") else 2


def compute_and_export(worker, destination):
    from .zk_peer_bundle import check_export_destination, export_attempt

    destination = check_export_destination(destination, config=worker.c)
    computed = worker.compute_local()
    if computed["status"] not in ("LOCAL_PROOF_COMPUTED", "LOCAL_PROOF_COMPUTED_REPLAY_REQUIRED"):
        return computed
    result = export_attempt(worker, destination, include_proof=True, attempt=computed["proving_attempt_id"])
    return dict(result, computation=computed)


def main(argv=None):
    p = argparse.ArgumentParser(prog="frost-signer zk-worker", description=__doc__)
    p.add_argument("action", choices=["request", "run", "prepare", "prove", "publish", "status", "attach-service", "service-key","archive-backup","archive-restore","observations","observation-backup","observation-restore", "export-bundle", "verify-bundle"])
    p.add_argument("--config", required=True)
    p.add_argument("--request", help="Local request JSON, never executable instructions")
    p.add_argument("--once", action="store_true")
    p.add_argument("--boundary", choices=["prepare", "prove", "export", "all"], default="prove",
                   help="run defaults to local proving; export computes and exports once; all permits publication")
    p.add_argument('--archive-file')
    p.add_argument('--accepted-backup-sha256')
    p.add_argument("--bundle-file", help="Public review ZIP to create or verify")
    p.add_argument("--attempt", help="Export this local attempt ID (export-bundle only)")
    p.add_argument("--without-proof", action="store_true", help="Export preparation evidence only")
    p.add_argument("--require-proof", action="store_true", help="Require a verifiable Groth16 seal")
    a = p.parse_args(argv)
    bundle_run = a.action == "run" and a.boundary == "export"
    bundle_command = bundle_run or a.action in ("export-bundle", "verify-bundle")
    if bundle_command != bool(a.bundle_file) or (a.boundary == "export" and not bundle_run):
        p.error("--bundle-file is required for export-bundle, verify-bundle or run --boundary export")
    if (a.attempt is not None or a.without_proof) and a.action != "export-bundle":
        p.error("--attempt and --without-proof require export-bundle")
    if a.require_proof and a.action != "verify-bundle":
        p.error("--require-proof requires verify-bundle")
    if a.action == "verify-bundle":
        from .zk_peer_bundle import verify_bundle

        c = load_config(a.config, review=True)
        context = SimpleNamespace(c=c, w3=Web3(Web3.HTTPProvider(c["evm_rpc"], request_kwargs={"timeout": 30})))
        return bundle_action(verify_bundle, context, a.bundle_file, require_proof=a.require_proof)
    c = load_config(a.config)
    worker = Worker(c)
    try:
        if bundle_run:
            return bundle_action(compute_and_export, worker, a.bundle_file)
        if a.action == "export-bundle":
            from .zk_peer_bundle import export_attempt

            return bundle_action(export_attempt, worker, a.bundle_file, include_proof=not a.without_proof,
                                 attempt=a.attempt)
        if a.action in ('prepare','prove','publish'):
            try:result=worker.advance(a.action)
            except Exception as exc:
                from .participant_errors import participant_exception_code
                result=dict(status='BLOCKED',reason=participant_exception_code(exc))
            print(json.dumps(result));return 2 if result['status']=='BLOCKED' else 0
        if a.action in ('observations','observation-backup','observation-restore'):
            from .zk_observations import observer
            log=observer(worker)
            if log is None:raise ValueError('ZK_OBSERVATION_POLICY')
            if a.action=='observations':
                try:log.fence();restriction=None
                except ValueError as exc:restriction=str(exc)
                result=dict(status='PROCESSED_OBSERVATIONS',events=len(log.rows),head=log.rows[-1][1] if log.rows else None,
                    pending_outbox=log.db.execute("SELECT COUNT(*) FROM core_observation_outbox WHERE status='PENDING'").fetchone()[0],
                    restriction=restriction,completeness_proven=False,ready=False)
            else:
                if not a.archive_file:raise ValueError('ZK_OBSERVATION_BACKUP_PATH')
                result=log.backup(a.archive_file) if a.action=='observation-backup' else log.restore(a.archive_file,a.accepted_backup_sha256)
            print(json.dumps(result));return 0
        if a.action in ('archive-backup','archive-restore'):
            if not c.get('archive') or not a.archive_file:raise ValueError('ZK_ARCHIVE_CONFIG_REQUIRED')
            from .zk_archive_ops import backup,restore
            print(json.dumps((backup if a.action=='archive-backup' else restore)(worker,a.archive_file)));return 0
        if a.action == "service-key":
            from .zk_worker_service import semantic_key
            request = read(a.request)
            if set(request) != {"need_ids"} or not request["need_ids"] or any(
                    k not in {n[0] for n in worker.inbox.needs()} for k in request["need_ids"]):
                raise ValueError("ZK_SERVICE_UNKNOWN_NEED")
            print(json.dumps(dict(semantic_key=Web3.to_hex(semantic_key(worker, request["need_ids"]))))); return 0
        if a.action == "attach-service":
            from .zk_worker_service import attach
            print(json.dumps(attach(worker, read(a.request)))); return 0
        if a.action == "request": print(json.dumps(worker.request(read(a.request)))); return
        if a.action == "status":
            coverage=worker.inbox.get('auto_coverage')
            print(json.dumps(dict(attempt=worker.inbox.get("attempt"), last_status=worker.inbox.get("last_status"),
                heartbeat=worker.inbox.get("heartbeat_status"),
                automation=worker.inbox.get('automation_status'),archive_configured=bool(c.get('archive')),
                funding_coverage=dict(job=coverage['job'],state=coverage['state'],need_count=len(coverage['need_ids'])) if coverage else None,
                reserved_cost_wei=str(sum(int(v) for _, v in worker.inbox.db.execute("SELECT * FROM reservations"))),
                needs=[dict(service_job_id=k, status=s) for k, _, s in worker.inbox.needs()]))); return 0
        run = getattr(worker, {"prepare": "prepare_service", "prove": "compute_local", "all": "tick"}[a.boundary])
        while True:
            try: result = run()
            except Exception as exc:
                result = worker_failure(exc)
                from .zk_worker_heartbeat import record_status
                record_status(worker, result["reason"])
            worker.inbox.put("last_status", result)
            if result["status"] == "WAITING_USEFUL_PROGRESS":
                from .zk_worker_heartbeat import record_status
                record_status(worker, "NO_PROGRESS")
            print(json.dumps(result), flush=True)
            if a.once: return 2 if result["status"] == "BLOCKED" else 0
            time.sleep(int(c["poll_seconds"]))
    finally: worker.close()

if __name__ == "__main__": raise SystemExit(main())
