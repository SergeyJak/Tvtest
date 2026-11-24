#!/usr/bin/env python3
"""Private, persistent IPTV viewing estimates and fault telemetry.

All writes happen in a bounded queue on a separate SQLite worker. Tracking is
opt-in for the gateway (--telemetry-db); the database contains neither upstream
URLs nor access tokens nor raw client IPs/user agents.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SESSION_GAP_SECONDS = 90
MAX_CREDIT_SECONDS = 30
RETENTION_DAYS = 30
MAX_EVENTS = 100_000
QUEUE_SIZE = 8192

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewing_sessions (
    id INTEGER PRIMARY KEY,
    viewer TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    estimated_seconds INTEGER NOT NULL DEFAULT 0,
    segment_requests INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS viewing_recent ON viewing_sessions(last_seen DESC);
CREATE INDEX IF NOT EXISTS viewing_identity ON viewing_sessions(viewer, channel_id, last_seen DESC);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    occurred_at INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS incident_channel_time ON incidents(channel_id, occurred_at);
CREATE INDEX IF NOT EXISTS viewing_channel_time ON viewing_sessions(channel_id, last_seen);
CREATE INDEX IF NOT EXISTS incident_recent ON incidents(occurred_at DESC);
"""
KINDS = {"upstream_error", "segment_slow", "stream_stalled",
         "stream_recovered", "client_error", "startup_timeout"}


def prepare_database(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Telemetry directory must not be accessible by other users")
    with sqlite3.connect(path, timeout=5) as db:
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=3000")
    os.chmod(path, 0o600)


def _read_or_create_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(secrets.token_bytes(32))
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Telemetry identity salt must be a private regular file")
    secret = path.read_bytes()
    if len(secret) != 32:
        raise ValueError("Invalid telemetry identity salt")
    return secret


def viewing_slot(channel: str) -> int:
    if channel.startswith("p2:"):
        return 2
    return 3 if channel.startswith("p3:") else 1


def query_viewing(path: Path, days: int = 7, slot: int = 0, channel: str = "",
                  session: int = 0, page: int = 0, rank_page: int | None = None,
                  recent_page: int | None = None) -> dict:
    if days not in (1, 7, 30) or slot not in (0, 1, 2, 3):
        raise ValueError("Unsupported history filter")
    if (not 0 <= session < 2**63 or not 0 <= page <= MAX_EVENTS or len(channel) > 128
            or ((session or page) and not channel)
            or (channel and slot and viewing_slot(channel) != slot)):
        raise ValueError("Invalid event selection")
    if any(p is not None and not 0 <= p <= MAX_EVENTS for p in (rank_page, recent_page)):
        raise ValueError("Invalid history page")
    empty = {"tracking_available": False, "days": days, "total_seconds": 0,
             "total_sessions": 0, "unique_channels": 0, "channels": [], "sessions": []}
    if not path.is_file():
        return empty
    params = {"cutoff": int(time.time()) - days * 86400, "prefix": f"p{slot}:*",
              "channel": channel, "session": session, "offset": page * 50,
              "rank_limit": 100 if rank_page is None else 20, "rank_offset": (rank_page or 0) * 20,
              "recent_limit": 100 if recent_page is None else 12, "recent_offset": (recent_page or 0) * 12}
    where = "last_seen >= :cutoff"
    if slot == 1:
        where += " AND channel_id NOT GLOB 'p[23]:*'"
    elif slot:
        where += " AND channel_id GLOB :prefix"
    # Several devices can watch the same stream during overlapping intervals.
    # Count each incident once in the ranking, and once per matching session.
    matches = """FROM viewing_sessions v JOIN incidents i ON i.channel_id = v.channel_id
                 AND i.occurred_at BETWEEN MAX(v.started_at, :cutoff) AND v.last_seen
                 WHERE v.last_seen >= :cutoff AND v.channel_id = """
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=3) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            by_channel = [dict(row) for row in db.execute(f"""
                WITH ranked AS (
                    SELECT channel_id, SUM(estimated_seconds) AS seconds, COUNT(*) AS sessions,
                           SUM(segment_requests) AS requests, MAX(last_seen) AS last_seen
                    FROM viewing_sessions WHERE {where} GROUP BY channel_id
                    ORDER BY seconds DESC, last_seen DESC, channel_id LIMIT :rank_limit OFFSET :rank_offset
                )
                SELECT r.*, (SELECT COUNT(DISTINCT i.id) {matches} r.channel_id) AS event_count
                FROM ranked r ORDER BY seconds DESC, last_seen DESC, channel_id""", params)]
            sessions = [dict(row) for row in db.execute(f"""
                SELECT id, channel_id, viewer, started_at, last_seen, estimated_seconds AS seconds,
                       segment_requests AS requests, (SELECT COUNT(*) FROM incidents i
                       WHERE i.channel_id = v.channel_id AND i.occurred_at BETWEEN
                       MAX(v.started_at, :cutoff) AND v.last_seen) AS event_count
                FROM viewing_sessions v WHERE {where} ORDER BY last_seen DESC, id DESC LIMIT :recent_limit OFFSET :recent_offset""", params)]
            count = db.execute(f"""
                SELECT COALESCE(SUM(estimated_seconds), 0), COUNT(*), COUNT(DISTINCT channel_id)
                FROM viewing_sessions WHERE {where}""", params).fetchone()
            data = {"tracking_available": True, "days": days, "total_seconds": count[0],
                    "total_sessions": count[1], "unique_channels": count[2],
                    "channels": by_channel, "sessions": sessions}
            if channel:
                scope = matches + ":channel" + (" AND v.id = :session" if session else "")
                events = [dict(row) for row in db.execute(f"""
                    SELECT DISTINCT i.id, i.occurred_at, i.kind, i.detail, i.duration_seconds
                    {scope} ORDER BY i.occurred_at DESC, i.id DESC LIMIT 51 OFFSET :offset""", params)]
                data["event_page"] = {"rows": events[:50], "more": len(events) > 50}
                if session:
                    interval = db.execute("""SELECT started_at, last_seen FROM viewing_sessions
                        WHERE channel_id=:channel AND id=:session AND last_seen>=:cutoff""", params).fetchone()
                    if interval:
                        data["event_page"]["interval"] = dict(interval)
            return data
    except sqlite3.Error:
        return empty


