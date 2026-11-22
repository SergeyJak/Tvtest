# EPG setup

Merge XMLTV feeds into one valid guide for the existing private M3U. Only real programme listings are included; channel IDs are the same stable IDs used by iptv_multi.py.

## Generate on VPS

From ~/Tvtest after merging this PR:

```bash
python3 iptv_epg.py --playlist forgpt.m3u8 --source /path/to/provider-epg.xml.gz --source /path/to/backup-epg.xml.gz --output epg.xml.gz
```

The --source values may be local XMLTV files or HTTPS URLs. Use existing/verified feeds, provider first. Sources are merged in order; first source wins for conflicting programme start times. Unmatched channels get no fabricated schedule. A JSON file with names mapped to XMLTV IDs may be supplied with --aliases epg-aliases.json.

Example private epg-aliases.json:

```json
{"Первый канал HD": "Channel1.ru"}
```

Read the generated counts: channels_total, channels_with_epg, programmes. No output file is replaced when every feed fails or no programmes match. Output is written atomically.

## Serve it

Add --epg-file /absolute/path/to/Tvtest/epg.xml.gz to the existing iptv_multi.py systemd ExecStart; keep all previous flags and the token file unchanged. Restart the existing service. The existing TV playlist URL remains unchanged.

The TV playlist then embeds x-tvg-url pointing to https://tv.heysmart.lv/TOKEN/epg.xml.gz, and each channel carries a matching tvg-id. The EPG route is protected by the existing access token. If a TV player does not pick up x-tvg-url, enter that EPG URL manually.

Schedule the same generation command daily with a systemd timer or cron. Keep all private playlists, token files, generated EPG, and credential-bearing source URLs off GitHub.

## Local test

```bash
python3 -m unittest discover -s tests -p 'test_epg.py' -v
```

EPG generation runs out-of-band and does not affect video buffer performance.