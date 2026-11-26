import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import iptv_telemetry as telemetry


class StabilityTests(unittest.TestCase):
    def test_reset_preserves_history_and_ignores_queued_old_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            try:
                now = int(time.time())
                for cid in ("a", "p2:b"):
                    store.observe_segment(cid, "tv", now-10, piece="1", source=1)
                    for ts in (now-9, now-8):
                        store.incident(cid, "client_error", timestamp=ts, diagnostics={"client": "tv"})
                store.flush()
                self.assertEqual(len(telemetry.query_stability(path)["episodes"]), 2)
                reset = telemetry.reset_stability(path)
                store.observe_segment("old", "tv", reset-1, piece="1", source=1)
                store.incident("old", "client_error", timestamp=reset, diagnostics={"client": "tv"})
                store.flush()
                for days in (1, 7, 30):
                    result = telemetry.query_stability(path, days)
                    self.assertEqual(result["since"], reset)
                    for key in ("live", "channels", "episodes"):
                        self.assertEqual(result[key], [])
                self.assertEqual(telemetry.query_incidents(path)["total"], 5)
                self.assertEqual(telemetry.query_viewing(path)["total_sessions"], 3)
                store.observe_segment("new", "tv", reset+1, piece="1", source=1)
                for ts in (reset+2, reset+3):
                    store.incident("new", "client_error", timestamp=ts, diagnostics={"client": "tv"})
                store.flush()
                result = telemetry.query_stability(path)
                self.assertEqual([row["channel_id"] for row in result["channels"]], ["new"])
                self.assertEqual(result["channels"][0]["errors"], 2)
                self.assertEqual(len(result["episodes"]), 1)
            finally:
                store.close()

    def test_reset_keeps_active_segment_sequence_private_until_new_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            try:
                now = int(time.time())
                store.observe_segment("ch", "tv", now-5, piece="mux.104", source=2)
                store.flush()
                reset = telemetry.reset_stability(path)
                # Another viewer's last pre-reset segment was still queued.
                store.observe_segment("ch", "queued", reset-2, piece="mux.104", source=2)
                store.flush()
                self.assertEqual(telemetry.query_stability(path)["live"], [])
                for viewer in ("tv", "queued"):
                    for offset in (1, 2):
                        store.incident("ch", "client_error", timestamp=reset+offset,
                                       diagnostics={"client": viewer})
                    store.observe_segment("ch", viewer, reset+3, piece="mux.103", source=1)
                store.flush()
                data = telemetry.query_stability(path)
                self.assertEqual(len(data["episodes"]), 2)
                for episode in data["episodes"]:
                    self.assertIsNone(episode["recovered"])
                    self.assertEqual(episode["reason"], "requests")
                for client in data["live"]:
                    self.assertEqual(client["state"], "interrupted")
                    self.assertEqual(client["source"], 1)
                    self.assertIsNone(client["progress"])
                store.observe_segment("ch", "tv", reset+4, piece="mux.105", source=2)
                store.flush()
                recovered = {r["viewer"]: r["recovered"] for r in telemetry.query_stability(path)["episodes"]}
                self.assertEqual(recovered, {"tv": reset+4, "queued": None})
            finally:
                store.close()

    def test_reset_recovers_missing_marker_and_preserves_cutoff_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            telemetry.prepare_database(path)
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM delivery_reset")
                db.execute("""INSERT INTO incidents(occurred_at,channel_id,kind,detail)
                              VALUES(?, 'ch','client_error','ERROR')""", (int(time.time())-1,))
            self.assertEqual(telemetry.query_stability(path)["channels"][0]["errors"], 1)
            reset = telemetry.reset_stability(path)
            telemetry.prepare_database(path)
            result = telemetry.query_stability(path)
            self.assertEqual(result["since"], reset)
            self.assertEqual(result["channels"], [])
            self.assertEqual(telemetry.query_incidents(path)["total"], 1)

    def test_other_connection_cannot_see_partial_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            telemetry.prepare_database(path)
            with sqlite3.connect(path) as reader:
                reader.execute("DROP TABLE delivery_reset")
                table_count = "SELECT COUNT(*) FROM sqlite_master WHERE name='delivery_reset'"
                visible_before_seed = []
                connect = sqlite3.connect

                def migration_connection(*args, **kwargs):
                    db = connect(*args, **kwargs)
                    def observe(sql):
                        if sql.strip().startswith("INSERT OR IGNORE INTO delivery_reset"):
                            visible_before_seed.append(reader.execute(table_count).fetchone()[0])
                    db.set_trace_callback(observe)
                    return db

                with patch.object(telemetry.sqlite3, "connect", migration_connection):
                    telemetry.prepare_database(path)
                self.assertEqual(visible_before_seed, [0])
                self.assertEqual(reader.execute("SELECT reset_at FROM delivery_reset").fetchall(), [(0,)])

    def test_concurrent_delivery_errors_reserve_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            t = int(time.time()) - 80
            def video(channel, viewer, offset, seq, source):
                store.observe_segment(channel, viewer, t + offset, piece=str(seq), source=source)
            def fail(channel, viewer, offset):
                store.incident(channel, "client_error", "HTTP_404_SEGMENT_CACHE_MISS",
                               timestamp=t + offset, diagnostics={"client": viewer})
            video("a", "tv", 1, 1, 2)
            video("p2:a", "tv", 2, 1, 2)
            store.incident("a", "upstream_error", "MANIFEST_HTTP_404", timestamp=t + 3)
            fail("a", "tv", 5)
            fail("a", "tv", 6)
            video("a", "tv", 7, 1, 2)  # Retry of the previous fragment.
            video("a", "other", 8, 12, 1)
            store.flush()
            data = telemetry.query_stability(path, 1)
            live = {(r["channel_id"], r["viewer"]): r for r in data["live"]}
            self.assertEqual(live[("a", "tv")]["state"], "interrupted")
            self.assertEqual(live[("p2:a", "tv")]["state"], "playing")
            self.assertEqual(live[("a", "other")]["state"], "playing")
            self.assertEqual(sum(r["errors"] for r in data["channels"]), 2)
            video("a", "tv", 11, 2, 2)
            store.flush()
            result = telemetry.query_stability(path, 1)
            self.assertEqual(result["episodes"][0]["recovered"], t + 11)
            self.assertEqual(result["episodes"][0]["started"], t + 5)
            self.assertEqual(result["episodes"][0]["recovery_slot"], 2)
            self.assertEqual(next(r for r in result["live"] if r["channel_id"] == "a"
                                  and r["viewer"] == "tv")["state"], "backup")

    def test_cached_retry_updates_source_without_advancing_or_recovering(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            t = int(time.time()) - 10
            store.observe_segment("ch", "tv", t, piece="mux.104", source=2)
            store.observe_segment("ch", "tv", t+1, piece="mux.103", source=1)
            store.flush()
            row, = telemetry.query_stability(path)["live"]
            self.assertEqual((row["last_video"], row["source"], row["state"]),
                             (t+1, 1, "playing"))
            self.assertEqual(row["progress"], t)
            for offset in (2, 3):
                store.incident("ch", "client_error", timestamp=t+offset, diagnostics={"client": "tv"})
            store.observe_segment("ch", "tv", t+4, piece="mux.104", source=2)
            store.flush()
            result = telemetry.query_stability(path)
            row, = result["live"]
            self.assertEqual((row["source"], row["progress"], row["state"]), (2, t, "interrupted"))
            self.assertIsNone(result["episodes"][0]["recovered"])

    def test_cleanup_keeps_only_active_old_episode_and_its_correct_recovery(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(telemetry, "time", wraps=time) as clock:
            now = int(time.time())
            old = now - 31 * 86400
            clock.monotonic.return_value = 0
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            for viewer in ("active", "gone"):
                for ts in (old, old+1):
                    store.incident("ch", "client_error", timestamp=ts, diagnostics={"client": viewer})
            store.flush()
            with sqlite3.connect(path) as db:
                # Model continuous failed requests spanning the retention boundary.
                db.execute("UPDATE delivery_clients SET seen=? WHERE viewer='active'", (now-2,))
                db.execute("UPDATE delivery_clients SET seen=? WHERE viewer='gone'", (now-200,))
            clock.monotonic.return_value = 6 * 3600 + 1
            store.observe_manifest("ch", "active", "mux.1", 10, now-1)
            store.flush()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT viewer FROM delivery_episodes").fetchall(), [("active",)])
            for ts in (now-1, now):
                store.incident("ch", "client_error", timestamp=ts, diagnostics={"client": "other"})
            store.observe_segment("ch", "active", now, piece="mux.2", source=2)
            store.flush()
            with sqlite3.connect(path) as db:
                self.assertEqual(dict(db.execute("SELECT viewer,recovered FROM delivery_episodes")),
                                 {"active": now, "other": None})

    def test_upgrade_repairs_dangling_and_reused_episode_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            telemetry.prepare_database(path)
            now = int(time.time())
            with sqlite3.connect(path) as db:
                db.execute("""INSERT INTO delivery_episodes(id,channel_id,viewer,reason,started,confirmed)
                           VALUES (1,'a','owner','requests',?,?)""", (now, now))
                for channel, viewer, episode in (("a", "owner", 1), ("a", "other", 1),
                                                  ("b", "owner", 1), ("a", "orphan", 42)):
                    db.execute("""INSERT INTO delivery_clients(channel_id,viewer,seen,first_error,episode)
                               VALUES (?,?,?,?,?)""", (channel, viewer, now, now, episode))
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            live = telemetry.query_stability(path)["live"]
            self.assertEqual([(r["channel_id"], r["viewer"]) for r in live if r["episode"]], [("a", "owner")])
            for channel, viewer in (("a", "other"), ("b", "owner"), ("a", "orphan")):
                store.observe_segment(channel, viewer, now, piece="mux.2", source=2)
            store.flush()
            self.assertIsNone(telemetry.query_stability(path)["episodes"][0]["recovered"])

    def test_stale_manifest_needs_recent_requests_and_prior_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            t = int(time.time()) - 240
            store.observe_segment("a", "tv", t, piece="1", source=1)
            for offset in (1, 11, 22, 35, 66):
                store.observe_manifest("a", "tv", "4", 10, t + offset, window_seconds=60)
                if offset == 35:
                    store.flush()
                    self.assertEqual(telemetry.query_stability(path)["episodes"], [])
                store.observe_manifest("a", "probe", "4", 10, t + offset)
            store.observe_segment("a", "tv", t + 235, piece="2", source=1)
            store.flush()
            result = telemetry.query_stability(path, 1)
            self.assertEqual(len(result["episodes"]), 1)
            self.assertEqual(result["episodes"][0]["reason"], "playlist")
            self.assertIsNone(result["episodes"][0]["recovered"])
            self.assertEqual(len(result["live"]), 1)
            self.assertEqual(result["live"][0]["state"], "playing")
