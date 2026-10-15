# Сценарии extension bridge

## Coverage map

D1: выбор ожидаемого профиля без чтения авторизации. D2: persistent halt. D3: DOM-селекторы не добавляются. D4–D7: идемпотентность, процессы, durable boundary, ошибки. D8: Windows и Unicode пути. D9: браузерный transport отсутствует. D10: bridge не запускает окно. D11–D13: валидация, безопасные ответы, CLI/MCP parity.

| Сценарий | Severity | Ожидаемое поведение | Проверка |
|---|---|---|---|
| Два процесса claim generation/upscale | Critical | Один active job, второй получает занято | Integration, процессы |
| Повтор enqueue с тем же ключом | High | То же задание; изменённые параметры отклонены | Unit/CLI/MCP |
| Повтор pre-submit gate | Critical | Нового permit нет | BDD/CLI/MCP |
| Сбой commit перед gate | Critical | Разрешение нажать кнопку не выдано | Unit fault injection |
| Перезапуск после gate до ответа | Critical | Возможная отправка не повторяется, требует разбора | Integration |
| Падение после принятия Flow | Critical | Unknown блокирует новые задания | BDD |
| Пауза после длительной генерации | High | Полная пауза от завершения, записана один раз | Unit fake clock |
| Перезапуск во время cooldown | High | Сохраняется прежнее время разрешения | Integration |
| CAPTCHA/unusual/403/429 | Critical | Сохраняется halt, новые claim и gate запрещены | BDD/CLI/MCP |
| Unusual activity с HTTP 200 | High | Сигнал UI останавливает сценарий независимо от статуса HTTP | Unit |
| Поздний finish после halt | Critical | Halt не снимается | Unit |
| Неизвестный исход и истекшая пауза | Critical | Очередь остаётся остановленной | Unit |
| Недописанный/HTML файл вместо MP4 | High | Completed не ставится, повторной генерации нет | Integration |
| Повтор finish | High | Пауза не пересчитывается, результат прежний | Unit |
| Профиль не настроен/не совпадает | High | Claim/gate запрещены | CLI/MCP |
| Импорт/старт отдельного entrypoint | Critical | Нет auth/client/Playwright/cookie imports и network calls | Subprocess |
| Список MCP tools | High | Только bridge tools, без upstream генерации/auth | Настоящий stdio MCP |
| Явное операторское восстановление | Critical | Неизвестное задание не возвращается в pending, halt не снимается обычным tool call | Unit/CLI |

## BDD

Офлайн-сценарии проверяют только наш контрольный код. Реальный submit в браузере требует отдельного поручения пользователя и не подменяется mocked browser.

```gherkin
Feature: Durable coordination through an existing Chrome extension
  Scenario: Lost acknowledgement cannot authorize a second submit
    Given a claimed job
    When the pre-submit checkpoint is committed and its acknowledgement is lost
    Then the next gate denies a new submit permit
  Scenario: An unknown outcome blocks the next job
    Given a job may have been submitted
    When its outcome becomes unknown
    Then the next generation and upscale remain blocked after restart
  Scenario: A Flow refusal survives restart
    Given Flow reports unusual activity
    When the bridge restarts
    Then no new browser action is authorized
```

## Known issues

Upstream `KNOWN_ISSUES.md` описывает HTTP 200 с отказом в теле, нестабильный browser lifecycle и ошибки скачивания после принятой генерации. Bridge не исправляет эти поверхности Flow; он исключает автоматический повтор и сохраняет различие между неизвестным исходом и завершённым локальным файлом.
