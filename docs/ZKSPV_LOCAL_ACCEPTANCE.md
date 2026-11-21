# Локальные проверки перед отложенным общим контуром

Срез: 2 ноября 2026. Пользователь отложил настоящий Core/FROST/Groth16/EVM-контур
из-за свободного места на устройстве. Новые Core/Docker/prover процессы для него
не запускались. Production qualification и независимая приёмка остаются открытыми.

## Исполняемые привязки обязательных строк

[Явные привязки](../ops/zkspv-acceptance-bindings.json) сопоставляют все 195 строк
§11.9–11.15 конкретным исполняемым локальным модулям. Это 55 модулей с исходными
fixtures, действиями и assertions. Подготовленный plan раскрывает имена тестов,
строки исходников, аргументы fixtures, source hashes и границы MODEL / native
crypto / SDK с chain fixtures / локальная EVM с явно тестовыми authority/SPV.

Один модуль может проверять несколько частей требования. Его успешный запуск
подтверждает именно эти локальные проверки; оставшиеся части строки, настоящий
денежный контур и независимая квалификация сохраняют собственный статус.
Число 195 не используется как процент полного покрытия ТЗ.

```sh
PYTHONPATH=signer-agent python -m ops.zkspv.acceptance_cases prepare --output NEW_PLAN.json
PYTHONPATH=signer-agent python -m ops.zkspv.acceptance_cases collect \
  --plan NEW_PLAN.json --python-xml LOCAL_PYTEST.xml \
  --forge-json ORDINARY_FORGE.json --forge-json STATE_FORGE.json \
  --output NEW_LOCAL_CASE_RESULTS.json
```

Сборщик требует выполнения каждой выбранной тестовой функции и всех её
записанных параметров. Failure, skipped, отсутствующий тест, изменённый source
или пустой JSON результата не дают локальный PASS. Контур помечен
`DEFERRED_BY_USER_FOR_DISK_CAPACITY`; `specification_row_fully_verified=false`.
Приватные deployment/identity/quote/plan/consent/need файлы настоящего контура
нужно получить при его подготовке и явно закрепить штатным suite bind.

## Воспроизведение локальных проверок

Команды выполняются из корня репозитория в Python 3.11+ с зависимостями
`signer-agent`, pytest и extra `evm-test`. Требуются проверенный FROST helper,
восстановленный worker host и pinned Forge/solc. Тесты API используют только
loopback HTTP; среда должна разрешать bind `127.0.0.1`. `tests/e2e` в этот набор
не входит. Контур Core/Docker/prover этими командами не поднимается.

До запуска проверьте [tool pins](../ops/artifacts/zkspv/acceptance-20261102/tools.json),
[исходную команду Python](../ops/artifacts/zkspv/acceptance-20261102/python-command.json)
и [native manifest](../ops/artifacts/zkspv/acceptance-20261102/host-build.json).
`ops/.local-tools` игнорируется Git: наличие этих бинарников в новом checkout
не предполагается. Для другого хоста инструменты собираются и проверяются отдельно;
чужой build manifest не устанавливает доверие к новому бинарнику.

Используйте новые каталоги вывода. Сначала фиксируется plan, затем исполняются
проверки и только после этого выполняется collect. Исходники и ABI между этими
стадиями не изменяются. Пример для сохранённых macOS-инструментов:

```sh
set -eu
LOCAL_CHECK_DIR=/absolute/path/to/new-local-checks
LOCAL_CASE_DIR=/absolute/path/to/new-local-results
FORGE="$PWD/ops/.local-tools/acceptance-20261102/forge"
SOLC="$PWD/ops/.local-tools/acceptance-20261102/solc"
mkdir -p "$LOCAL_CHECK_DIR" "$LOCAL_CASE_DIR"

export PYTHONPATH=signer-agent
export PYTHONDONTWRITEBYTECODE=1
export SATOHSI_REQUIRE_NATIVE=1
export SATOHSI_REQUIRE_EVM=1

python -m ops.zkspv.acceptance_cases prepare --output "$LOCAL_CHECK_DIR/case-plan.json"

"$FORGE" test --root web3 --use "$SOLC" --offline --threads 1 --json \
  > "$LOCAL_CHECK_DIR/ordinary-forge.json" 2> "$LOCAL_CHECK_DIR/ordinary-forge.log"
"$FORGE" test --root rust/zkspv/state-adapter --use "$SOLC" --offline --threads 1 --json \
  > "$LOCAL_CHECK_DIR/state-forge.json" 2> "$LOCAL_CHECK_DIR/state-forge.log"

python -m pytest --rootdir=. signer-agent/tests tests/zkspv \
  -q -p no:cacheprovider --junitxml="$LOCAL_CHECK_DIR/python-tests.xml" \
  > "$LOCAL_CHECK_DIR/python-tests.log" 2>&1

python -m ops.zkspv.acceptance_cases collect \
  --plan "$LOCAL_CHECK_DIR/case-plan.json" \
  --python-xml "$LOCAL_CHECK_DIR/python-tests.xml" \
  --forge-json "$LOCAL_CHECK_DIR/ordinary-forge.json" \
  --forge-json "$LOCAL_CHECK_DIR/state-forge.json" \
  --output "$LOCAL_CASE_DIR/local-case-results.json"
```

