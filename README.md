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

## Security

Repository is currently **public**. **Never commit** actual `.m3u`, `.m3u8`, `.env`, or reports, and never paste subscription URLs into issues or PRs. This repository contains no IPTV subscriptions or channel source links. The experimental gateway serves only on localhost by default; LAN exposure requires explicit --host 0.0.0.0 and a random access token.

## Roadmap (after we inspect real measurements)

1. Capture intermittent stalls across multiple channels.
2. Compare direct path vs EU relay/network route.
3. Compare the experimental local HLS gateway to direct playback before considering any paid relay.

## Tests

```bash
python -m unittest discover -s tests -v
```