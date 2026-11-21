# Bitcoin mainnet: изменения и проверка — 18 октября 2026

**Историческая область этого отчёта — Bitcoin mainnet V4 до расширения на Ethereum mainnet V5.**
Описанное ниже ограничение EVM Anvil/Sepolia относится к этому прогону.
Текущая настройка chain 1 описана в [V5 runbook](ETHEREUM_MAINNET_RUNBOOK.md).

**Полный прогон прошёл: 5 Rust / 426 Solidity / 954 Python / 15 E2E.**
Ошибок и пропусков нет. Rust/Solidity и все E2E проверены общим runner; весь
Python-набор повторён после последней правки mainnet-диагностики. Изменения
проверены в рабочем дереве; это не утверждение о новом GitHub commit.

Поддержка Bitcoin mainnet добавлена к существующему regtest CLI. Пошаговая
настройка: [mainnet V4](BITCOIN_MAINNET_RUNBOOK.md). Платёжный цикл и сообщения
проходят через EVM board; участники не пересылают друг другу файлы. Параметры своего
Core и секретное состояние остаются локальными.

## Что изменилось

- Приглашение V4 фиксирует Bitcoin-сеть, checkpoint/chainwork/предшественников,
  предел реорганизации, свежесть/отставание relay, размер порции заголовков и
  EVM confirmations. Участник проверяет эту политику своим Core до enrollment.
- Bootstrap использует `MainnetHeaderRelay` и `MainnetSPVGuard`, внешние token/feed
  с проверкой runtime и decimals, реальный перевод funding. Тестовые mint/token/feed
  не подставляются в этот профиль. Заголовки догружаются до активации потребителей.
- `inspect/trust`, подготовка и одобрение платежа, nonce/share, cached delivery,
  broadcast и settlement связаны с одной сетью и закреплённым графом контрактов.
  `btc-relay`, `btc-network-status` и `doctor` проверяют mainnet policy и готовность.
- Устаревший или остановленный relay блокирует доказательства на уровне guard,
  в том числе permissionless mint. Проверки CLI не являются единственным барьером.
- Резерв, escrow bind и обеспечение перед Bitcoin-подписью читаются из одного
  подтверждённого EVM-снимка. Неподтверждённый bind не позволяет создать nonce/share.
  EVM-подписи также используют подтверждённый снимок и проверяют его каноничность.
- RPC не пересылает cookie через redirects/proxy; mainnet запрещает удалённый
  plaintext HTTP. Проверяются файл cookie, JSON-RPC envelope/id, размер ответа и
  точные суммы в сатоши. Ошибки CLI/doctor не отражают произвольный текст провайдера.

Подробности и границы: [security review](BITCOIN_MAINNET_SECURITY_REVIEW.md).

## Результаты проверок

| Набор | Результат |
|---|---|
| Rust, locked build/test | 5 passed |
| Solidity, изоляция тестов с process environment | 426 passed в 42 suites, 0 failed/skipped |
| Python, весь набор после последней правки doctor | 954 passed, 0 failed/skipped |
| Общий E2E на свежих Anvil/Core | 15 passed, 0 failed/skipped; 28 минут 51 секунда |
| Исторические mainnet headers в настоящем Core 30.3 | 36 288 submitted, 36 289 checked с genesis; 4 043 fixture records и два retarget совпали |
| Live readiness исторического header-only Core | Отказ `MAINNET_CORE_NOT_SYNCED`, без подмены времени и отключения политики |
| Размеры контрактов | Все 96 укладываются в EIP-170/EIP-3860; минимальный runtime-запас 139 байт |
| Документация | 10 Mermaid-схем отрисованы; 27 CLI-примеров разобраны; локальные ссылки без ошибок |

