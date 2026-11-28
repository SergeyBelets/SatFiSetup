#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SatoshiFi custodian start script (Linux, Windows WSL2, macOS).
# Usage: bash satoshifi-start.sh --commit <40-hex SatFiSetup commit sent by the operator in Telegram>
# Rerun the same command at any time: finished steps are skipped.
set -euo pipefail
umask 022

COMMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 2 ;;
  esac
done
if ! [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Нужен аргумент --commit <40 hex символов>, его присылает оператор в Telegram."; exit 2
fi

SDK="$HOME/satfi-sdk"
H="$HOME/.satoshifi"
mkdir -p "$H" && chmod 700 "$H"
DONE="$H/start.done"; touch "$DONE"; chmod 600 "$DONE"
LOG="$H/start.log"
exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ✔ %s\n' "$*"; }
die()  { printf '\n✘ %s\n' "$1"; exit "${2:-1}"; }
done_step() { grep -qx "$1" "$DONE"; }
mark()  { echo "$1" >> "$DONE"; }
ask() {
  local answer
  while true; do
    read -r -p "   $1 [да/нет]: " answer </dev/tty
    case "$answer" in да|Да|ДА|y|Y|yes) return 0 ;; нет|Нет|НЕТ|n|N|no) return 1 ;; esac
  done
}
cfg() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$SDK/tools/satoshifi-start.json" "$1"; }

OS="$(uname -s)"; ARCH="$(uname -m)"
IS_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
if [ "$OS" = Darwin ]; then RAM_MB=$(( $(sysctl -n hw.memsize) / 1048576 )); else RAM_MB=$(( $(awk '/^MemTotal:/{print $2}' /proc/meminfo) / 1024 )); fi

# ---------------------------------------------------------------- 1. computer
if ! done_step computer; then
  say "1. Проверка компьютера"
  case "$OS-$ARCH" in
    Linux-x86_64) ok "Linux x86-64$([ $IS_WSL = 1 ] && echo ' (WSL2)')" ;;
    Darwin-arm64) ok "macOS Apple Silicon" ;;
    *) die "Платформа $OS-$ARCH не поддерживается. Windows: запустите внутри WSL2 Ubuntu." 10 ;;
  esac
  FREE_GB=$(df -Pk "$HOME" | awk 'NR==2{print int($4/1048576)}')
  [ "$FREE_GB" -ge 30 ] || die "Свободно ${FREE_GB} ГБ, нужно не меньше 30 ГБ." 10
  ok "Свободно на диске: ${FREE_GB} ГБ"
  [ "$RAM_MB" -ge 7500 ] || die "Оперативной памяти $((RAM_MB/1024)) ГБ, нужно не меньше 8 ГБ$([ $IS_WSL = 1 ] && echo ' внутри WSL2 (по умолчанию WSL получает половину памяти Windows)')." 10
  ok "Оперативная память: $((RAM_MB/1024)) ГБ"
  command -v nvidia-smi >/dev/null && ok "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)" \
    || echo "   ! GPU NVIDIA не найдена — для церемонии не нужна, понадобится позже для доказательств."
  mark computer
fi

# ---------------------------------------------------------------- 2. packages
if ! done_step packages; then
  say "2. Установка системных программ"
  if [ "$OS" = Linux ]; then
    echo "   Будут установлены через apt: git curl build-essential pkg-config libssl-dev python3-venv python3-dev"
    ask "Установить (понадобится пароль sudo)?" || die "Отказ от установки." 30
    sudo apt-get update -y
    sudo apt-get install -y git curl build-essential pkg-config libssl-dev python3-venv python3-dev
  else
    xcode-select -p >/dev/null 2>&1 || { xcode-select --install; die "Установите Command Line Tools и запустите скрипт снова." 40; }
    command -v python3 >/dev/null || die "Нужен Python 3.11+ (brew install python@3.12)." 10
  fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || die "Нужен Python 3.11 или новее." 10
  ok "Python $(python3 -V | cut -d' ' -f2)"
  mark packages
