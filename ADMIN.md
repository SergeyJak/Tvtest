# HeySmart TV admin dashboard

The dashboard is a **separate, read-only** Python service. It does not replace
`iptv_multi.py`, does not touch HLS workers, does not change the TV playlist token,
and is **not** exposed to the internet on its own. Russian-language UI:

- Overview: channels, active streams, buffer, slots, EPG percentage and programme count.
- Channel directory: 2,774+ channels, search, categories, active/EPG filters, pagination.
- EPG: coverage by category, compressed file size and modification time.
- Diagnostics: gateway availability, warmup timeouts, stream-limit denials.

The EPG coverage is calculated from real XMLTV `programme` entries, not the
number of `<channel>` headers. XMLTV is scanned in a background thread when
the file changes; the dashboard initially shows `Анализируется`.

## Deploy to VPS (existing user client_911_1)

1. Merge the PR, then pull the repository:

```bash
cd ~/Tvtest
git pull --ff-only
python3 -m unittest discover -s tests -p 'test_admin.py' -v
```

2. Install and run a separate systemd service (the example uses the known VPS paths):

```bash
sudo cp deploy/iptv-admin.service.example /etc/systemd/system/iptv-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now iptv-admin.service
sudo systemctl is-active iptv-admin.service
ss -ltn | grep ':8790'
```

The new service listens **only** on `127.0.0.1:8790` and generates a private
admin password on first start in `.iptv_admin_password` with mode 0600.
The password is **not** the TV playlist token. Read it locally for browser login:

```bash
cat ~/Tvtest/.iptv_admin_password
```

Never paste the password into chat, issues, logs or a public repository.

3. Add a route in your **existing HTTPS reverse proxy** for `/admin/`.
Do not replace the existing TV route. For Caddy, insert this before the
existing general-purpose `handle` route inside the current host block:

```caddy
@admin path /admin /admin/*
handle @admin {
    reverse_proxy 127.0.0.1:8790
}
```

For Nginx, add these locations within the existing `server` block for
`tv.heysmart.lv`, leaving the current TV proxy location unchanged:

```nginx
location = /admin { return 308 /admin/; }
location ^~ /admin/ {
    proxy_pass http://127.0.0.1:8790;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
}
```

Reload the existing proxy **after checking its config** (`caddy validate` or
`nginx -t`, depending on which proxy is actually installed).

4. Open `https://tv.heysmart.lv/admin/` and enter the admin password.
The dashboard authenticates via a dedicated session cookie:
`Secure; HttpOnly; SameSite=Strict; Path=/admin`. Session duration is 8 hours.

## Troubleshooting

```bash
sudo systemctl is-active iptv-admin.service
sudo journalctl -u iptv-admin.service -n 40 --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8790/admin/
```

The last command should return `200`. The admin APIs should return `401`
without a login cookie. The admin service may be accessible through an SSH
tunnel, but login over local HTTP requires explicit `--local-http`; do not set
this flag on the HTTPS production service.

## Security and next versions

All data endpoints require the admin session and return sanitized metadata;
the gateway's private source URLs and access token are never returned.
Login attempts are rate-limited. The admin process binds to loopback only.
Version 1 intentionally has no start/stop/restart buttons, user management
or channel editing. Those features require separate role checks and audit logs.

Do not proxy `/admin/` over plain HTTP, and never bind port 8790 publicly.
## Viewing history and incident journal

The **Просмотры** and **Ошибки и зависания** pages need the gateway's
opt-in SQLite recorder. The admin itself reads the database using the existing
WorkingDirectory, so you only need to enable the recorder on the gateway.
See [TELEMETRY.md](TELEMETRY.md) for a backup-safe systemd installer, the
retention policy and how duration/freeze detection works.

## TV connection links and XMLTV fallback

The admin panel now includes **Подключение ТВ** with two copyable URLs:

- `https://tv.heysmart.lv/<TV_TOKEN>/channels.m3u` for the IPTV playlist.
- `https://tv.heysmart.lv/<TV_TOKEN>/epg.xml.gz` for a manual XMLTV/EPG source when a player does not import the guide automatically.

The same private TV token is used by both links. The actual URLs are returned only by `/admin/api/tv-links` **after admin login**, never embedded in public HTML/JavaScript or logged. The admin API uses `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

The M3U header advertises the EPG with both `x-tvg-url` and `url-tvg` for broader player compatibility. The compressed guide endpoint supports `ETag` and `Last-Modified`; compatible players may request it conditionally and receive a zero-body `304 Not Modified`. Regular requests stream gzip in small chunks rather than copying the whole ~43 MB file into process RAM. Responses use `Cache-Control: private, max-age=300, must-revalidate`; clients without conditional request support may still redownload the file.

**Deployment:** after merging and pulling the PR, restart both services to load the updated Python handlers:

```bash
cd ~/Tvtest && git pull --ff-only
python3 -m unittest discover -s tests -p 'test_epg_http_cache.py' -v
sudo systemctl restart iptv-gateway.service iptv-admin.service
sudo systemctl is-active iptv-gateway.service iptv-admin.service
```

This does not modify Caddy, systemd units, EPG generation, or your private playlist. Restarting the gateway briefly interrupts live TV streams. The current guide is preserved.

## Three editable cabinets, each with their own M3U and EPG

The admin's **Кабинеты ТВ** page now lets the administrator rename three independent TV profiles, set each profile's upstream M3U URL (optional XMLTV URL) and copy its unique TV links. The old main profile starts from the existing local file and preserves the original token. All provider source URLs stay in an owner-only VPS directory and are never returned in admin APIs. Profiles route dynamically through the existing public HTTPS domain, so Caddy configuration is unchanged. See [PROFILES.md](PROFILES.md) for installation and the daily guide-refresh timer.

## Нагрузка VPS и IPTV в админке

Страница **Обзор** автоматически обновляет показатели каждые 15 секунд.
В двух отдельных панелях видны:

- **VPS:** использование CPU, RAM и диска; load average за минуту и число
  CPU; входящая и исходящая скорость сети (без loopback-трафика).
- **IPTV:** CPU и RSS-память процесса `iptv-gateway.service`, память/CPU
  административного процесса, объём кешированных сегментов, число потоков
  с зависанием, а также активные и доступные потоки для каждого
  настроенного ТВ-кабинета.

Те же ключевые проценты и RSS продублированы в **Диагностике**. Метрики
доступны только после входа в админку через существующий защищённый
`/admin/api/overview`. Отдельных веб-эндпоинтов для мониторинга без
авторизации нет.

CPU (%) и сеть (байт/с) рассчитываются **между двумя опросами**, поэтому
после первого открытия страницы показывается «—». CPU процесса показывает
проценты **от одного ядра** и может быть выше 100% на многоядерной VPS.
RAM процесса показывает RSS и не включает весь файловый кеш Linux.
При недоступном `systemctl show iptv-gateway.service` показания CPU/RAM
шлюза будут «—», но статистика VPS и ТВ-кабинетов продолжит работать.
Сервис `iptv-admin.service` может читать эти данные без root.
Система использует Linux `/proc` и `statvfs`, без сторонних библиотек,
отправки метрик наружу или доступа к приватным URL провайдера.

Ранее после настройки удалённого M3U панель могла видеть только
остановленный **legacy**-менеджер первого кабинета. Теперь показатели
потоков агрегируются по всем активным настроенным кабинетам, включая
основной с удалённым M3U, через локальный gateway.

После мержа выполни
`cd ~/Tvtest && git pull --ff-only && sudo systemctl restart iptv-admin.service`.
Перезапуск `iptv-gateway.service` для новых панелей не требуется.
