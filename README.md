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

## Security

Repository is currently **public**. **Never commit** actual `.m3u`, `.m3u8`, `.env`, or reports, and never paste subscription URLs into issues or PRs. This repository contains no IPTV subscriptions, channel links, or video re-streaming endpoint.

## Roadmap (after we inspect real measurements)

1. Capture intermittent stalls across multiple channels.
2. Compare direct path vs EU relay/network route.
3. Only if warranted, implement a private, authorized HLS gateway with limited buffering, per-channel monitoring, and spending caps.

## Tests

```bash
python -m unittest discover -s tests -v
```