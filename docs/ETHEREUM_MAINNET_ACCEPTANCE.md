# Ethereum mainnet V5 — проверка 22 октября 2026

**Статус: полный набор проверок прошёл на окончательном коде.**
Публичного развёртывания Ethereum mainnet и выплаты настоящих BTC в этой проверке не было.
Порядок запуска: [mainnet runbook](ETHEREUM_MAINNET_RUNBOOK.md),
[архитектура и схемы](ARCHITECTURE.md), [скрипты деплоя](STAGED_DEPLOYMENT.md).

## Реализовано и исправлено

- V5 связывает Ethereum chain `1`, Bitcoin mainnet, roster, внешние token/feed,
  финансовую политику и 24-часовые фазы DKG. Требуются минимум 12 подтверждений
  вместе с `finalized`, собственный Bitcoin Core и локальные gas/fee budgets.
- Обе фабрики создают продукты за положительную deployment fee. Exemption на
  chain `1` запрещён; проектные authorization/vault/router отделены от DAO.
  Allowance проверяется на receipt `approve`, его расходование — на receipt
  deployment, с canonical hash до/после чтения. Постоянные связи сверяются в
  текущем подтверждённом состоянии, поэтому resume не требует уже потраченный allowance.
- Перед каждой `dkg sign-evm` на chain `1` проверяются происхождение target,
  фактический coordinator и enrollment role. Завершённая signing session
  возвращает `finalized` без нового nonce/транзакции; отсутствующая или прерванная
  сессия блокируется. Legacy Anvil/Sepolia coordinator inputs сохраняют совместимость.
- `FundedFeeConverter` допускает chain `1`, Anvil и Sepolia, отклоняя другие сети.
  Он расходует внесённый inventory по явной фиксированной котировке и не имеет mint-пути.
- Forge wrapper исправлен для необязательного JSON-RPC `params`; staged/leaf
  операции используют 130% оценки с проверкой фактически подписанного бюджета.
  Bootstrap/DAO явно оценивают `pending`, без fallback на `latest`, и задают
  `max(ceil(estimate × 130 / 100), estimate + 125000)`. Это покрывает проверенные
  ветви нового timestamp/checkpoint и UTC-дня, вызвавшие OOG при оценке `latest`.
  Локальный cap и 2²⁴ проверяются **до подписи**, без обрезания gas limit или
  автоматического повышения бюджета. Сохранённые signed retries остаются побайтно прежними.

