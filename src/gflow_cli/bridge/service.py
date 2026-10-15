"""Fail-closed SQLite coordinator; this module never opens or controls a browser."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gflow_cli.bridge.paths import local_path

JSON = dict[str, Any]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
SIGNALS = {"flow_403", "flow_429", "captcha", "unusual_activity"}
ACTIVE = ("claimed", "submit_attempted", "started", "unknown")
APP_ID = 0x47464231


class BridgeError(Exception):
    """Local RFC 9457 failure; attention failures are never automatic retries."""

    def __init__(self, code: str, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after

    def problem(self) -> JSON:
        result: JSON = {
            "type": f"urn:gflow-bridge:{self.code}",
            "title": "Операция bridge отклонена",
            "status": 409,
            "detail": self.code,
            "instance": "gflow-bridge",
            "retryable": False,
            "remediation_hint": "Проверьте status и инструкцию docs/BRIDGE.md.",
        }
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        return result


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise BridgeError("invalid_identifier")
    return value


def _digest(value: str) -> str:
    if not DIGEST.fullmatch(value):
        raise BridgeError("invalid_sha256")
    return value


def _reason(value: str, acknowledge: bool) -> None:
    if not acknowledge or not 3 <= len(value.strip()) <= 256:
        raise BridgeError("operator_acknowledgement_required")
    if any(ord(c) < 32 for c in value) or "://" in value:
        raise BridgeError("invalid_reason")


@dataclass(frozen=True)
class Settings:
    """Process configuration only; no per-command database/profile overrides."""

    database: Path
    expected_profile: str
    cooldown_min: int = 25
    cooldown_max: int = 70

    def __post_init__(self) -> None:
        try:
            local_path(self.database)
        except (ValueError, OSError) as exc:
            raise BridgeError("local_state_db_required") from exc
        if not self.database.is_absolute():
            raise BridgeError("absolute_state_db_required")
        if not self.expected_profile.strip() or len(self.expected_profile) > 256:
            raise BridgeError("expected_profile_required")
        if not 25 <= self.cooldown_min <= self.cooldown_max <= 3600:
            raise BridgeError("invalid_cooldown")

    @classmethod
    def from_env(cls) -> Settings:
        database = os.environ.get("GFLOW_BRIDGE_STATE_DB", "")
        profile = os.environ.get("GFLOW_BRIDGE_EXPECTED_PROFILE", "")
        if not database:
            raise BridgeError("absolute_state_db_required")
        try:
            return cls(
                Path(database),
                profile,
                int(os.environ.get("GFLOW_BRIDGE_COOLDOWN_MIN", "25")),
                int(os.environ.get("GFLOW_BRIDGE_COOLDOWN_MAX", "70")),
            )
        except ValueError as exc:
            raise BridgeError("invalid_cooldown") from exc


class BridgeService:
    """One service for CLI and MCP, sharing a separately versioned local store."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clock: Callable[[], float] = time.time
        self.database = local_path(settings.database)
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BridgeError("storage") from exc
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if (version, application) == (0, 0) and not tables:
                db.execute("""CREATE TABLE bridge_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1), profile_hash TEXT NOT NULL,
                    halt TEXT, cooldown_until REAL NOT NULL DEFAULT 0,
                    operator_reason TEXT)""")
                db.execute("""CREATE TABLE bridge_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    request_key TEXT UNIQUE NOT NULL, request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
                    controller_id TEXT, token_hash TEXT, attempted_at REAL,
                    media_id TEXT, artifact_path TEXT, sha256 TEXT,
                    finished_at REAL, cooldown_until REAL, operator_reason TEXT)""")
                db.execute(
                    "INSERT INTO bridge_meta(id,profile_hash) VALUES(1,?)", (self._profile_hash(),)
                )
                db.execute(f"PRAGMA application_id={APP_ID}")
                db.execute("PRAGMA user_version=1")
            elif (version, application) != (1, APP_ID):
                raise BridgeError("incompatible_store")
            row = db.execute("SELECT profile_hash FROM bridge_meta WHERE id=1").fetchone()
            if row is None or row[0] != self._profile_hash():
                raise BridgeError("configured_profile_changed")

    def _profile_hash(self) -> str:
        return hashlib.sha256(self.settings.expected_profile.encode("utf-8")).hexdigest()

    @staticmethod
    def _commit(db: sqlite3.Connection) -> None:
        db.commit()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            self._commit(db)
        except (sqlite3.Error, OSError) as exc:
            raise BridgeError("storage") from exc
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> JSON:
        return {key: row[key] for key in row.keys() if key != "token_hash"}

    @staticmethod
    def _job(db: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM bridge_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise BridgeError("job_not_found")
        return row

    def _owned(self, db: sqlite3.Connection, job_id: int, token: str) -> sqlite3.Row:
        row = self._job(db, job_id)
        if not row["token_hash"] or not secrets.compare_digest(
            row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise BridgeError("ownership")
        return row

    def _profile(self, observed_profile: str) -> None:
        if observed_profile != self.settings.expected_profile:
            raise BridgeError("profile_mismatch")

    def _gate(self, db: sqlite3.Connection) -> None:
        meta = db.execute("SELECT * FROM bridge_meta WHERE id=1").fetchone()
        if meta["halt"]:
            raise BridgeError("halted")
        remaining = meta["cooldown_until"] - self.clock()
        if remaining > 0:
            raise BridgeError("cooldown", retry_after=remaining)

    def enqueue(self, kind: str, request_key: str, request_fingerprint: str) -> JSON:
        """Store identifiers and a SHA-256 fingerprint, never the generation payload."""
        if kind not in {"image", "video", "upscale"}:
            raise BridgeError("invalid_kind")
        _identifier(request_key)
        _digest(request_fingerprint)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM bridge_jobs WHERE request_key=?", (request_key,)
            ).fetchone()
            if row:
                if (row["kind"], row["request_fingerprint"]) != (kind, request_fingerprint):
                    raise BridgeError("idempotency_conflict")
                result = self._public(row)
            else:
                cursor = db.execute(
                    """INSERT INTO bridge_jobs
                    (kind,request_key,request_fingerprint,created_at) VALUES(?,?,?,?)""",
                    (kind, request_key, request_fingerprint, self.clock()),
                )
                assert cursor.lastrowid is not None
                result = self._public(self._job(db, cursor.lastrowid))
        return result

    def status(self, job_id: int | None = None) -> JSON:
        with self._transaction() as db:
            if job_id is not None:
                result = self._public(self._job(db, job_id))
            else:
                meta = db.execute(
                    "SELECT halt,cooldown_until FROM bridge_meta WHERE id=1"
                ).fetchone()
                active = db.execute(
                    "SELECT * FROM bridge_jobs WHERE state IN (?,?,?,?)", ACTIVE
                ).fetchone()
                result = {
                    "halt": meta["halt"],
                    "cooldown_until": meta["cooldown_until"],
                    "active": self._public(active) if active else None,
                }
        return result

    def list_jobs(self, limit: int = 100) -> JSON:
        if not 1 <= limit <= 1000:
            raise BridgeError("invalid_limit")
        with self._transaction() as db:
            result = {
                "jobs": [
                    self._public(row)
                    for row in db.execute("SELECT * FROM bridge_jobs ORDER BY id LIMIT ?", (limit,))
                ]
            }
        return result

    def claim(self, observed_profile: str, controller_id: str) -> JSON:
        self._profile(observed_profile)
        _identifier(controller_id)
        with self._transaction() as db:
            self._gate(db)
            if db.execute("SELECT id FROM bridge_jobs WHERE state IN (?,?,?,?)", ACTIVE).fetchone():
                raise BridgeError("busy")
            row = db.execute(
                "SELECT id FROM bridge_jobs WHERE state='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                raise BridgeError("empty")
            token = secrets.token_urlsafe(32)
            db.execute(
                "UPDATE bridge_jobs SET state='claimed',controller_id=?,token_hash=? WHERE id=?",
                (controller_id, hashlib.sha256(token.encode()).hexdigest(), row["id"]),
            )
            result = self._public(self._job(db, row["id"]))
            result["claim_token"] = token
        return result

    def prepare_submit(self, job_id: int, claim_token: str, observed_profile: str) -> JSON:
        """Commit attempted BEFORE returning one permit; never replay a lost permit."""
        self._profile(observed_profile)
        with self._transaction() as db:
            self._gate(db)
            row = self._owned(db, job_id, claim_token)
            if row["state"] != "claimed":
                raise BridgeError("already_attempted")
            db.execute(
                "UPDATE bridge_jobs SET state='submit_attempted',attempted_at=? WHERE id=?",
                (self.clock(), job_id),
            )
        return {
            "id": job_id,
            "action_permitted": True,
            "instruction": "Один submit через расширение. Потерянный ответ не повторять.",
        }

    def record_started(self, job_id: int, claim_token: str, media_id: str) -> JSON:
        _identifier(media_id)
        with self._transaction() as db:
            row = self._owned(db, job_id, claim_token)
            if row["state"] not in {"submit_attempted", "started"}:
                raise BridgeError("invalid_state")
            if row["media_id"] is not None and row["media_id"] != media_id:
                raise BridgeError("media_conflict")
            db.execute(
                "UPDATE bridge_jobs SET state='started',media_id=? WHERE id=?", (media_id, job_id)
            )
            result = self._public(self._job(db, job_id))
        return result

    def unknown(self, job_id: int, claim_token: str) -> JSON:
        with self._transaction() as db:
            row = self._owned(db, job_id, claim_token)
            if row["state"] not in {"submit_attempted", "started", "unknown"}:
                raise BridgeError("invalid_state")
            db.execute("UPDATE bridge_jobs SET state='unknown' WHERE id=?", (job_id,))
            db.execute("UPDATE bridge_meta SET halt=COALESCE(halt,'unknown_outcome') WHERE id=1")
            result = self._public(self._job(db, job_id))
        return result

    def halt(self, signal: str) -> JSON:
        if signal not in SIGNALS:
            raise BridgeError("invalid_signal")
        with self._transaction() as db:
            db.execute("UPDATE bridge_meta SET halt=COALESCE(halt,?) WHERE id=1", (signal,))
        return self.status()

    def finish(self, job_id: int, claim_token: str, artifact_path: str, sha256: str) -> JSON:
        _digest(sha256)
        path = Path(artifact_path)
        if not path.is_absolute():
            raise BridgeError("absolute_artifact_path_required")
        try:
            path = local_path(path)
        except (ValueError, OSError) as exc:
            raise BridgeError("local_artifact_path_required") from exc
        # Check ownership/state before costly I/O; recheck under the conditional commit.
        with self._transaction() as db:
            row = self._owned(db, job_id, claim_token)
            if row["state"] == "completed":
                return self._same_finish(row, path, sha256)
            if row["state"] not in {"submit_attempted", "started", "unknown"}:
                raise BridgeError("invalid_state")
        from gflow_cli.bridge.artifact import verify_artifact

        verify_artifact(path, sha256, row["kind"])
        with self._transaction() as db:
            row = self._owned(db, job_id, claim_token)
            if row["state"] == "completed":
                return self._same_finish(row, path, sha256)
            if row["state"] not in {"submit_attempted", "started", "unknown"}:
                raise BridgeError("invalid_state")
            finished = self.clock()
            until = finished + secrets.SystemRandom().randint(
                self.settings.cooldown_min, self.settings.cooldown_max
            )
            db.execute(
                """UPDATE bridge_jobs SET state='completed',artifact_path=?,sha256=?,
                finished_at=?,cooldown_until=? WHERE id=?""",
                (str(path), sha256, finished, until, job_id),
            )
            db.execute("UPDATE bridge_meta SET cooldown_until=? WHERE id=1", (until,))
            result = self._public(self._job(db, job_id))
        return result

    def _same_finish(self, row: sqlite3.Row, path: Path, sha256: str) -> JSON:
        if row["artifact_path"] != str(path) or row["sha256"] != sha256:
            raise BridgeError("finish_conflict")
        return self._public(row)

    def reconcile(self, job_id: int, reason: str, acknowledge: bool) -> JSON:
        """CLI-only operator closes an active job without ever requeuing it."""
        _reason(reason, acknowledge)
        with self._transaction() as db:
            row = self._job(db, job_id)
            if row["state"] not in ACTIVE:
                raise BridgeError("invalid_state")
            db.execute(
                "UPDATE bridge_jobs SET state='abandoned',operator_reason=? WHERE id=?",
                (reason, job_id),
            )
            # A possibly submitted job still requires a full conservative cooldown.
            if row["attempted_at"] is not None:
                until = self.clock() + self.settings.cooldown_max
                db.execute(
                    "UPDATE bridge_meta SET cooldown_until=MAX(cooldown_until,?) WHERE id=1",
                    (until,),
                )
            result = self._public(self._job(db, job_id))
        return result

    def resume(self, reason: str, acknowledge: bool) -> JSON:
        """CLI-only acknowledgement; does not erase cooldown or reopen any job."""
        _reason(reason, acknowledge)
        with self._transaction() as db:
            if db.execute("SELECT id FROM bridge_jobs WHERE state IN (?,?,?,?)", ACTIVE).fetchone():
                raise BridgeError("active_job_requires_reconciliation")
            db.execute("UPDATE bridge_meta SET halt=NULL,operator_reason=? WHERE id=1", (reason,))
        return self.status()
