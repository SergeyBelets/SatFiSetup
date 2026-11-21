# ZK-SPV: результат локальной реализации по ТЗ 2.16

Актуальный обзор кода и схем: [текущая архитектура](ZK_SPV_ARCHITECTURE.md).
Точный остаток реализации и проверки: [карта покрытия ТЗ](ZKSPV_FUNCTIONAL_COVERAGE.md).
43 workflow-рецепта и 195 observation slots multi-request suite реализованы; строки таблиц
§11.9–11.15 ещё не имеют полного единого qualified execution evidence.
Исторический [локальный пакет от 2 ноября](ZKSPV_LOCAL_ACCEPTANCE.md): 3269 Python, 834 ordinary
Solidity и 25 state/adapter tests, без ошибок и пропусков. Все три native-исключения
возвращены. Четыре сохранённых Groth16 receipt перепроверены без нового proving.
Настоящий общий контур отложен пользователем из-за места; независимая квалификация
и `QUALIFIED`/`FEE_QUALIFIED` остаются открытыми/выключенными соответственно.

Этот документ также сохраняет журнал последовательных этапов: формулировки
«ещё не подключено» внутри ранних proof/probe-этапов относятся к их snapshot.
Текущие границы определяются обзором и `source_snapshot` сводного отчёта.

Полная реализация ТЗ **не завершена**, production-переход запрещён. Обязательный
Citrea `bitcoin-da` закреплён и собран, но пока не пригоден для активации в
SatoshiFi. Существующий Bitcoin SPV сохранён. Нового самостоятельного
Bitcoin-consensus ядра, фиктивного guest/verifier или подмены proof подписью нет.

Исходный `Архив.zip` имеет требуемые 199 976 695 bytes и SHA-256
`117118f305e8471cb3e2626bd8623a612036afefe37cb4a1d9d94956a99d3868`.
Рабочая база: commit `733831ec48e3df96a2356623edf17771f64ba16d`, изначально чистая.
Сверка 3373 tracked-файлов не выявила изменённых относительно ZIP файлов:
53 файла отсутствуют в архиве (дополнительные `.deps` и dotfiles). Git commit
самого ZIP неизвестен; commit рабочего репозитория ему не приписывается.

## Что реализовано и проверяется

* Кандидат `BitcoinAddressRegistry`: неизменяемый intent/script/recipient,
  общая уникальность scripts, consumed/backing/reservations, отдельный адресный
  mint и generation-bound SMP1 mint. CUSTODY/DERIVATION статусы имеют отдельные
  nonce/generation и необратимые terminal states. Реальный кандидат verifier
  остаётся не квалифицирован; legacy signer profile отвергает новый граф,
  явные V3/V4 plans/input keys/shared journal подключены отдельным профилем.
  [Границы и проверки](ADDRESS_DEPOSIT_REGISTRY.md).
* `CallbackPoolRegistry`: происхождение пулов и S/CS-токенов из закреплённых
  CREATE-deployers, exact runtime hashes, взаимные bindings и точная withdrawal.
  Escrow проверяет это при lock/bind/replacement. Привязка registry однократная,
  через прежнюю денежную FROST authority. Bootstrap и проверки клиента обновлены.
* Воспроизведение дефекта исходного escrow в локальной EVM: поддельный callback
  откатывал общий settlement с настоящими MP. Новый lock отвергает его до
  переноса средств. SPV/FROST в этой узкой fixture — явно обозначенные заглушки.
* Исправлено соответствие maturity клиента контракту: coinbase mint ровно 100,
  обычный депозит — его `depth`; signer spend coinbase по-прежнему требует 101.
* `BitcoinMintAdmission`: общий ончейн-слот текущего pool nonce, атомарное создание
  FROST-сессии, неизменные экономические claims и deadline, ограниченная история
  попыток, сохранение finalized approvals и FROST-передача operator. Однократная
  привязка к новому пулу не расходует mint nonce. Полная waiting queue и измерение
  throughput/latency ещё не квалифицированы: [механизм и границы](BITCOIN_MINT_ADMISSION.md).
