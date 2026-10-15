# Extension bridge — план реализации

**Goal:** предоставить отдельные CLI/MCP инструменты, координирующие последовательные действия Codex через существующее официальное расширение Chrome, без доступа bridge к браузерной авторизации.

**Architecture:** `src/gflow_cli/bridge/`, отдельный entrypoint `gflow-bridge`, собственная SQLite БД вне upstream worker. Один сервис обслуживает CLI и изолированную MCP registry. Браузерные действия выполняет агент; существующие upstream generation/auth entrypoints не используются в этом маршруте.

**Predict verdict:** CAUTION, 8/10; обязательные условия записаны в [PREDICT.md](PREDICT.md), приёмка — в [SCENARIO.md](SCENARIO.md).

## Tasks

- [x] Форк, закрепление v0.85.0 и базовая офлайн-проверка: 126 passed.
- [x] Пять независимых pre-implementation personas, сценарии, согласованный scope.
- [x] Красные тесты очереди, crash/replay, остановки, паузы и CLI/MCP parity.
- [x] SQLite repository и общий сервис с fail-closed commit boundary.
- [x] Standalone CLI и отдельный MCP stdio server без импорта upstream runtime.
- [x] Проверка артефакта и идемпотентное завершение.
- [x] Документация и конфигурационный пример без личных данных.
- [x] Гигиена, ссылки, зеркало docs, lint/format, types, тесты/coverage, настоящая CLI/MCP проверка.
- [x] Независимый review diff и устранение замечаний.
- [x] Коммит/ветка форка, фиксация результата и ограничений.

## Progress

- 2026-10-15: независимые D1/D2/D3/D4/D14 и адаптивные CLI/MCP/docs/BDD проверки завершены. Исправлены exit code `invalid_state`, generated skill drift и ссылка нового документа на upstream вместо форка. Обзор исходной реализации закреплён на `f1d9dbe17493ab71f3e17f470022605958f8bd22`; исправления проверены отдельным ограниченным diff.
- 2026-10-15: D5 portable memory не выявила противоречий; primary private memory автора upstream отсутствует. Этот пробел остаётся UNAVAILABLE, общий council verdict — YELLOW по покрытию памяти. Это не утверждение о готовности к upstream merge или релизу; результат — локальный координатор в пользовательском форке.

- 2026-10-15: review P2 — CLI неверно относил `invalid_state` к ошибкам ввода и возвращал 11. Добавлен красный тест с реальным claimed заданием и преждевременным `record-started`; после исправления он возвращает 16, состояние задания остаётся claimed.

- 2026-10-15: реализация разрешена пользователем. Baseline v0.85.0; локальное окружение из lockfile. Браузер, авторизация и генерация не запускались.

- 2026-10-15: локальный bridge 0.1 реализован одним writer. TDD: первый прогон core дал красный ModuleNotFoundError; отдельная регрессия kind/container сначала дала 2 failed. Итог scoped: 44 passed, покрытие bridge 87.73%. Следующий этап — полные локальные gates и независимый review, затем commit.

## Decision Log

- Коды CLI: только явный список ошибок ввода/конфигурации получает 11; остальные отказы состояния/хранилища получают 16. Проверка подстрок `invalid`/`required` удалена; Problem Details и MCP контракт не изменены.

- Очередь самостоятельная: `generation_queue` обслуживается автоматическим upstream worker и не содержит upscale в codec.
- Политика — одна операция за раз, случайная пауза после завершения, persistent halt, отсутствие автоматического восстановления неизвестного submit.
- Новый CLI и MCP используют один сервис; main `gflow` CLI не изменяется и не объявляется безопасным для этого workflow.
- Нет нового browser transport, DOM-селекторов, cookies, CDP или login flow. Live generation не входит в разрешённую проверку этого этапа.

- БД имеет собственные application_id/user_version, привязку ожидаемого профиля по hash, секрет владения хранится только как hash. Переназначение TTL отсутствует; reconcile никогда не возвращает запись в pending.
- Пути только локальные: UNC/device/mapped drive, traversal и reparse ancestors запрещены. Проверка артефактов ограничена структурой контейнера и hash; full codec/визуальное качество не утверждаются.

## Validation

- После review fix: 171 passed in 36.70s, bridge coverage 87.97% (порог 80%); whole-src pyright 0 ошибок, repository-wide Ruff check/format 541 файл чисты. Отдельно `tests/test_plugin_manifests.py`: 15 passed. Plugin generator `--check`: 7 файлов / 2 skills синхронны. Ссылки, website PII/mirror, hygiene и portable council memory повторно зелёные.
- Wheel собран с новым entrypoint; ZIP-дубликатов нет, версия пакета и `__version__` совпадают. Полный upstream coverage/внешний CI/SonarCloud и живая генерация не заявляются проверенными.

- Review fix: `tests/bridge/test_adapters.py` — 4 passed; регрессия `invalid_state` до исправления дала 1 failed (11 вместо 16). Scoped ruff check и format check обоих изменённых Python-файлов чисты. Проверки выполнялись только с временной тестовой БД; рабочая очередь и браузер не использовались.

- База: 126 passed для worker и CLI/MCP parity/server, исходные гигиена/ссылки/docs/council checks зелёные.
- Для новой реализации обязательны реальные subprocess CLI и MCP stdio проверки офлайн, а не только прямые вызовы обработчиков.
- Браузерная интеграция и генерация должны отмечаться отдельно от локальных проверок. Поведение сайта не утверждается по unit-тестам.

- Scoped команда: `.venv/Scripts/python.exe -m pytest -q tests/bridge tests/features/test_extension_bridge_steps.py --cov=gflow_cli.bridge --cov-report=term-missing --cov-fail-under=80 --basetemp=tmp/pytest-bridge-writer` — 44 passed, 87.73%.
- Реальные subprocess CLI и MCP stdio проходят enqueue/claim/prepare/replay, finish с несоответствием kind и успешный finish, persistent halt. Отдельно проверены multiprocess claim race, crash после commit, commit fault и import/network isolation.
- Scoped ruff check и pyright с явным `.venv/Scripts/python.exe` interpreter: чисто. Репозиторные gates и независимый review ведёт координатор; генерация не разрешена и не тестировалась.

- Итог координатора по замороженному дереву: 170 passed in 36.46s (bridge + BDD + worker + upstream MCP parity/server), bridge coverage 87.73% при пороге 80%. Репозиторные ruff check/format чисты (541 файл), whole-src pyright: 0 ошибок. Гигиена, ссылки, PII, зеркало docs и council memory пройдены.

## Definition of done

Локальная контрольная поверхность реализована и проверена, ограничения описаны, персональные настройки отсутствуют в Git, общий halt/cooldown не обходится сменой job kind или CLI/MCP. Live генерация остаётся непроверенной до отдельного поручения пользователя.
