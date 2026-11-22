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