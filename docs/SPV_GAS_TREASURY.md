# Native gas-касса и оплата SPV-заданий

Текущий кодовый контур показан в [архитектуре и схемах](ZK_SPV_ARCHITECTURE.md).
Дополнительно реализованы [issuer admission, encrypted quote и opt-in payer](SPV_AUTOMATION_ARCHIVE.md)
с постоянными budget journals и общей worker publication. Разрешённое добровольное
финансирование конфедерацией сохраняется при пустой казне. Прежние локальные
receipts/governance-прогоны ниже относятся к собственным source snapshots;
общий live payment/prover/consumer E2E ещё отложен.

Компонент реализует локальную часть §16.21–16.37 ТЗ 2.16.
Полный gate `gas_funding` остаётся **UNKNOWN**. Настоящего квалифицированного
ZK-SPV state view, encrypted request transport и связанного с ними admission
сейчас нет. Компонент не добавлен в production bootstrap. Его локальные тесты
не доказывают production-доступность кворума, выполнение SLA или публикацию SPV-состояния.

## Обновление кода: state binding и собственное финансирование

Добавлен `ZkSPVServiceStateView` для mainnet V2 state/adapter. Bootstrap создаёт
view и неактивный payment registry до Fresh barrier; выбор payees/тарифа и
активация требуют прежнего enrolled FROST workflow. При полном проверенном
Bitcoin policy V2 клиент проверяет runtime, constructor provenance, factory,
state/adapter и policy hash вместо прежнего ограничения только chain 31337.
Локальные component fixtures сохраняют отдельный профиль.