* `inspect-funding` / `FUNDING_STRIPPED_100K_V1`: извлечение canonical stripped
  transaction через уже используемый закреплённый `rust-bitcoin`, проверка
  txid, размеров и malformed encoding. Добавлен отдельный линейный EVM
  `BitcoinFundingParser`: полный проход, canonical CompactSize, все input
  outpoints, выбранный output и txid; искусственного предела 256 нет.
  Сверены общие Python/Rust/EVM vectors, включая 2438 inputs, 11105 outputs и
  100000 bytes. Парсер подключён к кандидатному адресному mint; legacy SMP1
  сохраняет прежний parser и предел 32 outputs: [профиль и границы](BITCOIN_FUNDING_PROFILE.md).
* Native `ConfederationGasTreasury` и стабильный `SPVServicePaymentRegistry`:
  immutable weights, pull claims, exact-payer permissions, state/generation guards,
  paid ledger между эпохами и явный durable CLI. Добавлены enrolled FROST intents
  конфигурации/ротации/pause с общей проверкой перед private nonce/share. Отдельный
  автоматический `claimFor` caller проверяет фактический баланс, ограничивает gas/fees,
  общий периодический расход и попытки по payee/epoch; сохраняет резерв и подписанные
  bytes при сбоях, учитывает revert/нулевую выплату и останавливается при reorg.
  Production activation и полная
  цепочка funding → real SPV publication ещё не готовы:
  [механизм, клиент и границы](SPV_GAS_TREASURY.md).
* `ops/zkspv`: устойчивый локальный планировщик очереди/heartbeat с SQLite,
  раздельными jobs/attempts, worker fences, Fresh, EVM/Bitcoin revalidation,
  bounded status retries, воспроизводимые календарные симуляции и Decimal TCO.
  Это ранний planner/simulator. Исполняемый `signer_agent.zk_worker` отдельно
  подключает native prover, state reader, HPKE transport и optional funding;
  [automation/archive](SPV_AUTOMATION_ARCHIVE.md). Общий E2E ещё отложен.
* Проверка пакета подготовки deployment: отсутствующие параметры, неподтверждённые
  gates, подмена файлов, mock/remote proving и ослабление давности блокируются.
  Даже корректные JSON-статусы не заменяют canonical runtime/proof verification.
* Отдельная native-квалификация derivation через закреплённые FROST/Bitcoin
  библиотеки: неизменяемый intent, фиксированный TapLeaf, проверка tweak/parity и
  восемь настоящих пороговых подписей synthetic messages. Общий модуль теперь
  доступен через версионированный IPC; добавлены подписи настоящих Bitcoin
  sighashes смешанной BASE/DERIVED транзакции и кандидат EVM-verifier.
  Кандидатные registry/mint уже связаны с общим учётом; live signer/client,
  watch/recovery и qualification остаются незавершёнными. Ready выключен.
  [Код, границы и недостающая интеграция](DEPOSIT_DERIVATION_INPUTS.md).

## Квалификация исходного компонента

Основной snapshot: Citrea `2ed54463bb7c4c3f1ff29dcd66d4b28c26fb63c8`.
Сохранены исходники, fixtures, лицензии, аудитные PDF и diffs точных ревизий.
Хеши PDF проверены. Native `bitcoin-da` собран без изменений с его Cargo.lock
и Rust 1.88.0. Первый запуск native tests остановился из-за ENOSPC и не был
засчитан. Повторный прогон 28 октября выполнил все 6 original `verifier::tests`
офлайн на неизменённом полном checkout; все 887 файлов сверены с архивом до и
после запуска. Это baseline header suite, не все DA/service tests и не доказательство
совместимости исходного кода с полным ТЗ. Диагностические тесты функций, извлечённых
дословно, отдельно воспроизвели ошибки target=1, компактного кодирования и
signed retarget. Это evidence несовместимости, а не PASS Bitcoin-проверки.

