# Tvtest | IPTV playback diagnostics

**Purpose:** identify why a single IPTV subscription intermittently freezes across Samsung, LG, and Android TV, before building or paying for a relay.

## First step: 2-minute local baseline

Download your *private* M3U playlist to your computer (not to this repository), and run:

```bash
python iptv_probe.py --playlist /path/to/playlist.m3u8 --rounds 8 --interval 15
```

Windows:

```powershell
py -3 iptv_probe.py --playlist "C:\Users\you\Downloads\playlist.m3u8" --rounds 8 --interval 15
```

The script requires Python 3.10+, no pip install. It selects 8 varied channels, tests several times over ~2 minutes, and writes `iptv_health_report.csv` with **no stream URLs or subscription tokens**. You can use `--channel "НТВ"` (repeat for multiple names) or `--limit 12 --rounds 12 --interval 20` for a broader test. `--dry-run` shows the selection without connecting.

For meaningful comparison, test the **same playlist twice**: once on a computer connected to the home router (Ethernet preferred), once on a VPS in the EU. Avoid running tests while someone is streaming the same subscription if concurrent sessions are limited.

## How to read the report

- `status`: `OK`, `HTTP_403`, `HTTP_5XX`, `TIMEOUT`, `NETWORK_ERROR`, `PARTIAL_SEGMENT`, etc.
- `manifest_ttfb_ms`: response time before the stream playlist started arriving.
- `segment_ttfb_ms`: delay before the first video byte.
- `download_mbps`: measured segment-transfer speed over the sample.

A strong connection speed alone doesn't rule out intermittent upstream stalls. Monitor multiple rounds and compare the home/remote results before enabling a relay. A relay can help if the route is bad; it **cannot** repair an upstream feed that has stopped delivering segments. Sending the entire stream through metered hosting can be expensive.


## Next step: 10-minute continuous HLS stall test

The first 64/64 startup checks were fast (p95 first-byte delays 87–127 ms) but
**cannot rule out freezes later in playback**. The continuous monitor follows
consecutive live media segments from a *single channel*, instead of repeatedly
sampling the beginning of the same segment.

Run from the machine that showed the previous clean startup result:


 ```powershell
python iptv_continuous.py --playlist forgpt.m3u8 --channel "НТВ HD" --minutes 15
```

If that name doesn't match, preview with `--dry-run` or use another channel name.
The monitor downloads video segments, so **data usage can be significant**.
Avoid watching on another device simultaneously if your subscription limits
concurrent sessions. Results are written to `iptv_continuous_report.csv`
without any stream addresses or subscription tokens.

Important result codes:

- `OK`: full segment received in less time than its playback duration.
- `SLOW_SEGMENT`: the full segment took at least its playback duration to download. Repeated cases can cause buffering.
- `STALE_MANIFEST`: the live HLS manifest has not advanced for at least three target durations (minimum 15 seconds).
- `MISSED_WINDOW`: the newest playlist no longer contains some expected sequences; may mean polling/downloading fell behind.
- `SEGMENT_HTTP_...` / `MANIFEST_HTTP_...` / `NETWORK_ERROR`: video or playlist request failed.
- `PARTIAL_CAP`: segment exceeded the per-segment safety cap (default 32 MiB); this is **not** proof of successful complete playback.

**Interpretation:** compare errors and timing with the exact moment TV freezes.
If the continuous test is clean during a TV freeze, investigate player buffer,
codec/bitrate, Wi-Fi, and device settings. If it fails on the PC too, test the
same channel from a second network before paying for a relay. A relay cannot
restore missing segments from the upstream provider.

## Local buffering gateway experiment (no VPS)

**First try changing Edem/iLook CDN in your provider's playlist settings.** Their
FAQ advises a manual CDN change if streams buffer on the automatic server.
For Latvia, compare Germany, France and Automatic using **the same channel**,
with one test at a time. Regenerate/update your downloaded playlist if necessary.