Старый экспериментальный V5-профиль нужно пересобрать до первого bootstrap.
Для уже отправленных транзакций нужна [проверка миграции](ETHEREUM_MAINNET_RUNBOOK.md#5-bootstrap-и-три-dkg-через-ethereum)
с исходными journals/receipts; сбрасывать nonce или journal нельзя.

## Результаты на окончательном коде

| Проверка | Результат |
|---|---|
| Rust | 5 прошли |
| Solidity | 437 прошли; 179,419 с |
| Python | 1218 прошли; 20,83 с |
| Полный E2E-набор | 21 сценарий прошёл; ошибок, повторов и пропусков нет |
| V5 через установленный CLI | `VERIFIED`, все три DKG, платные фабрики, DAO approve/stake/unstake и идемпотентный повтор; 777,92 с |
| Размеры и ABI | 96 контрактов в лимитах; минимальный runtime-запас 139 байт у `MultiPoolDeployer`; ABI актуальны |
| Документация | 99 CLI-примеров, 47 Markdown-файлов; локальные ссылки без ошибок; 11 неизменившихся Mermaid |

Артефакты: [итоговая сводка](../ops/artifacts/ethereum-mainnet-final-20261022.summary.json),
[Rust](../ops/artifacts/ethereum-mainnet-final-20261022.rust.json),
[Solidity](../ops/artifacts/ethereum-mainnet-final-20261022.solidity.json),
[Python](../ops/artifacts/ethereum-mainnet-final-20261022.python.xml),
[сводный E2E JUnit](../ops/artifacts/ethereum-mainnet-final-20261022.xml),
[полный V5-сценарий](../ops/artifacts/ethereum-mainnet-focused-20261022.xml).

V5 проверен тремя участническими процессами в отдельных каталогах с отдельными
RPC routes: граф содержит **50 контрактов**, bootstrap — **168 шагов**.
За каждое из двух созданий уплачено 1 234 567 base units тестового reward token;
project treasury получила ровно **2 469 134**, allowance обеих фабрик израсходован.
Максимальный подписанный gas limit — **10 284 025**, ниже protocol cap.
Проверены 36 опубликованных ончейн-пакетов, из них 18 — HPKE envelopes:
**27 локальных секретных значений не обнаружены в этих пакетах**. Это проверка
конкретной границы публикации, а не доказательство отсутствия всех
криптографических уязвимостей.

E2E выполнены четырьмя изолированными процессами: V5 и три группы оставшихся
тестов с отдельными временными узлами. Последовательный E2E child общего runner
остановлен после collection; успешный выход одного полного runner не заявляется.
Ожидаемые 21 test ID и группы записаны в
[e2e-execution.json](../ops/artifacts/ethereum-mainnet-final-20261022.e2e-execution.json).
Итоговый JUnit содержит ровно этот набор, без повторов, ошибок и пропусков;
все четыре процесса завершились с кодом `0`. Хеши 1530 исходных файлов совпали
до и после прогона. Продолжительность групп — 726,12 / 577,42 / 601,58 с,
V5 — 777,92 с; параллельные времена не складываются в wall-clock длительность.

Реальный [Forge E2E](../ops/artifacts/ethereum-deploy-e2e-20261022.xml) проверяет
`base`/`factories`, budget proxy, finalized retry и отрицательные policy/cap случаи:
33 транзакции, 19 созданных контрактов. Четыре
[DAO gas E2E](../ops/artifacts/ethereum-dao-e2e-20261022.xml) покрывают
1 base unit / 10¹⁸ × следующий 12-секундный slot / UTC midnight, отсутствие
pending estimate и отказ до подписи при недостаточном cap. В
[измерениях газа](../ops/artifacts/ethereum-dao-gas-analysis-20261022.json)
исходной оценке 77 196 соответствует минимально достаточный UTC-лимит 187 923;
финальная транзакция с лимитом 202 196 прошла. Эти fixtures не прогнозируют
стоимость публичных транзакций. Focused проверки входят в полный набор и не суммируются с ним.

[Документация QA](../ops/artifacts/ethereum-mainnet-docs-20261022.json) проверяет
синтаксис CLI без RPC/транзакций и локальные ссылки. Все 11 Mermaid source hashes
совпадают с [ранее отрендеренными схемами](../ops/artifacts/ethereum-mainnet-docs-20261018.json);
повторное использование этого render proof обозначено явно.

## Область проверки и риски перед развёртыванием

V5 использует реальные контракты/Rust-криптографию на локальном Anvil chain `1`,
историческую симуляцию Bitcoin Core и тестовые token/feed. Это не публичный
Ethereum consensus и не mainnet-выплата. Реальные Bitcoin-платежи проверяются
отдельно в regtest; исторический [Core replay](BITCOIN_MAINNET_ACCEPTANCE.md)
не заменяет такую выплату.

Mainnet V5 повторно использует завершённый federation EVM DKG result для project
authorization. Независимая четвёртая project DKG через legacy manifest/package
CLI поддерживается только на Anvil/Sepolia. Отдельные mainnet-скрипты принимают
уже проверенный completed result/auth; `base`/`factories` не создают готовый DAO/Bitcoin wallet.

Остаются доверие к Bitcoin checkpoint и полноте relay-ветвей, доступность
Core/RPC/keepers, права эмитента/proxy-admin внешнего token/feed и конечный
inventory конвертера. Конвертация доступна любому держателю input token:
выгодная устаревшая фиксированная котировка позволяет выкупить reward inventory.
Глубокая Bitcoin-реорганизация не откатывает EVM mint/burn. Компрометация порога,
хостов и откат nonce journal требуют отдельной операционной защиты.
Независимый аудит не выполнялся. Подробнее:
[SPV/custody risks](BITCOIN_MAINNET_SECURITY_REVIEW.md),
[provenance](CONTRACT_PROVENANCE.md), [signer state](BITCOIN_SIGNER_TRUST.md).