Для этих трёх случаев добавлены отдельные минимальные
[reference patches](../rust/zkspv/patches/README.md), с проверкой исходного и
каждого промежуточного SHA-256. Vendor не изменён. Исправленные функции проверены
в debug/release на 832 target и 80 mainnet-retarget vectors; probe использует
доступный Rust 1.89.0 и не заменяет полный upstream build/tests на Rust 1.88.0.
Независимая квалификация патчей и полного verifier остаётся открытой.

Следующий отдельный [набор проверки переходов](../rust/zkspv/header-qualification/README.md)
добавляет отказ при переполнении cumulative chainwork вместо насыщения. Полные
извлечённые переходы до/после патчей сравниваются на 6111 исторических заголовках
Mainnet/Testnet4, включая шесть восстановлений на границах эпох. Используются
Rust 1.88.0 и точные версии зависимостей из upstream lock. Это тестовый harness
с отдельными adapters. Пятый патч проверяет знак, ноль, переполнение compact target
и лимит сети до вычисления work, включая сохранённый target Testnet4. Выполненная
арифметика закреплённой версии Bitcoin Core дала 45 064 эталонных результата
для 11 266 входов при четырёх лимитах. Они сверены с Rust: 13 тестов в каждом
из debug/release и 10 тестов в полном native-crate с пятью патчами прошли.
Ожидание target=1 приведено к Core отдельным тестовым diff; прежний результат
четырёх патчей сохранён в `evidence/chainwork-v1/`.
Ни один из этих результатов не является real ZK proof.

Добавлен отдельный [компонент времени пакета](../rust/zkspv/time-policy/README.md)
по п.5.5: Rust накапливает максимум timestamps всех успешно проверенных новых
заголовков, Solidity сравнивает его с `block.timestamp + 2 hours`. Время последнего
заголовка не подменяет максимум. Есть общий ABI, проверки границ, ошибок и слияния
частей пакета; интеграция с полным native verifier проверяется на 4074 mainnet
заголовках. Компоненты ещё не подключены к production state/adapter. Максимум
нужно включить в аутентифицированные public outputs настоящего guest/proof;
часы prover и неподписанные соседние поля не подходят.

Добавлен [fork/history-компонент](../rust/zkspv/fork-policy/README.md): сохранение
контекста боковых веток, выбор по строго большей chainwork, LCA, reorg 100/101,
постоянный halt с сохранением безопасного префикса и атомарный откат ошибок.
Каждая ветка получает собственный persistent root канонической истории; есть
Rust/Solidity membership с привязкой высоты и полного заголовка. Полный replay
восстанавливает контексты и требует совпадения с аутентифицированным state commitment.
Это новый критический прикладной код, проверенный локально; компактный witness для
возобновления proving, доступность истории и привязка всего перехода к real proof
остаются открытыми.

Добавлена [checkpoint factory](../rust/zkspv/checkpoint-policy/README.md) по п.5.2:
проверка 80-byte mainnet-заголовка на границе эпохи, target/PoW, десяти связанных
предков и минимальной chainwork. Начальный difficulty/MTP-контекст выводится из
заголовков; все его поля проверяются при привязке к native verifier. Work включает
checkpoint ровно один раз. Предки используются только для MTP и не дают SPV
подтверждений. Начальная cumulative work до checkpoint остаётся доверенной.
Проверены 4032 последующих mainnet-заголовка, два retarget, все поля контекста и
полный replay; три общих Rust/Solidity-вектора сопоставлены с существующим relay.
Итоги: 28 Rust, 24 native (включая 19 прежних) и 8 Solidity-тестов. Компонент ещё
не подключён к production guest/state; выбор и аутентификация production checkpoint
и независимый review остаются обязательными.