If the stream still stalls, try this single-channel diagnostic gateway. It
fetches full TS segments in the background and publishes them with a
45-second delay so the TV can play from the local cache. This does not repair
a consistently dead or very slow upstream stream. It adds roughly 45 seconds
(or more, depending on the player's own buffer) of live TV delay.

On your **home Windows PC**:


 ```powershell
ipconfig
python iptv_gateway.py --playlist forgpt.m3u8 --channel "VF Теория большого взрыва" --delay 45 --host 0.0.0.0 --advertise-host 192.168.1.100
```

Replace `192.168.1.100` with **your PC's actual local IPv4** from `ipconfig`.
Keep this PC powered on with the console open. Allow Python to listen on
**private networks only** in Windows Firewall; do not forward port 8787 on
your router. The gateway uses a random access token in its URL.

The process prints two links:
- `.../channels.m3u`: add this as a **new playlist URL** to Samsung, LG or Android TV (one test channel).
- `.../status`: open in a browser first. Wait until `"ready": true` before starting TV. Check `upstream_stalled`, `slow_segments`, and `last_error` if the picture still freezes.

Run without changing your existing playlist and compare the channel directly
vs the new buffered channel on the same TV. The buffer uses up to 128 MiB RAM
and downloads every segment of the selected channel, even with no TV connected.
Stop with Ctrl+C.

**Limits:** unencrypted MPEG-TS HLS only; unsupported tags (encrypted HLS,
fMP4 initialization maps, byte ranges, discontinuities and low-latency HLS)
are rejected explicitly in `last_error`. If the CDN stops generating
segments, the relay cannot invent missing video. One channel per running
gateway instance. Do not run a public streaming service or violate the
subscription's simultaneous-connection rules.

## Recovery after MANIFEST_HTTP_404

If a TV freezes and status shows `MANIFEST_HTTP_404`, the provider returned
**404 for the upstream HLS manifest**. Increasing `--delay` alone cannot fix
that. On the reported incident, 21 errors occurred and no new segments arrived
for 46 seconds. The stream was genuinely unavailable to the gateway at that time.

1. In Edem/iLook, keep Germany selected, then **download a fresh M3U** and
   replace your local `forgpt.m3u8` file. On another CDN, allow time for
   provider-side settings to take effect.
2. By default, the gateway now re-reads this local file after a manifest 404.
   If it contains a different URL for the same channel, it switches automatically
   without restarting the Python process.
3. For **fully automatic** refresh from your own authorized Edem playlist link,
   optionally supply its URL via an environment variable; the URL will not
   appear in process arguments, output, or GitHub files:

```powershell
$env:EDEM_PLAYLIST_URL = Read-Host "Your private Edem playlist URL"
python iptv_gateway.py --playlist forgpt.m3u8 --channel "VF Теория большого взрыва" --delay 45 --host 0.0.0.0 --advertise-host 192.168.94.109 --playlist-url-env EDEM_PLAYLIST_URL
```

The dynamic link must return a full M3U channel list and include **exactly one**
matching channel. A provider login page, a single HLS media manifest, or an
unchanged/expired playlist will not restore the missing video. The remote
playlist is fetched at most once per 20 seconds during 404s and is not stored.
The `--playlist-url-env` option is optional; without it, only the local
`forgpt.m3u8` is re-read on 404.

Monitor `consecutive_manifest_404`, `source_refresh_attempts`,
`source_changes` and `source_refresh_error` at the private status URL.
If the source stays unchanged and keeps returning 404, the provider/CDN must
restore the channel or provide another working stream; our gateway cannot
synthesize live segments. Never paste private URL or token into chat or a
public issue.

## Keep the same TV playlist URL across restarts

By default, the gateway now saves its LAN access token in
`.iptv_gateway_token` in the directory where it is launched. Restarting
with the same IP and port keeps **exactly the same** `/TOKEN/channels.m3u`
address. The token file is ignored by Git. Never publish the token or
forward port 8787 to the public internet.

**To preserve a link already entered into the TV before this change**,
copy just the token (the text between `:8787/` and `/channels.m3u`)
from the *existing* TV playlist URL into the local file **before**
restarting the upgraded gateway:

```powershell
# Run once in C:\Users\angular\IdeaProjects\Tvtest
$oldToken = Read-Host "Token from the EXISTING TV playlist URL"
Set-Content -Path .iptv_gateway_token -Value $oldToken -NoNewline
```

This is a local operation; do not paste your token into GitHub issues or chat.
After merging and `git pull`, stop the old gateway with Ctrl+C and restart
using the same `--host`, `--advertise-host`, and `--port`:

```powershell
python iptv_gateway.py --playlist forgpt.m3u8 --channel "VF Теория большого взрыва" --delay 90 --host 0.0.0.0 --advertise-host 192.168.94.109
```

The old TV URL continues to work. If you skip the one-time migration, the
first upgraded run creates a **new** token and you will need to update the
TV once; future restarts preserve that new link. If your PC's LAN IP
changes, reserve `192.168.94.109` for it in your router's DHCP settings.

To deliberately rotate access, stop the gateway, delete the local
`.iptv_gateway_token` file and restart. This invalidates older TV URLs.

## If TV freezes while upstream is healthy (ready: false)

A real incident showed `ready: false` with 15 cached segments, 124 MiB,
`last_error: null` and `upstream_stalled: false`. This can occur when a
previous upstream outage leaves a missing segment in the in-memory cache:
there are plenty of total segments, but not enough **consecutive** segments
to hold the full 90-second target delay. The old gateway then returned HTTP
503 for the local media manifest and could cause the TV player to stop.

The gateway now:
- Keeps a larger **256 MiB** cache by default, adjustable with `--cache-mib`.
- Requires the full configured delay when first starting, then temporarily
  reduces the effective delay to resume with 3 consecutive *newer* cached
  segments if a gap destroys the old delayed window.
- Does not advance the TV playlist cursor when somebody polls `/status`.
- Reports `contiguous_tail_segments`, `contiguous_tail_seconds`,
  `effective_buffer_delay_seconds`, `client_manifest_503`,
  `client_segment_404` and `seconds_since_segment_download`.

Use the same local IP, port and persisted gateway token to keep your existing
TV playlist URL:

```powershell
# Your EDEM_PLAYLIST_URL environment variable must already be set.
python iptv_gateway.py --playlist forgpt.m3u8 --channel "VF Теория большого взрыва" --delay 90 --cache-mib 256 --host 0.0.0.0 --advertise-host 192.168.94.109 --playlist-url-env EDEM_PLAYLIST_URL
```

If the upstream continues returning 404, this still cannot reconstruct missing
live video. If the TV player stops permanently on a previous 503, selecting
the existing channel again may be necessary after recovery. `ready:true`
means the gateway can publish its current media window; it is **not** a
guarantee that the TV is playing.

## TV stops while status says ready: true

`ready: true` means the gateway *could* serve some cached HLS. It does **not**
prove that the TV actually requested the gateway's media playlist.

If a TV stops and `last_published_sequence: null` with
`client_manifest_200: 0`, the current gateway process hasn't served
a successful `/index.m3u8` request. Verify the full **TV playlist URL**
printed by the currently running gateway is exactly what the TV has saved,
including the private token. An older token causes a local 404 without
incrementing `client_manifest_503` or `client_segment_404`.
An outdated PC IP or port causes connection failures, not 404.

The gateway now tracks private, aggregated HTTP response counters:
`client_playlist_200`, `client_manifest_200`,
`client_segment_200`, `client_segment_206`,
`client_auth_404`, and seconds since the last client HLS
request. It does **not** record client IP addresses, tokens or paths.
Poll `/status` from your browser and then start the TV channel:
`client_manifest_200` and `client_segment_200` should increase
when playback uses the gateway. Merely requesting `/status` doesn't increase them.

Separately, when `MANIFEST_HTTP_404` repeats, the upstream provider isn't
serving its live HLS manifest. Re-fetching the authorized Edem M3U can switch
to a newly returned channel URL, **but cannot restore** a feed when the
refreshed M3U still returns the same unusable URL (`source_changes: 0`).
Do not increase delay as a substitute for fixing repeated upstream 404s.

## Security

Repository is currently **public**. **Never commit** actual `.m3u`, `.m3u8`, `.env`, or reports, and never paste subscription URLs into issues or PRs. This repository contains no IPTV subscriptions or channel source links. The experimental gateway serves only on localhost by default; LAN exposure requires explicit --host 0.0.0.0 and a random access token.

## Roadmap: family IPTV service on VPS

See **[ROADMAP.md](ROADMAP.md)** for the phased plan: harden the current single-channel gateway, deploy on a permitted EU VPS, build a multi-channel catalog and shared caches, then add separate access for Sergey, Kristina and parents using authorized Edem accounts. Railway services remain untouched until a separate migration decision.

## Tests

```bash
python -m unittest discover -s tests -v
```