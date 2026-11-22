"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const fmt = new Intl.NumberFormat("ru-RU");
  const app = $("app");
  const loginScreen = $("login-screen");
  const state = {overview: null, channels: [], epgStatus: "", page: 0, size: 50, lastCatalog: 0, view: "overview", busy: false};
  const views = {overview:"ОБЗОР", channels:"КАНАЛЫ", epg:"ТЕЛЕПРОГРАММА", tv:"ПОДКЛЮЧЕНИЕ ТВ", history:"ПРОСМОТРЫ", incidents:"ОШИБКИ И ЗАВИСАНИЯ", system:"ДИАГНОСТИКА"};

  function text(id, value) {
    const node = $(id);
    if (node) node.textContent = value === null || value === undefined ? "—" : String(value);
  }
  function n(value) { return value === null || value === undefined ? "—" : fmt.format(value); }
  function bytes(value) {
    if (!value && value !== 0) return "—";
    return value < 1048576 ? Math.round(value / 1024) + " КБ" : (value / 1048576).toFixed(1).replace(".", ",") + " МБ";
  }
  function date(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString("ru-RU", {day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit"}) : "—";
  }
  function pct(covered, total) {
    return Number.isInteger(covered) && total > 0 ? Math.round(1000 * covered / total) / 10 : null;
  }
  function view(name) {
    if (!views[name]) return;
    state.view = name;
    for (const key of Object.keys(views)) $("view-" + key).hidden = key !== name;
    document.querySelectorAll(".nav-button").forEach(button => button.classList.toggle("active", button.dataset.target === name));
    text("crumb", views[name]);
    if (name === "channels") renderChannels();
    if (name === "epg") renderGroups();
    if (name === "tv") loadTvLinks();
    if (name === "history") loadHistory();
    if (name === "incidents") loadIncidents();
  }
  function showLogin() {
    state.overview = null;
    state.channels = [];
    for (const id of ["tv-m3u-url", "tv-epg-url"]) $(id).value = "";
    for (const id of ["tv-copy-m3u", "tv-copy-epg"]) $(id).disabled = true;
    app.hidden = true;
    loginScreen.hidden = false;
    $("password").value = "";
    $("password").focus();
  }
  function showApp() {
    loginScreen.hidden = true;
    app.hidden = false;
  }
  async function api(path, options) {
    const response = await fetch("/admin/api/" + path, Object.assign({credentials:"same-origin", cache:"no-store"}, options || {}));
    if (response.status === 401 && path !== "login") { showLogin(); throw new Error("Требуется вход"); }
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }
  function badge(id, value, isBad) {
    const node = $(id);
    node.textContent = value;
    node.classList.toggle("danger", !!isBad);
  }
  function element(tag, cls, value) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function updateOverview(data) {
    state.overview = data;
    const online = data.gateway_online;
    text("side-state", online ? "Сервер работает" : "Нет связи со шлюзом");
    $("side-dot").classList.toggle("off", !online);
    badge("overall-status", online ? "● Система работает" : "● Шлюз недоступен", !online);
    badge("system-badge", online ? "● На связи" : "● Нет связи", !online);
    text("metric-channels", n(data.channels));
    text("metric-streams", n(data.active_streams));
    text("metric-capacity", data.max_streams !== null ? " / " + n(data.max_streams) : "");
    const percent = pct(data.epg.covered, data.channels);
    text("metric-coverage", percent === null ? "—" : percent.toLocaleString("ru-RU") + "%");
    text("metric-coverage-sub", percent === null ? "Идёт проверка EPG" : n(data.epg.covered) + " каналов с программой");
    text("metric-programmes", n(data.epg.programmes));
    text("coverage-percent", percent === null ? "—" : percent.toLocaleString("ru-RU") + "%");
    text("legend-covered", n(data.epg.covered));
    text("legend-missing", data.epg.covered === null ? "—" : n(Math.max(0, data.channels - data.epg.covered)));
    $("coverage-circle").style.background = percent === null
      ? "conic-gradient(#34465a 0% 100%)"
      : "conic-gradient(#73e7d0 0% " + percent + "%, #34465a " + percent + "% 100%)";
    text("status-gateway", online ? "Работает" : "Недоступен");
    text("status-limit", n(data.max_streams));
    text("status-delay", data.delay_seconds === null ? "—" : n(data.delay_seconds) + " с");
    text("status-epg-date", date(data.epg.updated_at));
    text("status-epg-size", bytes(data.epg.size_bytes));
    const epgLabels = {ready:"Готова", loading:"Анализируется", missing:"Нет файла", error:"Ошибка"};
    text("status-epg-state", epgLabels[data.epg.status] || "Проверка");
    badge("epg-badge", data.epg.status === "ready" ? "● EPG доступен" : "● " + (epgLabels[data.epg.status] || "Проверка"), data.epg.status === "error" || data.epg.status === "missing");
    text("epg-big", percent === null ? "—" : percent.toLocaleString("ru-RU") + "%");
    text("epg-covered-total", data.epg.covered === null ? "Загрузка EPG…" : n(data.epg.covered) + " с EPG");
    text("epg-uncovered-total", data.epg.covered === null ? "—" : n(Math.max(0, data.channels - data.epg.covered)) + " без EPG");
    $("epg-progress").style.width = (percent || 0) + "%";
    text("epg-file-size", bytes(data.epg.size_bytes));
    text("epg-programmes", n(data.epg.programmes));
    text("epg-updated", date(data.epg.updated_at));
    text("sys-capacity", data.max_streams === null ? "—" : n(data.active_streams) + " / " + n(data.max_streams));
    text("sys-delay", data.delay_seconds === null ? "—" : data.delay_seconds + " с");
    text("sys-denials", n(data.stream_limit_denials));
    text("sys-timeouts", n(data.warmup_timeouts));
    text("sys-online", online ? "Online" : "Offline");
    text("sys-catalog", n(data.catalog_requests));
    text("sys-uptime", Math.floor(data.admin_uptime_seconds / 3600) + " ч " + Math.floor(data.admin_uptime_seconds % 3600 / 60) + " мин");
    text("last-updated", "Обновлено в " + new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit",second:"2-digit"}));
    text("footer-time", new Date().getFullYear());
    renderStreams(data.streams);
  }
  function renderStreams(streams) {
    const list = $("streams-list");
    list.replaceChildren();
    if (!streams.length) {
      list.append(element("div", "empty-state", "Сейчас нет активных трансляций. Когда телевизор включит канал, он появится здесь."));
      return;
    }
    streams.forEach(stream => {
      const row = element("div", "stream-card");
      row.append(element("div", "stream-avatar", "▶"));
      const info = element("div", "stream-details");
      info.append(element("div", "stream-name", stream.name || "Канал"));
      const subtitle = [stream.pinned ? "Закреплён" : "По запросу",
                        stream.cached_mib === null ? "" : "Кэш " + stream.cached_mib + " МБ",
                        stream.delay_seconds === null ? "" : "Буфер " + stream.delay_seconds + " с"].filter(Boolean).join(" · ");
      info.append(element("div", "stream-meta", subtitle));
      row.append(info);
      const mode = stream.stalled ? "error" : stream.ready ? "" : "warn";
      row.append(element("span", "stream-state " + mode,
                         stream.stalled ? "Сбой" : stream.ready ? "В эфире" : "Буферизация"));
      list.append(row);
    });
  }
  function updateGroups() {
    const select = $("channel-group");
    const value = select.value;
    const groups = [...new Set(state.channels.map(c => c.group || "Без категории"))].sort((a,b) => a.localeCompare(b, "ru"));
    select.replaceChildren(element("option", "", "Все категории"));
    select.firstChild.value = "";
    groups.forEach(group => { const option = element("option", "", group); option.value = group; select.append(option); });
    select.value = groups.includes(value) ? value : "";
  }
  function filtered() {
    const query = $("channel-search").value.toLocaleLowerCase("ru").trim();
    const group = $("channel-group").value;
    const filter = $("channel-filter").value;
    return state.channels.filter(c =>
      (!query || c.name.toLocaleLowerCase("ru").includes(query)) &&
      (!group || (c.group || "Без категории") === group) &&
      (filter === "all" || (filter === "epg" && c.epg === true) ||
       (filter === "missing" && c.epg === false) || (filter === "active" && c.active)));
  }
  function renderChannels() {
    const rows = filtered();
    const pageCount = Math.max(1, Math.ceil(rows.length / state.size));
    state.page = Math.max(0, Math.min(state.page, pageCount - 1));
    const table = $("channels-table");
    table.replaceChildren();
    const start = state.page * state.size;
    rows.slice(start, start + state.size).forEach(c => {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.append(element("span", "", c.name));
      name.append(element("small", "channel-id", c.id));
      tr.append(name);
      tr.append(element("td", "", c.group || "Без категории"));
      const epg = document.createElement("td");
      epg.append(element("span", "table-badge " + (c.epg ? "good" : "muted"),
                         c.epg === null ? "Проверка" : c.epg ? "Есть EPG" : "Нет EPG"));
      tr.append(epg);
      const active = document.createElement("td");
      active.append(element("span", "table-badge " + (c.active ? "good" : "muted"),
                            c.active ? "Активен" : "Не активен"));
      tr.append(active);
      table.append(tr);
    });
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = element("td", "", "По выбранным фильтрам каналы не найдены");
      td.colSpan = 4;
      tr.append(td);
      table.append(tr);
    }
    text("channels-total", n(state.channels.length) + " каналов");
    text("table-count", rows.length ? n(start + 1) + "–" + n(Math.min(start + state.size, rows.length)) + " из " + n(rows.length) : "0 каналов");
    text("page-info", n(state.page + 1) + " / " + n(pageCount));
    $("prev-page").disabled = state.page === 0;
    $("next-page").disabled = state.page + 1 >= pageCount;
  }
  function renderGroups() {
    const container = $("group-coverage");
    container.replaceChildren();
    if (!state.channels.length) {
      container.append(element("div", "empty-state", "Загрузка каталога каналов…"));
      return;
    }
    if (state.epgStatus !== "ready") {
      container.append(element("div", "empty-state", "Телепрограмма анализируется, покрытие появится после сканирования XMLTV."));
      return;
    }
    const groups = new Map();
    state.channels.forEach(channel => {
      const group = channel.group || "Без категории";
      const row = groups.get(group) || {name: group, total: 0, covered: 0};
      row.total++;
      if (channel.epg) row.covered++;
      groups.set(group, row);
    });
    const ranked = [...groups.values()].sort((a,b) => b.total - a.total).slice(0, 16);
    ranked.forEach(item => {
      const percent = pct(item.covered, item.total) || 0;
      const section = element("div", "group-item");
      const heading = element("div", "group-title");
      heading.append(element("span", "", item.name));
      heading.append(element("small", "", item.covered + " / " + item.total + " · " + percent.toLocaleString("ru-RU") + "%"));
      section.append(heading);
      const track = element("div", "group-track");
      const fill = document.createElement("div");
      fill.style.width = percent + "%";
      track.append(fill);
      section.append(track);
      container.append(section);
    });
  }
  async function loadChannels(force) {
    if (!force && Date.now() - state.lastCatalog < 45000) return;
    const data = await api("channels");
    state.channels = data.channels;
    state.epgStatus = data.epg_status;
    state.lastCatalog = Date.now();
    updateGroups();
    renderChannels();
    renderGroups();
  }
  function duration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    if (value < 60) return value + " с";
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    return hours ? hours + " ч " + minutes + " мин" : minutes + " мин";
  }
  function localTime(seconds) {
    if (!Number.isFinite(Number(seconds))) return "—";
    return new Date(Number(seconds) * 1000).toLocaleString("ru-RU", {
      day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit"
    });
  }
  async function loadTvLinks() {
    const m3u = $("tv-m3u-url");
    const epg = $("tv-epg-url");
    const m3uCopy = $("tv-copy-m3u");
    const epgCopy = $("tv-copy-epg");
    m3uCopy.disabled = true;
    epgCopy.disabled = true;
    text("tv-link-status", "");
    try {
      const data = await api("tv-links");
      if (!data.available || !data.m3u || !data.epg) throw new Error("No TV links");
      m3u.value = data.m3u;
      epg.value = data.epg;
      m3uCopy.disabled = false;
      epgCopy.disabled = false;
    } catch (_error) {
      m3u.value = "";
      epg.value = "";
      if (!app.hidden) text("tv-link-status", "Не удалось загрузить ссылки. Проверь файл токена на VPS.");
    }
  }
  async function copyTvLink(button) {
    const input = $(button.dataset.copy);
    if (!input || !input.value || button.disabled) return;
    const original = button.textContent;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(input.value);
      } else {
        input.focus();
        input.select();
        if (!document.execCommand("copy")) throw new Error("copy unavailable");
      }
      button.textContent = "Скопировано ✓";
      text("tv-link-status", "Ссылка скопирована. Вставь её в настройки IPTV-плеера.");
    } catch (_error) {
      input.focus();
      input.select();
      text("tv-link-status", "Выдели адрес и скопируй вручную.");
    }
    setTimeout(() => { button.textContent = original; }, 1800);
  }

  async function loadHistory() {
    const days = $("history-days").value;
    const ranking = $("history-ranking");
    const recent = $("history-recent");
    try {
      const data = await api("history?days=" + encodeURIComponent(days));
      text("history-total-time", duration(data.total_seconds));
      text("history-sessions", n(data.total_sessions));
      text("history-channels", n(data.unique_channels));
      text("history-recording", data.recording_active ? "Включена" : "Выключена");
      $("history-notice").textContent = data.recording_active
        ? "Учёт включён. Длительность приблизительная: учитываются успешные запросы HLS-сегментов, а не состояние экрана телевизора."
        : "Сбор данных на шлюзе не включён. Добавь --telemetry-db в действующий systemd ExecStart. Прошлые просмотры восстановить нельзя.";
      ranking.replaceChildren();
      recent.replaceChildren();
      if (!data.channels.length) ranking.append(element("div", "empty-state", "За выбранный период просмотренные каналы пока не обнаружены."));
      const longest = Math.max(1, ...data.channels.map(row => row.seconds));
      data.channels.slice(0, 20).forEach((row, i) => {
        const wrapper = element("div", "rank-row");
        wrapper.append(element("span", "rank-number", String(i + 1).padStart(2, "0")));
        const info = element("div", "");
        info.append(element("div", "rank-title", row.channel_name));
        info.append(element("div", "rank-sub", n(row.sessions) + " сессий · Последний запрос " + localTime(row.last_seen)));
        const track = element("div", "rank-progress");
        const bar = document.createElement("div");
        bar.style.width = Math.max(2, Math.round(row.seconds / longest * 100)) + "%";
        track.append(bar);
        info.append(track);
        wrapper.append(info);
        wrapper.append(element("div", "rank-duration", duration(row.seconds)));
        ranking.append(wrapper);
      });
      if (!data.sessions.length) recent.append(element("div", "empty-state", "История пока пуста."));
      data.sessions.slice(0, 12).forEach(row => {
        const node = element("div", "activity-row");
        const heading = element("div", "activity-row-top");
        heading.append(element("span", "activity-title", row.channel_name));
        heading.append(element("span", "activity-duration", duration(row.seconds)));
        node.append(heading);
        node.append(element("div", "activity-sub", localTime(row.started_at) + " → " +
          localTime(row.last_seen) + " · Устройство " + row.viewer.slice(0, 6)));
        recent.append(node);
      });
    } catch (error) {
      if (app.hidden) return;
      ranking.replaceChildren(element("div", "empty-state", "Не удалось получить историю просмотров."));
      recent.replaceChildren();
    }
  }
  const incidentLabels = {
    stream_stalled:["Зависание", "danger"], stream_recovered:["Восстановление", "success"],
    upstream_error:["Ошибка источника", "danger"], client_error:["Ошибка клиента", "warning"],
    startup_timeout:["Таймаут запуска", "warning"], segment_slow:["Медленный сегмент", "warning"]
  };
  async function loadIncidents() {
    const days = $("incident-days").value;
    const kind = $("incident-filter").value;
    const table = $("incident-table");
    try {
      const data = await api("incidents?days=" + encodeURIComponent(days) + "&kind=" + encodeURIComponent(kind));
      text("incident-stalls", n(data.summary.stream_stalled || 0));
      text("incident-upstream", n(data.summary.upstream_error || 0));
      text("incident-client", n((data.summary.client_error || 0) + (data.summary.startup_timeout || 0)));
      text("incident-slow", n(data.summary.segment_slow || 0));
      text("incident-total", n(data.total) + " событий");
      $("incident-notice").textContent = data.recording_active
        ? "Журнал собирается автоматически. «Зависание» означает отсутствие новых сегментов у источника, а не гарантированную остановку изображения на ТВ."
        : "Учёт событий на IPTV-шлюзе пока выключен. Для сбора добавь --telemetry-db в его ExecStart.";
      table.replaceChildren();
      if (!data.events.length) {
        const tr = document.createElement("tr");
        const td = element("td", "", "За выбранный период событий не найдено.");
        td.colSpan = 5;
        tr.append(td);
        table.append(tr);
      }
      data.events.forEach(row => {
        const tr = document.createElement("tr");
        tr.append(element("td", "", localTime(row.occurred_at)));
        tr.append(element("td", "", row.channel_name));
        const type = document.createElement("td");
        const info = incidentLabels[row.kind] || [row.kind, "neutral"];
        type.append(element("span", "incident-type " + info[1], info[0]));
        tr.append(type);
        tr.append(element("td", "incident-details", row.detail || "—"));
        tr.append(element("td", "", row.duration_seconds === null ? "—" : duration(row.duration_seconds)));
        table.append(tr);
      });
    } catch (error) {
      if (app.hidden) return;
      table.replaceChildren();
      const tr = document.createElement("tr");
      const td = element("td", "", "Не удалось загрузить журнал.");
      td.colSpan = 5;
      tr.append(td);
      table.append(tr);
    }
  }

  async function refresh(force) {
    if (state.busy || app.hidden) return;
    state.busy = true;
    $("refresh-button").disabled = true;
    try {
      const data = await api("overview");
      updateOverview(data);
      await loadChannels(force || state.epgStatus !== data.epg.status);
      if (state.view === "history") await loadHistory();
      if (state.view === "incidents") await loadIncidents();
    } catch (error) {
      text("last-updated", "Ошибка подключения");
      if (!app.hidden) badge("overall-status", "● Нет связи с админкой", true);
    } finally {
      state.busy = false;
      $("refresh-button").disabled = false;
    }
  }

  document.querySelectorAll(".nav-button").forEach(button => button.addEventListener("click", () => view(button.dataset.target)));
  document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => view(button.dataset.go)));
  ["channel-search", "channel-group", "channel-filter"].forEach(id => $(id).addEventListener("input", () => { state.page = 0; renderChannels(); }));
  $("prev-page").addEventListener("click", () => { state.page--; renderChannels(); });
  $("next-page").addEventListener("click", () => { state.page++; renderChannels(); });
  $("refresh-button").addEventListener("click", () => refresh(true));
  document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", () => copyTvLink(button)));
  $("history-days").addEventListener("change", loadHistory);
  $("incident-days").addEventListener("change", loadIncidents);
  $("incident-filter").addEventListener("change", loadIncidents);
  $("incident-refresh").addEventListener("click", loadIncidents);
  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    text("login-error", "");
    $("login-button").disabled = true;
    try {
      await api("login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({password:$("password").value})});
      $("password").value = "";
      showApp();
      view("overview");
      await refresh(true);
    } catch (_error) {
      text("login-error", "Неверный пароль или временная блокировка входа.");
    } finally {
      $("login-button").disabled = false;
    }
  });
  $("logout-button").addEventListener("click", async () => {
    try { await api("logout", {method:"POST"}); } catch (_error) {}
    showLogin();
  });
  (async () => {
    try {
      await api("session");
      showApp();
      await refresh(true);
    } catch (_error) {
      showLogin();
    }
  })();
  setInterval(() => refresh(false), 15000);
})();
