"""Owner withdrawal: a generated quote, durable primary request, then fee consent.

No operator JSON or raw ABI arrays are required. The primary and consent are
separate owner transactions; a partial run retains and reports the exact primary
request. It never recreates it, lowers P or silently selects a different source.
"""
import fcntl
import json
import os
import secrets
from pathlib import Path

from eth_utils import keccak
from web3.logs import DISCARD
from web3 import Web3

from . import crypto
from .bootstrap import artifact
from .btc_fee_reserve import read, number, digest, ZERO
from .withdrawal_fee_client import Owner

Q = 10**10
MAX_MONEY = 2100000000000000
OPTIONS = {"calculation", "asset", "fee_mode", "payout_sats", "total_units", "fee_cap_sats",
           "destination", "pool", "nft", "deadline", "max_rate", "refund_fee_cap", "refund_address"}
QUOTE_FIELDS = {"version", "operation_id", "owner", "chain_id", "bitcoin_network", "graph", "options", "cost",
                "custody_generation", "derivation_generation", "created_at", "expires_at", "anchor", "fee_estimate"}


def fee_function(config, discount=0):
    """Exact existing FeeManager arithmetic, including its pre-discount cap."""
    kind, fixed, minimum, coefficient = map(int, config)
    if kind not in (0, 1) or not 0 <= discount <= 10000 or not 0 <= fixed <= 10000 or not 0 <= coefficient <= 10000:
        raise ValueError("WITHDRAWAL_USER_FEE_PROFILE")
    def calculate(amount):
        base = amount * fixed // 10000 if kind == 0 else minimum + amount * coefficient // 10000
        return min(amount, base) * (10000 - discount) // 10000
    return calculate


def split(calculation, asset, fee_mode, cap, fee, available, *, payout=None, total=None):
    if asset not in ("MP", "S") or fee_mode not in ("BTC", asset) or calculation not in ("payout", "total"):
        raise ValueError("WITHDRAWAL_USER_OPTIONS")
    tranche = cap * Q if fee_mode != "BTC" else 0
    if available < tranche:
        raise ValueError("WITHDRAWAL_USER_BALANCE")
    if calculation == "total":
        if total is None or payout is not None or total > available or total <= tranche:
            raise ValueError("WITHDRAWAL_USER_BALANCE")
        main = total - tranche
        if asset == "MP":
            main = main // Q * Q  # Never silently spend sub-satoshi MP.
    else:
        if payout is None or total is not None:
            raise ValueError("WITHDRAWAL_USER_OPTIONS")
        target = payout * Q
        if asset == "MP":
            main = target
        else:
            lo, hi = target, available - tranche
            if hi < lo or hi - fee(hi) < target:
                raise ValueError("WITHDRAWAL_USER_BALANCE")
            # Canonical fixed/dynamic fees with a fixed valid NFT discount have
            # monotone net value. Bisection finds the least gross S tranche.
            while lo < hi:
                mid = (lo + hi) // 2
                if mid - fee(mid) >= target:
                    hi = mid
                else:
                    lo = mid + 1
            main = lo
    service = fee(main) if asset == "S" else 0
    principal = (main - service) // Q
    if not 0 < principal <= MAX_MONEY or main + tranche > available or calculation == "payout" and principal != payout:
        raise ValueError("WITHDRAWAL_USER_BALANCE")
    return dict(principal_sats=str(principal), main_units=str(main), fee_units=str(tranche),
                total_lock_units=str(main + tranche), service_fee_units=str(service),
                rounding_units=str(main - service - principal * Q),
                unspent_total_units=str(total - main - tranche) if total is not None else "0")