Добавлены `prepare-admit` и явный `submit` permission без перевода value.
Worker связывает локальные потребности с каноническим paid job и заново проверяет
его после reorg. Режимы `confederation` и `paid_or_confederation` допускают работу
за собственные native средства при нехватке денег в кассе. Лимиты и расходы
журналируются; `paid`, долг пользователя или право на возмещение не создаются.
Подробный порядок: [worker](ZK_SPV_WORKER.md#service-funding-and-executable-heartbeat-code-update).

Новый код ещё ожидает общего E2E; это не production activation. Описанные ниже
исходные local-only ограничения относятся к прежней квалификации component
fixtures, а не к текущей возможности проверки реального V2 state view.

## Касса

`ConfederationGasTreasury` хранит неизменяемые confederation scope, DKG authority,
registry, epoch, список из 1–32 уникальных ненулевых payees и положительные веса.
Веса задаются явно; доли DKG не определяют их автоматически. Нулевой адрес,
собственный адрес кассы, дубликаты и переполнение суммы весов отвергаются.

Накопительный расчёт:

```
receivedTotal = balance + totalReleased
entitled(payee) = floor(receivedTotal * weight(payee) / totalWeight)
claimable(payee) = entitled(payee) - released(payee)
```

Произведение считает закреплённый в репозитории OpenZeppelin 5.4 `Math.mulDiv`.
Исторический источник модели —
[PaymentSplitter v4.9.6 (MIT)](https://github.com/OpenZeppelin/openzeppelin-contracts/blob/v4.9.6/contracts/finance/PaymentSplitter.sol).
Заверения о независимом аудите этого нового кода из него не следуют.

`claim()` и permissionless `claimFor(payee)` используют общую reentrancy-защиту
и обновляют учёт перед переводом. Деньги всегда получает сам payee. Ошибка его
получения откатывает только эту выплату. Нулевая доля ничего не вызывает.
Округлённый остаток остаётся для следующих поступлений; forced native funds
учитываются через баланс. Events не являются полным источником receivedTotal.
Нет drain, sweep, перенаправления потерянного ключа, ERC-20/BTC/MP-конверсии или
общей функции произвольного исполнения. Старые кассы навсегда доступны для claim.

## Единый реестр оплаты

`SPVServicePaymentRegistry` привязан конструктором к factory/confederation,
её DKG authority, state view, state, adapter, policy и chain ID. Все эти значения
входят в неизменяемый domain. Registry сам создаёт канонические кассы и хранит
`isTreasury`, поэтому совместимых getters постороннего контракта недостаточно.
Создание предложения кассы не активирует его.

`configure` требует существующую FROST `consumeOperation` от authority
конфедерации. Подписываются касса, issuer, tariff version/hash, точная сумма,
handover commitment и отдельный configuration nonce. Каждая конфигурация или
`pauseAdmissions` повышает generation: прежние неоплаченные разрешения теряют
силу. Pause работает и при остановленном state/deactivated confederation.
Тарифные версии неизменяемы. Ротация кассы требует явный commitment плана
финансирования ранее оплаченной незавершённой работы; проверка исполнения этого
плана остаётся обязательством сервиса, а не доказанным свойством commitment.

Registered issuer — отдельная admission authority. `admit` фиксирует:

- непрозрачные job ID и semantic key, federation ID внутри конфедерации;
- точного payer, budget scope и первоначальный service deadline;
- наблюдаемые state hash/version, generation, срок приёма оплаты и уникальный nonce.

Изменить economic terms или продлить первоначальный deadline нельзя. Новый
permission для того же неоплаченного job требует номер предыдущего permission.
Semantic key закреплён за одним job во всех эпохах. Семантическую эквивалентность
различных opaque keys контракт установить не может: её обеспечивает доверенный
admission workflow, который ещё предстоит связать с настоящими request/job данными.
Есть максимум 256 одновременно открытых неоплаченных permissions. Cancel/expiry
освобождают слот, сохраняя экономическую идентичность job. Просроченные и отозванные
разрешения может очистить любой; активные — только issuer.

`fundRequest(jobId, permissionNonce)` принимает только точный `msg.value` от
зафиксированного payer. В той же транзакции проверяются конфедерация, initialized/
halted state, полные state hash/version, конфигурация, сроки и отсутствие оплаты.
Устаревший state отменяет перевод и paid marker; комиссия неудачной транзакции
не возвращается. Проверка Fresh не требуется: обновление stale state тоже надо
финансировать. State view обязан повышать version при каждом переходе, включая
возврат к старому digest. В репозитории пока есть только его интерфейс и test fixture.

`payments[jobId]` живёт в одном реестре для всех касс. Ротация, истечение quote,
повтор CLI или новая попытка исполнения не создают второй полный взнос.
Перевод в кассу и paid marker атомарны. Прямое пополнение кассы считается donation
и никогда не оплачивает job. Оплата не означает, что job выполнен; взнос распределяется
безусловно и не возвращается при неудаче сервиса. Heartbeat/security/startup reserve
остаётся отдельным источником средств, который здесь не создаётся автоматически.

## Клиент

### Пороговая конфигурация

В enrolled `conf_ceremony` доступны команды `frost-signer dkg gas-prepare-configure`,
`gas-prepare-pause`, `gas-propose`, `gas-execute`, `gas-status`. Они используют тот же
on-chain DKG board, независимые состояния участников, FROSTVerifier и общий
durable EOA journal, что и существующий signer. Каждая команда получает обычные
`--board`, `--role conf_ceremony`, `--rpc`, `--identity-file`, `--state`,
`--password-file` своего участника. Приватные DKG/FROST данные не передаются оператору.

Файл конфигурации содержит ровно шесть полей:

```json
{
  "treasury": "<canonical treasury address>",
  "issuer": "<admission issuer address>",
  "tariffVersion": "1",
  "tariffHash": "<bytes32 tariff commitment>",
  "amount": "<exact decimal wei>",
  "handoverCommitment": "<bytes32; required on treasury rotation>"
}
```

Порядок работы:

1. Оператор вызывает `gas-prepare-configure --gas-registry ADDRESS --configuration FILE
   --deadline UNIX_TIME` и сохраняет возвращённый public intent. Для отзыва
   admission используется `gas-prepare-pause --gas-registry ADDRESS --deadline UNIX_TIME`.
2. `gas-propose --package INTENT_FILE` создаёт FROST-сессию и публикует неизменяемый
   intent в enrolled board. Повтор с тем же файлом восстанавливает прежнюю сессию
   по durable receipt. Публикация требует `canPublishIntent`: исходного operator
   либо кастодиана, которому bootstrap уже разрешил публичную публикацию.
3. Каждый участник явно вызывает обычный `sign-evm --session ID`: сначала commitment,
   затем share/finalization. Gas-операции принимаются только из on-chain board,
   file-only и generic автоматическое подписание таких сессий блокируются.
4. `gas-execute --session ID` регистрирует настоящую FROST authorization и исполняет
   точную операцию. Исполнить её может любой enrolled participant; повтор после
   успешного выполнения возвращает `ALREADY_EXECUTED` без новой EOA-транзакции.

Public intent содержит именованные issuer/amount/tariff/handover, фактические
payees/weights, canonical treasury epoch/roster hash, state/adapter bindings,
DKG result и отдельный configuration nonce. До приватного nonce/share проверяются
canonical constructors, authority epoch, confederation ID/operator, coordinator,
operation hash и точные отображаемые условия. Общая защита SigningService повторяет
проверку на confirmed и latest снимках, включая cached-share путь. Изменение
configuration nonce делает незавершённую старую операцию непригодной для подписания.
Нельзя продлевать deadline старой публичной операции через coordinator.

Новая конфигурация пока разрешена только на локальной chain 31337, включая сам
signer guard: `PASS` в JSON не снимает production gate. Pause не требует available/
Fresh state, но по-прежнему требует правильную authority и пороговую подпись.
Production bootstrap и квалифицированный state view остаются отдельными задачами.
Это не замена operator или смена ключевого состава board. В custody-профиле
используется уже существующее разрешение кастодиальной эпохи; пороговая
авторизация и production gates сохраняются.

### Платежи и claims

`python -m signer_agent.gas_treasury` принимает reviewed local binding manifest.
Он проверяет точный runtime реестра, canonical direct CREATE с точными constructor
arguments, immutable domain/authority, происхождение дочерней кассы и canonical
read block. Manifest — локальное решение о доверии конкретной конфигурации,
не аттестация её production-готовности. Пример полей (адреса/хэши надо взять из
реальных проверенных deployment receipts):

```json
{
  "schema": "satoshifi.spv-gas-binding.v1",
  "chain_id": "31337",
  "evm_confirmations": "1",
  "registry": "<registry address>",
  "registry_creation_block": "<decimal block>",
  "registry_runtime_hash": "<keccak runtime>",
  "factory": "<confederation factory>",
  "confederation_id": "<id>",
  "state_view": "<state view address>",
  "authorization": "<confederation DKG authorization>",
  "domain": "<registry.domain()>"
}
```

```sh
export PYTHONPATH=signer-agent
python -m signer_agent.gas_treasury --rpc "$RPC" --binding binding.json \
  prepare-fund --job "$JOB" > reviewed-payment.json
python -m signer_agent.gas_treasury --rpc "$RPC" --binding binding.json \
  submit --request reviewed-payment.json --identity-file payer.json
python -m signer_agent.gas_treasury --rpc "$RPC" --binding binding.json \
  prepare-claim --treasury "$TREASURY" --payee "$PAYEE" > reviewed-claim.json
python -m signer_agent.gas_treasury --rpc "$RPC" --binding binding.json \
  submit --request reviewed-claim.json --identity-file funded-caller.json
```

Проверить payer/value/roster/terms нужно до `submit`: это необратимый operational
взнос, а не escrow. Новый платёж разрешён клиентом только на локальной chain 31337;
JSON-флаг `PASS` не снимает `GAS_SPV_PROFILE_NOT_QUALIFIED` на других сетях.
Claim старой кассы не зависит от работоспособности SPV state и не переводит value
от вызывающего. Для mainnet действуют прежние finalized/fee/gas ограничения
существующего transaction engine. Комиссию claim оплачивает вызывающий.

Запрос сохраняется как отдельный JSON, затем передаётся общему durable transaction
engine Bootstrap. Журнал `<identity>.spv-gas-transactions.sqlite` использует общий
EOA nonce journal с прочими командами. Повтор того же запроса проверяет canonical
receipt или повторяет те же подписанные bytes. Изменённые условия требуют новый
reviewed request. Уже подписанный запрос восстанавливается и после устаревания
permission, чтобы не оставлять незавершённую транзакцию в очереди EOA. Reorg или
revert требуют явного существующего recovery-процесса; журнал не сбрасывается.

### Ограниченный автоматический claim caller

`python -m signer_agent.gas_caller` реализует отдельный polling-клиент §16.34.
Он вызывает только `claimFor` указанного payee в проверенной канонической кассе,
всегда с нулевым value. Он не создаёт платежи за jobs, не активирует кассы и
не вызывает FROST/SPV операции. Ручные permissionless claims остаются доступны
независимо от его лимитов.

Шаблон `config/spv-gas-caller.example.json` содержит полный формат: общий budget
и routes с полным binding manifest, treasury, epoch, roster hash и approved payees.
Все адреса/хэши-заполнители требуется заменить проверенными значениями. Числа
в шаблоне предназначены для локального теста, не являются production тарифом.
Для каждого payee задаются фактический минимальный баланс `balance_floor_wei`
и минимально полезная выплата `min_claim_wei`. Все финансовые поля — десятичные
строки; пропущенные лимиты не получают неявного значения.

```sh
python -m signer_agent.gas_caller --rpc "$RPC" --config reviewed-caller.json \
  --identity-file funded-caller.json --once
python -m signer_agent.gas_caller --rpc "$RPC" --config reviewed-caller.json \
  --identity-file funded-caller.json --interval-seconds 30
```

До подписи клиент проверяет canonical runtime/provenance/roster, фактический
баланс получателя и claimable. Выплата должна покрывать недостающий баланс,
превышать `min_claim_wei` и быть не меньше `min_claim_to_fee_ratio` × максимальной
комиссии. Поступление dust не вызывает транзакцию. Caller заранее финансируется
оператором; после резервирования максимальной комиссии у него должен оставаться
`min_caller_balance_wei`. Доля ещё не полученных средств не считается балансом.

`gas_limit`, `max_fee_per_gas_wei`, `max_priority_fee_per_gas_wei` фиксируют пределы
каждой подписанной EIP-1559 транзакции. Перед подписью выполняется bounded
simulation; успешная симуляция не обещает успешный перевод. На mainnet дополнительно
действуют прежние ENV fee/gas caps и consensus finality. Новый клиент не снимает
production gate на активацию кассы/оплату jobs.

Журнал `<identity>.gas-caller.sqlite` фиксирует неизменяемую budget policy:
общий `max_period_cost_wei`, `max_transactions_per_period`, backoff и пожизненный
для пары scope/epoch/payee `max_attempts_per_payee_epoch`. Попытка учитывается
до подписи, включая неудачную симуляцию. Другие кассы-предложения той же эпохи,
donations, новые requests и изменение routes не обнуляют счётчики. Ротация
эпохи не обнуляет общий бюджет. Периоды — интервалы `period_seconds` от Unix epoch
по подтверждённому chain timestamp; backoff не сбрасывается на границе периода.

До подписи резервируется `gas_limit × max_fee_per_gas_wei`. Неизвестный результат
удерживает резерв, в том числе в следующем периоде; новая транзакция не создаётся.
После finality учитывается фактическая комиссия в периоде включения, даже при
revert или нулевой выплате. CLI заново читает released, claimable и фактический
баланс; чужой claim может уже закрыть потребность. Статусы `CLAIM_TOO_SMALL`,
`INSUFFICIENT_ENTITLEMENT`, `CALLER_RESERVE_SHORTAGE`, `BACKOFF`, `EPOCH_ATTEMPTS`
и `PERIOD_*` явно показывают причину отсутствия автоматической выплаты.

Журнал транзакций `<identity>.gas-caller-transactions.sqlite` разделяет общий
EOA nonce lock с остальными командами. После сбоя восстанавливаются ровно прежние
подписанные bytes. Только собственный canonical `claimFor` с проверенными raw
bytes и finalized receipt может подтвердить расходованный nonce после revert:
source journal сохраняет `REVERTED`, общий nonce journal — `CONFIRMED` с явными
`receipt_status=0`/`CLAIM_REVERTED`. Это разрешает обслуживание следующего payee,
не изменяя правила восстановления других операций. Между подтверждением nonce
и завершением учёта бюджета сохраняется стадия `SETTLING`. Reorg/неопределённый
receipt останавливает отправку; spent FROST state не затрагивается.

Policy нельзя сбросить редактированием config. Используется одна постоянная
identity с её журналами; параллельные процессы сериализуются. Копирование ключа
в другой каталог, удаление/откат журналов или отправка этим ключом из другой
программы находятся вне гарантий локального бюджета. Это не on-chain delegated
budget wallet; такой wallet пока не реализован. При исчерпании лимита ручной claim
остаётся доступен. Не нужно удалять журнал ради повторной попытки.

## Проверки и оставшиеся условия

`web3/test/SPVServicePayment.t.sol` проверяет распределение, dust/forced funding,
constructor constraints, повторные платежи, nonce/state/policy/generation races,
expiry, immutable terms/tariffs, ротацию и старые claims. Fuzz проверяет совокупный
учёт при разном порядке поступлений и выплат. Authority approval и state в этих
тестах синтетические; реальны hash/consumeOperation и новые контракты.

`tests/e2e/test_spv_gas.py` использует реальные Anvil receipts и отдельные CLI
процессы: fund → claimFor payee с нулевым балансом → его native gas transaction,
возобновление без нового nonce, stale-state revert, новая permission, ротация,
claim старой кассы и 256 отдельных admissions. Его результат не является
fund → encrypted messages → real SPV publication проверкой §16.28.

`tests/e2e/test_spv_gas_governance.py` проходит штатный bootstrap в трёх отдельных
каталогах участников, затем выполняет настоящие пороговые activation, rotation
и pause. В нём настоящие фабрика, DKG epoch, authorization, coordinator и FROST
подписи; синтетическим остаётся только новый SPV state view. Публичные агрегированные
подписи дополнительно проверяются независимой `coincurve.PublicKeyXOnly`.
Сценарий включает платёж после пороговой активации, старый claim после ротации,
повтор команды без нового nonce и блокировку старой сессии после pause.
Это отдельное свидетельство о governance, а не подтверждение real ZK proof или
полной публикации SPV-состояния: результаты находятся в `gas-governance-report.json`.

`tests/e2e/test_spv_gas_caller.py` проверяет реальными ограниченными транзакциями
отказ обычного получателя после успешной simulation, продолжение для другого payee,
конкурирующий claim с нулевой выплатой, usable native gas, восстановление после
подписи/сбоя RPC и после nonce acknowledgement, общий бюджет при ротации,
два отдельных CLI процесса и остановку после локального reorg. Authority/state
здесь синтетические. `gas-caller-report.json` содержит комиссии/receipts и snapshot
исходников; это локальная квалификация компонента, а не полный gas_funding gate.

Для production остаются квалифицированный immutable state/adapter view и его
проверки в клиенте, admission по реальной необходимости/SLA, финансовое согласие
и доверие до оплаты, квалификация пороговой активации на целевой сети,
bootstrap deployment manifest, реальные roster/weights/tariffs/reserves,
handover readiness, квалификация caller policy на целевой сети, полная проверка
нулевого баланса с криптографией и независимый review.
