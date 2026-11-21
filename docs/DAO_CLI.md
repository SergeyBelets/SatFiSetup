# Custodian DAO: настройка и работа через CLI

Пользовательский интерфейс командной строки — `frost-signer dao`. Он работает
с кошельком конкретного пользователя: баланс, передача governance-токенов,
stake/unstake, выплаты, DAO-подписант, голосования кастодианов и стейкеров.
HTTP API, общий операторский ключ и FROST-доли для этих действий не нужны.
Все ответы — JSON; целые суммы представлены строками без округления.

Поддерживаются **Anvil (31337), Sepolia (11155111) и Ethereum mainnet (1)**.
Для chain 1 сначала выполните [V6 bootstrap](ETHEREUM_MAINNET_RUNBOOK.md),
используйте реальные внешние активы, как минимум 12 подтверждений и finalized
состояние. Перед отправкой обязательны локальные gas/fee budgets из этой
инструкции. Выбор сети не меняет надёжность эмитента токена или oracle.

## 1. Что каждому подготовить

Если система ещё не развёрнута, сначала выполните
[«Первый запуск: код и адреса друзей»](FIRST_DKG_ONCHAIN.md) до `bootstrap verify → VERIFIED`.
Там описаны установка с чистого клона, свой кошелёк, газ, выбор сети,
обнаружение приглашения и три первые DKG через зашифрованный on-chain транспорт.
В примере Alice — оператор и представитель, Bob и Carol — кастодианы.
Все трое участвуют в федерационном DKG; денежные conf-EVM/Bitcoin DKG выполняют
только Bob/Carol. Статус кандидата, голос кастодиана и доля ключа — разные права.

В уже подготовленном клоне:

```sh
cd satoshify
source .venv/bin/activate
forge build --root web3
frost-signer dao --help
```

Нужен Python 3.11+ с актуальными security-исправлениями. Если прежнее окружение
создано на Python 3.9/3.10, создайте отдельное новое окружение (файлы кошелька,
DKG и журналы остаются в своих каталогах):

```sh
python3.11 -m venv .venv-security
source .venv-security/bin/activate
python -m pip install -r signer-agent/requirements.lock
python -m pip install --no-deps -e './signer-agent[test]'
```

При дальнейшем обновлении устанавливайте и `requirements.lock`, и пакет клиента.
В новом терминале активируйте именно выбранное окружение. Node.js и браузер не нужны. CLI использует ABI
из локальной сборки; работайте с тем же commit, что и остальные участники.

Каждый задаёт **свои** локальные параметры:

```sh
export SATOHSI_DAO_RPC='<свой RPC той же EVM-сети>'
export CHAIN_ID=11155111  # Anvil: 31337; Ethereum mainnet: 1
export EVM_CONFIRMATIONS=12  # chain 1: не ниже 12, дополнительно требуется finalized
export ME='<свой полный EVM-адрес>'
export ALICE='<адрес оператора из проверенного приглашения>'
export BOB='<известный адрес Bob>'
export IDENTITY="$HOME/.local/share/satoshify/identity.json"
export DAO_DIR="$HOME/.local/share/satoshify/friends-v1/dao"
mkdir -p "$DAO_DIR"
chmod 700 "$DAO_DIR"
export SATOHSI_DAO_CONFIG="$DAO_DIR/dao.json"
export DAO_STATE="$DAO_DIR/transactions.sqlite"
```

`IDENTITY` — уже созданный по инструкции первой DKG файл собственного EVM-ключа,
владелец — текущий пользователь, права 0600. Для нового кошелька используйте
скрытый локальный импорт из раздела подготовки той инструкции. Не вставляйте
private key в аргументы команды, репозиторий, чат или общий файл. FROST `.enc`
в качестве `--identity-file` не подходит. Кошельку нужен ETH для газа.

RPC можно передавать через `--rpc`, но переменная окружения удобнее и не помещает
URL в список аргументов процесса. Не публикуйте URL с ключом провайдера.

## 2. Самостоятельно найти и проверить DAO

Используйте `$BOARD`, который вы проверили через `setup discover` в первой DKG.
Если открыли новый терминал, повторите обнаружение по известному адресу Alice:

```sh
frost-signer setup discover --operator "$ALICE" --rpc "$SATOHSI_DAO_RPC"
export BOARD='<проверенный board нужного приглашения>'
frost-signer dao init --board "$BOARD" --chain-id "$CHAIN_ID" \
  --confirmations "$EVM_CONFIRMATIONS" --output "$SATOHSI_DAO_CONFIG"
frost-signer dao status --owner "$ME"
```

