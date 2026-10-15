"""Standalone JSON CLI. Operator recovery is deliberately unavailable through MCP."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, NoReturn

from gflow_cli.bridge.service import BridgeError, BridgeService, Settings


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise BridgeError("invalid_arguments")


def parser() -> argparse.ArgumentParser:
    root = Parser(
        prog="gflow-bridge", description="Локальная очередь для официального расширения Chrome."
    )
    commands = root.add_subparsers(dest="command", required=True, title="Команды")
    definitions: dict[str, tuple[str, list[tuple[str, Any, Any]]]] = {
        "enqueue": (
            "Добавить только идентификаторы и fingerprint задания.",
            [("kind", str, None), ("request-key", str, None), ("request-fingerprint", str, None)],
        ),
        "status": ("Показать состояние и остановку.", [("job-id", int, None)]),
        "list": ("Показать FIFO очередь.", [("limit", int, 100)]),
        "claim": (
            "Захватить следующее задание без срока автоматического перехвата.",
            [("observed-profile", str, None), ("controller-id", str, None)],
        ),
        "prepare-submit": (
            "Записать attempted и один раз разрешить submit.",
            [("job-id", int, None), ("claim-token", str, None), ("observed-profile", str, None)],
        ),
        "record-started": (
            "Записать наблюдаемый media ID без URL.",
            [("job-id", int, None), ("claim-token", str, None), ("media-id", str, None)],
        ),
        "finish": (
            "Проверить файл и начать обязательную паузу.",
            [
                ("job-id", int, None),
                ("claim-token", str, None),
                ("artifact-path", str, None),
                ("sha256", str, None),
            ],
        ),
        "unknown": (
            "Зафиксировать неизвестный исход и остановить очередь.",
            [("job-id", int, None), ("claim-token", str, None)],
        ),
        "halt": (
            "Сохранить отказ Flow: flow_403/flow_429/captcha/unusual_activity.",
            [("signal", str, None)],
        ),
        "reconcile": (
            "Оператор: закрыть активное задание после ручной проверки, без повтора.",
            [("job-id", int, None), ("reason", str, None)],
        ),
        "resume": (
            "Оператор: снять остановку после проверки, сохранить cooldown.",
            [("reason", str, None)],
        ),
        "mcp": ("Запустить отдельный MCP server только через stdio.", []),
    }
    for name, (description, options) in definitions.items():
        command = commands.add_parser(name, help=description, description=description)
        for option, value_type, default in options:
            required = default is None and not (name == "status" and option == "job-id")
            command.add_argument(
                f"--{option}",
                type=value_type,
                default=default,
                required=required,
                help="Значение параметра.",
            )
        if name in {"reconcile", "resume"}:
            command.add_argument(
                "--acknowledge",
                action="store_true",
                help="Подтверждаю ручную проверку и риск возможной отправки.",
            )
    return root


def main(argv: Sequence[str] | None = None) -> None:
    try:
        args = vars(parser().parse_args(argv))
        command = args.pop("command")
        service = BridgeService(Settings.from_env())
        if command == "mcp":
            from gflow_cli.bridge.server import create_server

            create_server(service).run(transport="stdio")
            return
        methods = {
            "list": service.list_jobs,
            "prepare-submit": service.prepare_submit,
            "record-started": service.record_started,
        }
        operation = methods.get(command) or getattr(service, command.replace("-", "_"))
        result = operation(**args)
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    except BridgeError as exc:
        sys.stdout.write(json.dumps(exc.problem(), ensure_ascii=True) + "\n")
        raise SystemExit(11 if "required" in exc.code or "invalid" in exc.code else 16) from None


if __name__ == "__main__":
    main()
