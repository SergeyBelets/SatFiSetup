"""Confirmed EVM log checkpoints with canonicality checks and auditable reorg rollback.
Indexing never approves, burns, refunds or signs an operation.
"""

import json
from web3 import Web3
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
            CREATE TABLE IF NOT EXISTS indexed_logs(chain_id TEXT,block_hash TEXT,height INTEGER,tx_hash TEXT,log_index INTEGER,address TEXT,topics TEXT,data TEXT,canonical INTEGER,PRIMARY KEY(chain_id,block_hash,tx_hash,log_index));"""
            )

    def poll(self, addresses, start, confirmations=12, limit=100):
        if confirmations < 1 or limit < 1 or limit > 1000:
            raise ValueError("INDEX_BOUNDS")
        with self.journal.db() as c:
            last = c.execute(
                "SELECT height,hash FROM indexed_blocks WHERE chain_id=? ORDER BY height DESC LIMIT 1",
                (self.chain_id,),
            ).fetchone()
            rewound = False
            while last:
                try:
                    block = self.w3.eth.get_block(last["height"])
                except BlockNotFound:
                    block = None
                except Exception:
                    raise ValueError("INDEX_RPC_UNAVAILABLE")
                if block is not None and block.hash.hex() == last["hash"]:
                    break
                rewound = True
                c.execute(
                    "UPDATE indexed_logs SET canonical=0 WHERE chain_id=? AND height>=?",
                    (self.chain_id, last["height"]),
                )
                c.execute(
                    "DELETE FROM indexed_blocks WHERE chain_id=? AND height=?",
                    (self.chain_id, last["height"]),
                )
                last = c.execute(
                    "SELECT height,hash FROM indexed_blocks WHERE chain_id=? ORDER BY height DESC LIMIT 1",
                    (self.chain_id,),
                ).fetchone()
            begin = max(start, last["height"] + 1 if last else start)
            tip = self.w3.eth.block_number - confirmations + 1
            end = min(tip, begin + limit - 1)
            for height in range(begin, end + 1):
                block = self.w3.eth.get_block(height)
                logs = self.w3.eth.get_logs(
                    {"fromBlock": height, "toBlock": height, "address": addresses}
                )
                if self.w3.eth.get_block(height).hash != block.hash:
                    raise ValueError("INDEX_REORG_DURING_READ")
                for log in logs:
                    if log.blockHash != block.hash:
                        raise ValueError("INDEX_LOG_BLOCK_MISMATCH")
                    c.execute(
                        "INSERT OR REPLACE INTO indexed_logs VALUES (?,?,?,?,?,?,?,?,1)",
                        (
                            self.chain_id,
                            block.hash.hex(),
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
                    (self.chain_id, height, block.hash.hex()),
                )
        return dict(
            status="RECOVERY_REQUIRED" if rewound else "INDEXED",
            checkpoint=str(max(begin - 1, end)),
            lag=str(max(0, tip - end)),
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
    result = Indexer(Web3(Web3.HTTPProvider(a.rpc)), Journal(a.state), a.chain_id).poll(
        a.addresses, a.from_block, a.confirmations
    )
    print(json.dumps(result))
    return 2 if result["status"] == "RECOVERY_REQUIRED" else 0
