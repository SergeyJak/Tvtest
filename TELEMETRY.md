# History of TV usage and streaming incidents

Two new admin pages:

- **Просмотры:** channels viewed, rough watch duration by channel, active date range (24h, 7d, 30d), recent activity sessions and anonymized viewer labels.
- **Ошибки и зависания:** all recorded upstream errors, slow segments, missing client segments, manifest startup timeouts, stream stalls and subsequent recoveries with timestamps and durations.

## What the data actually measures

These figures **do not prove a television was displaying video**. The gateway records successful HLS segment GET/206 requests (never playlist, HEAD or admin requests) and estimates elapsed wall-clock time between consecutive segment requests for the same anonymized client/channel. Very fast segment bursts do not add minutes. Each credited gap is capped at 30 seconds; gaps over 90 seconds start a new viewing session. Multiple viewers may add concurrent viewing time. A client identity is derived from a salted one-way hash of the client IP and User-Agent; no raw IP, UA, private source URLs or subscription tokens are stored.

Stalls mean upstream HLS sequences stopped advancing, or no new video segments were downloaded, beyond max(15 seconds, 3 target-segment durations). Stalls are reported once per stalled period, and a recovery event includes the total apparent outage. This is a source-side signal and **not proof of visible TV freezing**. Segments that download more slowly than their advertised duration are separately logged. HTTP 404 cache misses, streaming capacity errors and startup warmup timeouts are logged as client failures.

History begins only once recording is enabled on the gateway. No historic data can be recovered from the previous in-memory status.

## Install on the current VPS

1. Merge this PR, pull the repository and run tests:

```bash
cd ~/Tvtest && git pull --ff-only
python3 -m unittest discover -s tests -p 'test_telemetry.py' -v
```

2. Preview the proposed change to your *effective* systemd service. Your installation has an `override.conf` and the script will edit that, not the ignored base command:

```bash
sudo python3 deploy/enable_telemetry.py --dry-run
```

3. Enable it after reviewing the preview (a short restart of `iptv-gateway.service` is required):

```bash
sudo python3 deploy/enable_telemetry.py
```

The installer backs up the selected unit file to `.bak-telemetry`, keeps the existing EPG flag and playlist settings, verifies the effective ExecStart and rolls back automatically if validation/restart fails. It adds:

```text
--telemetry-db /home/client_911_1/Tvtest/.iptv_state/telemetry.sqlite3
```

The existing `iptv-admin.service` discovers the same SQLite database by default because both services use the same WorkingDirectory. Restart it to load the new pages:

```bash
sudo systemctl restart iptv-admin.service
sudo systemctl is-active iptv-gateway.service iptv-admin.service
```

Open https://tv.heysmart.lv/admin/ and refresh the browser. The two new tabs appear after successful login.

## Data storage

- A dedicated SQLite WAL database and 32-byte private identity salt in owner-only `~/Tvtest/.iptv_state/`, excluded by `.gitignore`.
- Video delivery is never synchronously blocked by SQLite. The bounded telemetry queue drops events if overfilled; missing events can result in under-counting.
- The most recent 30 days of incidents and sessions are retained; a further maximum of 100,000 incident rows applies. No data from before enabling the recorder.
- Admin API routes are session-protected: `/admin/api/history?days=7` and `/admin/api/incidents?days=7&kind=stream_stalled`.
- The database should be backed up if analysis over longer horizons matters. Data is not sent to any third-party analytics service.

## Troubleshooting

```bash
systemctl show -P ExecStart iptv-gateway.service | grep -o -- '--telemetry-db' || true
ls -lh ~/Tvtest/.iptv_state/telemetry.sqlite3*
sudo journalctl -u iptv-gateway.service -n 30 --no-pager
```

Do **not** paste full ExecStart lines or file contents: they may contain stream keys.

Future enhancement: TV-player heartbeat support would allow exact on-screen watching duration; this release intentionally uses honest estimates based on HTTP activity.
## Export and clear the error journal

In **Ошибки и зависания**, use **Экспорт CSV** to download all matching
events for the selected 24-hour, 7-day or 30-day period and selected event
type, not just the 200 events shown in the table. The CSV contains UTC
timestamps, channel names/IDs, event type, sanitized reason and duration;
private provider URLs and TV access tokens are excluded. Excel/Sheets formula
injection from untrusted channel names is escaped.

The CSV appends `cabinet_slot` and `diagnostics_json`. Export **Все события** to
see source faults and CDN decisions together, including after a gateway restart.
Older rows have an empty diagnostics cell. The gateway upgrades the database
on startup. All timestamps in the export are UTC.

- For `HTTP_503_STREAM_LIMIT`, compare the anonymous `client` across attempts.
  The snapshot lists the configured limit and each occupying channel with its
  idle time, pending manifest requests, pinned and standby flags. Clients behind
  the same address with the same player signature can share an identifier.
- For slow or stalled downloads, the slot identifies the source subscription.
  The snapshot includes the buffer reserve, sequence numbers and ages of the
  last download and publication. A source can serve as another TV's backup.
- For `cdn_decision`, the slot identifies the viewing cabinet. Follow
  `selected_slot` and `from_slot`, and compare the freshness, buffer and guest
  usage of each candidate. A reserve denied by its stream limit includes its
  limit and occupying channels at the time of refusal. A manifest startup
  timeout also records the candidates when Multi-CDN is active. Repeated polls
  with the same blocked selection and eligibility produce one decision entry.

**Очистить журнал** shows a destructive confirmation dialog, then deletes
*all stored incidents* (not only the selected period) from the on-VPS SQLite
database. It does not delete watch history, reset tokens or stop the collector.
New issues will start appearing immediately. The endpoint requires an
authenticated admin session, a confirmation phrase and a same-origin custom
request header. Export before deleting if you need the evidence.

## Resilient upstream recovery

Repeated HLS manifest failures now use capped exponential backoff (5, 10,
20, 40, 60 seconds after successive failures) rather than hammering a
provider every two seconds. The gateway re-resolves the stream URL from the
configured provider M3U on 404, or on repeated 500/502/503/504. Repeated
segment 404/5xx errors also trigger source refresh instead of exhausting
the entire stale segment window. After a changed HLS URL yields valid
media metadata, the old segment cache is invalidated to avoid mixing
two streams that reuse sequence numbers.

These changes reduce provider request storms and improve recovery from
rotated URLs. They cannot fix sustained source bandwidth/latency problems:
if fresh provider segments arrive slower than their encoded play time,
the stream may still buffer. New incidents will show whether 404/502
frequency and stalls drop after deployment.

Deploy after merging: pull latest changes and restart
`iptv-gateway.service` and `iptv-admin.service`. It is not necessary to
reinstall Caddy or reconfigure the remote M3U URL you've already saved.