`init --board` читает опубликованное предложение регистрации DAO из EVM, сверяет
конфедерацию, зарегистрированный набор контрактов и привязку к её DKG.
Никакой manifest, RPC-ключ или файл с долями у Alice запрашивать не нужно.
Treasury settlement должен совпадать с адресом в приглашении; несоответствие
останавливает `init` с `DAO_BOARD_TREASURY_MISMATCH`.
При нескольких кандидатах выбор автоматически не делается. `setup discover`
по умолчанию ищет в последних **8192 блоках** и возвращает границы
`searched_from_block` / `searched_to_block`, а также `complete_history`.
`NOT_FOUND_IN_RANGE` означает отсутствие приглашения только в проверенном диапазоне.
Для старого приглашения расширьте поиск через `setup discover --recent-blocks`
или задайте ему `--from-block`; `--from-block 0` явно запрашивает всю историю.
Слишком поздний начальный блок может скрыть нужное событие; ошибка RPC не считается
пустым успешным поиском. После выбора board DAO читает его индекс intents, без
повторного сканирования всей истории сообщений.

Альтернатива для уже известной и независимо проверенной фабрики:

```sh
frost-signer dao init --factory '<адрес ConfederationFactory>' \
  --confederation friends-v1-conf --chain-id "$CHAIN_ID" \
  --confirmations "$EVM_CONFIRMATIONS" --output "$SATOHSI_DAO_CONFIG"
```

Конфигурация версии 2 содержит только публичные адреса, сеть, контрольные хеши
кода, доказательства создания контрактов, опорный блок и число подтверждений.
RPC и ключи туда не записываются. При каждом вызове CLI заново проверяет сеть,
опорный блок, регистрацию, связи, runtime и происхождение системных контрактов.
Сохранённый `provenance[имя].block_number` используется как указатель на блок
создания. Для прямого CREATE проверяются полный блок, каноническая квитанция,
точный initcode и текущий runtime; поиск по старому `eth_getCode` не повторяется.
Публичный JSON не является доверенным кешем: доказательства из него проверяются
заново. Для вложенных authorization/validator ещё может потребоваться
историческое состояние. Полный блок и receipt создания должны оставаться доступны;
подробные границы приведены в [требованиях RPC](CONTRACT_PROVENANCE.md).
Это обнаруживает подмену/перезапуск стенда; начальный выбор правильного
приглашения или фабрики остаётся вашей проверкой. Файл не перезаписывается.
Для нового развёртывания используйте другой каталог.

Старый файл версии 1 останавливается с `DAO_CONFIG_PROVENANCE_REQUIRED_RUN_INIT`.
Повторите `init` для прежней проверенной системы с новым именем файла:

```sh
frost-signer dao init --board "$BOARD" --chain-id "$CHAIN_ID" \
  --confirmations "$EVM_CONFIRMATIONS" --output "$DAO_DIR/dao-v2.json"
export SATOHSI_DAO_CONFIG="$DAO_DIR/dao-v2.json"
frost-signer dao status --owner "$ME"
```

Используйте прежнюю согласованную политику `--confirmations`, если она отличается
от локального примера. Identity, DKG state и журнал транзакций не меняются.
Проверки не сертифицируют внешние reward token, oracle или yield market:
границы описаны в [проверке происхождения](CONTRACT_PROVENANCE.md).

`--confirmations 1` удобно для локальной репетиции. На Sepolia выберите согласованное
число подтверждений, например 3: это ожидание нескольких блоков, не доказательство
финальности Ethereum. Все `status`/списки читаются на одном последнем блоке и могут
отражать ещё не финализированное состояние; конфигурацию также создавайте после
принятого вами ожидания финальности bootstrap.

В `status` проверьте:

- адрес governance-токена, `wallet.token_balance` и `wallet.staked`;
- `wallet.dao_signer` и `wallet.is_custodian`;
- `base_rate_ppm = "120"`, `base_rate_percent = "0.012"` на старте;
- `use_reserve`, баланс резерва и последний завершённый расчёт;
- `next_settlement_day` — первые ещё не рассчитанные сутки.
- `treasury` — получатель дохода, который иначе распределялся бы LP,
  когда в дневном отчёте нет LP.

В новой версии settlement пустой список LP допустим. Если eligible stake есть,
его доля финансируется обычным образом, а оставшаяся LP-часть идёт в treasury.
Если нет ни eligible stake, ни LP, обе распределяемые части идут в treasury.
Без LP резерв не тратится на покрытие недополученной базовой доходности;
обычное пополнение резерва из избыточного дохода сохраняется. В результате
расчёта `lpIncome` равен нулю, фактический перевод записан в `treasuryIncome`.
Цена всё равно обязательна: отсутствие LP не включает резервную цену.