def query_incidents(path: Path, days: int = 7, kind: str = "all") -> dict:
    if days not in (1, 7, 30) or (kind != "all" and kind not in KINDS):
        raise ValueError("Unsupported incident filter")
    empty = {"tracking_available": False, "days": days, "kind": kind,
             "total": 0, "summary": {}, "events": []}
    if not path.is_file():
        return empty
    cutoff = int(time.time()) - days * 86400
    where = "WHERE occurred_at >= ?"
    values: list = [cutoff]
    if kind != "all":
        where += " AND kind = ?"
        values.append(kind)
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=3) as db:
            db.row_factory = sqlite3.Row
            # Summary cards describe the entire selected period, independently
            # of the event-kind table filter.
            summary = {row["kind"]: row["count"] for row in db.execute(
                "SELECT kind, COUNT(*) AS count FROM incidents "
                "WHERE occurred_at >= ? GROUP BY kind", (cutoff,))}
            filtered_total = db.execute(
                f"SELECT COUNT(*) FROM incidents {where}", values).fetchone()[0]
            events = [dict(row) for row in db.execute(
                f"""SELECT occurred_at, channel_id, kind, detail, duration_seconds
                    FROM incidents {where} ORDER BY occurred_at DESC, id DESC LIMIT 200""", values)]
            return {"tracking_available": True, "days": days, "kind": kind,
                    "total": filtered_total, "summary": summary, "events": events}
    except sqlite3.Error:
        return empty


def export_incident_rows(path: Path, days: int = 7, kind: str = "all"):
    """Iterate all matching incidents, not only 200 shown in the admin UI."""
    if days not in (1, 7, 30) or (kind != "all" and kind not in KINDS):
        raise ValueError("Unsupported incident filter")
    if not path.is_file():
        return
    cutoff = int(time.time()) - days * 86400
    where = "WHERE occurred_at >= ?"
    params: list = [cutoff]
    if kind != "all":
        where += " AND kind = ?"
        params.append(kind)
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute(
                f"""SELECT occurred_at,channel_id,kind,detail,duration_seconds
                    FROM incidents {where} ORDER BY occurred_at DESC, id DESC""", params
            ):
                yield dict(row)
    except sqlite3.Error as exc:
        raise OSError("Incident database unavailable") from None