Добавлен [единый переход и journal V1](../rust/zkspv/transition-policy/README.md):
checkpoint, восстановление принятого состояния, fork/history и временная сводка
выполняются вместе. Фиксированный ABI из 1024 bytes связывает прежнее/новое
состояние, EVM chain/address, checkpoint, весь поданный пакет и точную позицию
halt. Временной максимум включает новые боковые ветки и halt candidate, но не
повторы и восстановленную историю; timestamp канонической вершины хранится отдельно.
Solidity-компонент проверяет формат, deployment, текущее состояние и EVM-время.
Прошли 28 Rust, 28 native (24 прежних + 4 новых) и 8 Solidity-тестов. Это формат
и локальная интеграция перехода; аутентификация этих bytes настоящим proof с
закреплённым image ID и запись production state ещё не реализованы. Полный replay
не заменяет ограниченный по ресурсам witness для возобновления proving.

Добавлен отдельный [resume-компонент V2](../rust/zkspv/resume-policy/README.md).
Полный архив больше не является входом проверяющего переход: контексты и наличие/
отсутствие заголовков подтверждаются путями 256-bit Merkle index, а точный LCA —
членством общего заголовка и расхождением следующих заголовков обеих веток.
Сохранены правила 100/101, безопасный префикс halt и порядок приёма. Ввод ограничен
64 элементами, 5120 bytes заголовков и 2 275 554 bytes witness. На двух исторических
retarget один append требует 31 317 bytes witness и одного вызова Bitcoin verifier;
предыдущие заголовки внутри resume повторно не проверяются. Полный архив и его
построение остаются необходимы на стороне подготовки данных и растут с историей.
Это новый критический код: 40 Rust и 32 native-теста, отдельный статус review.
V2 commitment намеренно несовместим с V1; существующий journal/decoder V1 сохранён.
Добавлен [journal/input-компонент V2](../rust/zkspv/resume-transition/README.md):
1216-байтовый journal связывает старые/новые корни индекса, журнал порядка и число
известных заголовков. Solidity самостоятельно пересчитывает оба commitment,
проверяет EVM deployment, полное старое состояние, время, прогресс и halt.
Ограниченный runtime-neutral input codec использует максимум 2 281 208 байт;
append на границе retarget — 31 805 байт и один вызов настоящего verifier.
Этот компонент сам не создаёт proof. Настоящий компилируемый guest добавлен
отдельно ниже; production activation и durable archive service ещё отсутствуют.

Добавлен отдельный [state/adapter V2](../rust/zkspv/state-adapter/README.md) в
изолированном Foundry-проекте. Конструктор выводит исходные V2 commitments из
проверенного checkpoint и создаёт закреплённый настоящий Groth16 verifier и
собственный adapter. Неизменяемый профиль явно ограничен `PROOF_ONLY_V1`;
production-профиль этим не выбран. Запись состояния требует проверки точного
journal с закреплённым image ID; replay отклоняется, halt постоянный.
Fresh проверяет давность последнего полезного принятия и timestamp канонической
вершины. Checkpoint сам не открывает денежные методы. Permissionless materialize
связывает сырой заголовок с текущим canonical root; смена root аннулирует кэш без
обхода истории. Сохраняется фактическая ABI `IPaymentSPV`, действующие consumers
не переключались. После любого изменения canonical root, включая append,
используемый блок нужно повторно materialize; этот расход ещё входит в будущую TCO.
Положительные state-machine тесты используют обозначенный exact-call mock на
proof-границе. Обычный bytecode отдельно развёрнут в изолированном Anvil для трёх
реальных checkpoint: около 8,19 млн gas на создание state/verifier/adapter,
реальные verifier отклонили три фиктивных seal. Это не positive proof acceptance.
Экспериментальный guest/image ID и реальные STARK/Groth16 proof добавлены отдельными шагами ниже.
FROST-профиль, production bindings, полный денежный E2E и независимый review
ещё не закрыты.