fi

# ---------------------------------------------------------------- 3. SDK source
if ! done_step source; then
  say "3. Загрузка SDK (коммит $COMMIT)"
  REPO="https://github.com/SergeyBelets/SatFiSetup.git"
  if [ ! -d "$SDK/.git" ]; then git clone --quiet "$REPO" "$SDK"; fi
  git -C "$SDK" fetch --quiet origin
  git -C "$SDK" -c advice.detachedHead=false checkout --quiet "$COMMIT"
  [ "$(git -C "$SDK" rev-parse HEAD)" = "$COMMIT" ] || die "Коммит не совпал." 20
  [ -z "$(git -C "$SDK" status --porcelain --untracked-files=no)" ] || die "Файлы SDK изменены локально." 20
  chmod -R go-w "$SDK"
  ok "SDK на коммите $COMMIT, файлы совпадают с репозиторием"
  mark source
else
  [ "$(git -C "$SDK" rev-parse HEAD)" = "$COMMIT" ] || die "SDK на другом коммите, чем в команде. Сверьте коммит с оператором." 20
fi
[ "$(cfg chain_id)" = 1 ] || die "Конфигурация не для Ethereum mainnet." 20
OPERATOR="$(cfg operator)"; DEPLOYMENT="$(cfg deployment_id)"

# ---------------------------------------------------------------- 4. toolchains
export PATH="$HOME/.cargo/bin:$HOME/.foundry/bin:$PATH"
if ! done_step toolchains; then
  say "4. Rust $(cfg rust_toolchain) и Foundry $(cfg foundry_version)"
  if ! command -v rustup >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o "$H/rustup-init.sh"
    sh "$H/rustup-init.sh" -y --default-toolchain none --no-modify-path
  fi
  rustup toolchain install "$(cfg rust_toolchain)" --profile minimal
  if ! forge --version 2>/dev/null | grep -q "$(cfg foundry_version)"; then
    curl -sSfL https://foundry.paradigm.xyz -o "$H/foundry-install.sh"
    bash "$H/foundry-install.sh"
    "$HOME/.foundry/bin/foundryup" --install "v$(cfg foundry_version)"
  fi
  forge --version | grep -q "$(cfg foundry_version)" || die "Foundry $(cfg foundry_version) не установился." 10
  grep -q '.foundry/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.cargo/bin:$HOME/.foundry/bin:$PATH"' >> "$HOME/.bashrc"
  ok "Rust и Foundry установлены"
  mark toolchains
fi

