"""Exercise the actual process boundaries without any browser/network calls."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gflow_cli.bridge.service import BridgeService, Settings

ROOT = Path(__file__).resolve().parents[2]
MODULE = [sys.executable, "-m", "gflow_cli.bridge"]
MP4 = (
    b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00" + b"\x00\x00\x00\x08moov" + b"\x00\x00\x00\x09mdatx"
)


@pytest.fixture
def bridge_env(tmp_path):
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "GFLOW_BRIDGE_STATE_DB": str(tmp_path / "state.sqlite"),
        "GFLOW_BRIDGE_EXPECTED_PROFILE": "Test Profile",
    }


def cli(env, *args, success=True):
    result = subprocess.run(
        [*MODULE, *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert (result.returncode == 0) is success, result.stderr + result.stdout
    return json.loads(result.stdout)


def add(env, key="one", kind="video"):
    return cli(
        env, "enqueue", "--kind", kind, "--request-key", key, "--request-fingerprint", "a" * 64
    )


def take(env):
    return cli(env, "claim", "--observed-profile", "Test Profile", "--controller-id", "controller")


def test_real_cli_lifecycle_and_operator_recovery(bridge_env):
    job = add(bridge_env)
    assert add(bridge_env)["id"] == job["id"]
    owned = take(bridge_env)
    args = ["--job-id", str(job["id"]), "--claim-token", owned["claim_token"]]
    assert cli(bridge_env, "prepare-submit", *args, "--observed-profile", "Test Profile")[
        "action_permitted"
    ]
    replay = cli(
        bridge_env, "prepare-submit", *args, "--observed-profile", "Test Profile", success=False
    )
    assert replay["retryable"] is False
    cli(bridge_env, "unknown", *args)
    assert cli(bridge_env, "status")["halt"] == "unknown_outcome"
    cli(
        bridge_env,
        "reconcile",
        "--job-id",
        str(job["id"]),
        "--reason",
        "Checked manually",
        success=False,
    )
    cli(
        bridge_env,
        "reconcile",
        "--job-id",
        str(job["id"]),
        "--reason",
        "Checked manually",
        "--acknowledge",
    )
    cli(bridge_env, "resume", "--reason", "Checked manually", "--acknowledge")
    assert cli(bridge_env, "list")["jobs"][0]["state"] == "abandoned"
    assert cli(bridge_env, "status")["cooldown_until"] > 0


def test_multiprocess_claim_race(bridge_env):
    first = add(bridge_env)
    add(bridge_env, "two", "upscale")
    processes = [
        subprocess.Popen(
            [
                *MODULE,
                "claim",
                "--observed-profile",
                "Test Profile",
                "--controller-id",
                f"controller-{i}",
            ],
            env=bridge_env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(6)
    ]
    results = [json.loads(process.communicate(timeout=15)[0]) for process in processes]
    winners = [result for result in results if "claim_token" in result]
    assert len(winners) == 1
    assert winners[0]["id"] == first["id"]
    assert all(result.get("detail") == "busy" for result in results if "claim_token" not in result)


def test_crash_after_commit_loses_permission_not_checkpoint(bridge_env):
    job = add(bridge_env)
    owned = take(bridge_env)
    code = """
import os, sys
from gflow_cli.bridge.service import BridgeService, Settings
service = BridgeService(Settings.from_env())
service.prepare_submit(int(sys.argv[1]), sys.argv[2], 'Test Profile')
os._exit(77)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(job["id"]), owned["claim_token"]],
        env=bridge_env,
        cwd=ROOT,
        timeout=15,
    )
    assert result.returncode == 77
    assert cli(bridge_env, "status", "--job-id", str(job["id"]))["state"] == "submit_attempted"
    refused = cli(
        bridge_env,
        "prepare-submit",
        "--job-id",
        str(job["id"]),
        "--claim-token",
        owned["claim_token"],
        "--observed-profile",
        "Test Profile",
        success=False,
    )
    assert refused["detail"] == "already_attempted"


