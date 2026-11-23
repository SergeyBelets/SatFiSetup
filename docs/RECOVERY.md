# Восстановление работы SDK

Начинайте с тех же identity, board, DKG и локальных журналов, которыми
пользовались при отправке. Для Bitcoin сохраните исходные batch и txid.
Перед обновлением SDK остановите процессы, пишущие в журналы, и сохраните
резервные копии журнала операции, журнала EVM nonce и состояния подписанта.
Состояние смотрите в `status`, причину в `reason` или `code`, пояснение в
`message`. Для диагностики включите `--debug` и передайте только записи
`DEBUG_ERROR`. [Диагностика SDK](PEER_TESTING.md#как-собрать-диагностику-sdk).

Экспорты SVG и PNG доступны в GitHub и полном checkout. В participant archive
схемы доступны как блоки Mermaid на этой странице.

| Что наблюдаете | Действие | Схема |
| --- | --- | --- |
| Два участника отправили `finalizeSession` | Повторите работу с той же сессией и журналом. Если в старом журнале отсутствует привязка, выполните `bootstrap bind-legacy-nonce`. | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_1.svg) · [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_1.png) |
| Core временно не подтверждает наблюдение выплаты | При `OBSERVATION_UNAVAILABLE` повторите проверку исходного batch. При `RECOVERY_REQUIRED` проверьте цепочку и выполните `dkg btc-recover`. | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_RECOVERY_md_2.svg) · [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_RECOVERY_md_2.png) |
| Повтор уже исполненной операции | При `ALREADY_EXECUTED` перейдите к следующему шагу. При ошибке привязки или `OPERATION_PARAMETERS_MISMATCH` сверяйте исходный intent и board. | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_3.svg) · [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_3.png) |
| `EVM_PENDING_FEE_CAP_BELOW_BASE` | Дождитесь снижения base fee или подготовьте `bootstrap replace-pending` для исходной транзакции. | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_4.svg) · [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_4.png) |
| RPC участника отстаёт | Дождитесь требуемой глубины и повторите исходную команду. Для EVM можно указать проверенный RPC той же сети. | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_RECOVERY_md_5.svg) · [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_RECOVERY_md_5.png) |
| Срок batch S → BTC истёк в состоянии `Signing` | Откройте исходный batch и продолжите недостающие шаги с `--resume-expired`. | [Схема](#expired-s-btc-recovery) |

## 1. Гонка finalizeSession

```mermaid
flowchart TD
    accTitle: Две транзакции finalizeSession
    accDescr: SDK сверяет результат финализации. Для старого nonce без привязки оператор указывает исходный журнал. Исходная команда проверяет финальность отката и результат сессии.
    START["SDK читает сессию перед отправкой"] --> FINAL{"Тот же результат<br/>уже принят?"}
    FINAL -->|"Да"| DONE["Использовать принятый результат<br/>Продолжить с тем же DKG"]
    FINAL -->|"Нет, сессия открыта"| SEND["Симулировать и отправить finalizeSession"]
    FINAL -->|"Принят другой результат"| BLOCK["FROST_FINALIZATION_RESULT_MISMATCH<br/>Проверить сессию"]
    SEND --> RECEIPT{"Receipt финализирован?"}
    RECEIPT -->|"Ещё нет"| WAIT["Дождаться финальности<br/>Повторить работу с прежним журналом"]
    WAIT --> RECEIPT
    RECEIPT -->|"Да, успех"| DONE
    RECEIPT -->|"Да, откат"| MATCH{"В сессии совпадают сообщение<br/>и агрегированная подпись?"}
    MATCH -->|"Да"| BIND{"Nonce привязан к исходному журналу?"}
    BIND -->|"Старая запись без привязки"| REPAIR["bootstrap bind-legacy-nonce<br/>Сверить исходный журнал и код координатора"]
    REPAIR --> RETRY["Повторить исходную команду"]
    RETRY --> RECEIPT
    BIND -->|"Да"| LANE["SDK подтверждает использованный nonce<br/>Следующая транзакция использует новый nonce"]
    LANE --> DONE
    MATCH -->|"Нет"| ACK["Сверить причину отката<br/>bootstrap acknowledge-revert"]
```

Если откат успел попасть в журнал, следующая отправка с той же identity
сначала сверяет этот nonce. До финальности отката SDK может вернуть
`TRANSACTION_PENDING` с причиной `FROST_FINALIZATION_REVERT_AWAITING_FINALITY`.
При совпадении результата исходная операция сохраняет отметку об откате, а
nonce становится закрытым.

Для исторического вызова FROST-координатора без привязки nonce укажите
существующий `$STATE`, исходный приватный журнал подписанта, обычно
`<identity file>.transactions.sqlite`. Команда сверяет точную подписанную
транзакцию с записью в этом журнале и проверяет байткод координатора по
каноническому финализированному блоку на публичной EVM. Она записывает
привязку локально, без отправки транзакции. Затем повторите исходную команду
с тем же DKG. При откате `finalizeSession` повтор проверяет результат самой
сессии. Для ещё ожидающего вызова привязка также позволяет проверить
допустимость замены комиссии. SDK сообщает `EVM_LEGACY_NONCE_SOURCE_REQUIRED`,
если нужна такая привязка, и `EVM_LEGACY_NONCE_BOUND` после её проверки.

```sh
frost-signer bootstrap bind-legacy-nonce --identity-file "$IDENTITY" \
  --rpc "$RPC" --tx-hash "$TX_HASH" --source-journal "$STATE"
```

Для отдельного финализированного отката используйте локальную identity
отправителя. Эта команда подтверждает использованный nonce и оставляет
отметку об откате в журнале операции.

```sh
frost-signer bootstrap acknowledge-revert --identity-file "$IDENTITY" \
  --rpc "$RPC" --tx-hash "$TX_HASH"
```

[Восстановление EVM и отдельная процедура withdrawal CREATE](BOOTSTRAP.md#withdrawal-deployment-and-recovery).

## 2. Проверка Bitcoin-платежа после settlement

```mermaid
flowchart TD
    accTitle: Недоступное наблюдение и реорганизация Bitcoin
    accDescr: После перезапуска сначала проверяется точный платёж escrow. Завершённый платёж не запускается снова. Восстановление паузы опирается на исходный Bitcoin-платёж.
    START["Перезапуск flow payment<br/>Исходные batch и Bitcoin txid"] --> ESCROW{"Что известно о точном платеже?"}
    ESCROW -->|"Escrow уже рассчитан"| READ["Проверить платёж через Core"]
    ESCROW -->|"Тот же txid известен Core"| SETTLE["Продолжить settlement"]
    ESCROW -->|"Нужна отправка нового платежа"| SPEND["Проверить свежесть relay или ZK-состояния<br/>Перед подписью или broadcast"]
    READ --> STATE{"Состояние наблюдения"}
    STATE -->|"Таймаут, неполный ответ, нет блока<br/>или глубины пока недостаточно"| SOFT["OBSERVATION_UNAVAILABLE<br/>Повторить проверку того же txid"]
    SOFT --> READ
    STATE -->|"Другой канонический хеш,<br/>невалидный proof или ручная пауза"| HARD["RECOVERY_REQUIRED<br/>Пауза сохранена в журнале"]
    STATE -->|"Каноничен и ручной паузы нет"| GOOD["PAID<br/>Завершить повторный flow"]
    HARD --> REC["dkg btc-recover<br/>Исходный batch и state"]
    REC --> CHECK{"Escrow, txid, глубина<br/>и merkle proof совпали?"}
    CHECK -->|"Нет"| HARD
    CHECK -->|"Да"| RESUME["Снять проверенную паузу<br/>PAYMENTS_RESUMED"]
```

Один ответ Core `-5`, отсутствие блока на нужной высоте и недобор
подтверждений ведут к `OBSERVATION_UNAVAILABLE`. Если тот же блок виден,
но ещё не набрал глубину, причина может быть `BITCOIN_PAYMENT_DEPTH_PENDING`.
Дождитесь достаточной глубины и повторите проверку. SDK снимает временную
паузу после повторной проверки точного платежа и Merkle proof. Если RPC
возвращает другой канонический хеш на высоте исходного блока выплаты,
SDK сохраняет ручную паузу.
Запись паузы меняется атомарно. Ответ от проверки, начатой до её установки,
не может стереть более свежую паузу.

Старая пауза без классификации также требует `btc-recover`. Команда сверяет
точный оплаченный txid в каноническом EVM escrow и независимо проверяет его
через Core. Перед следующей подписью или broadcast нового платежа SDK снова
проверяет свежесть relay или ZK-состояния согласно профилю.
`PAYMENTS_NOT_PAUSED` означает, что локальной паузы нет. Для исполнения
команды используйте `btc()` из инструкции и исходный `BATCH`.

```sh
btc btc-recover --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

[Настройка `btc()` и порядок Bitcoin-расчёта](BITCOIN_ONCHAIN_RUNBOOK.md).

## 3. Повтор исполненной EVM операции

```mermaid
flowchart TD
    accTitle: Исполненный EVM intent
    accDescr: Перед повторной подписью SDK проверяет привязки intent и запись об исполнении. Тот же исполненный intent завершает проверку без новой подписи.
    START["Повторить dkg sign-evm<br/>или setup approve-custodian"] --> POLICY{"Сессия и intent совпадают<br/>по цели, хешу и DKG-группе?"}
    POLICY -->|"Нет"| ERR["Ошибка проверки<br/>Сверить исходный intent и board"]
    POLICY -->|"Да"| EXEC{"В подтверждённом состоянии<br/>исполнена та же операция?"}
    EXEC -->|"Да, запись и привязки проверены"| OK["ALREADY_EXECUTED<br/>Повторная сессия может оставаться открытой"]
    EXEC -->|"Нет"| HASH{"Hash helper совпадает<br/>с хешем intent?"}
    HASH -->|"Да"| SIGN["Обычные проверки и подпись"]
    HASH -->|"Нет"| ERR
```

Проверка сравнивает точный хеш операции, контракт и DKG-группу в подтверждённом
состоянии. Для ULR `setup approve-custodian` также сверяет запись об
использованной авторизации на точной цели и предусмотренное для этого вида
операции подтверждение в контракте. Поэтому подтверждённая
операция возвращает `ALREADY_EXECUTED`, даже если повторную сессию не
финализировали. Можно перейти к следующему шагу без нового FROST nonce.
[Обычный порядок `sign-evm`](BITCOIN_ONCHAIN_RUNBOOK.md#4-одобрить-reserve-и-опубликовать-план).

## 4. Замена ожидающей EIP-1559 транзакции

```mermaid
flowchart TD
    accTitle: Замена комиссии с сохранением nonce
    accDescr: Отправитель подтверждает fingerprint для точной замены. При потерянном ответе можно повторно отправить сохранённого кандидата с тем же fingerprint. SDK сверяет канонический receipt и финальность.
    START["Ожидание транзакции в исходном журнале"] --> STATE{"Транзакция уже в блоке?"}
    STATE -->|"Да"| WIN["Проверить канонический receipt и глубину<br/>На mainnet и Sepolia также finalized"]
    STATE -->|"Нет"| REVIEW["bootstrap replace-pending<br/>Задать fee cap, tip cap и max cost"]
    REVIEW --> APPROVE{"Условия допустимы<br/>и оператор подтверждает fingerprint?"}
    APPROVE -->|"Нет"| WAIT["Продолжить ждать исходную транзакцию<br/>Или подготовить новый расчёт"]
    APPROVE -->|"Да"| SEND["Отправить тот же вызов с тем же nonce<br/>Сохранить обе попытки"]
    SEND -->|"Ответ получен"| OBSERVE["Повторить исходную команду<br/>Определить, какой хеш включён"]
    SEND -->|"Ответ потерян"| RETRY["Повторить replace-pending<br/>С теми же лимитами и fingerprint"]
    RETRY --> OBSERVE
    OBSERVE --> WIN
    WIN --> RESULT{"Статус receipt"}
    RESULT -->|"Успех"| DONE["Использовать результат победителя"]
    RESULT -->|"Откат"| ACK["После сверки победителя сделать acknowledge-revert<br/>Исходный шаг остаётся откатом"]
```

На этапе `EVM_REPLACEMENT_REVIEW` сравните сеть, sender, nonce, target,
`data_hash` и `worst_case_wei`. Повторите команду с теми же лимитами и
`--confirm-replacement`. При `EVM_REPLACEMENT_SEND_UNCERTAIN` повтор с тем же
fingerprint использует уже сохранённую подписанную транзакцию. При изменении
лимитов подготовьте новый fingerprint. Статус `EVM_PENDING_FEE_CAP_BELOW_BASE`
применяется на mainnet и Sepolia. На mainnet исходная транзакция использует
заданные лимиты комиссии. Для повышения оператор обычно обновляет
`SATOSHIFI_MAX_FEE_PER_GAS_WEI` и `SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI`
в той же оболочке, сохраняя лимит газа и общий бюджет. Каждую замену оператор
подтверждает отдельно. Замена использует тот же DKG.

На mainnet и Sepolia SDK сверяет заданную глубину и финальность EVM. На
локальном стенде с chain ID `31337` применяется заданная глубина. Если замена
попала в блок и откатилась, сначала возобновите исходную команду для сверки
канонического receipt. Затем подтвердите использованный nonce командой
`bootstrap acknowledge-revert`. Статус исходного шага остаётся откатом.
Для CREATE и параллельных групп используйте процедуру восстановления bootstrap.
Если подписи ещё нет и отклонена сама оценка газа, сверьте лимит и повторите
исходную команду.

[Подготовка и подтверждение замены](BOOTSTRAP.md#replacing-a-pending-evm-call).

## 5. Отставание RPC участников

```mermaid
flowchart TD
    accTitle: Сверка EVM и Bitcoin при отставании RPC
    accDescr: Подписант сохраняет журнал и ждёт, когда RPC сможет повторно подтвердить исходный блок. Подтверждённый конфликт канонического хеша останавливает операцию для проверки.
    A["Исходная операция и сохранённый блок<br/>Сверить высоту, хеш и глубину"] --> B{"Что подтверждает RPC?"}
    B -->|"EVM не видит блок, receipt<br/>или подтверждённый nonce"| W["AWAITING_EVM_RPC или WAITING_DKG_RPC<br/>Контур ждёт исходное подтверждение"]
    B -->|"Bitcoin ниже сохранённой вершины<br/>или не хватает глубины"| V["Временное ожидание наблюдения Bitcoin"]
    B -->|"Хеш совпал, глубина достаточна"| C["Повторить точную проверку<br/>Продолжить с тем же журналом"]
    B -->|"Другой канонический хеш<br/>или подтверждённый глубокий fork"| X["Остановить новую подпись и отправку<br/>Сверить сохранённое доказательство"]
    W --> A
    V --> A
```

- `bootstrap`, `ops execute` и `dkg sign-evm` возвращают `AWAITING_EVM_RPC`
  при отставании нужных данных EVM. `dkg run-once` возвращает `WAITING_DKG_RPC`, пока
  RPC не видит сохранённый подтверждённый блок. Несовпадение известного хеша
  вызывает `EVM_REORG_RECOVERY_REQUIRED` или `DKG_ANCHOR_REORG`. Сверьте
  сохранённый блок перед продолжением.
- После распределения ULR тот же принцип сохраняет подписанный чекпоинт.
  SPV transport при отставании не сбрасывает входящий курсор и не ставит
  подтверждённые пакеты в очередь снова. Возобновите команды с исходным журналом.
- Перед новой EVM-подписью SDK сверяет последнюю успешную транзакцию адреса,
  завершённые операции восстановления и открытые резервирования в журнале nonce.
  Если `pending` nonce RPC ниже проверенной нижней границы, новая подпись ждёт.
  Уже зарезервированный nonce остаётся связанным с исходными байтами.
- При `DKG_REVIEW_REQUIRED` сверьте фазу, причину и подтверждённый хеш.
  Если решение оператора действительно состоит в прекращении церемонии,
  повторите исходный `dkg run-once` с `--confirm-abort FINGERPRINT` из
  поля `confirm_abort`. Используйте тот же bundle и состояние. `flow dkg`
  возвращает этот результат для затронутой роли и останавливает опрос.
  Если в FROST-сессии к дедлайну собран порог проверенных долей, bootstrap
  продолжает ждать финализации. После дедлайна новых долей контракт не принимает.
- Bitcoin-watch сообщает `WAITING_BITCOIN_RPC`, если текущая вершина ниже
  сохранённого курсора и наблюдаемые хеши на общей высоте не противоречат друг
  другу либо общего блока в локальном окне нет. Курсор и сведения о расходах
  сохраняются. ZK worker сообщает `ZK_OBSERVATION_CORE_BEHIND` при меньшем
  chainwork или другой вершине с тем же chainwork. Ранее подтверждённый
  глубокий reorg требует отдельного разбора.
- Для временно недоступной транзакции отдельная задача ZK worker ждёт с
  увеличивающейся паузой. Остальные задачи и heartbeat могут продолжиться.
  Через совместимый SPV transport ожидание передаётся как
  `OBSERVATION_UNAVAILABLE`.

Для смены отстающего EVM RPC повторите исходную команду с `--rpc`.
`bootstrap resume` работает с теми же deployment ID, состоянием и identity.
SDK сверяет сеть и сохранённые привязки операции с данными нового RPC.

```sh
frost-signer bootstrap resume --deployment "$DEPLOYMENT_ID" --state "$STATE" \
  --identity-file "$IDENTITY" --rpc "$EVM_RPC" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
```

`flow bootstrap`, `ops execute` и `dkg` также принимают локальный `--rpc`. Сохраните тот же
ключ операции или сессию DKG. Для возврата к ZK mint или settlement после
финализированного отката используйте повтор с актуальной материализацией.
[Восстановление ZK-вызова](ZK_SPV_BOOTSTRAP.md#retry-after-a-proof-call-reverts).

Если контур ZK временно не видит уже подтверждённый receipt, он сообщает
`WAITING` с причиной `EVM_RECEIPT_PENDING`. Если провайдер ещё не видит
сохранённый якорь, контур может сообщить `EVM_RPC_BEHIND`.
Принятый результат остаётся в журнале, зависимые этапы ждут, а независимые
задачи могут продолжиться. [Поведение контроллера](ZKSPV_CONTOUR.md).

## 6. Выход из DAO и возобновление продажи комиссий

```mermaid
flowchart TD
    accTitle: Возвраты после закрытия и смена EVM RPC
    A["Сохранить исходный pin, identity, ID и журнал"] --> B["Повторить ту же команду с текущим --rpc"]
    B --> C{"Новый RPC подтверждает исходные якоря?"}
    C -->|"Ещё не видит блок"| W["AWAITING_EVM_RPC, ждать"]
    C -->|"Хеш расходится"| X["EVM_REORG_RECOVERY_REQUIRED"]
    C -->|"Да"| D{"Источник активен?"}
    D -->|"Да"| E["Продолжить исходное действие и nonce"]
    D -->|"Нет"| F["Доступны cancel просроченной заявки<br/>и DAO unstake, claim, reimburse"]
```

При сбое провайдера повторите исходный вызов `fee-sale` или `dao` с рабочим
`--rpc`. Сохраните тот же журнал, идентификатор действия, identity и входные
данные, включая бюллетень или intent, если он нужен команде. SDK использует
наибольшую из сохранённой и запрошенной глубин. При отставании он ждёт блок,
а при несовпадении сохранённого хеша требует разбор reorg. Оператору не нужно
создавать новый nonce или новую FROST-сессию для того же действия.

При деактивации исходные `fee-sale cancel`, `dao in-kind unstake`, `claim`
и `reimburse` доступны с прежними привязками. Покупатель получает свой
стейблкоин после срока заявки. Стейкер или плательщик газа подписывает только
собственный вывод. Для новых заявок, стейка и управленческих операций SDK требует
активный граф. [Продажа комиссий и покрытие](FEE_FLOW.md#продажа-комиссий-s-и-mp) ·
[Выплаты DAO](CONFEDERATION_DAO_IN_KIND.md#приём-и-распределение).

<a id="expired-s-btc-recovery"></a>

## 7. Вывод S → BTC после дедлайна подписания

Для нового V3 при потере основного Bitcoin-кворума доступен
[отдельный профиль CLTV](BITCOIN_WITHDRAWAL_RECOVERY.md).
Просрочка подписания сама по себе не открывает возврат S.
Подтверждённый перенос и FROST-ротация возвращают обычный вывод с преемника.
[Порядок ротации](BITCOIN_WITHDRAWAL_RECOVERY.md#ротация-после-переноса).

У этого восстановления свой срок batch. `--deadline` в `dao in-kind withdraw`
новой сборки ограничивает только включение первоначальной заявки в EVM. После
включения сохранённые минимум чистой выплаты и предел miner fee проверяются
при повторном расчёте, включая RBF и settlement. Просрочка включения откатывает
создание заявки целиком. Для такого отката проверьте исходный receipt до
подачи новой заявки. [Условия заявки и котировка](CONFEDERATION_DAO_IN_KIND.md#btc-и-комиссия-майнеров).

```mermaid
flowchart TD
    accTitle: Возобновление связанной выплаты S в BTC
    accDescr: По исходному batch SDK проверяет escrow, состояние Signing и точный план. Выбранные участники завершают публикацию commitments и долей по тому же транскрипту, затем выплата отправляется и рассчитывается.
    A["btc-status<br/>Исходные board и batch"] --> B{"Подтверждённый Signing<br/>с точной связанной выплатой?"}
    B -->|"Нет"| X["Остановить попытку восстановления"]
    B -->|"Да, срок истёк"| C["Сверить Core, UTXO, план, комиссии и локальный журнал"]
    C --> D{"Есть все commitments?"}
    D -->|"Нет"| E["btc-commit --resume-expired<br/>Участники с недостающими commitments"]
    E --> D
    D -->|"Да"| F["btc-sign --resume-expired<br/>Участники с недостающими долями"]
    F --> G["btc-broadcast<br/>Исходная транзакция, тот же txid"]
    G --> H["btc-settle<br/>После подтверждения BTC и SPV"]
```

Для плана версии `5` с `BitcoinWithdrawalEscrowV2` продолжите тот же
batch после дедлайна, если EVM подтверждает `Signing` и исходный платёж всё
ещё связан с escrow. Используйте прежние DKG, identity, состояние подписанта,
журнал nonce и переменные из [инструкции `btc()`](BITCOIN_ONCHAIN_RUNBOOK.md#1-локальные-параметры).
Укажите оба исходных идентификатора явно.

```sh
btc btc-status --signing-board "$SIGNING_BOARD" --batch "$BATCH"

# Выбранные участники дополняют недостающие commitment, если они есть.
btc btc-commit --signing-board "$SIGNING_BOARD" --batch "$BATCH" --resume-expired

# После подтверждения всех commitment выбранные участники дополняют доли.
btc btc-sign --signing-board "$SIGNING_BOARD" --batch "$BATCH" --resume-expired

btc btc-broadcast --signing-board "$SIGNING_BOARD" --batch "$BATCH"
btc btc-settle --signing-board "$SIGNING_BOARD" --batch "$BATCH"
```

До подготовки новых подписных данных SDK повторно сверяет закреплённый граф,
сетевой профиль, состояние Bitcoin Core, резервы входов, полный план и
обязательства escrow. Получатели, суммы, лимиты комиссии, Bitcoin txid и
состав подписантов сохраняются. `BTC_EXPIRED_SIGNING_NOT_BOUND` означает,
что условия восстановления не выполнены. Для исходного batch без отдельного
fee reserve, который остался в `Reserved`, SDK поддерживает
[проверяемую FROST-отмену](BITCOIN_ONCHAIN_RUNBOOK.md#cancel-reserved-bitcoin-batch).
При `BTC_INPUT_CORE_CHANGED` дождитесь согласованного ответа Core и повторите
исходную команду для того же batch.

Если commitment участника уже опубликован, нужен исходный локальный nonce
или ранее сохранённая доля того же транскрипта. При
`NONCE_COMMITMENT_MISMATCH` восстановите точное локальное состояние и сверяйте
его с журналом nonce. Повтор проверенного вызова отправляет уже подготовленную
долю. Возврат S после начала подписания заблокирован контрактом.

<a id="unused-factory-id-recovery"></a>

## 8. Перенос резервации ID после остановки DKG

Процедура применима к неиспользованным ID новых `FederationFactory` и
`ConfederationFactory`. Оператор из `feeSetupAuthority()` подтверждает перенос
на точную новую сессию. Оба хеша условий и сам ID сохраняются.
Фабрика проверяет терминальную фазу в состоянии сети при исполнении
транзакции. SDK дополнительно проверяет глубину подтверждения и финальность
на Ethereum mainnet перед отправкой.

```mermaid
flowchart TD
    accTitle: Перенос неиспользованной резервации ID после остановки DKG
    A["Оператор сверяет factory, ID и текущую EVM_AUTH"] --> B{"Реестр зафиксировал Failed или Aborted<br/>в подтверждённом блоке?"}
    B -->|"Нет"| W["Дождаться блока RPC или завершить<br/>отдельную процедуру остановки DKG"]
    B -->|"Да"| I["Новое приглашение и enrollment<br/>Сохранить ID и хеши условий"]
    I --> N["Создать замену в исходном DKG registry<br/>Сверить registry, board и coordinator"]
    N --> C{"ID ещё не принят<br/>и новая DKG соответствует условиям?"}
    C -->|"Нет"| X["Отклонить перенос"]
    C -->|"Да"| P["Сверить хеши условий и подтвердить fingerprint"]
    P --> T["Factory атомарно переключает сессию<br/>и сохраняет оба хеша"]
    T --> R["Подтвердить квитанцию и новую привязку"]
    T -->|"Транзакция ждёт"| F["Сверить подтверждённый источник и DKG<br/>Одобрить fee bump или закрытие nonce"]
    F -->|"Точный перенос"| R
    F -->|"Подтверждённая отмена"| B
    R --> D["Завершить новую DKG, затем выполнить adoption"]
```

1. Сверьте текущие `reservedFederationSession(ID)` и
   `reservedFederationTerms(ID)` либо соответствующие getters конфедерации.
   Перенос доступен только до adoption. Уже принятая организация использует
   свой исходный путь до deploy.
2. Дождитесь `Aborted` или `Failed` в каноническом DKG-реестре фабрики.
   Для согласованного abort участник выполняет исходный
   `dkg run-once --confirm-abort` по [процедуре выше](#5-отставание-rpc-участников).
   Если фаза просрочена, запишите `Failed` отдельной транзакцией
   `advancePhase(bytes32)`. Для V1 она разрешена после
   `phaseDeadline`. Истечение времени и отставание RPC сами по себе не меняют
   фазу. Для прежней DKG в `Completed` действует обычный adoption.
   Дождитесь глубины подтверждения по профилю и финальности на Ethereum mainnet.
3. Для федерации и конфедерации из приглашения V6 подготовьте отдельный
   enrollment. Используйте проверенный отпечаток исходного V6 приглашения
   `OLD_INVITE_FINGERPRINT`, новый идентификатор запуска `RECOVERY_RUN_ID` и
   будущий Unix-дедлайн `APPROVAL_DEADLINE`. Исходные ID, настройки и хеши
   сохраняются. Новые epoch, session IDs и fingerprint видны при проверке.

   ```bash
   frost-signer setup reservation-invite --invite "$ORIGINAL_INVITE" \
     --accept-fingerprint "$OLD_INVITE_FINGERPRINT" --deployment-id "$RECOVERY_RUN_ID" \
     --deadline "$APPROVAL_DEADLINE" --output "$RECOVERY_INVITE"
   frost-signer setup publish --invite "$RECOVERY_INVITE" --rpc "$RPC" \
     --identity-file "$OPERATOR_IDENTITY" --state "$LOCAL/factory-transactions.sqlite"
   ```

   Опубликованный board и новый fingerprint каждый сверяет через свой RPC.
   Все приглашённые регистрируют транспортные ключи командой `setup join-chain`
   в **новом** локальном каталоге. На Ethereum mainnet и для Bitcoin mainnet
   с Sepolia нужен отдельный `--nonce-ledger-dir`. Для защиты от совместного
   отката локальных томов привяжите новый signer state через `--nonce-anchor-config`
   и сохраните его локальное окружение. Публичный Bitcoin требует свои флаги Core.
   [Параметры enrollment](FIRST_DKG_ONCHAIN.md) ·
   [Подготовка HTTPS-якоря](SIGNING_PROTOCOL.md#off-host-nonce-anchor).

   ```bash
   CEREMONY_ARGS=(
     --kind "$KIND" --factory "$FACTORY" --from-session "$OLD_DKG"
     --board "$RECOVERY_BOARD" --rpc "$RPC" --confirmations "$EVM_CONFIRMATIONS"
   )
   frost-signer setup reservation-ceremony "${CEREMONY_ARGS[@]}"
   frost-signer setup reservation-ceremony "${CEREMONY_ARGS[@]}" --send \
     --accept-fingerprint "$CEREMONY_FINGERPRINT" --identity-file "$OPERATOR_IDENTITY" \
     --state "$LOCAL/factory-transactions.sqlite"
   ```

   До отправки сверяются точный board, исходная factory, DKG registry, роль,
   обе сессии, хеши formation и токена и порядок участников. Если `feeSetupAuthority`
   отличается от `ceremonyRegistrar`, registrar разрешает создание только
   указанного session ID через `authorizeCeremonyCreator` и ждёт подтверждения.
   Из `RESERVATION_CEREMONY_READY` возьмите `NEW_DKG` и `role`. Участники
   этой роли выполняют `dkg run-once --board "$RECOVERY_BOARD" --role "$ROLE"`
   со своим RPC и новым состоянием. Для custodian-only конфедерации подпись
   федеративного представителя в этой DKG не нужна. Остальные роли нового
   приглашения остаются без созданной сессии. Замена для переноса должна
   иметь действующую фазу или `Completed`.
4. Выполните проверку. В `KIND` укажите `federation` или `confederation`.
   Сверьте адреса, старую и новую сессии и хеши с согласованными данными.
   После проверки передайте полученный `FINGERPRINT` и identity владельца резервации.

   ```bash
   RESERVATION_ARGS=(
     --kind "$KIND" --factory "$FACTORY" --entity-id "$ENTITY_ID"
     --from-session "$OLD_DKG" --to-session "$NEW_DKG"
     --rpc "$RPC" --chain-id "$CHAIN_ID" --confirmations "$EVM_CONFIRMATIONS"
   )
   frost-signer setup reservation-transfer "${RESERVATION_ARGS[@]}"

   frost-signer setup reservation-transfer "${RESERVATION_ARGS[@]}" --send \
     --accept-fingerprint "$FINGERPRINT" --identity-file "$OPERATOR_IDENTITY" \
     --state "$LOCAL/factory-transactions.sqlite"
   ```

5. При `RESERVATION_TRANSFERRED` сверьте подтверждённую EVM-квитанцию,
   новый session ID и прежние хеши. При потере ответа повторите команду
   с тем же fingerprint и журналом. SDK вернёт результат той же транзакции.
   Новая сессия становится единственным основанием для adoption.
   Прежняя сессия навсегда остаётся недоступной для резервирования на этой фабрике.

После подтверждённого `Completed` используйте метод и аргументы из
`review.adoption` команды `reservation-ceremony`. Для федерации это
`adoptFederationDKG`, для обычной конфедерации `adoptConfederationDKG`,
для конфедерации только кастодианов `adoptCustodianConfederationDKG`.
Вызов выполняет уполномоченный участник. Фабрика снова сверяет formation
с зарезервированным хешем. Для custodian-only board оператор после
`Completed` исполняет точный вызов `review.enable_custodian_publishers`.
После подтверждения кастодиан сможет публиковать намерения только для
сессий, которые создал сам. При deploy передайте точные имя и символ
из `review.token_metadata`. Исходный bootstrap-журнал остаётся закреплён
за старой сессией. Для произвольной multi-federation formation используйте
[отдельный фабричный сценарий](FEDERATION_CONFEDERATION_SETUP.md#несколько-федераций-в-конфедерации).
Этот сценарий доводит замену до adoption и фабричного deploy. Подключение
существующей BTC_SPEND роли и средств требует штатной процедуры continuity с
её собственными контекстом и проверками. [Bitcoin continuity](BITCOIN_CONTINUITY.md).
Для ранее развёрнутых неизменяемых фабрик действует их исходный ABI.

### Зависшая транзакция переноса

Даже общий recovery другого EVM-процесса перед повторной отправкой сверяет
сохранённый подтверждённый блок, точный calldata и актуальность обеих DKG.
При отставании RPC подождите или смените RPC с тем же журналом. Для
актуального переноса штатный `bootstrap replace-pending` даёт пересмотреть
газ с тем же nonce и calldata. Если перенос больше не нужен или прежний
якорь перестал быть каноническим, оператор может подтвердить замену на
нулевую транзакцию себе. Оба варианта используют исходный EOA и его журнал.

```bash
CANCEL_ARGS=(
  --rpc "$RPC" --identity-file "$OPERATOR_IDENTITY" --tx-hash "$PENDING_TX"
  --max-fee-per-gas-wei "$MAX_FEE" --max-priority-fee-per-gas-wei "$MAX_TIP"
  --max-cost-wei "$MAX_COST" --cancel-reservation
)
frost-signer bootstrap replace-pending "${CANCEL_ARGS[@]}"
frost-signer bootstrap replace-pending "${CANCEL_ARGS[@]}" \
  --confirm-replacement "$CANCEL_FINGERPRINT"
```

Транзакции конкурируют за один nonce. Повторяйте с тем же fingerprint до
`RESERVATION_TRANSFER_CANCELLED` либо квитанции исходной операции. Если
исходный перенос успел исполниться, сверяйте его квитанцию и состояние ID.
Если оригинал или его fee bump уже откатился, сначала прочитайте итог
`replace-pending`. После финальности подтвердите расход именно этого nonce
через `bootstrap acknowledge-revert --tx-hash "$MINED_TX" --rpc "$RPC"
--identity-file "$OPERATOR_IDENTITY"`. Новый перенос потребует новой
допустимой DKG и отдельного review.