# ---------------------------------------------------------------- 5. build
PY="$SDK/signer-agent/.venv/bin/python"
FS="$SDK/signer-agent/.venv/bin/frost-signer"
if ! done_step build; then
  say "5. Сборка SDK (20–60 минут)"
  [ -x "$PY" ] || python3 -m venv "$SDK/signer-agent/.venv"
  echo "   Сборка идёт в 1–2 потока, чтобы не исчерпать память; браузер и другие программы можно не закрывать."
  # Parallel solc/rustc can exhaust RAM and freeze the machine in swap. Fewer
  # threads, and on Linux a memory ceiling: if it is exceeded only the build is
  # killed, with a visible error, instead of the whole computer hanging.
  export FOUNDRY_THREADS=1 CARGO_BUILD_JOBS=2
  LIMIT=()
  CG="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers"
  if [ "$OS" = Linux ] && command -v systemd-run >/dev/null && grep -qw memory "$CG" 2>/dev/null; then
    LIMIT=(systemd-run --user --scope --quiet -p "MemoryMax=$((RAM_MB / 2))M" -p MemorySwapMax=0)
    ok "Предел памяти для сборки: $((RAM_MB / 2)) МБ"
  fi
  if ! (cd "$SDK" && ${LIMIT[@]+"${LIMIT[@]}"} "$PY" ops/bootstrap_host.py build); then
    (cd "$SDK" && "$PY" ops/bootstrap_host.py check) || true
    die "Сборка прервалась. Если выше есть «Killed» — не хватило памяти: закройте лишние программы и запустите ту же команду снова." 10
  fi
  chmod -R go-w "$SDK"
  STATUS=$(cd "$SDK" && "$PY" ops/bootstrap_host.py check | "$PY" -c "import json,sys;print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = READY_TO_CONFIGURE ] || { (cd "$SDK" && "$PY" ops/bootstrap_host.py check); die "Сборка не готова." 10; }
  ok "READY_TO_CONFIGURE"
  mark build
fi

# ---------------------------------------------------------------- 6. work key
ID="$H/identity.json"
if ! done_step identity; then
  say "6. Рабочий адрес"
  if [ -e "$ID" ]; then die "$ID уже существует. Новый ключ не создаётся." 20; fi
  "$PY" - "$ID" <<'PY'
import json, os, sys
from eth_account import Account
from signer_agent.dkg import atomic_private
Account.enable_unaudited_hdwallet_features()
account, words = Account.create_with_mnemonic(num_words=12)
print("\n   Сид-фраза рабочего адреса (запишите на бумагу, никому не показывайте):\n")
print("   " + words + "\n")
input("   Записали? Нажмите Enter — экран очистится. ")
print("\033[2J\033[H", end="")
check = input("   Для проверки введите 3-е и 9-е слова через пробел: ").split()
w = words.split()
if check != [w[2], w[8]]:
    sys.exit("Слова не совпали. Запустите скрипт снова — сид-фраза будет новой.")
atomic_private(sys.argv[1], json.dumps(dict(private_key=account.key.hex())).encode())
print("   Рабочий адрес:", account.address)
PY
  [ -e "$ID" ] || die "Ключ не сохранён." 30
  mark identity
fi
ME=$("$PY" -c "import json,sys;from eth_account import Account;print(Account.from_key(json.load(open(sys.argv[1]))['private_key']).address)" "$ID")

# ---------------------------------------------------------------- 7. Ethereum RPC
RPCF="$H/evm-rpc.url"
if ! done_step evm_rpc; then
  say "7. Ethereum RPC"
  echo "   Нужен свой URL Ethereum mainnet с архивными запросами (например, Alchemy)."
  read -r -s -p "   Вставьте URL и нажмите Enter (на экране не отобразится): " URL </dev/tty; echo
  "$PY" - "$URL" <<'PY'
import sys
from web3 import Web3
w3 = Web3(Web3.HTTPProvider(sys.argv[1], request_kwargs=dict(timeout=30)))
assert w3.eth.chain_id == 1, "не Ethereum mainnet"
w3.eth.get_block("finalized")
code = w3.eth.get_code("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", block_identifier=6100000)
assert len(code) > 0, "RPC не отвечает на архивные запросы"
print("   ✔ chain 1, finalized и архивные запросы работают")
PY
  printf '%s\n' "$URL" > "$RPCF"; chmod 600 "$RPCF"; unset URL
  mark evm_rpc
fi
RPC="$(cat "$RPCF")"

# ---------------------------------------------------------------- 8. Bitcoin RPC
COOKIE="$H/btc-rpc.cookie"; PORT="$(cfg bitcoin_shim_port)"
start_shim() {
  if ! pgrep -f "btc_rpc_shim.py --upstream" >/dev/null; then
    (setsid nohup "$PY" "$SDK/tools/btc_rpc_shim.py" --upstream "$(cfg bitcoin_upstream)" --port "$PORT" >> "$H/btc_rpc_shim.log" 2>&1 &)
    sleep 2
  fi
}
if ! done_step btc_rpc; then
  say "8. Bitcoin RPC (локальный прокси к $(cfg bitcoin_upstream))"
  [ -e "$COOKIE" ] || { "$PY" -c "import secrets;print('satoshifi:'+secrets.token_hex(16))" > "$COOKIE"; chmod 600 "$COOKIE"; }
  start_shim
  "$PY" - "$PORT" <<'PY'
import json, sys, urllib.request
def call(method, params):
    body = json.dumps(dict(jsonrpc="1.0", id=1, method=method, params=params)).encode()
    req = urllib.request.Request("http://127.0.0.1:%s" % sys.argv[1], body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["result"]
info = call("getblockchaininfo", [])
assert info["chain"] == "main" and not info["pruned"] and not info["initialblockdownload"], info
assert call("getindexinfo", ["txindex"])["txindex"]["synced"]
print("   ✔ Bitcoin mainnet, блок", info["blocks"], ", txindex синхронизирован")
PY
  mark btc_rpc
fi
start_shim

# ---------------------------------------------------------------- 9. monitoring
if ! done_step monitor; then
  say "9. Мониторинг (отчёт боту каждые 5 минут)"
  "$PY" - "$H/report.json" "$(cfg api_base)" "$DEPLOYMENT" "$ID" "$RPC" "$(cfg min_balance_wei)" "$(cfg bind_page)" <<'PY'
import json, os, sys
p, api, dep, ident, rpc, minb, page = sys.argv[1:]
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, json.dumps(dict(api_base=api, deployment=dep, identity_file=ident, rpc_url=rpc,
                             min_balance_wei=minb, report_period_s=300, bind_page=page, chain_id=1),
                        indent=1).encode()); os.close(fd)
PY
  mark monitor
fi
if ! pgrep -f "satoshifi_api.py report" >/dev/null; then
  (setsid nohup "$PY" "$SDK/tools/satoshifi_api.py" report --config "$H/report.json" >> "$H/report.log" 2>&1 &)
fi

# ---------------------------------------------------------------- 9b. binding
open_url() {
  if [ $IS_WSL = 1 ]; then powershell.exe -NoProfile -Command "Start-Process '$1'" >/dev/null 2>&1 || true
  elif [ "$OS" = Darwin ]; then open "$1" >/dev/null 2>&1 || true
  else xdg-open "$1" >/dev/null 2>&1 || true; fi
}
if ! done_step bound; then
  say "9b. Привязка рабочего адреса к вашему кошельку"
  echo "   Рабочий адрес: $ME"
  echo "   Откроется страница btcyield.online. Подключите тот личный кошелёк, адрес которого вы сообщили"
  echo "   оператору, и подпишите сообщение. Это не транзакция и ничего не стоит."
  if ! "$PY" "$SDK/tools/satoshifi_api.py" bind --config "$H/report.json" | while IFS= read -r line; do
      case "$line" in
        BIND_LINK*)     echo; echo "   Если браузер не открылся, откройте ссылку вручную:"; echo "   ${line#BIND_LINK }"; echo
                        echo "   Жду подписи (ссылка действует час, затем появится новая)..."; open_url "${line#BIND_LINK }" ;;
        BIND_ACCEPTED*) ok "Привязка принята" ;;
        BIND_REJECTED*) echo "   ✘ Привязка отклонена: ${line#BIND_REJECTED }" ;;
        BIND_EXPIRED*)  echo "   Ссылка истекла." ;;
      esac
    done; then
    die "Привязка не принята. NOT_ALLOWLISTED — оператор ещё не внёс ваш личный адрес; сообщите ему и запустите скрипт снова." 30
  fi
  mark bound
