# Проверка и объединение пользовательского архива — 18 октября 2026

Проверяется `satoshifi-updated (2).zip` по [исходному ТЗ из восьми разделов](UPGRADE_SPEC_2026_10_18.md).
Архив существенно реализует требуемые изменения, но его нельзя было безопасно
копировать поверх текущего репозитория. Он основан преимущественно на `12ebdec`
и не содержит последующего усиления проверки происхождения из `ec0ba82`.
Объединение выполнено с сохранением этих исправлений и прежних регрессий.

SHA-256 архива: `ca1f7f240b685b4651dc4f8d88d5b063e4c501287b95c69bcc74adac280e1f45`.
Исходный commit: `ec0ba8200d56dc4bdc3dd15def1815a4d6937ac7`.
[Журнал импорта](../ops/artifacts/upgrade-import-20261018.json) фиксирует исходные
файлы, исключения и стратегию объединения. Отсутствующие в архиве актуальные
файлы, CI и зависимости не удалялись. Готовые broadcast, отчёты о прохождении
тестов и ABI из архива не использовались как доказательство новой приёмки.
ABI пересозданы из объединённой сборки.

Это внутренний анализ кода и локальные воспроизводимые проверки. Публичного
развёртывания, внешнего аудита и запуска на трёх физических машинах здесь нет.

## Исправления при объединении

| Находка | Риск | Исправление и регрессия |
|---|---|---|
| Архив не содержит текущего canonical creation provenance | Копирование возвращало бы проверку только runtime; злонамеренный конструктор может вернуть тот же runtime с иным storage | Сохранены `contract_provenance.py`, проверки DKG board, Bitcoin-графа и DAO, старые негативные тесты. Обновлены аргументы нового escrow |
| Новый staged runner доверял runtime существующих компонентов | Поддельный конструктор мог пройти проверку runtime/getters | До Forge проверяются канонический CREATE, ABI конструктора, receipt, блок и поддерживаемый parent deployer. `creation_proof` сохраняется и повторно сверяется для `observed_existing` и новых адресов |
| Проверка ADMIN_ROLE только выбранного оператора | У исходного создателя чужого coordinator мог остаться ADMIN_ROLE | Исходный создатель определяется из creation proof. Его роль проверяется перед использованием; продолжить незавершённый base может надлежащий создатель, завершённый base обязан снять роль |
| Новые immutable-параметры не полностью связаны с приглашением | Минимумы вывода/treasury могли расходиться с тем, на что согласились участники | Bitcoin inspect показывает минимумы и обе глубины, включает их в fingerprint; V3 board discovery сравнивает их с приглашением. DAO discovery сравнивает treasury settlement |
| Приватные staged-каталоги не исключались из Git; существующие каталоги могли быть symlink/с широкими правами | Обычный `git add .` мог включить Foundry resume cache, а локальный вывод мог попасть в чужой каталог | Узкие исключения private/broadcast и реальный `git check-ignore`; проверка владельца, `O_NOFOLLOW`, режим 0700 на каждом приватном уровне и запрет shared-writable manifest parent. Public manifest остаётся доступным |
| Произвольный текст RPC-ошибок и HTTP-логов обходил редактирование вывода | Возможна публикация credential из RPC URL или варианта кодирования ключа | Только собственные фиксированные DeploymentError печатают диагностику; прочие — безопасный код типа. HTTP logging подавлен, Forge output редактирует URL fragments и варианты private key |
| Старые скрытые финансовые значения integration bootstrap | Приглашение не описывало всю выбранную финансовую политику | По решению пользователя значения стали обязательными в приглашении/CLI/плане, см. таблицу ниже |
| Кэш старой сборки после переноса DAO mocks | Два одноимённых артефакта, отказ клиента `ARTIFACT_NOT_UNIQUE` | Чистая пересборка; строгая проверка единственности сохранена. При обновлении старого checkout нужен `forge build --root web3 --force` |
| Общий процесс Forge с `vm.setEnv` | Два script-теста падали из-за общего окружения даже при `--threads 1` | Acceptance изолирует тесты с env-зависимостями по процессам, остальные — по suite. Список и имена результатов сверяются с полным discovery; failures/skips не игнорируются |

Приватные значения из старой Git-истории не копировались и не публиковались.
Ранее найденные ключи остаются скомпрометированными; статус и необходимые
действия владельца описаны в [предыдущем отчёте](../security_best_practices_report.md).

## Сверка ТЗ