Нужны независимая квалификация corrections и fork/history, production checkpoint
и криптографическая привязка полного перехода, включая временную сводку.
Локальный путь STARK → succinct → Groth16 → EVM verifier выполнен на Apple Silicon
через закреплённый нативный ARM64 Docker-образ. Старое предупреждение документации
об обязательном x86 опровергнуто этим конкретным запуском; результаты и границы
приведены ниже. Выбор production image ID/verifier bindings и принятие перехода
реальным state-контрактом ещё не квалифицированы.

Подробности: [selection report](../ops/artifacts/zkspv/selection-report.md),
[candidate manifest](../ops/artifacts/zkspv/candidate-manifest.json),
[upstream lock](../ops/artifacts/zkspv/upstream-lock.json),
[licenses](../ops/artifacts/zkspv/license-manifest.json),
[local proving report](../ops/artifacts/zkspv/local-proving-report.json),
[source qualification tools](../rust/zkspv/README.md).
Некоторые обязательные зависимости имеют GPL-условия; лицензия всей сборки
не объявляется автоматически MIT/Apache.

## Незавершённый обязательный scope

Обновление 2026-10-30: mainnet ZK-state/adapter подключены как явный вариант
production bootstrap/consumer через Bitcoin policy V2. V1 mainnet/testnet4 и
regtest сохранены. Конфигурация и локальная граница проверки описаны в
[ZK_SPV_BOOTSTRAP.md](ZK_SPV_BOOTSTRAP.md); это не production activation.