fi

# ---------------------------------------------------------------- 10. ETH
balance() { "$PY" -c "import sys;from web3 import Web3;w=Web3(Web3.HTTPProvider(sys.argv[1]));print(w.eth.get_balance(sys.argv[2]))" "$RPC" "$ME"; }
if ! done_step funded; then
  say "10. ETH на рабочий адрес"
  echo "   Рабочий адрес: $ME"
  echo "   Отправьте на него 0,05 ETH (сеть Ethereum). Жду поступления..."
  until [ "$(balance)" -ge "$(cfg min_balance_wei)" ]; do sleep 30; done
  ok "Баланс: $("$PY" -c "print($(balance)/1e18)") ETH"
  mark funded
fi

export SATOSHIFI_MAX_FEE_PER_GAS_WEI="$(cfg max_fee_per_gas_wei)"
export SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="$(cfg max_priority_fee_per_gas_wei)"
export SATOSHIFI_MAX_TX_GAS="$(cfg max_tx_gas)"
PDIR="$H/participant"; mkdir -p "$PDIR"; chmod 700 "$PDIR"
INV="$H/invitation.json"

# ---------------------------------------------------------------- 11. invitation
if ! done_step joined; then
  say "11. Готов, жду приглашения от оператора $OPERATOR"
  until "$FS" setup discover --operator "$OPERATOR" --rpc "$RPC" > "$INV.tmp" 2>/dev/null \
        && "$PY" - "$INV.tmp" "$DEPLOYMENT" "$ME" "$INV" <<'PY'
