# Проверка зависимостей — 17 октября 2026

Проверены 52 версии из `signer-agent/requirements.lock` через **pip-audit 2.9.0 / PyPI advisory API** и 119 registry-зависимостей из `frost-crypto/Cargo.lock` через **OSV v1/querybatch**. Отдельно сверены опубликованные advisories OpenZeppelin с vendored-версией 5.4.0. Наружу передавались только публичные имена пакетов и версии; исходники, RPC-адреса и секреты не отправлялись.

До обновления Python lock содержал **23 advisory в 6 пакетах**. После обновления повторная проверка тех же 52 пакетов дала **0 известных advisory**. В Rust устранён `RUSTSEC-2026-0097`; осталась одна информационная запись об отсутствии сопровождения target-specific `atomic-polyfill`. Это результат сопоставления с базами на указанную дату, а не доказательство отсутствия неизвестных ошибок.

Машиночитаемые результаты: [до исправлений](../ops/artifacts/security-dependencies-before.json), [после исправлений](../ops/artifacts/security-dependencies.json). В первом файле перечислены все 23 идентификатора и их CVE/GHSA aliases без дублирования aliases как отдельных находок.

## Python

| Пакет | Было → стало | Advisory | Применимость |
|---|---|---|---|
| aiohttp | 3.13.5 → 3.14.3 | 14; среди них [CVE-2026-69244 / GHSA-cq5v-8q36-5273](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273) | HTTP parser, cookies, redirects и ограничения ресурсов. Клиент проекта использует синхронный `Web3.HTTPProvider`, а не сервер aiohttp; наличие версии не означает достижимость всех 14 сценариев. |
| multidict | 6.7.1 → 6.9.1 | [CVE-2026-104874 / GHSA-54p9-h82j-f925](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925) | Утечка памяти в отдельных операциях над items-view. Прямого использования этих операций в клиенте не найдено. |
| pytest | 8.4.2 → 9.0.3 | [CVE-2025-71176 / GHSA-6w46-j5rx-g56g](https://docs.pytest.org/en/9.0.x/changelog.html#pytest-9-0-3-2026-04-07) | Тестовый инструмент; небезопасный временный каталог на многопользовательской машине. |
| python-dotenv | 1.2.1 → 1.2.2 | [CVE-2026-28684 / GHSA-mf9w-mj56-hr94](https://github.com/theskumar/python-dotenv/security/advisories/GHSA-mf9w-mj56-hr94) | Проблема записи через symlink в `set_key`/`unset_key`. Клиент использует чтение через `load_dotenv`; проблемные функции напрямую не вызываются. |
| requests | 2.32.5 → 2.33.0 | [CVE-2026-25645 / GHSA-gc5v-m9x4-r6x2](https://github.com/psf/requests/security/advisories/GHSA-gc5v-m9x4-r6x2) | Подмена временного файла в `extract_zipped_paths`; такого прямого вызова в проекте не найдено. |
| urllib3 | 2.6.3 → 2.8.0 | 5; среди них [CVE-2026-97688 / GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g) | HTTP-клиент используется через Web3/requests. Исправлены обработка chunked/deflate, ограничения размера и отдельные случаи proxy/redirect. Для каждого advisory нужны свои условия; реальная эксплуатация в развёрнутой системе не проверялась. |

Исправленные пакеты требуют как минимум Python 3.10; поддерживаемый минимум проекта повышен до **Python 3.11**. `pyproject.toml` также задаёт security floors, чтобы установка без lockfile не принимала старые уязвимые версии как удовлетворяющие зависимостям. Для воспроизводимого запуска используйте lockfile и актуальный security patch Python.

В отдельном чистом окружении Python **3.11.13** установлены все 52 зафиксированные версии и editable-клиент; `pip check` завершился успешно. Python 3.9/3.10 больше не являются поддерживаемой средой. Исторические acceptance-отчёты с Python 3.9 описывают прежние проверки и не переименованы задним числом.

## Rust

`rand` обновлён **0.8.5 → 0.8.6** в манифесте и lockfile: [RUSTSEC-2026-0097](https://rustsec.org/advisories/RUSTSEC-2026-0097.html), alias `GHSA-cq8v-f236-94qc`. Уязвимость требует custom logger, обращения к thread RNG из него и соответствующих features. Текущий crypto-клиент использует `OsRng`, а feature `log` не включена: достижимая эксплуатация этой ошибки в текущей сборке не подтверждена. Обновление всё равно устраняет совпадение по версии. После него все четыре Rust-теста прошли.

Осталась [RUSTSEC-2023-0089](https://rustsec.org/advisories/RUSTSEC-2023-0089.html): `atomic-polyfill 1.0.3` больше не сопровождается; исправленной версии нет. Цепочка в `cargo tree --target all`: `frost-core 2.2.0 → postcard 1.1.3 → heapless 0.7.17 → atomic-polyfill 1.0.3`.

В дереве активных зависимостей для `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu` и `aarch64-unknown-linux-gnu` этой crate нет. Запись сохранена как остаточный риск для других targets. FROST 2.2.0, HPKE 0.12.0 и формат ключей не менялись ради снятия информационного предупреждения. Смена FROST требует отдельной проверки совместимости протокола и состояния.

## OpenZeppelin

Пакеты `@openzeppelin/contracts` и `@openzeppelin/contracts-upgradeable`, включая вложенную копию contracts, имеют версию **5.4.0**. Проверены [официальный список advisories](https://github.com/OpenZeppelin/openzeppelin-contracts/security/advisories?state=published) и [security center](https://contracts.openzeppelin.com/security).

[CVE-2025-54070 / GHSA-9rcw-c2f9-2j55](https://github.com/OpenZeppelin/openzeppelin-contracts/security/advisories/GHSA-9rcw-c2f9-2j55) исправлена именно в 5.4.0; [CVE-2024-27094 / GHSA-9vx6-7xxf-x967](https://github.com/OpenZeppelin/openzeppelin-contracts/security/advisories/GHSA-9vx6-7xxf-x967) исправлена раньше, в 5.0.2. Совпадения с опубликованными диапазонами этих advisories не обнаружены. Это проверка зависимостей, а не аудит правильности использования OpenZeppelin нашими контрактами.

## Повторение проверки

Из корня репозитория создайте отдельное временное окружение аудитора:

```sh
AUDIT_DIR=$(mktemp -d)
python3.11 -m venv "$AUDIT_DIR"
"$AUDIT_DIR/bin/python" -m pip install 'pip-audit==2.9.0'
"$AUDIT_DIR/bin/pip-audit" -r signer-agent/requirements.lock \
  --no-deps --disable-pip --progress-spinner off --format json
```

`--no-deps --disable-pip` проверяет именно полный зафиксированный список без повторного разрешения зависимостей. Сам lockfile пока не содержит hashes пакетов: инструмент предупреждает об этом; hashes остаются отдельным усилением supply-chain защиты.

Для повторения Rust-проверки без установки cargo-audit используйте Python 3.11+ и публичный OSV API. Скрипт отправляет только registry-пакеты и версии, исключая локальный пакет проекта:

```sh
python3.11 - <<'PY'
import json, pathlib, tomllib, urllib.request
lock = tomllib.loads(pathlib.Path('frost-crypto/Cargo.lock').read_text())
packages = [p for p in lock['package'] if p.get('source', '').startswith('registry+')]
findings = []
for offset in range(0, len(packages), 25):
    batch = packages[offset:offset + 25]
    body = {'queries': [
        {'package': {'name': p['name'], 'ecosystem': 'crates.io'}, 'version': p['version']}
        for p in batch
    ]}
    request = urllib.request.Request('https://api.osv.dev/v1/querybatch',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=90) as response:
        results = json.load(response)['results']
    for package, result in zip(batch, results):
        for advisory in result.get('vulns', []):
            findings.append({'name': package['name'], 'version': package['version'],
                             'advisory': advisory['id']})
print(json.dumps({'package_versions': len(packages), 'findings': findings}, indent=2))
PY

cargo tree --locked --manifest-path frost-crypto/Cargo.toml --target all -i atomic-polyfill
cargo tree --locked --manifest-path frost-crypto/Cargo.toml --target x86_64-unknown-linux-gnu -i atomic-polyfill
cargo test --locked --manifest-path frost-crypto/Cargo.toml
```

Инвентаризация не включает аудит системных Python/OpenSSL/OS, npm dev-зависимостей vendored-библиотек или сторонних RPC-сервисов. Текущий продукт не устанавливает и не исполняет npm-проекты из `web3/lib`.