## 3. Как работают проверки и отправка

Все транзакционные команды **без `--send` только симулируются**.
Для этого достаточно публичного `--sender "$ME"`; ключ не нужен.
Успех — `SIMULATED`, отправки и расхода газа нет.

Для отправки нужны `--send --identity-file --state --id`.
`--id` — ваш уникальный идентификатор намерения. При обрыве повторяйте **ту же
команду с тем же ID и журналом**: исполнитель продолжит сохранённую транзакцию,
а не выполнит перевод или stake второй раз. Для нового действия нужен новый ID.
Изменение суммы/адреса/отправителя с прежним ID возвращает `IDEMPOTENCY_CONFLICT`.

`VERIFIED` означает успешную квитанцию с настроенным числом подтверждений.
`BROADCAST`/`CONFIRMING` и код выхода 2 означают ожидание: повторите исходную команду
после следующих блоков. `tx-status` показывает локальный прогресс, но сам ничего
не отправляет и не обновляет из цепочки:

```sh
frost-signer dao tx-status --state "$DAO_STATE" --id stake-001
```

Сохраняйте журнал и файлы `.evm-nonce.*` рядом с identity. Не запускайте копии
одного ключа с независимыми журналами. Журнал может содержать локальный RPC URL
и подписанные транзакции — храните его в собственном каталоге 0700. CLI не выводит
их в обычном ответе. После сбоя не удаляйте журнал и не меняйте ID до выяснения
судьбы исходной транзакции.

## 4. Получить governance-токены

Все 10 млн токенов создаются один раз на адрес `DAO_TOKEN_RECIPIENT`; в стандартном
интеграционном bootstrap это оператор. Статус кастодиана токены не выдаёт.
Первоначальный владелец может распределить согласованные доли обычным переводом:

```sh
# Выполняет владелец токенов своим identity, не получатель.
frost-signer dao transfer --to "$BOB" --amount 1000 --sender "$ME"
frost-signer dao transfer --to "$BOB" --amount 1000 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id bob-allocation-001
```

Bob проверяет `frost-signer dao status --owner "$BOB"` на своём компьютере.
Здесь и далее `--amount 100.25` означает 100,25 **целых токена**, не wei.
Используйте точку, без `1e18`; лишняя дробная точность отвергается.

## 5. Внести и вывести стейк

Сначала разрешите staking-контракту потратить конкретную сумму:

```sh
frost-signer dao approve --amount 100 --sender "$ME"
frost-signer dao approve --amount 100 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id approve-001
frost-signer dao stake --amount 100 --sender "$ME"
frost-signer dao stake --amount 100 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id stake-001
frost-signer dao status --owner "$ME"
```

CLI не устанавливает неограниченный allowance. `approve --amount 0` отзывает
остаток разрешения. Симуляция `stake` до подтверждения `approve` закономерно
завершится ошибкой. Каждый пользователь вносит токены со своего кошелька.

Внесённые после 00:00 UTC токены начинают участвовать со следующих полных суток.
Например, stake 17 октября в 15:00 UTC впервые учитывается за 18 октября;
выплата доступна после завершения этих суток **и их фактического расчёта**.
Пополнение не лишает старый eligible stake начисления за текущие сутки.

```sh
frost-signer dao unstake --amount 25 --sender "$ME"
frost-signer dao unstake --amount 25 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id unstake-001
```

Вывод выполняет только владелец. Выведенный eligible объём теряет начисление
за текущие незавершённые сутки. Уже рассчитанные выплаты можно получить и после
вывода стейка. Смена DAO-подписанта выводом не является.

## 6. Выбрать DAO-подписанта

Свой кошелёк используется по умолчанию. Для действующего кастодиана из
`status.custodians` дополнительная подпись согласия не требуется:

```sh
frost-signer dao set-signer --signer "$BOB" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id signer-bob-001
```

Для **другого своего кошелька** сначала подпишите EIP-712 согласие его ключом.
Оба файла остаются у вас; отдельный канал между друзьями не нужен:

```sh
export OTHER_IDENTITY='<локальный identity второго собственного кошелька>'
export OTHER='<его EVM-адрес>'
frost-signer dao signer-consent --owner "$ME" \
  --identity-file "$OTHER_IDENTITY" --output "$DAO_DIR/consent-001.json"
frost-signer dao set-signer --signer "$OTHER" --consent "$DAO_DIR/consent-001.json" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id signer-other-001
```