| Раздел | Реализация и проверяемые свойства | Покрытие |
|---|---|---|
| 0. Границы | Solidity 0.8.30, Osaka, via-IR, optimizer runs 1 сохранены. `RegtestHeaderRelay.sol` и `SimpleSPVContract.sol` не изменены; общий compact helper исправлен отдельно. Прежние проверки безопасности не отключены | Чистая сборка, ABI drift, размер всех project artifacts, полный регрессионный прогон |
| 1. Mainnet relay | Checkpoint на границе 2016, 10 предыдущих headers только для MTP, явные chainwork/reorg depth; compact/PoW, branch retarget, MTP, future-time, most-work, очистка старого хвоста, необратимый halt глубокой реорганизации и отказ SPV | `TargetsHelper`, `MainnetHeaderRelay`, `MainnetRelayReview`, `MainnetRelayIntegration`, cold gas tests; независимый Bitcoin Core replay |
| 2. Вывод MP/S | Округление net до сатоши до хеша/escrow, остаток в fee, reject zero, нормальный/metadata/NFT пути. Явные положительные минимумы P2WPKH/P2TR, direct без округления; refund и burn/fee только на правильной стадии | `WithdrawalRounding`, `EscrowBoundary`, proof-backed Bitcoin E2E, полный bootstrap |
| 3. Состав | `f>=1`, `c>=ceil(f/2)`, `t>=ceil(0.7n)`, `t>f`, `t-f<=minCust<=c`, максимум DKG 16. Упорядоченный полный roster, завершённая EVM_AUTH, полный путь adoption одной федерации, сохранение founders, nonfirst deactivate. BTC/EVM roster и threshold совпадают | `ConfederationComposition`, `FullFederationAdoption`, `BitcoinRosterBinding`, `PoolDKGRemediation` и V1 paid E2E |
| 4. DAO без LP | Immutable treasury; четыре сочетания stake/LP; пустой LP не расходует reserve на базу, остаток LP в treasury, `fundDay` в том числе с нулём; сохранение excess reserve; обязательная цена, точные переводы/rollback | `DAONoLPSettlement` fuzz/matrix, текущие `CustodianDAO` регрессии и сквозные расчёты |
| 5. Deployment fee | Отдельная project DKG/auth/vault/router; `deployStakerBps=0`, вся deployment fee в project treasury без DEPLOY_FEE source/settlement. Две последовательные payer операции с текущими nonce, однократная настройка фабрик | `DeploymentFeeStages`, staged E2E с настоящим FROST; allowance/revert/retry проверки |
| 6. Этапы | base не требует финансовых значений, явные project wallets/owner/shares/labels; DKG binding затем renounce ADMIN. Отдельные factories/router/payers/fees, подтверждённый manifest и resume. Legacy initiate/finish только 31337/11155111 | `DeploymentStages`, `FactoryLegacyGuards`, staged/reuse E2E, Python preflight/provenance/privacy regressions |
| 7. Signing board | Immutable registry/wallet/result/maxPacketBytes, только участники; Signing/input/kind/size; одинаковый retry идемпотентен даже после смены batch state, конфликт отклонён | `BitcoinSigningBoard`; формат в [описании board](../web3/docs/bitcoin-signing-board.md). Agent transport в этом ТЗ исключён |
| 8. Test USD/feed | Единственные определения mocks, 6/8 decimals, только 31337/11155111. Явные rounds с opening/closing и интервалами <=24h, без будущего/нулевых цен, конфликт immutable history отклонён, повтор идемпотентен | `SeedDAOTestFeed`, `RecheckSeedBoundary`, старые TWAP/settlement tests. Без истории нет fallback/skip-day |

## Явная политика первой DKG и bootstrap

Все участники проверяют приглашение **до enrollment**. V3 без обязательных
полей отвергается, значения не восстанавливаются из прежних defaults.
Старые приглашения V1/V2 читаются для существующих церемоний, но новый bootstrap
создаётся по V3. Полный CLI приведён в [первом запуске](FIRST_DKG_ONCHAIN.md)
и [bootstrap](BOOTSTRAP.md).

