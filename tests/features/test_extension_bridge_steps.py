"""Offline BDD for durable local coordination, without a browser stand-in."""

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.bridge.service import BridgeError, BridgeService, Settings

scenarios("extension_bridge.feature")


@given("an extension bridge job may have been submitted", target_fixture="bridge_context")
def prepared_job(tmp_path):
    service = BridgeService(Settings(tmp_path / "bridge.sqlite", "Test Profile"))
    job = service.enqueue("video", "one", "a" * 64)
    claim = service.claim("Test Profile", "controller")
    service.prepare_submit(job["id"], claim["claim_token"], "Test Profile")
    service.enqueue("upscale", "two", "b" * 64)
    return {"service": service, "job_id": job["id"], "token": claim["claim_token"]}


@when("the extension bridge restarts")
def restart(bridge_context):
    bridge_context["service"] = BridgeService(bridge_context["service"].settings)


@when("the extension bridge records an unknown outcome")
def unknown(bridge_context):
    bridge_context["service"].unknown(bridge_context["job_id"], bridge_context["token"])


@when("the extension bridge observes unusual activity")
def unusual(bridge_context):
    bridge_context["service"].halt("unusual_activity")


@then("the extension bridge denies another submit permit")
def no_replay(bridge_context):
    with pytest.raises(BridgeError, match="already_attempted"):
        bridge_context["service"].prepare_submit(
            bridge_context["job_id"], bridge_context["token"], "Test Profile"
        )


@then("generation and upscale remain stopped")
def stopped(bridge_context):
    with pytest.raises(BridgeError, match="halted"):
        bridge_context["service"].claim("Test Profile", "new-controller")
    assert bridge_context["service"].list_jobs()["jobs"][1]["state"] == "pending"