Production-квалификация guest/verifier, общего ZK-state/adapter и их
эксплуатационного контура остаётся открытой. Зашифрованный SPV request/ACK/status,
native treasury/governance/caller и admission/quote/payment код уже связаны с
worker; общий live proving/payment/consumer E2E ещё отложен. Relay certificate
profile не реализован. Production activation этим кодом не разрешается.
Код-кандидат адресных derivation, registry/mint и incident/generation реализован,
но не квалифицирован как полный end-to-end. Mint-only клиент и admission уже
проверяют поколения, Core/proof и точный receipt; [описание и границы](ADDRESS_DEPOSIT_REGISTRY.md#mint-client-and-admission).
Явный [Bitcoin signing profile](BITCOIN_ADDRESS_SIGNING.md) теперь связывает
V3 per-input spend с поколениями и общим nonce/spent journal.
[Адресный request/watch компонент](ADDRESS_REQUESTS_WATCH.md) реализует отдельный
шифрованный transport, durable ACK и bounded rescan/recovery из публичных bindings.
Клиент [FROST-регистрации](ADDRESS_REGISTRATION.md) связывает V2 requester
commitment с публичным typed intent, проверяет поколения перед private work и
сохраняет bounded историю явных retries и точного результата.
Адресный [SPV worker](ADDRESS_SPV_WORKER.md) подключён к зарегистрированным intent,
watch/consumer hooks и автоматическим admission/quote/payment с явными V5/V3 pins.
Остаются его operational qualification,
квалификация общего signing-контура, BTC fee reserve и полный reconciliation. Не реализованы три fee-субсидии BTC/MP/S, фактический
prevout/credit ledger, immutable consent receipts, refunds и bounded settlement
нового денежного пути. Их отсутствие — незакрытые требования, не N/A.

Исправление callback admission закрывает конкретный baseline defect, но не
весь раздел 22. Локальный receipt benchmark EVM parser не доказывает gas полного
funding/mint пути на ещё не заданной целевой сети.
Локальный scheduler не доказывает полноту неопубликованной Bitcoin-истории
§15.11; heartbeat не гарантирует обнаружение скрытого промежуточного deep reorg.

Целевая EVM, checkpoint, production Fresh/fee/latency profiles, controlled
prover, лицензирование, обязательства живого deployment и независимый review
не заданы/не подтверждены. Экономия и окупаемость не заявляются.
Итоговые gates: [decision.json](../ops/artifacts/zkspv/decision.json).

Отдельный regtest monetary E2E закрыт в описанной ниже тестовой границе. Это
проверяет совместимость обычного adapter с deposit registry и mint consumer,
но не снимает требования production bootstrap/mainnet, адресного режима и subsidy.

## Воспроизведение

Из корня репозитория, Python 3.11+, зависимости из существующего lock:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r signer-agent/requirements.lock
PYTHONPATH=signer-agent .venv/bin/python -m pytest signer-agent/tests tests/zkspv -q
cargo test --locked --manifest-path frost-crypto/Cargo.toml
forge build --root web3 --sizes
PYTHONPATH=signer-agent .venv/bin/python -c 'from ops.local_acceptance import run_solidity_suites; run_solidity_suites("ops/artifacts/zkspv/solidity-suites.json")'
```

Private Solidity runner изолирует environment-sensitive tests и не подменяет
production artifacts dynamic test linking. После изменений восстановить
обычный `forge build`, затем ABI и size checks. Полный существующий локальный
end-to-end runner — `python ops/local_acceptance.py`; конкретные выполненные
прогоны перечислены в [test-report.md](../ops/artifacts/zkspv/test-report.md).
Архивные отчёты 26 октября не являются результатами этого snapshot.

Упаковка квалификационного пакета: `rust/zkspv/tools/verify_upstream.py` сверяет
retained files. Networked download/build не считается offline real proving.

Связанные инструкции: [эксплуатация](ZK_SPV_OPERATIONS.md),
[миграция](ZK_SPV_MIGRATION.md), [обычный mint/treasury](BITCOIN_MINT_AND_TREASURY.md),
[действующий keeper](RELAY_KEEPER_AND_GAS.md).
## Собранный mainnet guest

`rust/zkspv/guest-integration` добавляет настоящий RISC Zero guest и статический
адаптер публичного mainnet API. Подключён полный закреплённый upstream crate с
теми же пятью исправлениями verifier. Размер входа ограничен до выделения памяти;
guest публикует точный журнал V2 длиной 1216 байт. Native-тесты проверяют 4032
mainnet-заголовка и два retarget. Отчёт связывает исходники, зависимости, ELF,
программу со штатным kernel и независимо вычисленный image ID.

Изолированные попытки построения composite STARK имеют отдельный proof-report.
Только `LOCAL_STARK_PASS` с совпадающими хешами подтверждает проверенный receipt;
сборка или одно исполнение zkVM этого не доказывают. Groth16 и положительная
проверка в EVM вынесены в отдельный этап ниже. Выбор production image/profile/checkpoint, подключение денежных
потребителей и независимое ревью остаются открытыми.

Повторная попытка 29 октября: удалены 11 временных кэшей сборки, освобождено
3,78 ГиБ. Проверки места и запрета сети прошли; локальный STARK prover работал
1800 секунд без dev-mode и был остановлен заданным тайм-аутом. Receipt не получен,
статус — `INCOMPLETE`. Это уже не блокировка запуска из-за диска, но и не
доказательство невозможности получить proof. Подробности и хеши артефактов:
[отчёт попытки](../ops/artifacts/zkspv/guest-proof-retry-report.json).

Следующий запуск с лимитом 7200 секунд завершился успешно: настоящий composite
STARK получен за 3408,052 секунды и отдельно проверен за 27,336 секунды.
Статус — `LOCAL_STARK_PASS`. Receipt размером 6 191 460 байт подтверждает один
mainnet append на высоте 32257; image ID и журнал из 1216 байт совпали с
квалифицированной сборкой и native-результатом. Неверный image ID и изменённый
журнал отвергнуты. Сеть была запрещена ОС, dev-mode отключён; все 233 хеша
исходников, включая launcher, совпали. Предыдущая неудачная попытка сохранена
в `evidence/local-proof-timeout-1800`, успешная — в `evidence/local-proof`.
[Отчёт успешного запуска](../ops/artifacts/zkspv/guest-proof-extended-report.json).
Этот STARK-этап сам по себе не закрывает полный набор guest-сценариев и production-приёмку.

## Настоящий Groth16 и локальная EVM

`rust/zkspv/groth16-integration` продолжает от того же receipt. Succinct-сжатие
заняло 630,515 секунды, Groth16-обёртка — 107,193 секунды. Итоговый receipt —
2929 байт, EVM seal — 260 байт с selector `73c457ba`. Внешняя сеть запрещена;
SDK использует только localhost RPC, Docker-контейнер — `--network none`.
Image/journal и подмены отдельно проверены SDK с выключенным dev-mode.

В одноразовой Anvil закреплённый настоящий verifier принял proof: 250 656 gas.
Изменённые image ID, journal digest и seal отклонены. Статус —
`LOCAL_GROTH16_EVM_PASS`; публичных транзакций нет. Журнал связывает тестовый
state address `0x1111111111111111111111111111111111111111`; принятие
`submitTransition` проверено следующим отдельным запуском с новым proof.
Production consumers, FROST, полный набор guest-сценариев и независимое ревью
остаются открытыми. [Подробности и воспроизведение](../rust/zkspv/groth16-integration/README.md).

## Настоящий proof и обновление state

[State proof integration](../rust/zkspv/state-proof-integration/README.md) получил
новый STARK → succinct → Groth16 для фактически развёрнутого `ZkBitcoinStateV2`.
Вход меняет только адрес принимающего контракта; guest и Bitcoin witness прежние.
Все 251 хеш исходников и 49 артефактов совпали. `submitTransition` принят за
437 624 gas: высота 32256 → 32257, версия 0 → 1. Второй state, replay и изменённый
журнал отклонены без изменения состояния. Настоящий заголовок материализован за
457 517 gas; Fresh и отказ адаптера после истечения state age проверены.

Статус — `LOCAL_STATE_PROOF_PASS`, 18 focused regression checks прошли. Это один
append в одноразовой Anvil с историческими часами, без положительного теста
Bitcoin transaction inclusion, денежного consumer E2E или production-активации.
Последующее покрытие retarget и TEST_ONLY fork/halt описано ниже. Production
профиль/checkpoint, FROST и независимое ревью остаются открытыми.
Docker и все 9 прежних контейнеров восстановлены.

## Proof-сценарии retarget и TEST_ONLY fork/halt — 2026-10-30

[Отдельный набор сценариев](../rust/zkspv/proof-scenarios/README.md) получил
настоящие STARK → succinct → Groth16 для всех трёх входов. SDK независимо
проверил каждый этап; обычный закреплённый EVM verifier принял каждый proof
за 250 644 gas и отклонил подмены image ID, journal и seal.

Retarget использует неизменённый mainnet guest: блок 34271 рассчитывает новую
цель, блок 34272 проверяется уже с ней. Это условный переход от состояния,
восстановленного native replay, без recursive proof предыдущей истории и без
приёмки retarget фактическим production state. Второй retarget 36288 остаётся
native debug/release evidence.

Fork/halt используют отдельный тестовый guest с image ID
`91cbdff8eaa0f6e2968e8cdce61d1be57a2aadd75c1f34e0f5f0a518de16a948`,
доменом `SATOSHIFI_TEST_POLICY_V1` и синтетической работой, без проверки Bitcoin
PoW/bits/MTP. Mainnet guest и production decoder не принимают эти доказательства
и журналы. TestPolicyReceiver принял reorg 100 за 433 846 gas и halt на глубине
101 за 460 050 gas; сохранён безопасный префикс, malformed suffix проигнорирован,
повторное продолжение после halt запрещено в guest. Replay и изменённый journal
отклонены в EVM без дальнейшего изменения принятого состояния.

Статус набора — `LOCAL_SCENARIO_PROOFS_PASS`; 25 focused regression checks прошли.
Это закрывает выбранную тестовую границу fork/halt, но не квалифицирует настоящие
mainnet-ветки с reorg 100/101. Production-активации и публичных транзакций нет.
Наш Anvil и prover-контейнеры завершены, временный Docker-образ удалён,
все девять прежних сервисов Docker работают.


## Сквозной regtest ZK-депозит — 2026-10-30

По согласованной границе добавлен отдельный TEST_ONLY regtest guest и
`TestRegtestDepositState`. Mainnet guest и `ZkBitcoinStateV2` не изменены.
Настоящие Bitcoin Core транзакции направлены на P2TR-кошелёк реального тестового
DKG и содержат SMP1-привязку к получателю, pool, сети и распределению.
После шести подтверждений настоящий STARK → succinct → Groth16 принят локальной
EVM. Обычные `ZkSpvAdapter`, `BitcoinDepositRegistry`, `MiningPoolV1` и
`PoolMpToken` выпустили 10^16 MP base units под 1 000 000 regtest-сатоши.
Регистрация и mint разрешены настоящими 2-of-3 FROST-сессиями тестовой федерации.

Проверены баланс, total supply, backing, consumed outpoint, allocation и nonce.
Отклонены mint до proof/материализации заголовка, неверный Merkle-путь/index,
подмена получателя, replay и повторное потребление даже с новой действительной
FROST-авторизацией. Второй депозит после Fresh expiry остался невыданным и
непотреблённым. Bitcoin Core независимо проверил оба inclusion proof через
`gettxoutproof`/`verifytxoutproof`.

STARK: 1720.629 с, succinct: 275.472 с, Groth16: 108.567 с. Приём состояния:
377080 gas, материализация: 457447 gas, mint: 353349 gas. Native debug/release:
по 3 теста с совпадением journal/witness; общая focused regression: 30 PASS.
Старый receipt bridge требовал 1216-byte journal; его не меняли. Отдельный
512-byte regtest bridge независимо перепроверил исходный receipt, сохранив
неуспешную попытку старого инспектора; STARK повторно не вычислялся.

Это тест линейного regtest-перехода от явно заданного checkpoint и фактического
consumer mint. Он не квалифицирует mainnet consumer/bootstrap, production
transport, address mode, subsidy или внешний review. Временные Core/Anvil
остановлены, prover-контейнер и скачанный для него образ удалены; исходные
девять Docker-сервисов продолжили работу.

Подробности и воспроизведение: [deposit-integration/README.md](../rust/zkspv/deposit-integration/README.md);
[source/artifact-bound report](../rust/zkspv/deposit-integration/report.json).

## Consumer-driven local prover

The optional [local worker](ZK_SPV_WORKER.md) connects participant requests to
native witness construction, pinned self-hosted proving, independent receipt
verification and durable state/header transactions. The local integration report
uses a genuine cached mainnet receipt with simulated Core/inbox observation;
fresh proving from a live consumer and encrypted cross-host transport remain
unqualified. Legacy and manual ZK configurations remain available.

## Encrypted transport and grouping code

Added the separate `SPVMessageBoard` and V3 worker/client adapters for encrypted
request/ACK/status, on-chain key epochs and operational delegates. Ready needs
share execution groups without replacing paid jobs or extending deadlines.
The [runbook](SPV_ENCRYPTED_TRANSPORT.md) documents public routing metadata, limits,
recovery and the exact code/test boundary. Focused HPKE/queue checks and compilation
are separate from the still-pending combined proving/payment/consumer E2E.

## Automatic service funding and durable archive code

Added opt-in issuer/payer automation and canonical encrypted quotes, plus a
permanent segmented archive with native replay restoration and resident preparation.
The [runbook](SPV_AUTOMATION_ARCHIVE.md) describes limits, V3-to-V4 continuity and
separation from money/nonce journals. Focused native and mocked EVM checks do not
replace the deferred combined proving/payment/consumer E2E.