| Поле | Единицы / ограничение |
|---|---|
| `treasury`, `dao_token_recipient` | Явные ненулевые EVM-адреса получателя дохода и начальной эмиссии DAO |
| `wrap_fee_bps`, `unwrap_fee_bps`, `wrap_staker_bps`, `deploy_staker_bps` | Целые bps, 0..10000; 10000 = 100% |
| `converter_numerator`, `converter_denominator` | Положительные uint256, отношение **минимальных единиц** output/input. Тестовый курс не является рыночной ценой |
| `converter_funding` | Положительное число минимальных единиц test USD (6 decimals) для финансирования тестового converter |
| `dao_use_reserve` | Явное boolean начальной политики; впоследствии меняется штатным governance |
| `integration_fee_exemption` | Явное `true` для integration-пути; не переключает фабрику позднее в платный режим |
| `bitcoin_confirmations` | 6..2^32−1, глубина Bitcoin для deposits/withdrawals; не EVM receipt confirmations |
| `min_p2wpkh_sats`, `min_p2tr_sats` | Положительные uint256 сатоши на заявку; пример ТЗ 294/330 передаётся явно |
| `min_custodians` | Положительный минимум, удовлетворяющий правилам состава выбранной конфедерации |

Для трёх друзей новая политика конфедерации требует threshold **3/3**.
Обмен enrollment/DKG идёт через EVM board: приватные DKG-пакеты зашифрованы,
дополнительный канал передачи файлов не нужен. Всем нужны свои EVM-ключи,
газ, доступ к RPC и защищённое локальное состояние. Первичная DKG требует
участия всех n; численный порог подписи не отменяет это требование.

## Размеры и Bitcoin fixtures

Все **95** concrete project contracts/libraries проходят EIP-170/EIP-3860
по [новому inventory](../ops/artifacts/upgrade-contract-sizes-20261018.json).
В статическом creation size ещё нет аргументов; отдельные тесты кодируют
максимальные динамические аргументы и действительно вызывают CREATE.

| Контракт | Runtime до, байт | Runtime после | Запас до 24576 |
|---|---:|---:|---:|
| MultiPoolDeployer | 24347 | 24437 | **139** |
| SimpleMultiPoolDeployer | 23692 | 23789 | 787 |
| FROSTCoordinator | 23433 | 23433 | 1143 |
| ConfederationFactory | 20866 | 21592 | 2984 |
| BitcoinWithdrawalEscrow | 15587 | 15972 | 8604 |
| CustodianDAODailySettlement | 8027 | 9111 | 15465 |
| MainnetHeaderRelay | — | 5390 | 19186 |
| BitcoinSigningBoard | — | 2523 | 22053 |

`PoolInitcodeLimits` проверяет реальные pool/token deployers, максимальный
16-членный roster, максимально помещающиеся pool IDs и совместный бюджет
name/symbol, права токенов и следующий nonce. Python проверяет полное initcode
до подписи. Неограниченные произвольные строки не обещаются поддерживаемыми.

Независимо повторён Bitcoin Core **30.3.0** replay **36 289 headers** и сверка
**4043 fixture records** с двумя retarget. [Машиночитаемый результат](../ops/artifacts/upgrade-mainnet-fixtures-20261018.json)
содержит hashes, версию binary, проверенные anchors и первичные ссылки Core.
Исходный fixture manifest Core 29 не переписан: дополнительная проверка имеет
отдельный entry point. Исторический checkpoint 32256 предназначен для теста,
а не для развёртывания с реальными средствами.

## Изменения прежних fixtures

- Escrow constructor получает два обязательных минимума; DAO settlement — treasury.
- Федерации/конфедерации в затронутых fixtures используют допустимые roster,
  minCustodians и threshold. Отдельные generic DKG тесты 2/3 и 3/5 сохранены:
  ограничения конфедерации не меняют общий DKG-протокол.
- BTC fixtures получают тот же упорядоченный roster и порог, что EVM authorization.
- Test USD/feed вынесены из тестового файла без второй копии определения;
  rounds/финансовая политика передаются явно.
- Onboarding/bootstrap fixtures дополнены обязательной V3 политикой; E2E
  утверждают treasury, threshold и снятие ADMIN_ROLE. Сохранены outsider,
  двойной сбор комиссии, точный баланс и откат неуспешной транзакции.
- Script fixture scopes не ослабляют проверки: process isolation относится
  только к тестовому глобальному окружению Forge.

## Повторение проверки

Из корня репозитория с установленными locked dependencies и toolchain:

```sh
# При обновлении старой сборки: убрать stale artifacts перемещённых mocks.
forge build --root web3 --force
python ops/generate_abis.py --check
python ops/check_contract_sizes.py --output ops/artifacts/upgrade-contract-sizes-20261018.json
python ops/local_acceptance.py --junitxml ops/artifacts/upgrade-lifecycle-20261018.xml
python ops/verify_mainnet_headers_current.py --bitcoind /path/to/bitcoin-30.3/bin/bitcoind \
  --output ops/artifacts/upgrade-mainnet-fixtures-20261018.json
```