def clear_incidents(path: Path) -> int:
    """Delete stored incidents only. Viewing sessions and privacy salt stay."""
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            db.execute("DELETE FROM incidents")
            db.commit()
            return count
    except sqlite3.Error:
        raise OSError("Incident database unavailable") from None


class Telemetry:
    def __init__(self, path: Path):
        self.path = Path(path)
        prepare_database(self.path)
        self.salt = _read_or_create_secret(self.path.with_suffix(".salt"))
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0
        self._closed = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="iptv-telemetry")
        self._worker.start()

    def viewer(self, address: str, user_agent: str) -> str:
        identity = (address[:80] + "|" + user_agent[:160]).encode("utf-8", "replace")
        return hmac.new(self.salt, identity, hashlib.sha256).hexdigest()[:16]

    def observe_segment(self, channel_id: str, viewer: str, timestamp: float | None = None) -> None:
        self._enqueue(("watch", int(time.time() if timestamp is None else timestamp),
                       channel_id, viewer, "", None))

    def incident(self, channel_id: str, kind: str, detail: str = "",
                 duration_seconds: float | None = None,
                 timestamp: float | None = None) -> None:
        if kind not in KINDS:
            return
        # Labels must be sanitized categories, never exception strings/URLs.
        sanitized = re.sub(r"[^A-Za-z0-9_ .:-]", "_", detail)[:100]
        self._enqueue(("incident", int(time.time() if timestamp is None else timestamp),
                       channel_id, kind, sanitized, duration_seconds))

    def _enqueue(self, item: tuple) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("PRAGMA synchronous=NORMAL")
            last_cleanup = time.monotonic()
            while True:
                item = self.queue.get()
                try:
                    if item is None:
                        break
                    typ, ts, cid, field, detail, duration = item
                    if typ == "watch":
                        previous = db.execute("""
                            SELECT id,last_seen FROM viewing_sessions
                            WHERE viewer=? AND channel_id=?
                            ORDER BY last_seen DESC LIMIT 1
                        """, (field, cid)).fetchone()
                        if previous and 0 <= ts - previous[1] <= SESSION_GAP_SECONDS:
                            credit = min(ts - previous[1], MAX_CREDIT_SECONDS)
                            db.execute("""
                                UPDATE viewing_sessions SET last_seen=?,
                                estimated_seconds=estimated_seconds+?,
                                segment_requests=segment_requests+1 WHERE id=?
                            """, (ts, credit, previous[0]))
                        else:
                            db.execute("""
                                INSERT INTO viewing_sessions
                                (viewer,channel_id,started_at,last_seen)
                                VALUES (?,?,?,?)
                            """, (field, cid, ts, ts))
                    else:
                        db.execute("""
                            INSERT INTO incidents
                            (occurred_at,channel_id,kind,detail,duration_seconds)
                            VALUES (?,?,?,?,?)
                        """, (ts, cid, field, detail, duration))
                    db.commit()
                    if time.monotonic() - last_cleanup > 6 * 3600:
                        cutoff = int(time.time()) - RETENTION_DAYS * 86400
                        db.execute("DELETE FROM incidents WHERE occurred_at < ?", (cutoff,))
                        db.execute("DELETE FROM viewing_sessions WHERE last_seen < ?", (cutoff,))
                        db.execute("""
                            DELETE FROM incidents WHERE id NOT IN
                            (SELECT id FROM incidents ORDER BY id DESC LIMIT ?)
                        """, (MAX_EVENTS,))
                        db.commit()
                        last_cleanup = time.monotonic()
                except sqlite3.Error:
                    db.rollback()
                    self.dropped += 1
                finally:
                    self.queue.task_done()
        finally:
            db.close()

    def flush(self) -> None:
        self.queue.join()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.queue.put(None)
            self._worker.join(timeout=10)
