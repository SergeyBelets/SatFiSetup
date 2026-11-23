"""Confirmed EVM log checkpoints with canonicality checks and auditable reorg rollback.
Indexing never approves, burns, refunds or signs an operation.
"""

import json
from .evm_network import read_block, verify_anchor
from .evm_rpc import evm_web3
from web3.exceptions import BlockNotFound
from .safe_errors import DiagnosticArgumentParser
from .operations import Journal, canonical


class Indexer:
    def __init__(self, w3, journal, chain_id):
        self.w3 = w3
        self.journal = journal
        self.chain_id = str(chain_id)
        if str(w3.eth.chain_id) != self.chain_id:
            raise ValueError("CHAIN_MISMATCH")
        with journal.db() as c:
            c.executescript(
                """CREATE TABLE IF NOT EXISTS indexed_blocks(chain_id TEXT,height INTEGER,hash TEXT,PRIMARY KEY(chain_id,height));
            CREATE TABLE IF NOT EXISTS indexed_logs(chain_id TEXT,block_hash TEXT,height INTEGER,tx_hash TEXT,log_index INTEGER,address TEXT,topics TEXT,data TEXT,canonical INTEGER,PRIMARY KEY(chain_id,block_hash,tx_hash,log_index));
            CREATE INDEX IF NOT EXISTS indexed_logs_height ON indexed_logs(chain_id,height);"""
            )

    def poll(self, addresses, start, confirmations=12, limit=100):
        if start < 0 or confirmations < 1 or not 1 <= limit <= 1000:
            raise ValueError("INDEX_BOUNDS")
        query = "SELECT height,hash FROM indexed_blocks WHERE chain_id=? ORDER BY height DESC LIMIT ?"
        with self.journal.db() as c:
            indexed = c.execute(query, (self.chain_id, limit + 1)).fetchall()
        last = indexed[0] if indexed else None
        tip = -1
        try:
            tip = self.w3.eth.block_number - confirmations + 1
            if self.chain_id == "1":
                tip = min(tip, read_block(self.w3, 12).number)
            if last and last["height"] > tip:
                raise ValueError("EVM_RPC_BEHIND")
            if not last and tip < start:
                return dict(status="INDEXED", checkpoint=str(start - 1), lag="0")
            anchor = self.w3.eth.get_block(tip)
            forked = []
            for row in indexed[:limit]:
                try:
                    verify_anchor(self.w3, row["height"], row["hash"], safe=anchor)
                except ValueError as error:
                    if str(error) != "EVM_REORG_RECOVERY_REQUIRED":
                        raise
                    forked.append(row["height"])
                else:
                    break
            remaining = indexed[len(forked):]
            checkpoint = remaining[0]["height"] if remaining else start - 1
            begin = max(start, checkpoint + 1)
            end = min(tip, begin + limit - len(forked) - 1)
            observed = []
            for height in range(begin, end + 1):
                block = self.w3.eth.get_block(height)
                logs = self.w3.eth.get_logs(
                    {"fromBlock": height, "toBlock": height, "address": addresses}
                )
                verify_anchor(self.w3, height, block.hash)
                if any(log.blockHash != block.hash for log in logs):
                    raise ValueError("INDEX_LOG_BLOCK_MISMATCH")
                observed.append((height, block.hash.hex(), logs))
            verify_anchor(self.w3, anchor.number, anchor.hash)
        except (BlockNotFound, ValueError) as error:
            if isinstance(error, BlockNotFound) or str(error) in (
                    "EVM_RPC_BEHIND", "EVM_CONFIRMATIONS_PENDING"):
                checkpoint = last["height"] if last else start - 1
                return dict(status="AWAITING_EVM_RPC", checkpoint=str(checkpoint),
                            lag=str(max(0, tip - checkpoint)))
            if str(error) == "EVM_REORG_RECOVERY_REQUIRED":
                raise ValueError("INDEX_REORG_DURING_READ") from None
            raise

        # The RPC scan holds no SQLite write transaction. If another poll
        # committed while it ran, leave that newer checkpoint untouched.
        with self.journal.db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute(query, (self.chain_id, limit + 1)).fetchall() != indexed:
                return dict(status="INDEX_CHECKPOINT_CHANGED")
            for height in forked:
                c.execute(
                    "UPDATE indexed_logs SET canonical=0 WHERE chain_id=? AND height=? AND canonical=1",
                    (self.chain_id, height),
                )
                c.execute("DELETE FROM indexed_blocks WHERE chain_id=? AND height=?", (self.chain_id, height))
            for height, block_hash, logs in observed:
                for log in logs:
                    c.execute(
                        "INSERT OR REPLACE INTO indexed_logs VALUES (?,?,?,?,?,?,?,?,1)",
                        (
                            self.chain_id,
                            block_hash,
                            height,
                            log.transactionHash.hex(),
                            log.logIndex,
                            log.address,
                            canonical([x.hex() for x in log.topics]),
                            log.data.hex(),
                        ),
                    )
                c.execute(
                    "INSERT INTO indexed_blocks VALUES (?,?,?)",
                    (self.chain_id, height, block_hash),
                )
                checkpoint = height
        return dict(
            status="RECOVERY_REQUIRED" if forked else "INDEXED",
            checkpoint=str(checkpoint),
            lag=str(max(0, tip - checkpoint)),
        )


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("--rpc", required=True)
    p.add_argument("--chain-id", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--addresses", nargs="+", required=True)
    p.add_argument("--from-block", type=int, required=True)
    p.add_argument("--confirmations", type=int, default=12)
    a = p.parse_args(argv)
    result = Indexer(evm_web3(a.rpc), Journal(a.state), a.chain_id).poll(
        a.addresses, a.from_block, a.confirmations
    )
    print(json.dumps(result))
    return 0 if result["status"] == "INDEXED" else 2