Остановитесь при ненулевом exit code. Source drift, missing/skipped/failed checks
не устраняются заменой source hashes или старого XML. Проверки ABI и fee pins:
`python ops/generate_abis.py --check --zk` и
`python ops/generate_fee_runtime_hashes.py --check`. Их расхождение требует
проверенной согласованной сборки; hash gate не отключается.

Сохранённый summary получен отдельным `ops.zkspv.acceptance_evidence`: помимо
сырых результатов ему требуется `python-command.json` с точной командой, env,
срезом исходников, временем и exit code. Простое копирование нового XML в старый
каталог не является повторением acceptance. Исторические результаты не переписываются.

## Восстановленный native host

Default для локальных worker-тестов теперь
`ops/.local-tools/zk-worker/satoshifi-zk-worker-host`; переменная
`SATOHSI_ZK_WORKER_HOST` позволяет указать другой проверенный путь.
`SATOHSI_REQUIRE_NATIVE=1` превращает отсутствие host в ошибку вместо skip.
Бинарник сохраняется отдельно от удаляемого build-кэша.

Восстановление использовало тот же compiler SHA-256, прежние Rust sources и
проверенный полный upstream archive с закреплёнными corrections. Это native-only
сборка: старый Solidity parent report не переименовывался в новую квалификацию.
Build manifest и log сохранены отдельно; dependencies и target удалены после
копирования бинарника. Три прежние исключённые проверки входят в native-прогон:
неоплаченный consumer против heartbeat, достаточный Fresh без повторной оплаты,
объединение двух потребностей в один подготовленный proof.

## Исправления, найденные полным Solidity-прогоном

- Family fence выполняется вместе с исходными guards; прежние local generation/
  closed отказы сохраняют порядок и сообщения. Global root checks сохранены.
- Stateless fee verifier pins получаются из обычного Foundry profile. Дополнительный
  solc-js builder теперь использует те же нормализованные remappings с trailing
  slash. Изменение metadata является изменением runtime hash и не игнорируется.
- Initcode boundary test проверяет реальный CREATE nonce deployer после создания
  частей bytecode, затем его увеличение на каждом успешном pool deployment.

Полный Python-набор дал **3269 passed, 0 failures/errors/skipped** без исключений.
Native subset из 38 проверок входит в этот набор и не прибавляется к нему второй раз.
Полный ordinary Solidity-прогон дал **834 Success**, state/adapter — **25 Success**.
Это контрактные тесты с обозначенными fixtures; новый настоящий Groth16 при их
исполнении не генерируется. Bounds проверены для 129 production contracts;
минимальный runtime margin — 66 bytes у `MultiPoolDeployer`.

## Пакет для квалификации

[Сборщик qualification bundle](../ops/zkspv/qualification_bundle.py) связывает
current sources, executable local case results, native build и runtime/initcode/
ABI metadata кандидатов. Он проверяет, что actual source gates остаются
`QUALIFIED=false` и `FEE_QUALIFIED=false`, и оставляет independent review пустым.
Он не присваивает собственным проверкам статус независимого review.

Отдельно перепроверены четыре сохранённых Groth16 receipt: state-bound mainnet,
mainnet retarget и два явно TEST_ONLY fork/halt. Проверка SDK выполнялась с
запрещённой сетью и без нового proving. Сохранены 42 точных исторических файла,
восстановленных по прежним SHA-256 из Git/пакета. Старый исполняемый FROST helper
с SHA-256 `d93516a7…` недоступен; полная воспроизводимость того исторического
запуска явно остаётся неполной. Современный helper не подставляется под старый hash.

До активации нужны настоящий итоговый контур, квалификация Bitcoin corrections,
адресного и fee-purpose профилей, canonical family root/SDK и независимый review.
Выбранная policy §15.11 сохранена: mainnet guest неизменён, известный глубокий
reorg блокирует локальный worker; полнота неопубликованной истории и общий
on-chain halt не гарантируются.

## Результаты и остаток

| Артефакт | Что проверяет |
|---|---|
| [Итоговый local acceptance](../ops/artifacts/zkspv/acceptance-20261102/local-acceptance-report.json) | 3269 / 834 / 25, команды, source/artifact hashes, нулевые failures/errors/skips |
| [Local case results](../ops/artifacts/zkspv/acceptance-20261102/local-case-results.json) | 195 привязок; для каждой указаны исполняемые модули и fixture/model boundaries |
| [Qualification review](../ops/artifacts/zkspv/acceptance-20261102/qualification-review.json) | Current source/build кандидатов, четыре retained receipt rechecks и ещё не пройденные gates |
| [Tool pins](../ops/artifacts/zkspv/acceptance-20261102/tools.json) | Версии, официальные download checksums и восстановленный native host |
| [Исторические source bytes](../ops/artifacts/zkspv/historical-sources/manifest.json) | 42 точных файла; отсутствующий старый FROST executable отмечен отдельно |

Остаются приватные входы и исполнение настоящего общего контура, независимая
квалификация/review и production параметры/измерения. Перенос живых обязательств
C4 нужен только при выбранной миграции и требует собственного inventory/протокола.
Локальные test programs не переводят эти пункты в PASS.
