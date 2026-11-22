# Three IPTV cabinets managed from the admin UI

The existing HeySmart TV admin page `https://tv.heysmart.lv/admin/` contains a **Кабинеты ТВ** tab with exactly **three independent TV cabinets**. Each cabinet can be renamed and configured with its own external M3U URL (EDEM/iLook or another authorized source), and has its own private TV M3U link. When XMLTV is configured or discovered from the uploaded M3U header, a separate EPG URL is generated for that cabinet.

## Existing TV link is preserved

**Cabinet 1 / Основной** initially uses the current local `forgpt.m3u8`, existing `.iptv_gateway_token` and current `epg.xml.gz`. No changes to the old M3U/EPG addresses. Set a remote M3U URL on cabinet 1 only when ready to switch its subscription source: the **public M3U link/token stays identical**, while the contents switch to the newly configured source. The original `forgpt.m3u8` file is untouched as a local backup. The original prewarmed stream is stopped when cabinet 1 switches to a different source.

**Cabinets 2 and 3** are initially inactive. Configure each through the UI to activate it. Each receives a different strong random TV access token and private on-demand streaming manager, with a conservative one-active-channel limit per cabinet (to avoid mixing subscriptions). There are no additional TCP ports, Caddy changes or separate VPS required. The gateway dynamically loads source updates from the private profile configuration.

## In the admin UI

1. Open `https://tv.heysmart.lv/admin/`, sign in, select **Кабинеты ТВ**.
2. Edit **Название кабинета** (for example «Мой ТВ», «Родители», «Кристина»).
3. Paste the original subscription provider's **M3U URL**. It must be a reachable public HTTP/HTTPS URL returning UTF-8 `#EXTM3U`. The server validates and caches a local copy before accepting the update. If an account is already configured, leave the M3U field empty to rename without replacing the source.
4. **EPG URL (optional):** if your provider supplies XMLTV separately, paste its URL; otherwise the app looks for `x-tvg-url` or `url-tvg` in the M3U header.
5. Click **Сохранить кабинет**. Copy that cabinet's **M3U** link into its television app. If the app does not auto-load EPG, copy the corresponding **XMLTV** URL into its EPG settings.

Provider URLs and secrets are **never returned to the browser after saving**. The UI shows the domain only, not the path/query. Public TV links do contain the cabinet's access token; share them only with authorized viewers. All configured source URLs and cached provider channel URLs live under owner-only `~/Tvtest/.iptv_profiles/` (directory 0700; files 0600; excluded by `.gitignore`). No source passwords go into GitHub, systemd arguments, HTML or logs.

## Deployment on existing VPS

This version is backward-compatible with your current Caddy route and systemd units. After merging the PR:

```bash
cd ~/Tvtest
git pull --ff-only
python3 -m unittest discover -s tests -p 'test_profiles.py' -v
sudo systemctl restart iptv-gateway.service iptv-admin.service
sudo systemctl is-active iptv-gateway.service iptv-admin.service
```

Restarting `iptv-gateway.service` briefly interrupts ongoing streams. Existing M3U and EPG links do not change. Do **not** paste any real EDEM URLs or TV tokens into the chat.

## Daily remote M3U and EPG refresh

Newly configured cabinets build their own EPG from their channel IDs asynchronously after saving. The admin UI may show **EPG подготавливается** until the first build finishes. `EPG ошибка` means generation failed, not that the M3U link is invalid. The guide generator uses streaming XMLTV and atomic files; failed refreshes do not overwrite the old guide. If no EPG source is supplied or discovered, no EPG URL is shown. Cabinet 1's old local EPG continues to use its existing timer until its source is switched.

Install one timer to refresh configured profile sources and individual EPG guides every day, without restarting live streams:

```bash
cd ~/Tvtest
sudo cp deploy/iptv-profile-refresh.service.example /etc/systemd/system/iptv-profile-refresh.service
sudo cp deploy/iptv-profile-refresh.timer.example /etc/systemd/system/iptv-profile-refresh.timer
sudo systemctl daemon-reload
sudo systemd-analyze verify iptv-profile-refresh.service iptv-profile-refresh.timer
sudo systemctl enable --now iptv-profile-refresh.timer
systemctl list-timers --all iptv-profile-refresh.timer
```

The 04:45 UTC refresh is low priority and sequentially processes the configured cabinets. Each cabinet keeps its own XMLTV file; generated EPG is not interchangeable between cabinets. The existing `iptv-epg.timer` can remain active for the legacy local cabinet. If you switch cabinet 1 to a remote M3U, that legacy timer is no longer needed (but may be kept until you verify the new EPG works).

## Checks without exposing tokens

```bash
curl -sS -o /dev/null -w 'Admin: %{http_code}\n' https://tv.heysmart.lv/admin/
sudo systemctl is-active iptv-gateway.service iptv-admin.service
ls -ld ~/Tvtest/.iptv_profiles
```

Expected: Admin 200, both services active, directory permissions `drwx------`.

## Technical limitations

- The gateway is a shared server, but cabinet URLs, upstream sources, channel lists and EPG are isolated. If the same channel appears in multiple cabinets, telemetry distinguishes the cabinet as well.
- A provider's limit on simultaneous streams still applies to each provider account. Each new cabinet has a one-channel gateway limit; this is not a bypass of subscription limits.
- Source updates are validated before saving, but replacing cabinet 1's M3U immediately switches its playlist contents; do so when live TV interruption is acceptable.
- The admin password is still one shared administrator login; these three **cabinet profiles are not three separate administrator accounts**.
