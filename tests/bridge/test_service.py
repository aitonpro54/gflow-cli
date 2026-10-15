import pytest

from gflow_cli.bridge.service import BridgeError, BridgeService, Settings


@pytest.fixture
def service(tmp_path):
    return BridgeService(Settings(tmp_path / "state.sqlite", "Test Profile", 25, 70))


def enqueue(service, key="one", kind="video"):
    return service.enqueue(kind, key, "a" * 64)


def claim(service):
    return service.claim("Test Profile", "controller-1")


def test_idempotency_and_conflict(service):
    job = enqueue(service)
    assert enqueue(service) == job
    with pytest.raises(BridgeError, match="conflict"):
        service.enqueue("upscale", "one", "a" * 64)


def test_fifo_is_global_and_no_ttl_reclaim(service):
    first = enqueue(service)
    enqueue(service, "two", "upscale")
    owned = claim(service)
    assert owned["id"] == first["id"]
    service.clock = lambda: 999999999999.0
    with pytest.raises(BridgeError, match="busy"):
        claim(service)


def test_profile_and_token_gates(service):
    job = enqueue(service)
    with pytest.raises(BridgeError, match="profile"):
        service.claim("Other Profile", "controller-1")
    owned = claim(service)
    with pytest.raises(BridgeError, match="ownership"):
        service.prepare_submit(job["id"], "incorrect", "Test Profile")
    with pytest.raises(BridgeError, match="profile"):
        service.prepare_submit(job["id"], owned["claim_token"], "Other Profile")


def test_prepare_is_one_time_and_restart_safe(service):
    job = enqueue(service)
    owned = claim(service)
    permit = service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    assert permit["action_permitted"] is True
    restarted = BridgeService(service.settings)
    with pytest.raises(BridgeError, match="already_attempted"):
        restarted.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    with pytest.raises(BridgeError, match="busy"):
        claim(restarted)


def test_failed_commit_never_authorizes(service, monkeypatch):
    job = enqueue(service)
    owned = claim(service)

    def fail(_db):
        raise OSError("simulated commit failure")

    monkeypatch.setattr(service, "_commit", fail)
    with pytest.raises(BridgeError, match="storage"):
        service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    monkeypatch.undo()
    assert service.status(job["id"])["state"] == "claimed"


@pytest.mark.parametrize("signal", ["flow_403", "flow_429", "captcha", "unusual_activity"])
def test_halt_persists_and_blocks_gate(service, signal):
    job = enqueue(service)
    owned = claim(service)
    service.halt(signal)
    restarted = BridgeService(service.settings)
    with pytest.raises(BridgeError, match="halted"):
        claim(restarted)
    with pytest.raises(BridgeError, match="halted"):
        restarted.prepare_submit(job["id"], owned["claim_token"], "Test Profile")


def test_unknown_is_durable_and_never_requeued(service):
    job = enqueue(service)
    owned = claim(service)
    service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    service.unknown(job["id"], owned["claim_token"])
    restarted = BridgeService(service.settings)
    assert restarted.status(job["id"])["state"] == "unknown"
    with pytest.raises(BridgeError, match="halted"):
        claim(restarted)
    restarted.reconcile(job["id"], "Checked manually", True)
    assert restarted.status(job["id"])["state"] == "abandoned"
    restarted.resume("Checked manually", True)
    assert restarted.status()["halt"] is None


def test_artifact_failure_does_not_complete(service, tmp_path):
    job = enqueue(service)
    owned = claim(service)
    service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    artifact = tmp_path / "bad.mp4"
    artifact.write_bytes(b"<html>refused</html>")
    with pytest.raises(BridgeError, match="artifact"):
        service.finish(job["id"], owned["claim_token"], str(artifact), "b" * 64)
    assert service.status(job["id"])["state"] == "submit_attempted"


def test_finish_cooldown_once_and_late_success_keeps_halt(service, tmp_path):
    import hashlib

    job = enqueue(service)
    owned = claim(service)
    service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    service.record_started(job["id"], owned["claim_token"], "media-1")
    artifact = tmp_path / "файл с пробелом.mp4"
    artifact.write_bytes(
        b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        + b"\x00\x00\x00\x08moov"
        + b"\x00\x00\x00\x09mdatx"
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    service.clock = lambda: 100000000000.0
    service.halt("flow_429")
    completed = service.finish(job["id"], owned["claim_token"], str(artifact), digest)
    service.clock = lambda: 100000001000.0
    assert service.finish(job["id"], owned["claim_token"], str(artifact), digest) == completed
    state = BridgeService(service.settings).status()
    assert state["halt"] == "flow_429"
    assert 25 <= completed["cooldown_until"] - completed["finished_at"] <= 70
    assert "claim_token" not in service.status(job["id"])


@pytest.mark.parametrize("minimum,maximum", [(0, 70), (24, 70), (70, 25), (25, 3601)])
def test_invalid_cooldown(tmp_path, minimum, maximum):
    with pytest.raises(BridgeError):
        Settings(tmp_path / "state.sqlite", "Test Profile", minimum, maximum)


@pytest.mark.parametrize("kind,suffix", [("video", ".png"), ("image", ".mp4")])
def test_finish_rejects_wrong_kind(service, tmp_path, kind, suffix, valid_png):
    import hashlib

    job = enqueue(service, kind=kind)
    owned = claim(service)
    service.prepare_submit(job["id"], owned["claim_token"], "Test Profile")
    artifact = tmp_path / ("wrong" + suffix)
    content = (
        valid_png
        if suffix == ".png"
        else b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        + b"\x00\x00\x00\x08moov"
        + b"\x00\x00\x00\x09mdatx"
    )
    artifact.write_bytes(content)
    with pytest.raises(BridgeError, match="artifact_kind"):
        service.finish(
            job["id"], owned["claim_token"], str(artifact), hashlib.sha256(content).hexdigest()
        )
    assert service.status(job["id"])["state"] == "submit_attempted"
