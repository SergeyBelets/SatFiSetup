# Bitcoin-платёж без обмена файлами между участниками

Эта инструкция продолжает [первую DKG](FIRST_DKG_ONCHAIN.md) после успешного
bootstrap и [закрепления Bitcoin registry каждым участником](BITCOIN_SIGNER_TRUST.md).
V6 поддерживает все сочетания Ethereum mainnet/Sepolia/local и Bitcoin
mainnet/testnet4/regtest: [матрица и команды flow](NETWORKS_AND_AUTOMATION.md).
Для публичного Bitcoin нужны reviewed checkpoint и live Core policy из
[Bitcoin инструкции](BITCOIN_MAINNET_RUNBOOK.md). Для Ethereum chain 1
сначала выполните [V6 bootstrap](ETHEREUM_MAINNET_RUNBOOK.md): paid fees,
finalized Ethereum и локальные gas budgets обязательны при любом выборе Bitcoin.
Все суммы Bitcoin ниже — целые сатоши. У MP федерации и S конфедерации V3
по `10^10` базовых единиц на сатоши. Для переноса MP команды подготовки и
расчёта исходного платежа запускаются на board федерации. Для погашения S
они запускаются на board конфедерации и готовят план v5. Оплата майнерам входит
в погашаемую сумму S. [Порядок MP → S → BTC](BITCOIN_CUSTODY_TRANSFER.md#профиль-v3-с-прямым-btc-резервом) ·
[Формула выплаты в V3](CONFEDERATION_DAO_IN_KIND.md#btc-и-комиссия-майнеров).

В текущем custody-профиле эту инструкцию выполняют кастодианы денежного ключа,
а не представитель федерации. В примере Alice/Bob/Carol Bitcoin- и conf-EVM
roster — Bob/Carol, threshold 2, индексы Bitcoin-плана **1 и 2**.
Bootstrap разрешает кастодианам публикацию публичных approval intents;
старый оператор после старта не обязателен. Слово «оператор» ниже обозначает
кастодиана, который ведёт платёж и его локальный journal. Для старого board без
custody delegation публикация intents остаётся за его исходным оператором.

Каждому нужны код одной версии, свой EVM identity, своё зашифрованное состояние
DKG и пароль к нему, доступ к EVM RPC и собственному доверенному Bitcoin Core.
Адрес `DKGOnchainBoard` обнаруживается по известному адресу друга-оператора через
`setup discover`. Файлы identity, password и state остаются на своей машине.
Копировать их или nonce journal друзьям нельзя. Дополнительный канал передачи
планов, commitments и signature shares не нужен.

Для regtest все Bitcoin Core должны видеть **одну и ту же синхронизированную regtest-цепочку**.
Свежие независимые regtest-узлы этого не обеспечивают: при отдельных машинах
заранее настройте их P2P-подключение к общей тестовой сети. Это требование к
инфраструктуре Bitcoin, а не канал обмена пакетами DKG или подписи.
Включите `txindex=1` в каждом Core и дождитесь завершения индексации;
`bitcoin-cli -regtest getindexinfo` должен показывать `txindex.synced=true`.
Если индекс включён после создания цепочки, сначала дождитесь его построения.
CLI запрашивает подтверждённые транзакции через `getrawtransaction` без blockhash;
без индекса проверка settlement может остановиться с `OBSERVATION_UNAVAILABLE`.
Локальный launcher `ops/local_nodes.py` уже запускает Core с `-txindex=1`.
Для mainnet/testnet4 каждый использует собственный полностью синхронизированный,
непрореженный Core выбранной сети с готовым `txindex`, согласованный с V6 policy.
CLI проверяет genesis/checkpoint/chainwork/свежесть, а не только имя сети.

## 1. Локальные параметры

Используйте переменные из инструкции DKG: `BOARD`, `RPC`, `IDENTITY`,
`PARTICIPANT_DIR`. Добавьте адрес своего Core и локальный путь к его cookie:

```sh
BTC_RPC=http://127.0.0.1:18443
BTC_COOKIE=/absolute/path/to/bitcoin/regtest/.cookie

btc() {
  frost-signer dkg "$@" \
    --board "$BOARD" --role btc_ceremony --rpc "$RPC" \
    --identity-file "$IDENTITY" \
    --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
    --password-file "$PARTICIPANT_DIR/password" \
    --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
}
```

`--board` всегда указывает на DKG board. Отдельный `--signing-board` указывает на
Bitcoin board. RPC URL и cookie не публикуются. Каталог состояния должен
принадлежать участнику и быть недоступным для записи другим пользователям.
Для mainnet замените локальные `BTC_RPC` и `BTC_COOKIE` на свой mainnet Core
(обычно loopback-порт 8332 и cookie в корне его datadir). Саму сеть и policy CLI
читает из принятого V6 приглашения и закреплённого registry; отдельный shared
policy-файл, `--bitcoin-network` или `--relay` участникам не нужны.

Перед mainnet/testnet4-платежом любой участник проверяет и при необходимости догружает
закреплённый raw relay:

```sh
btc btc-network-status
btc btc-relay
btc btc-network-status
```

Эти команды не требуют batch или signing-board. `btc-network-status` только
читает. `btc-relay` отправляет не более `max_headers_per_sync` заголовков за
вызов через EVM journal, используя policy и адреса из pinned graph. Продолжайте
при `IN_PROGRESS`; readiness подтверждает `MAINNET_RELAY_READY` с проверенным
Core, свежестью, chainwork и требуемой глубиной EVM. В regtest network-status
возвращает `REGTEST_NETWORK_READY`; этот статус не заменяет public-network проверки.
Testnet4 сохраняет machine-коды `MAINNET_*` для совместимости. `flow relay`
обслуживает эти же bounded batches в цикле; пример — [здесь](NETWORKS_AND_AUTOMATION.md#продолжение-конкретного-bitcoin-платежа-и-relay).

## 2. Развернуть Bitcoin board

После `trust-bitcoin-registry` оператор выполняет:

```sh
btc btc-deploy-board --max-packet-bytes 4096
```

Сохраните `signing_board` из ответа в локальную переменную `SIGNING_BOARD`.
Повтор команды использует durable journal и возвращает тот же контракт.
CLI проверяет точный runtime, канонический constructor, wallet DKG и закреплённый
registry. Число `4096` — явный лимит публичного пакета; CLI принимает 512–4096.
Это технический лимит, не комиссия. Максимум плана в контракте — 16384 байта.
Для уже существующего board такой же проверке подвергается каждый запуск.

## 3. Подготовить платёж

Предварительно пользователь создал withdrawal, а pool `processWithdrawal`
получил необходимые FROST/custodian approvals: у пула `withdrawalPrepared=true`,
а соответствующий запрос escrow находится в `Pending`; MP и S заблокированы.
Это ещё не сжигание токенов. См.
[жизненный цикл escrow](BITCOIN_WITHDRAWALS.md#escrow-and-payment) и
[операционный API](OPERATIONAL_API.md). Оператор читает ключи запросов из escrow;
получателей и суммы CLI берёт непосредственно из этих записей.

Выберите `BATCH` — новый непустой 32-байтовый идентификатор, hex без `0x`.
`DEADLINE` — согласованный будущий Unix timestamp. `BACKING_INPUT` и `FEE_INPUT`
имеют вид `txid:vout`, где txid в обычном Bitcoin display-порядке.
`REQUEST_KEY` — bytes32 ключ подготовленного escrow-запроса.
Пример с `FEE_INPUT` относится к графу MP. Его Bitcoin-казна покрывает
miner fee отдельным UTXO вне обеспечения MP. Для погашения S в V3 укажите
только полностью учтённые входы резерва конфедерации. В плане v5 комиссия
майнеров вычитается из выплат в пределах лимитов заявок.

```sh
btc btc-prepare --signing-board "$SIGNING_BOARD" --batch "$BATCH" \
  --input "$BACKING_INPUT" --input "$FEE_INPUT" \
  --withdrawal "$REQUEST_KEY" \
  --fee 1000 --max-fee 2000 --deadline "$DEADLINE" --signers 1 2
```

1000/2000 здесь — пример явной политики комиссии; выберите подходящие значения
для выбранной Bitcoin-сети. Для mainnet не копируйте тестовую комиссию без
проверки текущих условий и собственных лимитов. Индексы начинаются с 1 и соответствуют порядку участников
собственной BTC DKG. Требуется ровно её threshold участников; для нового примера
это 2/2. Старый 3/3 wallet требует `--signers 1 2 3` и свою исходную сборку/
policy; индексы не копируют из другого приглашения. Состав фиксируется на весь batch. Команда проверяет UTXO, зрелость, script,
суммы, backing и выходы; change возвращается на тот же P2TR wallet.
Публичный план хранится в локальном зашифрованном state оператора.

Для [передачи внешнего MP](BITCOIN_CUSTODY_TRANSFER.md) здесь используется
**исходный** граф и его настоящий roster/threshold; `REQUEST_KEY` получен из
`custody-transfer request`. Для V3 выделите один точный выход на полную
сумму заявок одного gateway. Остальные выплаты на тот же скрипт проведите
отдельным batch. После исходного `btc-settle` другая сторона выполняет
`custody-transfer finalize`. При выводе полученных S или cBTC подпись и
расчёт выполняет граф назначения.

## 4. Одобрить reserve и опубликовать план

Оператор публикует публичное намерение:

```sh
btc btc-propose --signing-board "$SIGNING_BOARD" --batch "$BATCH" --operation reserve
```

Каждый участник получает с цепочки полный план и номер EVM-сессии:

```sh
btc btc-pending
```

Проверьте входы, получателей, суммы, fee/max_fee, deadline и выбранных подписантов.
Установите `SESSION` из ответа и выполните команду ниже **по одному разу у всех**,
затем **ещё по одному разу у всех** — round commitments и round shares:

```sh
frost-signer dkg sign-evm \
  --board "$BOARD" --role conf_ceremony --rpc "$RPC" --session "$SESSION" \
  --identity-file "$IDENTITY" \
  --state "$PARTICIPANT_DIR/conf_ceremony.enc" \
  --password-file "$PARTICIPANT_DIR/password" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
```

Для другого профиля используйте роль EVM authorization, указанную в intent.
BTC-ключ не подписывает EVM-авторизацию. В mainnet EVM-подписант также проверяет
платёжный план, UTXO и relay через свой Core до одобрения; поэтому два локальных
Bitcoin-аргумента нужны и этой команде. После финализации оператор выполняет:

```sh
btc btc-execute --signing-board "$SIGNING_BOARD" --batch "$BATCH" --operation reserve
btc btc-publish-plan --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

PlanHash включает chain ID, адрес Bitcoin board, BTC result, batch и точные
канонические байты плана. Другой board для того же wallet не является заменой.
Изменить опубликованный план нельзя.

## 5. Одобрить bind

```sh
btc btc-propose --signing-board "$SIGNING_BOARD" --batch "$BATCH" --operation bind
```

Все снова запускают `btc-pending`, проверяют новый intent, берут **новый SESSION**
и проходят два раунда `sign-evm` из шага 4. Затем оператор выполняет:

```sh
btc btc-execute --signing-board "$SIGNING_BOARD" --batch "$BATCH" --operation bind
```

Bind фиксирует точный txid и escrow request keys. Нельзя заменить получателя,
комиссию или входы после одобрения. Reserve и bind — отдельные авторизации.

## 6. Подписать Bitcoin-транзакцию

Каждый выбранный участник читает `btc-pending`; адрес signing board и batch
обнаруживаются автоматически из последнего payment intent. Для параллельных
платежей всегда передавайте явные `--signing-board` и `--batch`.

```sh
btc btc-status --signing-board "$SIGNING_BOARD" --batch "$BATCH"
btc btc-commit --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

После появления commitments **всех выбранных участников для всех входов**:

```sh
btc btc-sign --signing-board "$SIGNING_BOARD" --batch "$BATCH"
btc btc-status --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

Для автоматического продолжения **одного проверенного batch** вместо повторных
`btc-commit`/`btc-sign` можно запустить:

```sh
frost-signer flow payment --actor signer --board "$BOARD" --rpc "$RPC" \
  --identity-file "$IDENTITY" --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
  --password-file "$PARTICIPANT_DIR/password" \
  --signing-board "$SIGNING_BOARD" --batch "$BATCH" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE" \
  --interval 5 --timeout 86400
```

Команда не выбирает новый платёж и не одобряет reserve/bind intents. При `--once`
commit и sign могут выполниться за один опрос, если все commitments уже готовы.
Публичные packets всех входов отправляются через bounded `publishBatch` одного
подписанта. Сначала сохраняются все секреты и неизменные выходные bytes, затем
публикуются батчи; точный retry не генерирует новые nonce.

Подписант независимо проверяет Core UTXO и escrow, затем переводит batch в
Signing до раскрытия первого commitment. На board уходят только два публичных
nonce points и `z_i` конкретной подписи. Секретные nonce и доля ключа остаются
локально. Повтор команды передаёт те же публичные байты; новый nonce на том же
входе не генерируется. При отсутствующем/подменённом spent journal работа
останавливается. Нельзя удалять журнал ради повторного запуска.

Если для действия уже сохранены пакеты **всех входов**, CLI может повторить
доставку после deadline или расходования UTXO. Regtest допускает такую точную
повторную доставку без обращения к Core. Mainnet сохраняет проверки собственного
Core и здоровья закреплённого relay даже для полностью сохранённых пакетов.
Сначала он повторно проверяет board, одобренный план, Signing state, публичные
sighashes, точный транскрипт и spent journal. Это только доставка тех же байтов:
новая подпись после deadline запрещена. При неполном cache применяется обычная
полная проверка; потерянные nonce или signature shares не восстанавливаются
удалением состояния.

## 7. Отправить и завершить расчёт

Любой участник с активным BTC state может дождаться подписей, отправить
платёж и завершить settlement одной командой. После перезапуска повторите её
с теми же `--state`, `--signing-board` и `--batch`.

```sh
frost-signer flow payment --actor operator --board "$BOARD" --rpc "$RPC" \
  --identity-file "$IDENTITY" --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
  --password-file "$PARTICIPANT_DIR/password" \
  --signing-board "$SIGNING_BOARD" --batch "$BATCH" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE" \
  --interval 5 --timeout 86400
```

Для ручной отправки выполните:

```sh
btc btc-broadcast --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

CLI проверяет каждую долю и итоговые BIP340 signatures, сохраняет подписанные
байты до отправки, проверяет mempool acceptance и возвращает `txid`/`wtxid`.
После нужного числа подтверждений (в bootstrap минимум 6):

```sh
btc btc-settle --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

Команда получает proof из Core, доставляет заголовки выбранной сети и вызывает
escrow settlement. Для mainnet синхронизация ограничена policy: ответ
`AWAITING_BITCOIN_RELAY` требует следующего `btc-settle` либо `btc-relay`, пока
не выполнены условия свежести и EVM depth; этот ответ ещё не означает оплату. Только проверенный платёж приводит к MP/S burn и начислению
unwrap fees. Повтор settlement возвращает уже оплаченный результат без нового
сжигания. Тестовый Core требует явного майнинга блоков; CLI не майнит их скрыто.

После settlement `btc-status` и повторный `btc-settle` с указанным Core снова
проверяют каноничность и глубину платежа. При перезапуске `flow payment`
сначала сверяет точный платёж в escrow, до сбора FROST-подписей и отправки.
Канонический рассчитанный платёж завершает flow с `PAID`. Если совпадающую
транзакцию уже видит Core, flow продолжает settlement. Для новой Bitcoin-
траты перед подписью и отправкой проверяется свежесть relay или ZK-состояния
согласно профилю.
Недоступность RPC, неполный ответ провайдера, один код Core `-5` или
недобор подтверждений при совпадающем якоре дают `OBSERVATION_UNAVAILABLE`.
При недоборе глубины SDK сообщает `BITCOIN_PAYMENT_DEPTH_PENDING`.
Следующая команда подписи или broadcast сначала
повторяет наблюдение исходного txid и сверяет его merkle proof. После
успешной проверки работа продолжается. `flow payment` повторяет также сбои
Core во время подготовки broadcast или settlement в пределах своего
`--timeout`. Ответ содержит безопасный
`error_code` и, если Core вернул JSON-RPC ошибку, числовой `rpc_error_code`
(при HTTP-ошибке также `http_status`);
текст провайдера, cookie и URL с учётными данными не записываются.

Если Core показывает другой канонический хеш на высоте блока settlement,
сохраняется `RECOVERY_REQUIRED`. SDK применяет ту же проверку якоря,
когда Core не нашёл txid. SDK атомарно сверяет текущую запись при обновлении наблюдения, чтобы
результат старой проверки сохранил более свежую ручную паузу. Снять её можно
явной командой восстановления. Каждый остановленный участник сверяет
оплаченный txid с escrow и запускает для **исходного** batch:

```sh
btc btc-recover --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

`PAYMENTS_RESUMED` означает, что тот же оплаченный txid снова каноничен,
имеет требуемую escrow глубину и проверенный Core merkle proof. Для этой
проверки достаточно канонической EVM-записи escrow и актуального Bitcoin Core,
даже если relay ещё догоняет сеть. Перед следующей подписью SDK снова проверяет
готовность relay. Команда сохраняет локальный журнал с Bitcoin/EVM anchors.
Если proof не подтверждён, пауза остаётся. `PAYMENTS_NOT_PAUSED` означает
отсутствие локальной паузы. Старые паузы без классификации также снимаются
этой командой только после проверки.
Для отдельного операторского `bitcoin confirm` доступна аналогичная команда
`bitcoin recover` с теми же аргументами Core, сети и `--state`, `--batch`;
она проверяет операторский журнал и не заменяет `dkg btc-recover` на машинах
участников. Удаление state или `.spent.sqlite` не является восстановлением.

Статус без Bitcoin RPC сообщает только факт EVM settlement
с `bitcoin_observation=NOT_CHECKED`. Такая локальная остановка не отзывает уже
опубликованные подписи и не останавливает чужие машины автоматически.

Проверяйте поле `status` в JSON-ответе, а не только код завершения процесса:

| Команда или этап | Успешный статус |
| --- | --- |
| `btc-network-status` для mainnet | `MAINNET_RELAY_READY` |
| `btc-relay` для mainnet | `MAINNET_RELAY_READY`; `IN_PROGRESS` требует продолжения |
| `btc-prepare` | `BTC_PLAN_PREPARED` |
| `btc-propose` | `AWAITING_PARTICIPANTS` и номер `session` |
| `btc-execute` | `BTC_OPERATION_EXECUTED` |
| `btc-publish-plan` | `PLAN_PUBLISHED` |
| `btc-commit` / `btc-sign` | `COMMITTED` / `SIGNED` |
| `btc-broadcast` | `BROADCAST`, `txid` и `wtxid` |
| Первый `btc-settle` | `BTC_PAYMENT_SETTLED`. При `AWAITING_BITCOIN_RELAY` или `AWAITING_ZK_HEADER_MATERIALIZATION` дождитесь SPV и повторите вызов |
| Проверка уже оплаченного платежа | `PAID`; `OBSERVATION_UNAVAILABLE` требует повтора, `RECOVERY_REQUIRED` — ручного разбора и `btc-recover` |
| `btc-recover` | `PAYMENTS_RESUMED` либо `PAYMENTS_NOT_PAUSED` |

`btc-status` также показывает `AWAITING_COMMITMENTS`, `READY_TO_SIGN` или
`READY_TO_BROADCAST` и списки отсутствующих пакетов. Наблюдение Bitcoin находится
в его поле `payment`; `NOT_CHECKED` означает отсутствие проверки Core, а не
подтверждённую окончательность. `RECOVERY_REQUIRED` — результат безопасной
остановки и не означает успешное разрешение проблемы даже при коде выхода 0.

## Отказы и ограничения

- Публичный Bitcoin требует согласованной V6 policy. Relay использует
  `MainnetHeaderRelay` или `Testnet4HeaderRelay` с `MainnetSPVGuard`. ZK
  использует собственные state и adapter выбранной сети. Halt, неверный
  checkpoint или отставание Core/SPV блокируют новые денежные операции.
- Недостающий commitment/share отображается в `btc-status`; сменить состав после
  подписания нельзя. Отказ участника блокирует текущую попытку.
- После Signing исчезновение транзакции из mempool, timeout и потеря связи не
  разрешают возврат MP/S. Требуется предусмотренное протоколом восстановление.
- Для V3 подготовьте замену через `btc-prepare --previous BATCH_ID` с теми же
  упорядоченными входами и заявками. Каждый новый batch проходит отдельные
  reserve, bind и Bitcoin FROST.
  [Лимиты и расчёт RBF](CONFEDERATION_DAO_IN_KIND.md#btc-и-комиссия-майнеров).
- Получатель withdrawal не может совпадать с P2TR script самого общего wallet:
  CLI отклоняет такую заявку, чтобы не смешивать выплату principal и change.
- Новый board имеет расширенный runtime и требует нового deployment; старый
  immutable board без `publishPlan` CLI отвергнет. Старые file-based планы не
  конвертируются в новые одобрения автоматически.
- Платёжные суммы, scripts, roster, тайминг, публичные commitments и `z_i` видны
  в цепочке. Шифруются секретные DKG-пакеты и локальное состояние. Криптографическая
  защита ключа не обеспечивает приватность метаданных платежа.
- Общий rollback или клонирование state **вместе с** spent journal, заражённый host
  и ложный RPC не решаются on-chain транспортом. Нужны независимые узлы, защита
  машины/backup и внешняя проверка перед средствами с реальной стоимостью.

Доказательства и границы: [отдельная проверка безопасности](BITCOIN_ONCHAIN_SECURITY_REVIEW.md),
[сквозной тест](../tests/e2e/test_bitcoin_onchain.py).

## Personal fee V4 profile

See [withdrawal fees](WITHDRAWAL_FEE_SUBSIDY.md) for owner consent, BTC/MP/S, named carry and typed refunds. Use `bitcoin withdrawal-fee build`, execute the prepared typed request and owner `approveVersion`, then participant `btc-import-plan --plan-file plan.json` before normal reserve/bind. V4 replacements select the replacement helpers automatically; large public plans/intents resume in 8 KiB parts. The actual fee purpose remains unqualified, so these commands do not constitute activation. Ordinary funding requires 6 confirmations; coinbase fee credits require 101 for spending.
