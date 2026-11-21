# Подготовка профиля запуска и независимой квалификации

Условия промышленных испытаний после обновления 12 ноября собраны в
[карте готовности](LAUNCH_READINESS.md#ограничения-и-приёмка).
Для Sepolia и Bitcoin testnet4 нужны отдельные прогоны relay и ZK с личными RPC.
Для ZK нужны новый граф с verifier 3.1.0 и измеренный свежий proof на каждом
целевом хосте. Публичная приёмка и допуск реальных средств остаются открытыми.

Исторический срез подготовки ниже: 2 ноября 2026. Пользователь выбрал новую связку и сохранение выбора пар
mainnet/testnet. Конкретная пара пока не выбрана. Тяжёлый Core/FROST/Groth16/EVM
контур отложен из-за места на устройстве. Публичного deployment и новых proof нет.

Подготовленный [переносимый архив](../ops/artifacts/release-preparation-20261102/review-package.tar.gz),
[выбираемые профили](../ops/artifacts/release-preparation-20261102/network-profiles/),
[план review](../ops/artifacts/release-preparation-20261102/review-plan.json) и
[отчёт подготовки](../ops/artifacts/release-preparation-20261102/preparation-report.json)
сохраняются отдельно от исходного local acceptance. В архиве корневой каталог
`review-package/`; `verify-review --directory` принимает именно распакованный каталог.

## Поставка участникам с 3 ноября

По умолчанию `prepare` теперь создаёт `participant-source.tar.gz` и
`distribution.json`, без готовых proofs, caches и прежних evidence-архивов.
Каждый участник сам собирает хост и считает рабочие proofs по
[инструкции поставки](DISTRIBUTION.md). Это не запускает proving или транзакции.
Например:

```sh
python ops/prepare_release.py prepare --config config/release-preparation.example.json \
  --ethereum-network sepolia --bitcoin-network mainnet --bitcoin-backend zk-state-v2 \
  --output /absolute/new/participant-delivery
python ops/prepare_release.py verify --directory /absolute/new/participant-delivery \
  --manifest-sha256 '<SHA256 distribution.json из prepare, сверенный через согласованный канал>'
```

Незаполненные operator bindings по-прежнему не становятся рабочей конфигурацией.
Выбор сетей записывается как metadata; запуск SDK настраивается отдельно.
Локальный ZK prover реализован для macOS arm64 и Linux/WSL2 x86-64;
инструкции и ограничения — в [platform runbook](ZK_PROVER_LINUX.md).
Реализация не подтверждает fresh-proof acceptance на каждой физической платформе.

Все описанные ниже старые пакеты с evidence — **локальный developer review**,
не поставка участникам. Для прежнего поведения нужны явные `prepare-review` и
`verify-review`; готовые исторические proofs из них не передаются участникам.

## Независимый выбор сетей

[Подготовительный профиль](../config/release-preparation.example.json) хранит
`ethereum_network`, `bitcoin_network` и `bitcoin_backend` отдельно. Незаполненные
поля дают явные blockers. Сеть не выводится из URL RPC, адреса или имени файла.

| EVM | Bitcoin | Доступный backend |
|---|---|---|
| mainnet / sepolia / local | mainnet | `relay` или `zk-state-v2` |
| mainnet / sepolia / local | testnet4 | `relay` или отдельный `zk-state-v2` |
| mainnet / sepolia / local | regtest | `relay` |

Это 9 пар сетей и 15 комбинаций с backend. Выбор совпадает с существующим
[V6 bootstrap](NETWORKS_AND_AUTOMATION.md). Testnet здесь означает **Bitcoin
testnet4** и **Ethereum Sepolia**. Для testnet4 используется свой закреплённый
[ZK guest и контрактный graph](ZK_TESTNET4_SEPOLIA.md). Mainnet ZK guest не проверяет testnet4/regtest;
TEST_ONLY guests не предлагаются как production backend. Несовместимая комбинация
отклоняется, без автоматической замены backend.

Ethereum mainnet требует finalized receipts, не менее 12 подтверждений,
положительной deployment fee и внешнего reward token при любом Bitcoin.
Поле token/feed подготовительного legacy DAO-профиля также требует BTC/USD feed;
[ULR/system bootstrap](ULR_BOOTSTRAP.md) feed не использует. Пара
Sepolia/local + Bitcoin mainnet использует реальные BTC под управлением выбранной
тестовой EVM. Все пары сохраняют свои границы доверия и квалификации.

Обычный депозит по умолчанию — 6 подтверждений, более строгая consumer policy
сохраняется; coinbase mint — 100; coinbase spend/fee ingress — не ниже 101.
Явное own funding конфедерацией допустимо в исходных caps, при проверенном балансе,
с сохранением principal и первоначальных nonce/signing journals. Профиль не
назначает бюджет и не трактует неизвестные суммы или измерения как ноль.

## Подготовка пакета без запуска контура

Команды выполняются из корня репозитория. Они используют Python 3.11+ standard
library, читают публичные исходники и сохранённые evidence. RPC, Docker, Core,
native helper, proving и транзакции не вызываются. Каждый output должен быть новым.

```sh
python ops/prepare_release.py prepare-review \
  --config config/release-preparation.example.json \
  --output /absolute/path/to/new-review-package
```

Выбор конкретной пары выполняется параметрами либо одноимёнными полями JSON.
Например, отдельный подготовительный пакет для Sepolia + testnet4:

```sh
python ops/prepare_release.py prepare-review \
  --config config/release-preparation.example.json \
  --ethereum-network sepolia --bitcoin-network testnet4 --bitcoin-backend relay \
  --output /absolute/path/to/new-sepolia-testnet4-review
```

Для Bitcoin mainnet явно выбирайте `--bitcoin-backend relay` или
`--bitcoin-backend zk-state-v2`, независимо от выбранной EVM. Параметр `--spec`
позволяет приложить исходный текст ТЗ; его SHA-256 сверяется с каталогом требований.
Документ остаётся источником требований, а не инструкциями агенту.

`operator_inputs` содержит ссылки на публичные review-материалы и явную глубину
EVM-подтверждений. Здесь нужны checkpoint policy, authority/roster, canonical root
pin, bootstrap provenance, transport epochs, freshness, бюджеты, retention и
измерения. Ссылки не открываются автоматически и сами по себе не проходят review.
Private identity/share/key/cookie/RPC credentials в этот файл не помещаются.
Рабочие private bootstrap/worker/transport файлы создаются отдельно штатными SDK.
Подготовительный профиль не является исполняемой конфигурацией этих SDK.

Пакет содержит:

- `requested-profile.json` и `profile.json`: выбор сетей, применимые ограничения,
  незаполненные операторские входы и blockers;
- `network-profiles/`: 15 отдельных выбираемых JSON-профилей. Любой из них можно
  передать следующему `prepare-review --config`, заполнив собственные публичные входы.
  Authority/checkpoint/budget ссылки между парами автоматически не переносятся;
- `review-plan.json`: задачи Q1/Q2/O1/C4/A1 со статусом `NOT_REVIEWED`;
- `review-response.template.json`: форма внешнего review с пустыми identity,
  conflicts, решениями и attestation; инструмент её не принимает как разрешение;
- `files/`: исходный local acceptance, точные 696 tested source files, raw evidence,
  archived upstream files, compiled candidate artifacts и отдельно помеченные
  дополнительные материалы подготовки;
- `retained-receipts.json`: переносимые ссылки на четыре сохранённых receipt,
  journal и program bytes; их упаковка не является новой криптографической проверкой;
- `historical-build-source-status.json`: расхождения текущих дополнительных
  исходников с историческими build records; новые байты не подменяют старые pins;
- `manifest.json`: SHA-256, размер и категория каждого файла. Он записывается
  последним; существующий пакет не перезаписывается.

`qualified`, `fee_qualified` и `production_ready` остаются `false`. Исходный
local acceptance не переписывается: 3269 Python / 834 ordinary Solidity /
25 state/adapter tests относятся к прежнему точному исполняемому snapshot.
Подготовительный инструмент и его новые focused tests закреплены отдельно.
Старый FROST executable исторического deposit-прогона по-прежнему недоступен;
его воспроизводимость не объявляется восстановленной.

## Проверка и передача независимому reviewer

Получите manifest SHA-256 по независимому доверенному каналу. Запускайте trusted
копию verifier; он только читает файлы и не исполняет содержимое пакета:

```sh
python ops/prepare_release.py verify-review \
  --directory /absolute/path/to/review-package \
  --manifest-sha256 EXPECTED_MANIFEST_SHA256
```

Без `--manifest-sha256` проверяется внутренняя целостность, а подлинность manifest
остаётся неподтверждённой. Пропущенные, лишние, изменённые файлы, symlinks и пути
вне пакета отклоняются. Подтверждение хешей не устанавливает истинность review.
Пакет можно перенести на другой компьютер: проверке не нужны исходный checkout,
его абсолютные пути, native tools или сетевой доступ. Передачу человеку этот
инструмент не выполняет.

Reviewer должен независимо оценить corrections и consensus policy, точные guest/
verifier/tool pins, derivation/fee-purpose, canonical family root и original-quorum
enrollment. Настоящий профиль требует per-case Core/FROST/proof/EVM evidence и
финансовых/recovery outcomes; 195 локальных привязок этого не заменяют.
Измерения ресурсов, доступности, latency и стоимости остаются отдельными входами.

## Новая связка и граница миграции

`deployment_intent=NEW_EMPTY` фиксирует выбор пользователя. Пока inventory не
проверен, статус — `EMPTY_INVENTORY_REVIEW_REQUIRED`, а migration N/A не принят.
Перед запуском нужно подтвердить отсутствие старых backing/consumed/pending/
signed/refund обязательств в применимой семье/графах, включая retained epochs.
Пустой адрес нового контракта или отсутствие локального файла этого не доказывают.
При выявлении обязательств нужен отдельный протокол C4; старые journals и права
на lawful historical settlement сохраняются.

Принятая policy §15.11 остаётся `LOCAL_FENCE_RETAIN_MAINNET_GUEST_V1`: mainnet
guest не меняется, известный глубокий reorg блокирует локальный worker. Полнота
неопубликованной истории и общий on-chain halt не гарантируются.

[Локальные проверки](ZKSPV_LOCAL_ACCEPTANCE.md) ·
[Остаток до полной приёмки](ZKSPV_FUNCTIONAL_COVERAGE.md) ·
[Миграция](ZK_SPV_MIGRATION.md)