import json, sys
found = [i for i in json.load(open(sys.argv[1])).get("invitations", [])
         if i["invitation"].get("deployment_id") == sys.argv[2]]
if not found: sys.exit(1)
if len(found) > 1: sys.exit("Найдено несколько приглашений с этим deployment ID — свяжитесь с оператором.")
json.dump(found[0], open(sys.argv[4], "w"), indent=1)
PY
  do sleep 60; done
  rm -f "$INV.tmp"
  "$PY" - "$INV" "$ME" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); i = d["invitation"]
parts = i.get("participants", [])
print("\n   Приглашение найдено")
print("   deployment ID:   ", i.get("deployment_id"))
print("   участники:       ", len(parts))
for n, p in enumerate(parts, 1): print("      %d. %s%s" % (n, p, "   ← это вы" if p.lower() == sys.argv[2].lower() else ""))
for k in ("threshold", "min_custodians", "deadline", "chain_id", "evm_confirmations"):
    if k in i: print("   %-17s %s" % (k + ":", i[k]))
if "deployment_fee" in i: print("   deployment_fee:   ", i["deployment_fee"])
print("\n   КОД ПРИГЛАШЕНИЯ (fingerprint):", d["fingerprint"])
if sys.argv[2].lower() not in [p.lower() for p in parts]:
    sys.exit("Вашего рабочего адреса нет в составе — свяжитесь с оператором.")
PY
  echo
  ask "Код приглашения совпал с тем, что оператор прислал в Telegram, и вы вступаете?" || die "Вы отказались от вступления." 30
  BOARD=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['board'])" "$INV")
  FP=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['fingerprint'])" "$INV")
  "$FS" setup join-chain --board "$BOARD" --rpc "$RPC" \
    --accept-fingerprint "$FP" --identity-file "$ID" --directory "$PDIR" \
    --bitcoin-rpc "http://127.0.0.1:$PORT" --bitcoin-cookie "$COOKIE"
  mark joined
fi
BOARD=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['board'])" "$INV")

# ---------------------------------------------------------------- 12. DKG
say "12. Церемония создания ключей"
if pgrep -f "flow dkg --board $BOARD" >/dev/null; then
  ok "Церемония уже идёт (лог: $H/dkg.log)"
else
  (setsid nohup "$FS" flow dkg --board "$BOARD" --rpc "$RPC" --identity-file "$ID" --directory "$PDIR" \
     --bitcoin-rpc "http://127.0.0.1:$PORT" --bitcoin-cookie "$COOKIE" --interval 15 --timeout 604800 \
     >> "$H/dkg.log" 2>&1 &)
  ok "Церемония запущена в фоне (лог: $H/dkg.log)"
fi
echo
echo "   Оставьте компьютер включённым. Проверка в любой момент: bash ~/satfi-sdk/tools/satoshifi-check.sh"
echo "   После перезагрузки запустите эту же команду — скрипт продолжит с того же места."
