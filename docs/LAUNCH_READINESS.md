# Готовность к запуску: код, схемы и входные данные

[Схемы: SVG/PNG и hashes текущих Mermaid-исходников](DIAGRAMS.md).

## Промышленный тест на Sepolia и Bitcoin testnet4

Соберите проверяемую поставку из текущих исходников. Каждый участник задаёт
свой архивный HTTPS Bitcoin RPC и Sepolia RPC и проходит `configure check-rpc`.
Для relay выполните новый холодный старт, паузы финансирования, депозиты,
перенос custody, вывод и восстановление при остановке подписанта. Используйте
отдельный deployment ID для каждого режима и тестовые активы.

Для ZK используются SDK и неизменяемый verifier RISC Zero 3.1.0 с селектором
`0xe71e6620`. Для теста после обновления создайте граф с новым state и
verifier. Переход с уже используемого графа 3.0.0 требует отдельной миграции. Каждый
участник устанавливает собственные инструменты, считает свежий proof и
проверяет receipt. Выбранный publisher отправляет proof в новый тестовый граф.
Результаты архивных proofs 3.0.3 остаются историческими. Публичный прогон и
измерения на целевых Linux/WSL2 входят в приёмку. [Установка prover](ZK_PROVER_LINUX.md)
· [Advisory RISC Zero](https://github.com/risc0/risc0-ethereum/security/advisories/GHSA-mm6c-35cm-g4x2).

Для возобновления bootstrap сохраняйте профиль, SQLite-журналы, ключевые
эпохи и исходный комплект Solidity-артефактов. Новые записи закрепляют ABI,
точный creation/runtime bytecode и места ссылок, поэтому изменение source maps
при эквивалентной сборке не останавливает resume. Старые журналы сохраняют
проверку своего исходного полного хеша.

### Ограничения и приёмка

| Область | Ограничение и проверка перед запуском |
| --- | --- |
| Сети и RPC | Relay поддерживает regtest, testnet4 и mainnet. ZK поддерживает отдельные профили testnet4 и mainnet. Для удалённого стенда каждый участник до enrollment проверяет свой Sepolia RPC и архивный Bitcoin Core HTTPS RPC с `txindex`. Участникам нужны подтверждённые тестовые ETH для газа, тестовые BTC для денежных сценариев и согласованные USD-бюджеты. [Холодный старт](COLD_START.md) |
| Bitcoin | Не более 50 адресов на batch вывода. Минимум получателю 294 sats для P2WPKH и 330 sats для P2TR. Для ZK предел перехода 64 headers, предел reorg 100 блоков. Глубина денежного подтверждения и Fresh проверяются отдельно. [Математика Bitcoin](LAUNCH_MATHEMATICS.md#bitcoin-подтверждения-maturity-и-fresh--разные-проверки) |
| ZK prover | macOS arm64 или Linux/WSL2 x86-64, Groth16 в pinned контейнере linux/amd64. Свободное место от 8 GiB для preflight. Измерьте время, RAM и пиковое место на свежем proof каждого участника. [Установка и изоляция](ZK_PROVER_LINUX.md) |
| Дневной снимок ULR | До 4096 адресов в inventory вместе с действующими кастодианами, до 64 казн. Лимит board 32 KiB, полный snapshot intent до 4 MiB при публикации SHA-256. Перед подписанием SDK проверяет нижнюю границу газа. Проверьте полный расчёт и исполнение при целевом составе. [Казначейства](ULR_TREASURIES.md) |
| Возобновление | Используйте тот же deployment ID и журнал. При зависшем nonce сначала сверяются журнал, on-chain nonce и receipts, а закрытие nonce требует отдельного решения оператора. Для legacy journal сохраните оригинальный комплект артефактов. [Восстановление bootstrap](BOOTSTRAP.md#withdrawal-deployment-and-recovery) |
| Допуск средств | `graph_configured` подтверждает конфигурацию. `ready`, `QUALIFIED`, `FEE_QUALIFIED` и `production_ready` требуют отдельных проверок. Для испытаний используйте testnet4 BTC и Sepolia ETH. |

Решение о допуске реальных средств требует отдельной проверки FROST/HPKE
из-за транзитивного `crypto-bigint 0.5.5` и прогона ротации ключей.
Удаление роли само по себе не отзывает существующую DKG share. [RustSec RUSTSEC-2026-0387](https://rustsec.org/advisories/RUSTSEC-2026-0387.html).

[Холодный старт с нуля](COLD_START.md): единый порядок подготовки, ролей,
финансирования, DKG, relay/ZK, ручной рассылки и первого smoke.

[Сквозной сценарий FROST и ZK](FROST_ZK_LIFECYCLE.md): отдельные DKG,
уравнения подписи и proof, nonce recovery, гонки публикаций и Bitcoin settlement.

[Математическое описание](LAUNCH_MATHEMATICS.md): единицы, точное округление,
границы выплат, UTC-учёт, APY и предпосылки безопасности.

[Запуск с нуля](CROSS_PLATFORM_QUICKSTART.md) · [Тестовые режимы](SYSTEM_TEST_MODES.md) ·
[Полный bootstrap](ULR_BOOTSTRAP.md).

Обновление 4 ноября: [отдельные ключи кастодианов и передача BTC](BITCOIN_CUSTODY_TRANSFER.md).
В новой custody policy представитель исключён из денежных EVM/BTC DKG;
внешний MP меняется на cBTC только после фактического исходного платежа,
погашения MP и SPV-приёма назначения. Старые приглашения не мигрируют.
Повторная проверка закрыла старые operator-only ограничения публикации
и запись intent/chunks в чужие или будущие FROST-сессии. Результаты,
команды и границы — в [тестовом runbook](SYSTEM_TEST_MODES.md#проверка-custody-от-4-ноября-2026).
`graph_configured=true` по-прежнему не означает `ready=true`.

Обновление поставки от 3 ноября: [каждый ZK-участник сам считает рабочие proofs](DISTRIBUTION.md).
В participant archive нет готовых receipts/seals, inputs/journals и caches.
Новый SDK разделяет локальный расчёт и публикацию; собственный cache требует
локальной записи происхождения. Прежние acceptance-отчёты ниже относятся к
своим исходным snapshots и не подтверждают эти новые изменения.

Эта карта относится к новому `--system-policy` Bitcoin + ULR deployment.
Готовность исходников и шаблонов не означает, что уже выбраны участники, RPC,
живые адреса контрактов и финансирование. Файлы `*.example.json` являются
шаблонами; локальные тестовые адреса из acceptance-отчётов не становятся
адресами будущего запуска.

## Порядок запуска

```mermaid
flowchart TD
    S["Одна поставка: source и pins без working proofs"] --> H["Каждый: native build, identity, свой RPC/Core<br/>ZK tools и isolation preflight до enrollment"]
    H --> C["Configure wizard, validate и offline build<br/>Сети, имена, H/m, reserve/bps, APY"]
    C --> E["ETH для первых EVM-транзакций<br/>Invitation review, publish и enrollment"]
    E --> F["Optional funding pause перед DKG<br/>Confirmed balances и явное снятие"]
    F --> D["Три DKG: federation EVM<br/>Conf EVM и BTC: только кастодианы"]
    D --> R["Resume того же deployment<br/>Root pin и target-bound FROST review"]
    R --> G{"Текущий барьер плана"}
    G -->|"Relay"| L["Sync Core, canonical work, health и depth"]
    G -->|"ZK"| Z["State и verifier 3.1.0<br/>Каждый считает свой proof"]
    Z --> ZP["Проверенный seal 0xe71e6620<br/>Один publisher, полезный update и Fresh"]
    G -->|"Governance distribution"| U["Create: выпуск 10 млн одному адресу<br/>Ручная рассылка и свежий fingerprint"]
    G -->|"Подписи"| A["Нужные роли: свои FROST и custodian approvals"]
    L --> R
    ZP --> R
    U --> R
    A --> R
    G -->|"Все барьеры пройдены"| V["VERIFIED без defer<br/>Graph configured, ready остаётся false"]
    V --> T["Sepolia + testnet4<br/>Тестовые депозиты, custody, вывод и восстановление"]
    V -->|"Опционально NFT"| N["Независимый NFT pin и public intent<br/>FROST create/mint; conf custodian minimum"]
    N --> Q["Read-only quote без расхода<br/>NFT use через одобренный pool владельца"]
    Q --> T
```

Для ZK первая полезная публикация нужна на барьере Bitcoin readiness, до
обычного consumer deployment. Явный структурный этап `--defer-zk-freshness`
допускает подготовку consumer/heartbeat graph; он не разрешает денежные операции
и не заменяет окончательный verify без defer. Личный worker и initial heartbeat настраиваются
после появления адреса state; checkpoint сам по себе не Fresh. Дальнейшие
рабочие переходы участники также считают самостоятельно.

Linux, macOS и Ubuntu внутри Windows/WSL2 используют один SDK и протокол.
Локальный ZK prover реализован для macOS arm64 и Linux/WSL2 x86-64. Для relay
выбираются Bitcoin mainnet/testnet4/regtest; ZK выбирает отдельный pinned guest mainnet или testnet4; regtest ZK запрещён.
EVM выбирается независимо: 1, 11155111 или 31337. `graph_configured=true`
не переключает `ready` или `QUALIFIED`.
Каждый ZK-участник устанавливает свои tools и проходит isolation preflight
по [Linux ZK runbook](ZK_PROVER_LINUX.md). Неподдерживаемая платформа блокируется
при ZK enrollment. Компонентные проверки Linux прошли; полный fresh-proof
и физический WSL2 прогон остаются отдельной приёмкой.

## Экономический граф ULR

```mermaid
flowchart TD
    Issue["Один выпуск: 10 млн ULR<br/>Один согласованный получатель"] --> Pause["Пауза bootstrap<br/>Ручная рассылка ULR"]
    Pause --> Wallet["Кошельки владельцев"]
    Wallet --> Stake["Общий ULRStaking"]
    Wallet --> Weight["Вес владельца: кошелёк плюс собственный stake"]
    Stake --> Weight
    Weight --> Candidate["Tracked nomination и вес не менее H<br/>Кандидат; H задан до enrollment"]
    Candidate --> Approve["Отдельное одобрение действующего состава<br/>Не выдаёт новую долю DKG"]
    Approve --> Active["Действующий кастодиан"]
    Stake --> Delegate["Делегирование голосов действующему кастодиану"]
    Active --> Snapshot["Общий исторический снимок суток UTC"]
    Weight --> Snapshot
    USD["Свободный USD каждой казны"] --> Pay["floor превышения над R, умноженного на bps/10000 и C/G"]
    Snapshot --> Pay
    Pay --> Rewards["USD в общий staking reward pool"]
    Budget["Отдельный prefunded USD-бюджет"] --> APY["Startup: APY, цена, срок<br/>Выплата ограничена балансом"]
    APY --> Rewards
    Rewards --> Claim["Claim владельцу stake"]
```

Снимок включает всех держателей от H (default 200 000 ULR) и действующих кастодианов с
меньшим положительным остатком; один владелец учитывается в системном
знаменателе один раз. Собственный stake не уменьшает вес. Делегирование меняет
голосование, все награды сохраняются у владельца. Внесённый после 00:00 stake участвует
со следующих суток UTC (особый случай ровно 00:00 — в математике); свободный вывод и отдельный claim сохраняются.
Точная формула, округление и полнота inventory описаны в
[казначейском runbook](ULR_TREASURIES.md).

## Повторное использование с другими участниками

```mermaid
sequenceDiagram
    participant New as Новая группа
    participant Root as Исходная root-группа
    participant Chain as EVM contracts
    New->>Chain: Новое приглашение и три DKG
    Note over New,Chain: Conf EVM и BTC: только новые кастодианы
    New->>Chain: Импорт общих ULR, USD, staking, snapshots и root
    New->>Root: Запрос включения нового address registry
    Root->>Chain: Проверка графа и FROST enrollment
    New->>Chain: Собственный binding и custodian approvals
    Chain-->>New: Новый независимый граф, без нового выпуска ULR
    Note over New,Root: Enrollment общего root не передаёт BTC и не выдаёт долю нового ключа
```

Новая группа не получает полномочия исходной root-authority или общей
snapshot-authority. Она использует свои identity, encrypted states и journals.
Для ненулевой доли дохода её кастодианам нужны собственные ULR; `reuse` их не
раздаёт. Порядок действий исходной и новой групп приведён в
[разделе о второй конфедерации](CROSS_PLATFORM_QUICKSTART.md#13-следующая-конфедерация-с-другими-людьми).

## Форматы и источники данных

Уточнения исходного ТЗ, принятые в ходе работы:

| Вопрос | Принятая конфигурация |
| --- | --- |
| Статус от согласованного H | Кандидат по кошельку + собственному stake; включение в действующий состав требует отдельного одобрения |
| Вес дохода | Все держатели от H и действующие кастодианы с меньшим положительным весом; в числителе — действующий состав конфедерации |
| Казна и остаток | Свободный баланс одного общего USD; R, не выплаченная по bps и доле часть остаются в казне; отдельный утверждённый operational spend может уменьшить остаток ниже R |
| Выбор SPV | Relay или ZK выбирается при развёртывании в рамках поддерживаемых сетей |
| Выпуск MP | Проверка Bitcoin-доказательства и отдельное FROST-одобрение сохраняются |
| Делегирование | Голоса направляются действующему кастодиану; stake и награды принадлежат владельцу |
| Денежные ключи конфедерации | Отдельные EVM/BTC DKG только кастодианов; кандидат/делегат не получает долю ключа автоматически |
| MP → cBTC между графами | Реальный Bitcoin-платёж и погашение исходного MP до выпуска cBTC; после приёма исходные участники для вывода не нужны |
| NFT | Опциональный post-bootstrap FROST create/mint; право issuer у manager, quote не расходует quota, consuming caller — одобренный contract-pool |

Эти уточнения заменяют соответствующие первоначальные формулировки об
автоматическом составе кастодианов, обязательном ZK и оценке любых активов казны
в долларах. Казна USD и Bitcoin UTXO/reserve для miner fees — разные учёты.
Исторический снимок фиксирует момент баланса; отдельный минимальный срок владения
или исключение займа на границе суток этим профилем не вводятся.

Версионные правила проверяются исполняемыми SDK validators, а не только
синтаксисом JSON. Они повторно проверяются при подготовке, подписи и исполнении.

| Данные | Формат и источник | Что подставить для запуска |
| --- | --- | --- |
| Полный системный профиль | Version 1; `bootstrap_system.bundle`; `system-bootstrap-*.example.json` | Сгенерировать `system-profile` из собственной сборки и согласованных policy |
| Отдельные ключи конфедерации | `bitcoin_custody`, `conf_ceremony` и `btc_ceremony` только кастодианов | До enrollment выбрать порог, источники и runtime hashes; старые профили не обновляются |
| Передача BTC под новый ключ | `BitcoinCustodyTransfer`; отдельные source/destination registry и escrow | Исходный расчёт и погашение MP, точный канонический выход назначения, только затем выпуск cBTC; см. runbook передачи |
| Имена токенов | `token_metadata`: четыре роли с `name` / `symbol`; `token-metadata.example.json` | Задать до enrollment; при reuse сохранить `ulr.token` общего токена |
| ULR create/reuse | Version 1; `bootstrap_ulr.validate` | Первый create либо `ulr.reuse_policy` реального verified manifest |
| Bitcoin backend | Relay policy V1 либо ZK policy V2; network validators | Собственный checkpoint/Core; для ZK — реальные state/adapter bindings |
| Приглашение и три ceremony | V6; `onboarding.validate_invite` и проверенные enrollments | Сети, адреса, порядок, threshold, custodian minimum, сроки и расходы |
| Bitcoin family root | `create-1` или pin из исходного verified manifest | Исходный board, authority и независимо проверенный fingerprint |
| ULR treasury pin | Version 1; `ulr_treasury.load_pin` | Реальные staking/snapshots/treasuries, chain ID и confirmations |
| NFT pin | Version 1; `nft_governance.validate_pin` / `nft init` | Свои chain/confirmations, board/role, factory и fee manager; точные create/mint intents отдельно |
| Дневной inventory | `ulr-snapshot-inputs.example.json`; `snapshot_arguments` | Предыдущие сутки, последний исторический блок, полный inventory и evidence hash |
| Snapshot evidence | Version 3; SDK и контрактный ABI | Формирует SDK по архивному RPC; не копировать пример дня или block 1 |
| ZK worker и contour | `zkspv-*.example.json`; соответствующие `load_config`/`validate_spec`/plan builders | Личные пути, host hash, actual consumers, лимиты, запросы и согласованные bindings |
| Старый single-share service | `signer-agent/.env.example` | Это отдельный прежний сервис; новые DKG shares туда не вставляются |

`system-profile` пересчитывает commitments из текущего production artifact,
публичного qualification report и service policy. Commitments шаблонов
проверяются относительно сборки. Публичный report с qualification pending
сохраняет этот статус после хеширования.

## Что должен подготовить оператор и каждый участник

| Уровень | Обязательные данные |
| --- | --- |
| Общие | Согласованная версия/fingerprint; две сети и backend; упорядоченный roster и thresholds; deployment ID; получатель выпуска ULR; treasury; финансовые параметры и лимиты |
| Каждый хост | Свои EVM RPC и gas; identity; каталог 0700; прежний EOA journal; свои Core RPC/cookie для публичного Bitcoin; собственные DKG states и password |
| Sepolia + testnet4 | Синхронизированный непрореженный Core с txindex через loopback или удалённый HTTPS; общая checkpoint policy; тестовый ETH и BTC; один согласованный USD, runtime hash/decimals и средства оператора для converter |
| После bootstrap | Реальные verified bindings; pin на каждом хосте; keeper/watch configuration; полный inventory и архивный EVM RPC для снимков; ежедневные snapshot/pay вызовы |
| Новый ZK proof | macOS arm64 или Linux/WSL2 x86-64; isolation preflight; точные tool/container/guest pins; собственный bound contour plan; приватные worker/service inputs; минимум 8 GiB свободного диска и фактический ресурсный бюджет prover |

RPC credentials, ключи, journals и nonce-пакеты не являются общими данными.
Файлы создаются на собственном хосте; оператор не заполняет их чужими ключами.
На Windows храните их в Linux home WSL2.
Для удалённых RPC каждый участник выполняет `configure check-rpc` со своим
доступом и согласованным приглашением до enrollment.
[Порядок проверки для коллег](PEER_TESTING.md#sepolia-и-bitcoin-testnet4-без-локальных-нод).

## Статус проверки

В локальном прогоне после обновления 12 ноября прошли 4033 SDK-теста, 30 тестов
ZK state/adapter и 9 Operations E2E. Verifier 3.1.0 принял официальный тестовый
receipt и отклонил неверные claims. `zk-cached` проверил EVM consumers и отказ
историческому seal, `zk-testnet4` проверил локальный граф и native witness.
Отдельный SDK-тест на fixtures проверил формирование board digest для 300
владельцев и отклонение подменённого файла.
Свежий рабочий Groth16, публичный create/reuse на Sepolia/testnet4 в обоих
режимах и прогон на физических Linux/WSL2 остаются пунктами приёмки.
[Тестовые режимы](SYSTEM_TEST_MODES.md) · [Результаты предыдущей проверки безопасности](SECURITY_LOGIC_REVIEW_2026_11_03.md#повторная-проверка-12-ноября-2026).

Актуализация 5 ноября: [конфигуратор и полная функциональная приёмка](../ops/artifacts/launch-configurator-20261105/README.md)
фиксируют 921 Solidity, 3436 SDK/ops, create/reuse, отдельный startup claim и refund.
[Проверка математического среза](../ops/artifacts/launch-docs-math-20261105/README.md)
сохраняет свои формулы и source hashes. [Текущая сверка документации](../ops/artifacts/docs-consistency-20261105/README.md)
проверяет соответствие схем/API коду, команды и новую поставку. Это не новый публичный
запуск и не подтверждение fresh testnet4 Groth16 или физического WSL2.
Предыдущие отчёты ниже сохраняют свои даты и границы.


Предыдущий [прогон custody-профиля](../ops/artifacts/custody-transfer-20261104/README.md)
подтвердил 908 контрактных тестов, 3344 разных SDK/ops-проверки, полный
create/reuse с другим roster и настоящую Core 30.3 regtest-передачу между двумя
native FROST-кошельками. Вывод назначения выполнен без исходных участников и
представителя федерации назначения. Это локальная проверка relay/regtest;
fresh ZK и физические Linux/WSL2 для этого изменения не подтверждены.

[Предыдущая проверка исходников, конфигураций, CLI и схем](../ops/artifacts/launch-readiness-20261103-final/validation.json)
хранится отдельно от [предыдущих протокольных прогонов](../ops/artifacts/portable-system-v1/validation.json).
Схемы выше экспортируются из тех же Mermaid sources и проверяются локальным
renderer без внешних запросов. Формальная JSON-проверка шаблона не подтверждает
наличие контракта или полноту historical inventory.
Предыдущий полный SDK-прогон: **3027 passed**. Проверены 113
CLI-примеров для 55 команд и 25 Mermaid-схем текущих архитектурных документов;
три новые схемы дополнительно осмотрены визуально. Эти результаты относятся к
локальной проверке, а не к состоявшемуся запуску с внешними участниками.
Изменения независимого proving отдельно проверены в
[новом отчёте](../ops/artifacts/independent-proving-20261103/validation.json):
3123 SDK/packaging/release tests и финальные 148 профильных тестов. Настоящий
исторический receipt проверен с отказом принять его за собственный proof.
Подготовка sources из распакованной поставки проверена без готовых proofs;
fresh proving и полная холодная сборка proving tools остаются неподтверждёнными.
Актуальные экспорты порядка запуска, экономики ULR и сценария reuse указаны
в [индексе схем](DIAGRAMS.md). Экспорты в предыдущих отчётах относятся к их срезам.

Повторить audit из собранного checkout можно командой:

```sh
python -m ops.audit_launch_readiness --output /absolute/new-launch-audit
```

Для проверки SVG из этой команды передайте `--diagrams` с manifest локального
рендера одной страницы `docs/LAUNCH_READINESS.md`.

Audit читает публичные файлы и вызывает только CLI `--help`; примеры отправки
транзакций не исполняются. Для проверки на другой машине задайте в PATH её
собственные native tools. CLI option-проверка не заменяет семантические
проверки реальных addresses, Core, балансов и подписанных профилей.

Чистая сборка Ubuntu 24.04 x86-64, 487 portable SDK-тестов, native Rust и
Core regtest smoke подтверждены в локальном Linux-контейнере под CPU-эмуляцией:
[отчёт платформ](../ops/artifacts/linux-portability-20261103/validation.json),
[начало тестов для коллег](PEER_TESTING.md). Физическая Windows/WSL2-машина,
полный чистый Bitcoin E2E-набор после последних изменений harness и новый
monetary ZK contour пока не подтверждены. Текущая
машина также должна пройти ресурсный preflight; нехватка места не обходится.
Production qualification, измерения и независимая приёмка остаются отдельными
требованиями. `qualified=false` сохраняется.
