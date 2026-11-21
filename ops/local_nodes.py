#!/usr/bin/env python3
"""Foreground persistent Anvil/regtest stack. Ctrl-C stops only its own child processes."""
import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--directory", required=True)
    p.add_argument("--evm-port", type=int, default=8545)
    p.add_argument("--bitcoin-port", type=int, default=18443)
    a = p.parse_args()
    for binary in ("anvil", "bitcoind"):
        if not shutil.which(binary):
            raise SystemExit("MISSING_TOOL: " + binary)
    if "v30.3" not in subprocess.check_output(["bitcoind", "--version"], text=True):
        raise SystemExit("BITCOIN_VERSION_REQUIRE_30_3")
    root = Path(a.directory).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock = (root / "nodes.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    btc = root / "bitcoin"
    btc.mkdir(exist_ok=True, mode=0o700)
    nodes = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        nodes.append(
            subprocess.Popen(
                [
                    "anvil",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(a.evm_port),
                    "--silent",
                    "--state",
                    str(root / "anvil.json"),
                    "--preserve-historical-states",
                ],
                stdout=subprocess.DEVNULL,
            )
        )
        nodes.append(
            subprocess.Popen(
                [
                    "bitcoind",
                    "-regtest",
                    "-server",
                    "-txindex=1",
                    "-listen=0",
                    "-printtoconsole=0",
                    "-datadir=" + str(btc),
                    "-rpcport=" + str(a.bitcoin_port),
                ],
                stdout=subprocess.DEVNULL,
            )
        )
        for _ in range(100):
            if any(c.poll() is not None for c in nodes):
                raise RuntimeError("NODE_START_FAILED")
            if (btc / "regtest/.cookie").exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("NODE_START_TIMEOUT")
        result = dict(
            status="NODES_RUNNING",
            evm_rpc="http://127.0.0.1:" + str(a.evm_port),
            bitcoin_rpc="http://127.0.0.1:" + str(a.bitcoin_port),
            bitcoin_cookie=str(btc / "regtest/.cookie"),
            mode="integration",
            bitcoin_network="regtest",
            chain_id="31337",
        )
        (root / "endpoints.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        while all(c.poll() is None for c in nodes):
            time.sleep(1)
        raise RuntimeError("NODE_EXITED")
    except KeyboardInterrupt:
        pass
    finally:
        for c in nodes:
            if c.poll() is None:
                c.terminate()
        for c in nodes:
            try:
                c.wait(timeout=20)
            except subprocess.TimeoutExpired:
                c.kill()
                c.wait()
        lock.close()


if __name__ == "__main__":
    main()