426 записей Forge включают три унаследованные проверки getter `testHelpersEnabled`;
423 — поведенческие тесты. Python выдаёт предупреждение об устаревшем API
`websockets.legacy`; ошибок совместимости `pip check` не обнаружено. E2E также
выдаёт предупреждения `MismatchedABI` при фильтрации чужих событий из общих receipts;
проверки ожидаемых событий и постусловий проходят.

Новый V4 E2E отдельно успешно выполнил первую BTC DKG: три самостоятельных
CLI-процесса на Anvil достигли `ACTIVE`/`VERIFIED`, получили одинаковый результат
и передали шесть HPKE-пакетов через board. Девять проверенных приватных значений
не обнаружены в публичных пакетах и выводе CLI. Для Core в этом тесте используется
явно обозначенная историческая симуляция; криптографические вычисления выполняет
настоящий Rust signer.

Mainnet-guard проверен с реальными потребителями deposit/escrow: валидное
историческое coinbase-доказательство разрешает mint, stale/halt прекращают
приём доказательств; escrow collateral не сжигается и резерв не освобождается
после начала signing при отказе проверки. Синтетический payment root в отдельном
Solidity harness явно обозначен как тестовый.

## Сохранённые результаты

- [Общий отчёт и SHA-256 исходников](../ops/artifacts/bitcoin-mainnet-validation-20261018.json).
- [Rust](../ops/artifacts/bitcoin-mainnet-20261018.rust.json),
  [Solidity](../ops/artifacts/bitcoin-mainnet-20261018.solidity.json),
  [Python JUnit](../ops/artifacts/bitcoin-mainnet-20261018.python.xml),
  [E2E JUnit](../ops/artifacts/bitcoin-mainnet-20261018.xml).
- [Настоящий Core и исторические mainnet headers](../ops/artifacts/mainnet-policy-core-20261018.json),
  [размеры контрактов](../ops/artifacts/mainnet-contract-sizes-20261018.json),
  [проверка документации](../ops/artifacts/mainnet-docs-20261018.json).

## Что эти проверки не подтверждают

Реальная выплата BTC в mainnet не отправлялась. Новые threshold-платежи проверяются
на regtest; исторические mainnet PoW, retarget, chainwork, Merkle proofs и отказ
неготового настоящего Core проверяются отдельно. Нельзя включить произвольную
новую транзакцию в уже добытый исторический блок.

Bitcoin mainnet и Ethereum mainnet — разные сети. Общий bootstrap остаётся
ограничен EVM Anvil/Sepolia, постоянным освобождением создаваемых фабрик от DEPLOY
fee и конвертером с публичной фиксированной котировкой и конечным inventory.
Поддержка Bitcoin mainnet сама по себе не делает такой EVM-профиль пригодным
для хранения значимых средств. Production EVM deployment и платный граф фабрик
требуют отдельной подготовки; тесты не заменяют внешний аудит.

SPV доверяет принятому checkpoint и наиболее тяжёлой поданной ветке. Нужны свои
синхронизированные Core и постоянная доставка заголовков. Глубокая Bitcoin-
реорганизация не отменяет уже исполненный mint/burn в EVM. Сохраняются доверие
к внешним token/feed и их upgrade/admin, риски компрометации компьютера, полного
отката состояния вместе с nonce-журналом и сговора порогового числа участников.

## Воспроизведение

После установки зависимостей по [первой инструкции](FIRST_DKG_ONCHAIN.md):

```sh
python ops/local_acceptance.py --junitxml ops/artifacts/bitcoin-mainnet-20261018.xml
python ops/verify_mainnet_policy.py --bitcoind /absolute/path/to/bitcoin-30.3/bin/bitcoind \
  --output ops/artifacts/mainnet-policy-core-20261018.json
```

Первый runner собирает Rust/Solidity, проверяет ABI/размеры, выполняет все unit-
и E2E-тесты на временных loopback-узлах. Второй воспроизводит сохранённые mainnet
заголовки в отдельном Core без peers, кошелька и отправки Bitcoin-транзакций.