Согласие привязано к сети, staking-контракту, обоим адресам и nonce владельца.
После любой смены подписанта прежнее согласие больше не подходит для нового
действия. Создайте новый файл; существующий не перезаписывается. Эта команда
подписывает локальным EOA; создание согласия контрактного кошелька/ERC-1271
в данной CLI-команде не реализовано.

Вернуться к себе:

```sh
frost-signer dao set-signer --signer self \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id signer-self-001
```

Подписант не получает токены, право unstake или claim владельца, статус кастодиана
либо MPC-доли. Новой DKG не происходит. Голоса и обычные DAO-транзакции публичны
в EVM; шифрование DKG-пакетов не скрывает их calldata.

## 7. Найти и получить начисления

```sh
frost-signer dao rewards --owner "$ME"
# Для старых начислений укажите дату первого eligible дня из status:
frost-signer dao rewards --owner "$ME" --from-day 2026-10-18 --limit 32
frost-signer dao day --day 2026-10-18
```

Без `--from-day` показываются последние 32 рассчитанных дня, а не вся история.
Используйте `next_from_day` для следующей страницы. `total_claimable` — сумма
**только показанной страницы**. Даты в примере замените своими: CLI принимает
также целочисленные номера суток из `rows.day_number`.

```sh
frost-signer dao claim --days 2026-10-18 2026-10-19 --sender "$ME"
frost-signer dao claim --days 2026-10-18 2026-10-19 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id claim-001
```

За один вызов — 1–32 уникальных дня; CLI сортирует их. Указывайте рассчитанные,
ещё не полученные дни. Выплата поступает владельцу в reward-токене, указанном
в конфигурации. При пустой истории сначала проверьте `next_settlement_day`:
время само по себе доход не создаёт.

В текущем контракте, если за сутки вообще нет eligible stake, доля стейкеров
(включая комиссии) переходит поставщикам ликвидности. Это относится в том числе
к первым суткам запуска; новый CLI не меняет это отмеченное в обзоре ТЗ правило.

Поставщик ликвидности отдельно получает свой накопленный доход:

```sh
frost-signer dao claim-lp \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id lp-claim-001
```

Оператор должен обеспечить реальные поступления и последовательно закрывать
сутки по [потоку комиссий](FEE_FLOW.md), `frost-signer dao-report --help` и
[операционным командам](OPERATIONAL_API.md). Суточный отчёт требует отдельного
FROST-одобрения. Новый пользовательский CLI не создаёт доход, не подставляет
TVL и не подписывает отчёт за кастодианов. Корректность входных BTC/TVL/LP-данных
по-прежнему требует проверки перед подписанием.

## 8. Голосование кастодианов: ставка и резерв

Сначала посмотрите список и текущую ставку:

```sh
frost-signer dao proposals --from-id 1 --limit 20
frost-signer dao status
```

Действующий кастодиан или его текущий DAO-подписант создаёт предложение:

```sh
export CUSTODIAN='<адрес представляемого действующего кастодиана>'
frost-signer dao propose-rate --percent 0.013 --custodian "$CUSTODIAN" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id rate-001
frost-signer dao proposals
```

`--percent 0.013` означает **0,013% в сутки**, не 1,3%. `0.012` — исходная ставка.
Минимальный шаг — 0,0001 процентного пункта. В ответе списка найдите нужный `id`,
параметр, значение, срок и `requiredVotes`. Создание предложения не добавляет голос.
Если есть `next_from_id`, продолжите список с `--from-id` этого значения.
То же правило пагинации действует для `decisions`.

Каждый голосующий выполняет со своего компьютера:

```sh
export PROPOSAL='<id выбранного предложения>'
frost-signer dao vote --proposal "$PROPOSAL" --custodian "$CUSTODIAN" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id rate-vote-001
```

Порог — `ceil(2N/3)` на момент создания: 11 → 8, 3 → 2, 2 → 2.
В bootstrap-примере только Bob и Carol — кастодианы, значит нужны оба.
Делегированные токены не увеличивают голоса. Один signer-адрес также не может
отдать два голоса в одном предложении: если два кастодиана назначили одного
подписанта, для обоих голосов им нужно выбрать разные signer-адреса.

После достижения порога любой кошелёк может исполнить предложение:

```sh
frost-signer dao execute --proposal "$PROPOSAL" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id rate-execute-001
```

Новая ставка действует со **следующих суток UTC**. До этого `status` показывает
старую действующую ставку. Голосование/исполнение имеют срок семь суток;
более новое исполненное предложение того же параметра отменяет возможность
исполнения старого. `proposals` показывает `expired` и `superseded`.

Предложение включить использование резерва:

```sh
frost-signer dao propose-reserve --enabled true --custodian "$CUSTODIAN" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id reserve-001
```

Затем `proposals → vote → execute` с **ID этого** предложения и новыми ID операций.
Выключение — `--enabled false`. Используется тот же порог 2/3, ставка не меняется.

Для добровольного пополнения резерва требуются reward-токены на собственном кошельке:

```sh
frost-signer dao approve-reserve --amount 10 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id reserve-approve-001
frost-signer dao fund-reserve --amount 10 \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id reserve-fund-001
```

Это безвозвратное пополнение общего резерва, не пользовательский депозит с выводом.
Оно не включает `USE_RESERVE` автоматически. В тестовом bootstrap токен выплат — TUSD;
этот CLI не предоставляет произвольный mint.

## 9. Решения стейкеров

```sh
frost-signer dao decisions --from-id 1 --limit 20
```

Проверьте `metadataURI`, `contentHash`, `snapshotDay`, `deadline` и кворум.
CLI выводит URI как текст, не скачивает и не исполняет его содержимое.
Список может быть пустым: создание решения выполняется отдельно через
`CustodianDAODecisionBoard.createDecision` после FROST-одобрения его helper hash;
это операторская процедура, а не право любого держателя токена.

Текущий DAO-подписант голосует за явно указанного владельца eligible stake:

```sh
export OWNER='<адрес владельца стейка, которого вы представляете>'
export DECISION='<id проверенного решения>'
frost-signer dao decision-vote --decision "$DECISION" --owner "$OWNER" --choice for \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id decision-vote-001
```

Варианты: `for`, `against`, `abstain`. Вес — eligible stake владельца за snapshot-сутки,
голос один на владельца. Для нескольких делегировавших владельцев нужны отдельные
команды и отдельные ID операций. После срока любой кошелёк фиксирует результат:

```sh
frost-signer dao decision-finalize --decision "$DECISION" \
  --send --identity-file "$IDENTITY" --state "$DAO_STATE" --id decision-finalize-001
```

Это голосование фиксирует решение сообщества. Оно само не вызывает произвольные
контракты и не заменяет кастодиальное голосование по ставке.

## 10. Ошибки и завершение репетиции

| Ответ | Действие |
|---|---|
| `CHAIN_MISMATCH` | Исправьте RPC; не меняйте ожидаемую сеть ради обхода проверки. |
| `DAO_NOT_REGISTERED` | Оператор должен завершить регистрацию DAO и bootstrap. |
| `DAO_DISCOVERY_REQUIRES_ONE_REGISTERED_FACTORY` | Проверьте board, диапазон блоков и регистрацию; CLI не выбирает неизвестную фабрику. |
| `DAO_ANCHOR_CHANGED` | Возможен reorg или сброс Anvil. Проверьте цепочку перед новым `init`. |
| `DAO_CONFIG_BINDING_CHANGED` | Адреса/код не совпали. Не исправляйте хеш вручную без расследования. |
| `PRIVATE_FILE_PERMISSIONS` | Свой обычный файл, владелец — вы, права 0600; симлинки запрещены. |
| `CONTRACT_ERC20InsufficientAllowance` | Подтвердите отдельный `approve` на нужную сумму. |
| `CONTRACT_InvalidSignerConsent` | Неверный второй кошелёк либо устаревший nonce; создайте новое согласие. |
| `CONTRACT_NotCustodianSigner` | Проверьте кастодиана и его текущего DAO-подписанта. |
| `CONTRACT_ThresholdNotMet` | Ещё не набрано нужное число голосов. |
| `CONTRACT_DayNotFunded` | Сутки ещё не рассчитаны; дождитесь оператора. |
| `IDEMPOTENCY_CONFLICT` | Прежний ID относится к другому намерению; сначала проверьте `tx-status`. |
| `BROADCAST` / `CONFIRMING` | Повторите исходную команду с тем же ID/журналом после блоков. |

Успешная совместная репетиция: каждый сам обнаружил DAO, получил токены,
внёс стейк, проверил полномочия выбранного подписанта; кастодианы провели
предложение через порог и увидели изменение со следующих суток; после
финансирования и расчёта суток владелец получил reward-токены и вывел стейк.
Ни один участник не передаёт свой ключ оператору.

Для разработчика: `pytest signer-agent/tests/test_dao_cli.py -q` проверяет
точность сумм, даты, защиту ключей и ошибки; полный `python ops/local_acceptance.py`
включает установленный CLI поверх реальной DKG/регистрации, голосование,
EIP-712 delegation, профинансированные выплаты и discovery через board.