Toolchain: Foundry/Anvil 1.8.5, Solidity 0.8.30, Rust 1.89.0, Python 3.11,
Bitcoin Core 30.3.0. Acceptance запускает собственные loopback nodes и останавливает
их; не использует деньги/ключи оператора и не обращается к публичной сети для broadcast.

Полный runner завершился с кодом **0**. После последней узкой правки маскирования
относительного RPC-пути повторён весь Python-набор; Solidity при этом не менялся.

| Проверка | Итог |
|---|---|
| Rust | 4 passed, 0 failed / skipped |
| Foundry | 408 passed в 39 suites, 0 failed / skipped; 177.14 секунды |
| Python, окончательный прогон | 557 passed, 0 failed / skipped; 12.73 секунды |
| E2E с Anvil/Core | 13 passed, 0 failed / skipped; 1023.02 секунды |
| ABI drift, runtime/initcode gate, `git diff --check` | Passed |

408 Foundry entries включают 405 тестов и три автоматически обнаруженных
одноимённых getter-вызова `testHelpersEnabled`; это не три дополнительные
проверки безопасности. Объявления на interface/abstract без creation code
отдельно перечислены в discovery report, исполняемые дочерние тесты выполнены.

Bootstrap дошёл до `VERIFIED` за **147 шагов**: **46 контрактов**, **38** реальных
top-level CREATE, максимальное полное initcode в этом сценарии **24 721 байт**.
Отдельные script/max-argument tests покрывают более крупные конструкции.
Три участника использовали отдельные рабочие каталоги и RPC routes; CLI проверил
одинаковый Bitcoin fingerprint и обнаружение DAO. Из 36 on-chain DKG packets
18 были HPKE envelopes; ни одно из 27 проверенных локальных секретных значений
в публичном корпусе не найдено. Это проверка конкретного сценария, а не
доказательство невозможности любой будущей утечки.

Сохранились предупреждение deprecated `websockets.legacy`, compiler/linter notices
и ожидаемые `MismatchedABI` при фильтрации чужих событий в receipt. Падений,
пропусков тестов и повышенных лимитов размера ими не скрыто. Узлы остановлены
штатным cleanup runner.

Доказательства: [итоги и хеши проверенного кода](../ops/artifacts/upgrade-validation-20261018.json),
[Foundry discovery/results](../ops/artifacts/upgrade-lifecycle-20261018.solidity.json),
[Python JUnit](../ops/artifacts/upgrade-lifecycle-20261018.python.xml),
[E2E JUnit и публичные свойства запусков](../ops/artifacts/upgrade-lifecycle-20261018.xml).
Артефакт проверки фиксирует рабочее дерево перед коммитом; точное содержимое
проверенных исходников закреплено хешами в validation record. Результат
проверки не означает развёртывание в публичной сети.

## Что остаётся перед реальным развёртыванием

- Поддерживаемый операторский Bitcoin-профиль — **regtest**. Новый mainnet relay
  и signing board — проверенные контрактные компоненты, но полноценная mainnet
  policy агента и его подключение к signing board в границы этого ТЗ не входят.
- Mainnet relay доверяет выбранному checkpoint/chainwork; нужны независимая
  проверка недавней точки, синхронизация tip до подключения потребителей и
  мониторинг. Нет автоматического keeper, freshness guard или восстановления
  после постоянного halt. Их отсутствие не маскируется древним fixture checkpoint.
- Переиспользование системных контрактов выполняется через Python staged runner.
  Прямые Forge-скрипты проверяют runtime/getters, но не историю CREATE. RPC
  должен отдавать историю; RPC, локальная сборка и машина остаются доверенными.
- Контракты immutable: это не миграция работающей системы. Старые pin/state,
  UTXO reservations и подписанные транзакции нельзя переносить удалением журналов.
- Test USD/feed и финансируемый тестовый converter не подходят для реальных
  средств. Внешние oracle, reward token, market и отчёт TVL требуют отдельной
  проверки; canonical provenance внутренних контрактов их не сертифицирует.
- Откат или клонирование encrypted signer state вместе с nonce journal остаётся
  риском повторного nonce. Нужны резервирование/восстановление без отката spent
  history. On-chain roster, суммы, голоса и signing commitments/shares публичны;
  шифрование защищает приватные DKG-пакеты, а не все метаданные системы.
- Запас runtime MultiPoolDeployer — только 139 байт; следующий Solidity diff
  обязательно проходит тот же size gate. Предыдущие остаточные риски из
  [security report](../security_best_practices_report.md) остаются применимыми.
