#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Read-only SatoshiFi custodian check: never sends transactions or takes journal locks.
H="$HOME/.satoshifi"; SDK="$HOME/satfi-sdk"; PY="$SDK/signer-agent/.venv/bin/python"
echo "== SatoshiFi check $(date '+%F %T')"
echo "Шаги установки: $(tr '\n' ' ' < "$H/start.done" 2>/dev/null)"
for p in "btc_rpc_shim.py --upstream:Bitcoin-прокси" "satoshifi_api.py report:Отчёты боту" "flow dkg --board:Церемония DKG"; do
  pgrep -f "${p%%:*}" >/dev/null && echo "  ✔ ${p##*:} работает" || echo "  ✘ ${p##*:} НЕ работает"
done
if [ -f "$H/identity.json" ] && [ -f "$H/evm-rpc.url" ]; then
  "$PY" - "$H/identity.json" "$H/evm-rpc.url" <<'PY'
import json, sys
from eth_account import Account
from web3 import Web3
me = Account.from_key(json.load(open(sys.argv[1]))["private_key"]).address
w3 = Web3(Web3.HTTPProvider(open(sys.argv[2]).read().strip(), request_kwargs=dict(timeout=20)))
latest, pending = w3.eth.get_transaction_count(me), w3.eth.get_transaction_count(me, "pending")
print("  Рабочий адрес: %s  баланс %.4f ETH  транзакций %d%s" % (me, w3.eth.get_balance(me) / 1e18, latest,
      "  ! %d в очереди" % (pending - latest) if pending > latest else ""))
PY
fi
if [ -f "$H/dkg.log" ]; then
  echo "  Последние строки церемонии:"; tail -n 5 "$H/dkg.log" | cut -c1-200 | sed 's/^/    /'
  age=$(( $(date +%s) - $(stat -c %Y "$H/dkg.log" 2>/dev/null || stat -f %m "$H/dkg.log") ))
  [ "$age" -gt 3600 ] && echo "  ! Лог церемонии не обновлялся $((age/60)) мин — ЗАМИРАНИЕ? Запустите satoshifi-start.sh ещё раз и сообщите оператору."
fi
[ -f "$H/start.log" ] && { echo "  Последние строки установки:"; tail -n 3 "$H/start.log" | cut -c1-200 | sed 's/^/    /'; }