def test_import_isolation_and_network_guard(bridge_env):
    code = """
import socket, sys
socket.socket.connect = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network forbidden'))
socket.create_connection = socket.socket.connect
from gflow_cli.bridge.cli import main
from gflow_cli.bridge.service import BridgeService, Settings
from gflow_cli.bridge.server import create_server
create_server(BridgeService(Settings.from_env()))
forbidden = ('gflow_cli.api', 'gflow_cli.auth', 'gflow_cli.mcp', 'playwright', 'browser_cookie3')
for name in sys.modules:
    assert not name.startswith(forbidden), name
main(['status'])
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=bridge_env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["active"] is None


def test_missing_configuration_and_profile_pin(bridge_env):
    missing = {
        key: value for key, value in bridge_env.items() if key != "GFLOW_BRIDGE_EXPECTED_PROFILE"
    }
    assert cli(missing, "status", success=False)["detail"] == "expected_profile_required"
    add(bridge_env)
    changed = {**bridge_env, "GFLOW_BRIDGE_EXPECTED_PROFILE": "Other Profile"}
    assert cli(changed, "status", success=False)["detail"] == "configured_profile_changed"


def test_restart_cooldown_blocks_cli_and_mcp_gate(bridge_env, tmp_path):
    service = BridgeService(Settings(Path(bridge_env["GFLOW_BRIDGE_STATE_DB"]), "Test Profile"))
    job = service.enqueue("video", "one", "a" * 64)
    owned = service.claim("Test Profile", "controller")
    service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    artifact = tmp_path / "result.mp4"
    artifact.write_bytes(
        b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        + b"\x00\x00\x00\x08moov"
        + b"\x00\x00\x00\x09mdatx"
    )
    result = service.finish(
        job["id"],
        owned["claim_token"],
        str(artifact),
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    add(bridge_env, "two", "upscale")
    busy = cli(
        bridge_env,
        "claim",
        "--observed-profile",
        "Test Profile",
        "--controller-id",
        "next",
        success=False,
    )
    assert busy["detail"] == "cooldown"
    assert busy["retry_after"] > 0
    assert cli(bridge_env, "status")["cooldown_until"] == result["cooldown_until"]


def test_actual_mcp_stdio_registry_and_cli_shared_state(bridge_env, tmp_path, valid_png):
    async def run():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "gflow_cli.bridge", "mcp"], cwd=ROOT, env=bridge_env
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams, read_timeout_seconds=15) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                assert names == {
                    "bridge_enqueue",
                    "bridge_status",
                    "bridge_list",
                    "bridge_claim",
                    "bridge_prepare_submit",
                    "bridge_record_started",
                    "bridge_finish",
                    "bridge_unknown",
                    "bridge_halt",
                }
                for tool in listing.tools:
                    assert not {"database", "profile", "transport"} & set(
                        tool.input_schema.get("properties", {})
                    )

                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.is_error
                    return json.loads(result.content[0].text)

                job = await call(
                    "bridge_enqueue",
                    {"kind": "video", "request_key": "one", "request_fingerprint": "a" * 64},
                )
                assert cli(bridge_env, "status", "--job-id", str(job["id"]))["kind"] == "video"
                owned = await call(
                    "bridge_claim", {"observed_profile": "Test Profile", "controller_id": "mcp"}
                )
                args = {
                    "job_id": job["id"],
                    "claim_token": owned["claim_token"],
                    "observed_profile": "Test Profile",
                }
                assert (await call("bridge_prepare_submit", args))["action_permitted"] is True
                assert (await call("bridge_prepare_submit", args))["problem"][
                    "detail"
                ] == "already_attempted"
                owned_args = {key: args[key] for key in ("job_id", "claim_token")}
                wrong_file = tmp_path / "wrong.png"
                wrong_file.write_bytes(valid_png)
                mismatch = await call(
                    "bridge_finish",
                    {
                        **owned_args,
                        "artifact_path": str(wrong_file),
                        "sha256": hashlib.sha256(valid_png).hexdigest(),
                    },
                )
                assert mismatch["problem"]["detail"] == "artifact_kind_mismatch"
                await call("bridge_record_started", {**owned_args, "media_id": "media-1"})
                await call("bridge_halt", {"signal": "unusual_activity"})
                assert (await call("bridge_prepare_submit", args))["problem"]["retryable"] is False
                await call("bridge_unknown", owned_args)
                artifact = tmp_path / "result.mp4"
                artifact.write_bytes(MP4)
                finish_args = {
                    **owned_args,
                    "artifact_path": str(artifact),
                    "sha256": hashlib.sha256(MP4).hexdigest(),
                }
                receipt = await call("bridge_finish", finish_args)
                assert receipt["state"] == "completed"
                assert await call("bridge_finish", finish_args) == receipt
                assert (await call("bridge_status", {}))["halt"] == "unusual_activity"
                assert len((await call("bridge_list", {}))["jobs"]) == 1
        assert cli(bridge_env, "status")["halt"] == "unusual_activity"

    asyncio.run(run())


def test_cli_finish_rejects_wrong_kind_then_checks_file(bridge_env, tmp_path, valid_png):
    job = add(bridge_env, kind="image")
    owned = take(bridge_env)
    owned_args = ["--job-id", str(job["id"]), "--claim-token", owned["claim_token"]]
    cli(bridge_env, "prepare-submit", *owned_args, "--observed-profile", "Test Profile")
    cli(bridge_env, "record-started", *owned_args, "--media-id", "media-1")
    wrong_file = tmp_path / "wrong.mp4"
    wrong_file.write_bytes(MP4)
    problem = cli(
        bridge_env,
        "finish",
        *owned_args,
        "--artifact-path",
        str(wrong_file),
        "--sha256",
        hashlib.sha256(MP4).hexdigest(),
        success=False,
    )
    assert problem["detail"] == "artifact_kind_mismatch"
    artifact = tmp_path / "result.png"
    artifact.write_bytes(valid_png)
    digest = hashlib.sha256(valid_png).hexdigest()
    receipt = cli(
        bridge_env, "finish", *owned_args, "--artifact-path", str(artifact), "--sha256", digest
    )
    assert receipt["state"] == "completed"
    assert (
        cli(bridge_env, "finish", *owned_args, "--artifact-path", str(artifact), "--sha256", digest)
        == receipt
    )
    cli(bridge_env, "halt", "--signal", "flow_429")
    assert cli(bridge_env, "status")["halt"] == "flow_429"