class Withdrawal:
    def __init__(self, inspector):
        self.i = inspector
        self.owner = Owner(inspector)

    def graph(self, ctx):
        f = ctx["withdrawal_fees"]
        if f is None:
            raise ValueError("WITHDRAWAL_FEE_GRAPH")
        return dict(reserve=ctx["fee"].address, registry=ctx["registry"].address,
                    escrow=ctx["escrow"].address, mining=ctx["pool"].address,
                    token=read(ctx["pool"], ctx["block"].number, "mpToken"), credits=f["credits"].address)

    def identity(self):
        from .spv_automation import identity
        return identity(self.i.c["identity_file"])

    def destination(self, address):
        return crypto.call("bitcoin_address_script_v1", address=address, network=self.i.c["bitcoin_network"])

    def simple(self, ctx, address):
        from .bitcoin_trust import checked_runtime
        from .callback_admission import review_callback_configuration
        n = ctx["block"].number
        if not Web3.is_checksum_address(address) or address == ZERO:
            raise ValueError("WITHDRAWAL_FEE_OWNING_POOL")
        checked_runtime(self.i.w3, address, "SimpleMultiPool", n)
        # This pool is an internal CREATE child. The authenticated callback
        # registry/leaf creation record below proves its origin; a generic
        # direct-CREATE search cannot authenticate a canonical factory child.
        admission, _, _ = review_callback_configuration(ctx["escrow"], read(ctx["escrow"], n, "callbackPoolRegistry"), n)
        s = self.i.w3.eth.contract(address=address, abi=artifact("SimpleMultiPool")["abi"])
        if not read(admission, n, "isSimplePool", address) or read(s, n, "withdrawalEscrow") != ctx["escrow"].address:
            raise ValueError("WITHDRAWAL_FEE_OWNING_POOL")
        return s

    def evaluate(self, ctx, options):
        if not isinstance(options, dict) or set(options) != OPTIONS:
            raise ValueError("WITHDRAWAL_USER_OPTIONS")
        o, n, owner = options, ctx["block"].number, self.identity()
        asset, mode = o["asset"], o["fee_mode"]
        cap = number(o["fee_cap_sats"], 1, int(read(ctx["fee"], n, "protocolFeeCap")))
        number(o["max_rate"], 1)
        number(o["refund_fee_cap"], 0, int(read(ctx["fee"], n, "protocolFeeCap")))
        number(o["deadline"], ctx["block"].timestamp + 1)
        destination = self.destination(o["destination"])
        refund = self.destination(o["refund_address"]) if mode == "BTC" else None
        if mode != "BTC" and o["refund_address"] is not None:
            raise ValueError("WITHDRAWAL_USER_OPTIONS")
        token = self.i.w3.eth.contract(address=self.graph(ctx)["token"], abi=artifact("PoolMpToken")["abi"])
        available = int(read(token, n, "balanceOf", owner))
        fee, limit, s = lambda _: 0, 2**256 - 1, None
        if asset == "S":
            if o["nft"] is not None:
                number(o["nft"], 0, 2**256 - 1)
            s = self.simple(ctx, o["pool"])
            available = int(read(s, n, "getAvailableUserPoolBalance", owner, token.address))
            shares = self.i.w3.eth.contract(address=read(s, n, "getShareToken"), abi=artifact("MultiPoolShareToken")["abi"])
            available = min(available, int(read(shares, n, "balanceOf", owner)))
            manager = read(s, n, "feeManager")
            if manager != ZERO:
                from .bitcoin_trust import checked_runtime
                checked_runtime(self.i.w3, manager, "FederationFeeManager", n)
                f = self.i.w3.eth.contract(address=manager, abi=artifact("FederationFeeManager")["abi"])
                discount = 0
                modifier = read(f, n, "nftModifier")
                if o["nft"] is not None and modifier != ZERO:
                    checked_runtime(self.i.w3, modifier, "FederationNFTModifier", n)
                    nft = self.i.w3.eth.contract(address=modifier, abi=artifact("FederationNFTModifier")["abi"])
                    identifier = number(o["nft"], 0, 2**256 - 1)
                    discount, limit, valid = read(nft, n, "getDiscount", identifier)
                    if not valid or read(nft, n, "ownerOf", identifier) != owner:
                        raise ValueError("WITHDRAWAL_USER_NFT")
                fee = fee_function(read(f, n, "getUnwrapFeeConfig"), int(discount))
        elif o["pool"] is not None or o["nft"] is not None:
            raise ValueError("WITHDRAWAL_USER_OPTIONS")
        payout = number(o["payout_sats"], 1, MAX_MONEY) if o["payout_sats"] is not None else None
        total = number(o["total_units"], 1, 2**256 - 1) if o["total_units"] is not None else None
        # The discount's volume applies only to the ordinary withdrawal, not the
        # fee-only S tranche; no second unwrap/NFT charge is added to that cap.
        cost = split(o["calculation"], asset, mode, cap, fee, available, payout=payout, total=total)
        if int(cost["main_units"]) > int(limit):
            raise ValueError("WITHDRAWAL_USER_NFT")
        minimum = int(read(ctx["escrow"], n, "minP2WPKHSats" if destination["script"].startswith("0014") else "minP2TRSats"))
        if int(cost["principal_sats"]) < minimum:
            raise ValueError("WITHDRAWAL_USER_MINIMUM")
        if asset == "S":
            # Cross-check local inversion against the real FeeManager call.
            if manager != ZERO:
                actual = read(f, n, "calculateFee", False, int(cost["main_units"])) if o["nft"] is None else read(
                    f, n, "quoteFeeWithNFT", False, int(cost["main_units"]), number(o["nft"]), owner)
                if int(actual) != int(cost["service_fee_units"]):
                    raise ValueError("WITHDRAWAL_USER_FEE_PROFILE")
        return cost, dict(balance_units=str(available), balance_after_lock_units=str(available - int(cost["total_lock_units"])),
            script=destination["script"], destination=destination["address"], refund_script=refund["script"] if refund else "",
            refund_address=refund["address"] if refund else None), s

    def prepare(self, options, *, ttl="300"):
        ctx = self.i.context()
        cost, display, _ = self.evaluate(ctx, options)
        self.i.fence(ctx)
        b = ctx["block"]
        expires = min(b.timestamp + number(ttl, 1, 3600), int(options["deadline"]))
        estimate = dict(status="UNAVAILABLE", source="Bitcoin Core estimatesmartfee", sat_per_vbyte=None, expires_at=str(expires))
        try:
            from .bitcoin_rpc import satoshis
            row = self.i.bitcoin.call("estimatesmartfee", 6)
            if row.get("feerate") is not None:
                estimate.update(status="AVAILABLE", sat_per_vbyte=str((satoshis(row["feerate"]) + 999) // 1000))
        except (ValueError, RuntimeError):
            pass  # Fee estimation is optional; it cannot create a funding claim.
        quote = dict(version="1", operation_id=secrets.token_hex(32), owner=self.identity(), chain_id=self.i.c["chain_id"],
            bitcoin_network=self.i.c["bitcoin_network"], graph=self.graph(ctx), options=options, cost=cost,
            custody_generation=str(read(ctx["registry"], b.number, "custodyGeneration")),
            derivation_generation=str(read(ctx["registry"], b.number, "derivationGeneration")),
            created_at=str(b.timestamp), expires_at=str(expires), anchor=dict(number=str(b.number), hash=b.hash.hex()), fee_estimate=estimate)
        return dict(status="WITHDRAWAL_QUOTE", quote=quote, quote_hash=digest(quote), **display,
            treasury_promised_for_request_sats="0", treasury_total_promised_sats=str(read(ctx["fee"], b.number, "promisedSats")),
            fee_funding_qualified=bool(read(ctx["withdrawal_fees"]["verifier"], b.number, "FEE_QUALIFIED")),
            blocked_by="FEE_PURPOSE_UNQUALIFIED" if options["fee_mode"] == "BTC" and not read(ctx["withdrawal_fees"]["verifier"], b.number, "FEE_QUALIFIED") else None,
            maximum_token_refund_units=cost["fee_units"], btc_funding_and_refund="SEPARATE_BITCOIN_FEES" if options["fee_mode"] == "BTC" else "NOT_REQUIRED",
            restrictions=["Exact per-version owner consent is still required before BTC signing.",
                          "No automatic cancellation after signing; RBF requires consent to each new exact version.",
                          "Fee budget does not guarantee inclusion time or bypass maturity, Fresh, halt or signer availability."], ready=False)

    def validate_quote(self, quote, accepted):
        if (not isinstance(quote, dict) or set(quote) != QUOTE_FIELDS or quote["version"] != "1"
                or accepted != digest(quote) or quote["owner"] != self.identity()
                or quote["chain_id"] != self.i.c["chain_id"] or quote["bitcoin_network"] != self.i.c["bitcoin_network"]):
            raise ValueError("WITHDRAWAL_USER_QUOTE")
        identifier = quote["operation_id"]
        if not isinstance(identifier, str) or len(identifier) != 64 or bytes.fromhex(identifier).hex() != identifier:
            raise ValueError("WITHDRAWAL_USER_QUOTE")
        ctx = self.i.context()
        if self.graph(ctx) != quote["graph"]:
            raise ValueError("WITHDRAWAL_FEE_GRAPH")
        if self.i.w3.eth.get_block(int(quote["anchor"]["number"])).hash.hex() != quote["anchor"]["hash"]:
            raise ValueError("WITHDRAWAL_USER_REORG")
        return ctx

    def fresh(self, quote, *, primary=False):
        ctx = self.validate_quote(quote, digest(quote))
        now = max(ctx["block"].timestamp, self.i.w3.eth.get_block("latest").timestamp)
        if (primary and now >= int(quote["expires_at"])) or now >= int(quote["options"]["deadline"]):
            raise ValueError("WITHDRAWAL_USER_QUOTE_EXPIRED")
        if (str(read(ctx["registry"], ctx["block"].number, "custodyGeneration")) != quote["custody_generation"]
                or str(read(ctx["registry"], ctx["block"].number, "derivationGeneration")) != quote["derivation_generation"]):
            raise ValueError("WITHDRAWAL_FEE_GENERATION")
        self.i.fence(ctx)
        if primary:
            cost, _, _ = self.evaluate(ctx, quote["options"])
            if cost != quote["cost"]:
                raise ValueError("WITHDRAWAL_USER_QUOTE_CHANGED")
        return ctx

    def request(self, ctx, quote, key):
        r = read(ctx["escrow"], ctx["block"].number, "getRequest", key)
        o, cost = quote["options"], quote["cost"]
        expected_pool = o["pool"] if o["asset"] == "S" else ZERO
        script = bytes.fromhex(self.destination(o["destination"])["script"])
        if (r[0] != expected_pool or o["asset"] == "MP" and int(r[1]) != int(quote["operation_id"], 16)
                or r[2] != quote["graph"]["token"] or r[3] != quote["owner"]
                or int(r[4]) != int(cost["principal_sats"]) * Q or bytes(r[5]) != script):
            raise ValueError("WITHDRAWAL_USER_REQUEST")
        return r

    def submit(self, quote, accepted):
        self.validate_quote(quote, accepted)
        # Separate workflow lock: Owner.call also takes the shared fee lock.
        fd = os.open(str(self.i.journal.path) + ".withdrawal.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self._submit(quote)
        finally:
            os.close(fd)

    def _submit(self, q):
        key = "withdrawal-user:" + q["operation_id"]
        entry = self.i.journal.put(key, q)
        saved = entry["result"]
        o, cost, g = q["options"], q["cost"], q["graph"]
        op = digest([g["credits"], q["operation_id"], digest(q)])
        if not saved:
            script = bytes.fromhex(self.destination(o["destination"])["script"])
            if o["asset"] == "MP":
                token = self.i.w3.eth.contract(address=g["token"], abi=artifact("PoolMpToken")["abi"])
                ctx = self.i.context()
                # The exact primary allowance is independent of fee consent.
                if int(read(token, ctx["block"].number, "allowance", q["owner"], g["escrow"])) < int(cost["main_units"]):
                    self.owner.send(op + ":primary-allowance", "PoolMpToken", token.address, "approve",
                        [g["escrow"], int(cost["main_units"])], lambda: self.fresh(q, primary=True))
                target, kind, method = g["escrow"], "BitcoinWithdrawalEscrow", "requestDirect"
                args = [int(q["operation_id"], 16), token.address, int(cost["main_units"]), script]
            else:
                target, kind, method = o["pool"], "SimpleMultiPool", "requestWithdrawalQuoted"
                args = [g["token"], int(cost["main_units"]), script, int(cost["service_fee_units"]),
                        int(cost["principal_sats"]) * Q, int(o["nft"] or "0"), o["nft"] is not None]
            receipt = self.owner.send(op + ":primary", kind, target, method, args, lambda: self.fresh(q, primary=True))
            ctx = self.i.context()
            events = ctx["escrow"].events.Escrowed().process_receipt(receipt, errors=DISCARD)
            events = [e for e in events if e["address"] == g["escrow"]]
            if len(events) != 1:
                raise ValueError("WITHDRAWAL_USER_REQUEST")
            request = bytes(events[0]["args"]["id"])
            self.request(ctx, q, request)
            saved = dict(request=request.hex(), primary_anchor=dict(number=str(receipt.blockNumber), hash=receipt.blockHash.hex()))
            self.i.journal.update(key, "PRIMARY_CONFIRMED", saved)
        request = bytes.fromhex(saved["request"])
        ctx = self.i.context()
        if self.i.w3.eth.get_block(int(saved["primary_anchor"]["number"])).hash.hex() != saved["primary_anchor"]["hash"]:
            raise ValueError("WITHDRAWAL_USER_REORG")
        self.request(ctx, q, request)
        b = read(ctx["withdrawal_fees"]["credits"], ctx["block"].number, "getBudget", request)
        if int(b[4]) == 0:
            self.fresh(q)
            mode = ("NONE", "BTC", "MP", "S").index(o["fee_mode"])
            consent = [request, 1, mode, o["pool"] if mode == 3 else ZERO, int(o["fee_cap_sats"]),
                int(cost["fee_units"]), int(o["max_rate"]), int(o["refund_fee_cap"]), int(o["deadline"]),
                int(q["custody_generation"]), int(q["derivation_generation"]),
                bytes.fromhex(self.destination(o["refund_address"])["script"]) if mode == 1 else b""]
            self.owner.call("consentFee" if mode == 3 else "consent", [self.owner_wire(consent)])
            ctx = self.i.context()
            b = read(ctx["withdrawal_fees"]["credits"], ctx["block"].number, "getBudget", request)
        if (int(b[3]) != ("NONE", "BTC", "MP", "S").index(o["fee_mode"]) or int(b[6]) < int(o["fee_cap_sats"])
                or b[0] != q["owner"] or b[1] != g["token"]):
            raise ValueError("WITHDRAWAL_USER_CONSENT")
        self.i.journal.update(key, "CONSENT_CONFIRMED", saved)
        return self.status(q["operation_id"])

    @staticmethod
    def owner_wire(consent):
        from .bootstrap import Bootstrap
        return Bootstrap.wire(consent)

    def status(self, operation_id):
        entry = self.i.journal.get("withdrawal-user:" + operation_id)
        q, saved = entry["request"], entry["result"]
        ctx = self.validate_quote(q, digest(q))
        if not saved:
            return dict(status="WITHDRAWAL_NOT_CONFIRMED", operation_id=operation_id, ready=False)
        if self.i.w3.eth.get_block(int(saved["primary_anchor"]["number"])).hash.hex() != saved["primary_anchor"]["hash"]:
            raise ValueError("WITHDRAWAL_USER_REORG")
        request = bytes.fromhex(saved["request"])
        r = self.request(ctx, q, request)
        from .withdrawal_fee_cli import status
        fee = status(self.i, request)
        return dict(status="WITHDRAWAL_REQUESTED" if fee["revision"] != "0" else "WITHDRAWAL_FEE_CONSENT_REQUIRED",
            operation_id=operation_id, request=request.hex(), local_id=str(r[1]), primary_state=("NONE", "PENDING", "BOUND", "PAID", "REFUNDED")[int(r[7])],
            quote_hash=digest(q), cost=q["cost"], fee=fee, ready=False)

    def version_quote(self, operation_id, batch):
        from .withdrawal_fee_client import quote
        primary = self.status(operation_id)
        if "request" not in primary:
            raise ValueError("WITHDRAWAL_USER_REQUEST")
        if len(batch) != 32:
            raise ValueError("WITHDRAWAL_FEE_QUOTE_BINDING")
        exact = quote(self.i, batch, bytes.fromhex(primary["request"]))
        if exact["principal_sats"] != primary["cost"]["principal_sats"]:
            raise ValueError("WITHDRAWAL_USER_REQUEST")
        return dict(status="WITHDRAWAL_VERSION_QUOTE", operation_id=operation_id, quote=exact, ready=False)

    def approve_version(self, operation_id, batch, accepted):
        exact = self.version_quote(operation_id, batch)["quote"]
        if accepted != exact["terms_hash"]:
            raise ValueError("WITHDRAWAL_FEE_QUOTE_BINDING")
        return self.owner.call("approveVersion", ["0x" + batch.hex(), "0x" + exact["request"],
            exact["revision"], "0x" + exact["terms_hash"]])

    def cancel(self,operation_id):
        """Owner exit before Signing; expiry/outage is never cancellation proof."""
        entry=self.i.journal.get('withdrawal-user:'+operation_id);q=entry['request'];saved=entry['result']
        ctx=self.validate_quote(q,digest(q))
        if not saved:raise ValueError('WITHDRAWAL_USER_REQUEST')
        key=bytes.fromhex(saved['request']);r=self.request(ctx,q,key)
        if r[7]==4:return self.status(operation_id)
        def check():
            current=self.validate_quote(q,digest(q));row=self.request(current,q,key)
            if row[7] not in (1,2):raise ValueError('WITHDRAWAL_FEE_SIGNATURE_MAY_EXIST')
            root=read(current['withdrawal_fees']['payments'],'latest','rootForRequest',key)
            if bytes(root)!=bytes(32) and int(read(current['batch'],'latest','batchState',root))!=3:
                raise ValueError('WITHDRAWAL_FEE_SIGNATURE_MAY_EXIST')
            if row[7]==2 and int(read(current['batch'],'latest','batchState',row[6]))!=3:
                raise ValueError('WITHDRAWAL_FEE_SIGNATURE_MAY_EXIST')
        check()
        target=ctx['escrow'].address;kind='BitcoinWithdrawalEscrow';method='refund';args=[key]
        if q['options']['asset']=='S':target=r[0];kind='SimpleMultiPool';method='cancelWithdrawal';args=[int(r[1])]
        self.owner.send(operation_id+':cancel-primary',kind,target,method,args,check)
        result=self.status(operation_id)
        if result['primary_state']!='REFUNDED':raise ValueError('WITHDRAWAL_USER_REQUEST')
        return result
