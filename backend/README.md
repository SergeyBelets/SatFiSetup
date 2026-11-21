# SatoshiFi backend

Serverless backend for btcyield.online/create-confederation.html:
AWS SAM stack with `satoshifi-api` and `satoshifi-watch` Lambda functions.

Specification: wrapper ТЗ (operator-owned). Secrets live only in SSM under `/satoshifi/*`.

## Signed-text formats

`src/satoshifi/common/` builds and checks every text that wallets and keys sign
(spec section 5.3). `site/js/satoshifi-formats.js` is the browser twin.
Both are checked against the same vectors:

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                   # Python side
(cd tests/js && npm install --no-audit --no-fund && node check_vectors.js)   # JS side
```

`tests/vectors/make_vectors.py` regenerates the vectors. Its keys are derived
from public strings: test-only, never fund them.
