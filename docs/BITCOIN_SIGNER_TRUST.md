# Привязка Bitcoin-подписанта к развёрнутой системе

Срез 31 октября: [общая архитектура и схемы](ZK_SPV_ARCHITECTURE.md).
Текущий legacy signer **не подключает** граф с `BitcoinAddressRegistry`:
`inspect_registry` возвращает `BTC_ADDRESS_SIGNING_PROFILE_REQUIRED` до нового
private signing. Новый явный профиль `--address-signing` подключает plan V3,
текущие поколения, per-input BASE/DERIVED binding и общий nonce/spent journal.
Порядок допуска и восстановления описан в [адресном Bitcoin signer](BITCOIN_ADDRESS_SIGNING.md).
Реальный derivation verifier остаётся неквалифицированным; профиль не выдаёт Ready.
Старые конфигурации без address registry
сохраняют существующую проверку graph и nonce anti-rollback.

Адресный mint имеет отдельный допуск: `inspect-bitcoin-registry` и
`trust-bitcoin-registry` с `--mint-only --batch-registry BATCHES` проверяют тот же
неизменяемый граф и закреплённый derivation verifier, сохраняя pin отдельно в
`btc-mint-registry`. Этот допуск не разрешает `btc-commit` / `btc-sign`.
Для адресных операций используйте [клиент mint и admission](ADDRESS_DEPOSIT_REGISTRY.md#mint-client-and-admission).
Поколения и текущие статусы проверяются при каждой попытке и не закрепляются
навсегда в fingerprint. Реальный verifier остаётся `QUALIFIED=false`.

До первого `btc-commit` каждый участник должен проверить и локально закрепить
адрес `BitcoinBatchRegistry`. Адрес внутри платёжного JSON сам по себе не является
доказательством одобрения: чужой контракт способен возвращать похожие ответы.
Команды ниже выполняются после завершения bootstrap, настройки escrow и deposit
registry и получения `VERIFIED`. Они не отправляют транзакции и не раскрывают
секретные доли.

## Настройка через on-chain board

Используйте собственные `RPC`, `IDENTITY` и `PARTICIPANT_DIR` из
[инструкции первой DKG](FIRST_DKG_ONCHAIN.md). `BOARD` обнаруживается там по адресу
друга-оператора. Здесь `BOARD` — адрес **`DKGOnchainBoard`**, в котором находятся
приглашение, DKG-пакеты и одобрения настройки системы. Дополнительный канал
передачи файлов для этой привязки не требуется. Для публичного Bitcoin используется V6
из [Bitcoin-инструкции](BITCOIN_MAINNET_RUNBOOK.md) и [матрицы сетей](NETWORKS_AND_AUTOMATION.md), а для chain 1 — из
[Ethereum mainnet инструкции](ETHEREUM_MAINNET_RUNBOOK.md); задайте собственные
`BTC_RPC` и `BTC_COOKIE`. Mainnet inspect/trust требуют live-проверки своего Core
и relay. В regtest два аргумента Core в примерах ниже можно опустить.

Отдельный [`BitcoinSigningBoard`](../web3/docs/bitcoin-signing-board.md) хранит
публичные материалы Bitcoin-подписи. Он не является источником приглашения и не
подставляется в `--board` этих команд. Для платежа CLI использует
`--signing-board`; адрес и batch можно обнаружить через `btc-pending` из
публичного намерения в DKG board. Полный порядок — в
[инструкции Bitcoin-платежа](BITCOIN_ONCHAIN_RUNBOOK.md).

1. Получите публичную конфигурацию из цепочки:

   ```sh
   frost-signer dkg inspect-bitcoin-registry \
     --board "$BOARD" --role btc_ceremony --rpc "$RPC" \
     --identity-file "$IDENTITY" \
     --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
     --password-file "$PARTICIPANT_DIR/password" \
     --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
   ```

   Команда ищет исполненное FROST-одобрение привязки escrow в событиях board.
   Она требует единственный подходящий реестр, связь с BTC-церемонией и именно
   `conf_ceremony` из приглашения. Проверьте `graph`: сеть, участников, порог,
   результаты обеих DKG, адреса реестра, authorization, coordinator и escrow.
   В `withdrawal_policy` проверьте `min_p2wpkh_sats` и `min_p2tr_sats`:
   это минимумы одной заявки в целых сатоши, согласованные в приглашении V6.
   Они обязательны и входят в fingerprint; значения 294/330 в примере
   являются выбранной политикой стенда, а не скрытыми значениями клиента.
   В `bitcoin_confirmations` проверьте глубину deposit и withdrawal: оба
   значения должны совпадать с `bitcoin_confirmations` приглашения V6.
   Для mainnet дополнительно проверьте `bitcoin_network=mainnet`, полную
   `bitcoin_policy`, `evm_confirmations` и контракты `relay`/`spv_guard`.
   `network_observation` — текущая проверка готовности; она не заменяет fingerprint
   неизменяемой конфигурации и не фиксирует готовность навсегда.
   Скопируйте `fingerprint` из этого локального результата.

2. Закрепите проверенный результат:

   ```sh
   export BTC_GRAPH_FINGERPRINT='<fingerprint из предыдущей команды>'
   frost-signer dkg trust-bitcoin-registry \
     --board "$BOARD" --role btc_ceremony --rpc "$RPC" \
     --identity-file "$IDENTITY" \
     --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
     --password-file "$PARTICIPANT_DIR/password" \
     --accept-fingerprint "$BTC_GRAPH_FINGERPRINT" \
     --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
   ```

   Ожидаемый статус — `BTC_REGISTRY_PINNED`. Каждый друг выполняет обе команды
   у себя. При одинаковой системе fingerprint совпадает, даже если RPC разные.

3. Продолжайте обычные `dkg btc-commit` и `dkg btc-sign` с тем же локальным
   participant state. Перед публикацией nonce клиент проверяет закреплённый адрес,
   код всей цепочки контрактов, связи и сохранённый блок Ethereum.

Для развёртывания без board используются `--manifest /path/to/btc-manifest.json`
и `--batch-registry 0x...` вместо `--board`/`--role`; сначала выполните inspect,
затем trust с проверенным fingerprint. Это отдельная процедура с заранее
проверенным адресом. Для mainnet bundle должен содержать проверенное V6 onboarding;
голый legacy manifest не устанавливает mainnet policy. Присланный платёжный план
не выбирает реестр или сеть автоматически.

## Обновление существующих установок

Старый state без привязки останавливается с `BTC_REGISTRY_NOT_PINNED`, прежде чем
будет создан nonce или отправлен `beginSigning`. Выполните шаги 1–2 над текущим
state. После импорта DKG-пакета эту проверку также нужно повторить: экспорт
ключевого пакета не переносит доверие к развёртыванию или историю nonce.

Старый pin, содержащий только runtime/graph, возвращает
`BTC_REGISTRY_PROVENANCE_REQUIRED`. Повторите шаги 1–2 для того же адреса:
обновлённый fingerprint включает доказательства создания. Клиент проверит
прежний graph и его block anchor и дополнит существующий pin; удалять state
или spent journal не нужно.

Повтор trust с тем же fingerprint безопасен. Автоматическая смена реестра для
существующего BTC-кошелька запрещена (`BTC_REGISTRY_ALREADY_PINNED`), поскольку
у другого реестра независимые резервации UTXO. Миграция требует отдельной процедуры
учёта уже подписанных транзакций; не удаляйте pin/state ради обхода ошибки.
`BTC_REGISTRY_ANCHOR_REORG` и `BTC_REGISTRY_GRAPH_CHANGED` также требуют
разбирательства, а не перепривязки вслепую.

## Проверяемая граница доверия

Клиент сравнивает инструкции runtime с локальной сборкой, проверяет согласованность
повторяющихся immutable-полей и проверяет все связи через getters. Authorization
должен быть `DKGAuthorization`, связанный с завершённой EVM_AUTH на том же
DKGCoordinator, с тем же упорядоченным составом и порогом, что и BTC-кошелёк.
Coordinator и verifier также должны соответствовать сборке. Проверяются escrow,
deposit registry и глубина Bitcoin-подтверждений не меньше шести. Для regtest
требуется точный runtime `RegtestHeaderRelay` и Core `chain=regtest`. Публичный Bitcoin V6
требует runtime и constructor provenance `MainnetHeaderRelay` либо `Testnet4HeaderRelay` и `MainnetSPVGuard`,
совпадение checkpoint/chainwork/reorg policy и EVM depth. Core обязан сообщать
`chain=main` либо `chain=testnet4`, правильный genesis, завершённую синхронизацию и готовый txindex;
pruned/IBD/stale узлы отклоняются. Canonical relay head/work, возраст и отставание
сверяются со своим Core на подтверждённом EVM snapshot.
Sepolia остаётся EVM-сетью; Bitcoin-сеть выбирается подписанным приглашением.
`dkg btc-network-status` повторяет readiness, а `dkg btc-relay` догружает bounded
batch заголовков из pinned graph без обмена файлами или передачи private nonce.
Проверка конструктора escrow включает оба минимума вывода. Новый контракт
имеет другой код и не подменяет старое immutable-развёртывание: старый pin
нельзя автоматически перенести на новый адрес. Новая версия escrow требует
нового согласованного развёртывания; миграция действующего Bitcoin-кошелька
с уже подписанными транзакциями остаётся отдельной задачей.

Кроме runtime, проверяются канонический код создания, аргументы конструктора,
квитанции и происхождение вложенного authorization. Участник сохраняет
аутентифицированные доказательства в отдельном защищённом кеше; при повторе
проверяются текущий runtime и канонические якоря, а изменяемые permissions,
финальность и Bitcoin-состояние читаются заново. Для прямого CREATE с известным
блоком нужны блок/receipt создания, но не старое состояние RPC. Первая проверка
вложенного authorization и поиск неизвестного блока могут требовать исторических
code/`eth_call`; недоступное обязательное доказательство останавливает проверку.
Подробности: [происхождение контрактов](CONTRACT_PROVENANCE.md).

Копия подлинных контрактов не получает возможности одобрять платежи без того же
EVM-кворума. Но у такой копии отдельные резервации: поэтому runtime-проверка
дополняется однократной привязкой конкретного адреса. Клиент доверяет своему RPC,
локальной сборке и защищённому состоянию участника. Одновременный откат encrypted
state и spent journal по-прежнему требует независимого журнала или ротации,
как описано в [протоколе подписания](SIGNING_PROTOCOL.md).

## BTC fee reserve and reconciliation

See [the reserve runbook](BTC_FEE_RESERVE.md) for the graph-scoped canonical allocations,
principal-preserving settlement, independent Core inventory and typed FROST commands.
Use `bitcoin fee-reserve` and the private `--fee-reserve-config` on participants.
Address service V2 pins the same policy without resetting watch or nonce journals.
Ready and operational qualification remain pending; long combined E2E is deferred.
