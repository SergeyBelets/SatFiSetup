# Deployment и миграция

Срез 2 ноября 2026: [карта покрытия и конкретный остаток](ZKSPV_FUNCTIONAL_COVERAGE.md).
Для следующего запуска пользователь выбрал **новую связку**. Подготовка сохраняет
[выбор mainnet/testnet пар и backend](PRODUCTION_PREPARATION.md). Empty inventory
ещё требует независимой проверки; `live_migration` не становится N/A от выбора
режима или пустого локального файла.

Original-graph catalog и retained-old/separate-target handover реализованы;
[canonical family root](BITCOIN_KEY_FAMILY.md) добавляет on-chain status inheritance
между допущенными registry на одном независимо закреплённом root. Реальный
asset/accounting transition остаётся отдельной условной реализацией. Сохранение старого графа не равно
переносу обязательств. Для доказанно пустого нового deployment migration может
быть N/A; неполный inventory такого доказательства не даёт.

Публичного deployment, paid proving, аренды оборудования и переноса реальных
средств в этом запуске нет. Подготовительный `config/zkspv.example.json` содержит
null для неизвестных значений и не проходит activation preflight. Сеть и
checkpoint нельзя выбирать из имени RPC или подставлять из тестовой fixture.

## Проверки перед отложенным запуском

[Локальная приёмка](ZKSPV_LOCAL_ACCEPTANCE.md) включает 3269 Python и 834 + 25
Solidity tests. Три native-проверки возвращены; новые результаты не означают
migration acceptance или независимую квалификацию. Общий Core/FROST/Groth16/EVM
контур отложен пользователем из-за места. Перед его возобновлением потребуются
собственные deployment/identity/quote/consent/need файлы и проверенный запас ресурсов.
При нехватке казны допустимо явно ограниченное confederation own funding.

## Callback admission в новой обычной связке

Изменённый bootstrap добавляет `CallbackPoolRegistry` после escrow и однократно
связывает его через действующее FROST approval до обслуживания S/CS callbacks.
Registry закрепляет escrow, authorization и канонические simple/confederation
deployer roots. Root deployers фиксируют реально созданные pool и share-token
runtime hashes; arbitrary getters или код похожего registry не доказывают
admission. Клиенты проверяют exact runtime/constructor/CREATE provenance всех
связей до Bitcoin nonce. Direct MP/treasury paths сохраняются.

У нового `MultiPoolDeployer` осталось мало места до EIP-170; актуальные sizes
и constructor payload limits проверять заново после каждого изменения compiler,
dependency или initcode. Не повышать EVM code/gas limits ради прохождения.
Production ABI следует генерировать после обычной сборки, отдельно от dynamic
test artifacts.

## Существующие живые обязательства

Старые immutable escrow/registry/guard не исправляются изменением клиентского
JSON. Старые deployers не имеют новых CREATE records; это не основание добавлять
произвольные старые pools в allowlist. До миграции составить inventory:
pending withdrawals, Locked/Reserved/Signing families, executable replacements,
SMP1 allocations, уже оплаченные Bitcoin txids, unminted outpoints, current/old
key epochs, actual callbacks и native claims.

Не переводить новую подпись на другую денежную authority без разрешённого
протокола. Существующая подписанная выплата требует canonical proof settlement
по своей семье, а не безусловного refund/reset. Не переносить spent/nonce journal
из старого backup поверх более нового и не запускать два клона signer. Для
несовместимого callback нужен отдельный проверенный continuity plan;
в этом snapshot универсального export/import или recovery administrator нет.

`live_migration` остаётся UNKNOWN, пока не доказано отсутствие обязательств либо
проверена конкретная миграция. N/A допустим лишь при доказанном новом deployment.
Тестовая новая связка сама по себе не доказывает состояние пользовательской сети.

## Условия будущего ZK deployment

Сначала квалифицировать corrected pinned candidate с baseline/native tests и
реальным EVM-compatible proof на контролируемом поддерживаемом оборудовании.
Минимальная state/adapter/consumer интеграция, encrypted requests/heartbeat,
service-job treasury, адресные SDK, BTC/MP/S subsidy и multi-request suite уже
реализованы. Квалифицировать точные public outputs, canonical history,
reorg/halt/Fresh, relay authorization и итоговые профили; закрыть кодовые gaps
и полный сценарный контур из карты покрытия. Проверки этих функций не наследуют
аудит Citrea, а исправления текста ТЗ не являются выполненными функциями.

Перед публичной активацией нужны target manifest, exact runtime/constructor
provenance, proof/control/image IDs, согласованный adapter всех consumers,
полные реальные receipts/gas budgets, сохранение существующей давности и
confirmations, measured catch-up/SLA/TCO, independent review конкретного
snapshot и continuity всех живых обязательств. Локальные тесты и schema check
не дают такого разрешения. Команды публичной отправки намеренно не подготовлены
с фиктивными адресами/ключами; существующий staged deployment сохраняется.
