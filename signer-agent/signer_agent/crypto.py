"""Local participant IPC. Never log request/response bodies: they contain secrets."""
import json
import os
import subprocess
from pathlib import Path


def call(op, **params):
    binary = Path(os.environ.get('SATOHSI_CRYPTO_BIN', str(Path(__file__).resolve().parents[2] / 'frost-crypto/target/debug/satohsi-frost-crypto')))
    result = subprocess.run([str(binary), '--ipc'], input=json.dumps(dict(op=op, **params)),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('CRYPTO_VALIDATION_FAILED')
    return json.loads(result.stdout)
