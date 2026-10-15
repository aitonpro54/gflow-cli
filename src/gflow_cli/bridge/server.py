"""Isolated MCP registry; no upstream tools, HTTP transport, auth or browser runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from gflow_cli.bridge.service import JSON, BridgeError, BridgeService

if TYPE_CHECKING:
    from mcp.server import MCPServer


def create_server(service: BridgeService) -> MCPServer:
    from mcp.server import MCPServer

    server = MCPServer(
        "gflow-bridge",
        version="0.1.0",
        instructions=(
            "Локальный координатор; браузером управляет агент через официальное расширение. "
            "После prepare_submit разрешён один submit. Потерянный permit не повторяется. "
            "При отказе Flow вызовите halt, при неизвестном исходе unknown. "
            "Никаких автоматических повторов и восстановления через MCP."
        ),
    )

    def invoke(operation: Callable[[], JSON]) -> JSON:
        try:
            return operation()
        except BridgeError as exc:
            return {"problem": exc.problem()}

    @server.tool()
    def bridge_enqueue(kind: str, request_key: str, request_fingerprint: str) -> JSON:
        """Добавить image/video/upscale; хранить только ключ и SHA-256, без prompt/URL."""
        return invoke(lambda: service.enqueue(kind, request_key, request_fingerprint))

    @server.tool()
    def bridge_status(job_id: int | None = None) -> JSON:
        """Показать задание или общий halt/cooldown; не раскрывает claim token."""
        return invoke(lambda: service.status(job_id))

    @server.tool()
    def bridge_list(limit: int = 100) -> JSON:
        """Показать до 1000 заданий общей FIFO очереди."""
        return invoke(lambda: service.list_jobs(limit))

    @server.tool()
    def bridge_claim(observed_profile: str, controller_id: str) -> JSON:
        """Захватить одно задание после наблюдения профиля; TTL перехвата нет."""
        return invoke(lambda: service.claim(observed_profile, controller_id))

    @server.tool()
    def bridge_prepare_submit(job_id: int, claim_token: str, observed_profile: str) -> JSON:
        """Сначала durable attempted, затем одно разрешение submit. Replay отклоняется."""
        return invoke(lambda: service.prepare_submit(job_id, claim_token, observed_profile))

    @server.tool()
    def bridge_record_started(job_id: int, claim_token: str, media_id: str) -> JSON:
        """Сохранить наблюдаемый ID результата, без signed URL и ответов сервера."""
        return invoke(lambda: service.record_started(job_id, claim_token, media_id))

    @server.tool()
    def bridge_finish(job_id: int, claim_token: str, artifact_path: str, sha256: str) -> JSON:
        """Проверить hash/базовый формат файла и сохранить паузу один раз; halt остаётся."""
        return invoke(lambda: service.finish(job_id, claim_token, artifact_path, sha256))

    @server.tool()
    def bridge_unknown(job_id: int, claim_token: str) -> JSON:
        """Неизвестный исход после возможного submit: durable halt без автоматического повтора."""
        return invoke(lambda: service.unknown(job_id, claim_token))

    @server.tool()
    def bridge_halt(signal: str) -> JSON:
        """Остановить при flow_403, flow_429, captcha или unusual_activity, даже при HTTP 200."""
        return invoke(lambda: service.halt(signal))

    return server
