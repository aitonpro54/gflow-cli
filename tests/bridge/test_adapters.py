"""Adapter contract tests complement the real CLI and stdio process tests."""

import argparse
import asyncio
import hashlib
import json

import pytest

from gflow_cli.bridge.cli import main, parser
from gflow_cli.bridge.server import create_server
from gflow_cli.bridge.service import BridgeService, Settings


def test_cli_configuration_and_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GFLOW_BRIDGE_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("GFLOW_BRIDGE_EXPECTED_PROFILE", "Test Profile")
    main(["enqueue", "--kind", "image", "--request-key", "one", "--request-fingerprint", "a" * 64])
    assert json.loads(capsys.readouterr().out)["state"] == "pending"
    main(["status"])
    assert json.loads(capsys.readouterr().out)["active"] is None
    with pytest.raises(SystemExit) as error:
        main(
            [
                "enqueue",
                "--kind",
                "image",
                "--request-key",
                "one",
                "--request-fingerprint",
                "b" * 64,
            ]
        )
    assert error.value.code == 16
    assert json.loads(capsys.readouterr().out)["detail"] == "idempotency_conflict"
    with pytest.raises(SystemExit) as error:
        main(["status", "--database", str(tmp_path / "other.sqlite")])
    assert error.value.code == 11
    assert json.loads(capsys.readouterr().out)["retryable"] is False


def test_cli_invalid_state_is_state_failure(tmp_path, monkeypatch, capsys):
    database = tmp_path / "state.sqlite"
    monkeypatch.setenv("GFLOW_BRIDGE_STATE_DB", str(database))
    monkeypatch.setenv("GFLOW_BRIDGE_EXPECTED_PROFILE", "Test Profile")
    service = BridgeService(Settings(database, "Test Profile"))
    job = service.enqueue("video", "one", "a" * 64)
    owned = service.claim("Test Profile", "controller")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "record-started",
                "--job-id",
                str(job["id"]),
                "--claim-token",
                owned["claim_token"],
                "--media-id",
                "media-1",
            ]
        )
    assert error.value.code == 16
    problem = json.loads(capsys.readouterr().out)
    assert problem["detail"] == "invalid_state"
    assert problem["retryable"] is False
    assert service.status(job["id"])["state"] == "claimed"


def test_cli_mcp_parameters_and_operator_exemptions(tmp_path):
    async def run():
        service = BridgeService(Settings(tmp_path / "state.sqlite", "Test Profile"))
        server = create_server(service)
        tools = {tool.name: tool for tool in await server.list_tools()}
        subcommands = next(
            action for action in parser()._actions if isinstance(action, argparse._SubParsersAction)
        ).choices
        exemptions = {"reconcile", "resume", "mcp"}
        assert {name.replace("bridge_", "").replace("_", "-") for name in tools} == set(
            subcommands
        ) - exemptions
        for name, command in subcommands.items():
            if name in exemptions:
                continue
            cli_args = {action.dest for action in command._actions if action.dest != "help"}
            tool = tools["bridge_" + name.replace("-", "_")]
            assert cli_args == set(tool.input_schema["properties"])

    asyncio.run(run())


def test_mcp_adapter_roundtrip_and_failures(tmp_path, valid_png):
    async def run():
        service = BridgeService(Settings(tmp_path / "state.sqlite", "Test Profile"))
        server = create_server(service)

        async def call(name, **args):
            result = await server.call_tool("bridge_" + name, args)
            return json.loads(result.content[0].text)

        job = await call("enqueue", kind="image", request_key="one", request_fingerprint="a" * 64)
        assert (await call("status", job_id=job["id"]))["state"] == "pending"
        assert len((await call("list", limit=1))["jobs"]) == 1
        assert (await call("claim", observed_profile="Other Profile", controller_id="mcp"))[
            "problem"
        ]["retryable"] is False
        owned = await call("claim", observed_profile="Test Profile", controller_id="mcp")
        args = {"job_id": job["id"], "claim_token": owned["claim_token"]}
        await call("prepare_submit", **args, observed_profile="Test Profile")
        await call("record_started", **args, media_id="media-1")
        await call("unknown", **args)
        await call("halt", signal="flow_403")
        artifact = tmp_path / "result.png"
        artifact.write_bytes(valid_png)
        result = await call(
            "finish",
            **args,
            artifact_path=str(artifact),
            sha256=hashlib.sha256(valid_png).hexdigest(),
        )
        assert result["state"] == "completed"
        state = await call("status")
        assert state["halt"] == "unknown_outcome"
        assert state["cooldown_until"] > result["finished_at"]

    asyncio.run(run())
