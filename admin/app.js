"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const fmt = new Intl.NumberFormat("ru-RU");
  const app = $("app");
  const loginScreen = $("login-screen");
  const state = {overview: null, channels: [], epgStatus: "", page: 0, size: 50, lastCatalog: 0, view: "overview", busy: false, cdnHome: null, cdnChannelByHome: {}};
  const views = {overview:"ОБЗОР", cdn:"CDN-МОНИТОР", channels:"КАНАЛЫ", epg:"ТЕЛЕПРОГРАММА", tv:"КАБИНЕТЫ ТВ", history:"ПРОСМОТРЫ", incidents:"ОШИБКИ И ЗАВИСАНИЯ", system:"ДИАГНОСТИКА"};

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
    if (name === "tv") loadTvProfiles();
    if (name === "history") loadHistory();
    if (name === "incidents") loadIncidents();
  }
  function showLogin() {
    state.overview = null;
    state.channels = [];
    $("profiles-container").replaceChildren();
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
    if (!response.ok) { const details = await response.json().catch(() => ({})); throw new Error(details.error && /[а-яА-Я]/.test(details.error) ? details.error : "HTTP " + response.status); }
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
  function storage(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const amount = Number(value);
    if (amount < 1024) return Math.round(amount) + " Б";
    if (amount < 1048576) return (amount / 1024).toFixed(1).replace(".", ",") + " КБ";
    if (amount < 1073741824) return (amount / 1048576).toFixed(1).replace(".", ",") + " МБ";
    return (amount / 1073741824).toFixed(2).replace(".", ",") + " ГБ";
  }
  function rate(value) { return value === null || value === undefined ? "—" : storage(value) + "/с"; }
  function percentLabel(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value))
      ? "—" : Number(value).toLocaleString("ru-RU", {maximumFractionDigits:1}) + "%";
  }
  function resourceBar(id, value) {
    const bar = $(id);
    if (!bar) return;
    const available = value !== null && value !== undefined && Number.isFinite(Number(value));
    const pctValue = available ? Math.max(0, Math.min(100, Number(value))) : 0;
    bar.style.width = pctValue + "%";
    bar.classList.toggle("high", available && Number(value) >= 85);
    bar.classList.toggle("critical", available && Number(value) >= 95);
    bar.parentElement.setAttribute("role", "progressbar");
    bar.parentElement.setAttribute("aria-valuemin", "0");
    bar.parentElement.setAttribute("aria-valuemax", "100");
    bar.parentElement.setAttribute("aria-valuenow", available ? String(Math.round(pctValue)) : "0");
    bar.parentElement.setAttribute("aria-valuetext", available ? percentLabel(value) : "Нет данных");
  }
  function updateResources(data) {
    const vps = data.resources?.vps || {};
    const service = data.resources?.service || {};
    const gateway = service.gateway || {};
    const admin = service.admin || {};
    const ram = vps.memory || {};
    const disk = vps.disk || {};
    text("vps-cpu", percentLabel(vps.cpu_percent));
    text("vps-ram", percentLabel(ram.percent));
    text("vps-ram-detail", ram.total_bytes === undefined ? "Нет данных" :
         storage(ram.used_bytes) + " / " + storage(ram.total_bytes));
    text("vps-disk", percentLabel(disk.percent));
    text("vps-disk-detail", disk.total_bytes === undefined ? "Нет данных" :
         storage(disk.used_bytes) + " / " + storage(disk.total_bytes));
    text("vps-load", vps.load_1m === null || vps.load_1m === undefined ? "—" :
         Number(vps.load_1m).toLocaleString("ru-RU") +
         (vps.cores ? " / " + vps.cores + " CPU" : ""));
    text("vps-rx", rate(vps.rx_bytes_per_second));
    text("vps-tx", rate(vps.tx_bytes_per_second));
    resourceBar("vps-cpu-bar", vps.cpu_percent);
    resourceBar("vps-ram-bar", ram.percent);
    resourceBar("vps-disk-bar", disk.percent);
    text("service-cpu", percentLabel(gateway.cpu_percent));
    text("service-ram", storage(gateway.rss_bytes));
    const serviceShare = ram.total_bytes && gateway.rss_bytes !== null &&
                         gateway.rss_bytes !== undefined
      ? (100 * gateway.rss_bytes / ram.total_bytes) : null;
    resourceBar("service-cpu-bar", gateway.cpu_percent);
    resourceBar("service-ram-bar", serviceShare);
    text("service-cache", data.cached_mib === undefined ? "—" :
         Number(data.cached_mib).toLocaleString("ru-RU") + " МБ");
    text("service-stalled", n(data.stalled_streams));
    text("service-admin-ram", storage(admin.rss_bytes));
    text("service-admin-cpu", percentLabel(admin.cpu_percent));
    text("service-health", data.gateway_online ? "● На связи" : "● Нет связи");
    text("sys-vps-cpu", percentLabel(vps.cpu_percent));
    text("sys-vps-ram", percentLabel(ram.percent));
    text("sys-vps-disk", percentLabel(disk.percent));
    text("sys-service-cpu", percentLabel(gateway.cpu_percent));
    text("sys-service-ram", storage(gateway.rss_bytes));
    const cabins = $("service-cabins");
    cabins.replaceChildren();
    if (!data.cabins?.length) {
      cabins.append(element("div", "resource-note", "Кабинеты недоступны для мониторинга"));
    } else {
      data.cabins.forEach(item => {
        const row = element("div", "resource-cabin");
        row.append(element("span", "", item.name || "Кабинет " + item.slot));
        row.append(element("strong", "", n(item.active_streams) + " / " + n(item.max_streams) + " потоков"));
        cabins.append(row);
      });
    }
  }
  function isLiveCdn(report) {
    return !!report && report.tuned !== false &&
      (report.seconds_since_last_probe == null || report.seconds_since_last_probe < 35);
  }
  function renderMultiCdn(rows) {
    const card = $("cdn-pilot-panel");
    const pilots = Array.isArray(rows) ? rows.filter(isLiveCdn) : [];
    card.hidden = pilots.length === 0;
    if (!pilots.length) return;
    const current = [...pilots].sort((a,b) =>
      Number(a.seconds_since_last_probe ?? 1e9) - Number(b.seconds_since_last_probe ?? 1e9))[0];
    text("cdn-selected", (current.channel || "Канал") + ": " +
      (current.selected_cabinet ? "Кабинет " + current.selected_cabinet : "Накопление буфера"));
    text("cdn-switches", n(current.switches || 0));
    const target = $("cdn-sources");
    target.replaceChildren();
    (current.sources || []).forEach(source => {
      const row = element("div", "cdn-source");
      const name = element("strong", "", "CDN · " + (source.name || "Кабинет " + source.cabinet));
      const state = element("span", "cdn-health " + (source.fresh ? "good" : source.ready ? "warn" : ""),
          source.fresh ? "● В эфире" : source.ready ? "● Нет новых сегментов" : "● Прогрев");
      const active = source.cabinet === current.selected_cabinet;
      if (active) name.append(element("small", "cdn-selected-label", " ВЫБРАН"));
      const note = element("small", "cdn-source-note",
          "Буфер " + (source.buffer_seconds == null ? "—" : source.buffer_seconds + " с") +
          " · последний сегмент " + (source.age_seconds == null ? "—" : source.age_seconds + " с назад"));
      row.append(name, state, note);
      target.append(row);
    });
  }
  function renderCdnDiagnostics(data, active, home) {
    const target = $("cdn-monitor-diagnostic");
    target.replaceChildren();
    const diag = (data.multi_cdn_diagnostics_by_cabin || {})[String(home)] ||
      (home === 1 ? data.multi_cdn_diagnostics : null);
    const tvStreams = Array.isArray(data.streams) ? data.streams : [];
    let message = "";
    let detail = "";
    let state = "neutral";
    if (!data.gateway_online) {
      message = "Шлюз IPTV недоступен.";
      detail = "Проверь iptv-gateway.service на VPS.";
      state = "warn";
    } else if (!diag) {
      message = "Шлюз не отдаёт данные диагностики CDN.";
      detail = "Возможны разные версии админки и IPTV-шлюза. Обнови код на VPS и перезапусти iptv-gateway.service.";
      state = "warn";
    } else if (!diag.enabled) {
      message = "Эксперимент Multi-CDN отключён.";
      detail = "На VPS существует .iptv_profiles/multicdn.disabled.";
      state = "warn";
    } else if (!diag.primary_channel_found) {
      message = "Каталог выбранного кабинета пуст.";
      detail = "Проверь M3U-ссылку и обновление списка каналов в этом кабинете.";
      state = "warn";
    } else if (active && !isLiveCdn(active)) {
      message = "Просмотр этого канала завершён.";
      detail = "Ниже показаны данные последнего просмотра.";
    } else if (!active) {
      const own = tvStreams.find(stream => Number(stream.profile_slot) === home && !stream.standby);
      if (own) {
        message = "Есть активная трансляция, но её Multi-CDN ещё не активирован.";
        detail = "Проверь, что плеер открывает актуальную ссылку /channel/.../index.m3u8.";
      } else {
        message = "Этот телевизор пока не запрашивал канал.";
        detail = "Включи любой канал через M3U своего кабинета.";
      }
    } else if (active?.sources?.length < 2) {
      message = "Сейчас у этого канала нет готового резерва.";
      detail = "Другой кабинет не содержит точно совпадающего канала или его поток занят просмотром.";
      state = "warn";
    } else {
      message = "Multi-CDN активирован, данные о переключениях доступны.";
      detail = "Источник и буфер обновляются при запросах плейлиста телевизором.";
      state = "good";
    }
    target.classList.toggle("warn", state === "warn");
    target.classList.toggle("good", state === "good");
    target.append(element("strong", "", message));
    target.append(element("p", "", detail));
    if (diag?.cabins?.length) {
      const list = element("div", "cdn-diagnostic-cabins");
      diag.cabins.forEach(cabin => {
        const label = !cabin.configured ? "Кабинет не настроен" :
          !cabin.channel_found ? "Канал не найден" :
          cabin.playing ? "Идёт просмотр" : "Канал доступен";
        const row = element("div", "cdn-diagnostic-cabin");
        row.append(element("span", "", (cabin.name || "Кабинет " + cabin.slot) +
                   " · " + label));
        list.append(row);
      });
      target.append(list);
    }
  }

  function renderCdnMonitor(data) {
    const rows = Array.isArray(data.multi_cdn) ? data.multi_cdn : [];
    const available = rows.find(isLiveCdn);
    if (![1, 2, 3].includes(state.cdnHome)) {
      state.cdnHome = available ? Number(available.home_cabinet) : 1;
    }
    const namesForSelector = new Map((data.cabins || []).map(cabin =>
      [Number(cabin.slot), cabin.name || "Кабинет " + cabin.slot]));
    const switcher = $("cdn-cabin-switch");
    switcher.replaceChildren();
    [1, 2, 3].forEach(slot => {
      const button = element("button", "cdn-cabin-button" +
        (state.cdnHome === slot ? " active" : ""),
        namesForSelector.get(slot) || "Кабинет " + slot);
      button.type = "button";
      button.dataset.slot = String(slot);
      button.setAttribute("aria-pressed", String(state.cdnHome === slot));
      if (rows.some(item => Number(item.home_cabinet) === slot && isLiveCdn(item))) {
        button.append(element("span", "cdn-cabin-online", " ●"));
      }
      switcher.append(button);
    });
    const homeRows = rows.filter(item => Number(item.home_cabinet) === state.cdnHome)
      .sort((a,b) => Number(a.seconds_since_last_probe ?? 1e9) -
                      Number(b.seconds_since_last_probe ?? 1e9));
    const selectedId = state.cdnChannelByHome[state.cdnHome];
    const active = (selectedId && homeRows.find(item => item.channel_id === selectedId)) ||
      homeRows[0] || null;
    const picker = $("cdn-channel-select");
    picker.replaceChildren();
    if (!homeRows.length) {
      const option = element("option", "", "Нет просмотренных каналов");
      option.value = "";
      picker.append(option);
      picker.disabled = true;
    } else {
      picker.disabled = false;
      homeRows.forEach(item => {
        const option = element("option", "",
          (isLiveCdn(item) ? "● " : "○ ") +
          (item.channel || "Без названия"));
        option.value = item.channel_id || "";
        picker.append(option);
      });
      picker.value = active.channel_id || "";
    }
    text("cdn-channel-help", !active
      ? "Начни просмотр любого канала в этом кабинете."
      : isLiveCdn(active)
        ? "Сейчас смотрят: " + active.channel
        : "Последний просмотр: " + active.channel + " (неактивен)");
    renderCdnDiagnostics(data, active, state.cdnHome);
    const streamLive = !!data.gateway_online && isLiveCdn(active);
    const names = new Map((data.cabins || []).map(cabin =>
      [Number(cabin.slot), cabin.name || "Кабинет " + cabin.slot]));
    const sourceItems = active?.sources || [];
    sourceItems.forEach(item => {
      if (item.name) names.set(Number(item.cabinet), item.name);
    });
    const cabinName = slot => slot == null ? "—" : (names.get(Number(slot)) || "Кабинет " + slot);
    const selectedName = active?.selected_cabinet ? cabinName(active.selected_cabinet) : null;
    const progress = active?.last_playlist_progress_seconds;
    const delayed = streamLive && progress != null && Number(progress) >= 18;
    badge("cdn-monitor-status", !data.gateway_online ? "● Шлюз недоступен" :
      !active ? "○ Ожидание просмотра" :
      !streamLive ? "○ Канал неактивен" :
      delayed ? "● Плейлист задерживается" :
      selectedName ? "● Источник выбран" : "○ Прогрев",
      !data.gateway_online || delayed);
    text("cdn-monitor-selected", streamLive ? (selectedName || "Прогрев") : "—");
    text("cdn-monitor-selected-sub", !active ? "Включи любой канал в выбранном кабинете" :
      !streamLive ? "Нет текущего просмотра" : "Источник сегментов для телевизора");
    text("cdn-monitor-progress", streamLive && progress != null ? duration(progress) : "—");
    text("cdn-monitor-cache", active && Number.isFinite(Number(active.cached_video_seconds))
      ? duration(active.cached_video_seconds) : "—");
    text("cdn-monitor-cache-sub", active?.virtual_segment_sequence == null ?
      "Нет опубликованных сегментов" : "Последний виртуальный сегмент № " + n(active.virtual_segment_sequence));
    text("cdn-monitor-switches", active ? n(active.switches || 0) : "—");
    const sourceContainer = $("cdn-monitor-sources");
    sourceContainer.replaceChildren();
    [1, 2, 3].forEach(slot => {
      const source = sourceItems.find(item => Number(item.cabinet) === slot);
      const selected = streamLive && Number(active?.selected_cabinet) === slot;
      const card = element("div", "cdn-monitor-source" + (selected ? " selected" : ""));
      const top = element("div", "cdn-monitor-source-top");
      const label = element("div", "");
      label.append(element("span", "eyebrow", "КАБИНЕТ " + slot));
      label.append(element("h3", "", cabinName(slot)));
      const healthy = streamLive && source?.fresh;
      const statusText = !streamLive ? "Нет просмотра" :
        !source ? "Нет телеметрии" :
        selected && healthy ? "● Выбран" :
        selected ? "● Выбран · задержка" :
        healthy ? "● Готовый резерв" :
        source.ready ? "● Поток задерживается" : "○ Буферизация";
      top.append(label, element("span", "cdn-monitor-source-status " +
        (healthy ? "good" : source?.ready && streamLive ? "warn" : ""), statusText));
      card.append(top);
      const buffer = source?.buffer_seconds;
      const video = source?.contiguous_tail_seconds;
      const bufferPercent = streamLive && Number.isFinite(Number(buffer))
        ? Math.min(100, Math.max(0, 100 * Number(buffer) / 90)) : 0;
      const meterHead = element("div", "cdn-monitor-meter-head");
      meterHead.append(element("span", "", "Запас HLS"));
      meterHead.append(element("strong", "",
        streamLive && buffer != null ? duration(buffer) : "—"));
      card.append(meterHead);
      const track = element("div", "cdn-buffer-track");
      track.setAttribute("role", "progressbar");
      track.setAttribute("aria-label", "Запас HLS относительно 90 секунд");
      track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "90");
      track.setAttribute("aria-valuenow", streamLive && buffer != null
        ? String(Math.max(0, Math.min(90, Math.round(buffer)))) : "0");
      const bar = element("div", "cdn-buffer-fill" + (bufferPercent < 20 ? " low" : ""));
      bar.style.width = bufferPercent + "%";
      track.append(bar);
      card.append(track);
      const details = element("div", "cdn-source-facts");
      [
        ["Телевизоров на CDN", streamLive && active?.source_viewer_load
          ? n(active.source_viewer_load[String(slot)] || 0) + " / 2" : "—"],
        ["Видео в кэше", streamLive && video != null ? duration(video) : "—"],
        ["Новый сегмент", streamLive && source?.age_seconds != null
          ? duration(source.age_seconds) + " назад" : "—"],
        ["Плейлист CDN", streamLive && source?.published_age_seconds != null
          ? duration(source.published_age_seconds) + " назад" : "—"],
        ["Скачан сегмент", streamLive && source?.downloaded_sequence != null
          ? "#" + n(source.downloaded_sequence) : "—"],
        ["Выдан CDN", streamLive && source?.published_sequence != null
          ? "#" + n(source.published_sequence) : "—"],
        ["Медленных сегментов", streamLive ? n(source?.slow_segments) : "—"],
      ].forEach(([title, value]) => {
        const fact = element("div", "cdn-source-fact");
        fact.append(element("span", "", title), element("strong", "", value));
        details.append(fact);
      });
      card.append(details);
      sourceContainer.append(card);
    });
    const events = $("cdn-monitor-events");
    events.replaceChildren();
    const history = active?.switch_history || [];
    text("cdn-last-event", history.length ? "Событий: " + n(history.length) : "Нет событий");
    if (!active || !history.length) {
      events.append(element("div", "empty-state", !active
        ? "История появится после первого просмотра любого канала."
        : "Пока переключений не было."));
      return;
    }
    const reasons = {
      START:"Начало просмотра", PUBLISH_STALL:"Плейлист перестал продвигаться",
      SOURCE_UNAVAILABLE:"Источник недоступен",
      HOME_RECOVERED:"Домашний CDN восстановился",
      SOURCE_RESTARTED:"Перезапуск исходного потока"
    };
    history.forEach(event => {
      const item = element("div", "cdn-monitor-event");
      const time = Number(event.at);
      item.append(element("time", "", Number.isFinite(time) && time > 0
        ? new Date(time * 1000).toLocaleString("ru-RU", {
          day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit"
        }) : "—"));
      item.append(element("strong", "", event.kind === "start"
        ? "Начальный источник: " + cabinName(event.to_cabinet)
        : cabinName(event.from_cabinet) + " → " + cabinName(event.to_cabinet)));
      item.append(element("span", "", reasons[event.reason] || "Переключение источника"));
      events.append(item);
    });
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
    updateResources(data);
    renderMultiCdn(data.multi_cdn);
    renderCdnMonitor(data);
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
  function profileField(card, id, title, value, placeholder, readonly, type) {
    const container = element("div", "profile-field");
    const label = element("label", "", title);
    label.htmlFor = id;
    container.append(label);
    const input = element("input", "");
    input.id = id;
    input.type = type || "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.readOnly = !!readonly;
    input.value = value || "";
    input.placeholder = placeholder || "";
    if (id.endsWith("-name")) { input.maxLength = 50; input.required = true; }
    container.append(input);
    card.append(container);
    return input;
  }
  function renderTvProfiles(profiles) {
    const container = $("profiles-container");
    container.replaceChildren();
    profiles.forEach(profile => {
      const slot = profile.slot;
      const card = element("article", "panel tv-profile-card");
      const header = element("div", "profile-card-head");
      const left = element("div", "");
      left.append(element("div", "eyebrow", "КАБИНЕТ " + slot));
      left.append(element("h2", "", profile.name));
      header.append(left);
      const badge = element("span", "profile-state " + (profile.configured ? "ready" : ""),
                            profile.configured ? "● Настроен" : "○ Не настроен");
      header.append(badge);
      card.append(header);
      const detail = profile.legacy ? "Работает с существующим локальным M3U"
        : profile.source_host ? "Источник: " + profile.source_host
        : "Вставь M3U от своего провайдера";
      card.append(element("p", "profile-source", detail));
      const form = element("form", "profile-edit-form");
      profileField(form, "profile-" + slot + "-name", "Название кабинета", profile.name, "Название", false);
      profileField(form, "profile-" + slot + "-source", "Новая ссылка на M3U", "",
                   profile.configured ? "Оставь пустым, чтобы сохранить текущий источник" : "https://provider.example/playlist.m3u", false, "url");
      profileField(form, "profile-" + slot + "-epg-source", "Отдельный EPG URL (необязательно)", "",
                   "Если в M3U нет XMLTV или хочешь указать другой", false, "url");
      const save = element("button", "primary-btn profile-save-btn", "Сохранить кабинет ↗");
      save.type = "submit";
      form.append(save);
      const feedback = element("div", "profile-feedback");
      feedback.id = "profile-" + slot + "-feedback";
      feedback.setAttribute("role", "status");
      form.append(feedback);
      form.addEventListener("submit", event => { event.preventDefault(); saveTvProfile(slot, save); });
      card.append(form);

      const urls = element("div", "profile-links");
      const linkHeader = element("div", "eyebrow", "ССЫЛКИ ДЛЯ ТЕЛЕВИЗОРА");
      urls.append(linkHeader);
      [["m3u", "M3U · каналы", profile.m3u],
       ["epg", "XMLTV · программа", profile.epg]].forEach(([kind, label, value]) => {
        const group = element("div", "profile-url-row");
        const id = "profile-" + slot + "-" + kind + "-url";
        const title = element("label", "", label);
        title.htmlFor = id;
        group.append(title);
        const inner = element("div", "tv-link-control");
        const input = element("input", "");
        input.id = id; input.readOnly = true; input.spellcheck = false;
        input.value = value || "";
        input.placeholder = kind === "epg" && profile.epg_status === "pending"
          ? "EPG подготавливается" : kind === "epg" ? "EPG не найден" : "Сначала добавь M3U";
        inner.append(input);
        const button = element("button", "copy-link-btn", "Копировать");
        button.type = "button"; button.dataset.copy = id; button.disabled = !value;
        inner.append(button);
        group.append(inner);
        urls.append(group);
      });
      const epgLabels = {ready: "EPG готов", pending: "EPG подготавливается",
                         unavailable: "Нет источника EPG", error: "Ошибка генерации EPG. Проверь XMLTV URL."};
      urls.append(element("div", "profile-epg-hint", epgLabels[profile.epg_status] || ""));
      card.append(urls);
      container.append(card);
    });
  }
  async function loadTvProfiles() {
    try {
      const data = await api("profiles");
      renderTvProfiles(data.profiles || []);
    } catch (_error) {
      if (!app.hidden) $("profiles-container").replaceChildren(
        element("div", "empty-state", "Не удалось загрузить кабинеты ТВ."));
    }
  }
  async function saveTvProfile(slot, button) {
    const name = $("profile-" + slot + "-name").value.trim();
    const source = $("profile-" + slot + "-source").value.trim();
    const epg = $("profile-" + slot + "-epg-source").value.trim();
    const body = {name};
    if (source) body.m3u_url = source;
    if (epg) body.epg_url = epg;
    button.disabled = true;
    text("profile-" + slot + "-feedback", "Проверяем и сохраняем настройки…");
    try {
      const result = await api("profiles/" + slot, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      // Clear source URL from editable controls, then redraw from sanitized
      // data; the provider's private query strings are never displayed again.
      renderTvProfiles(result.profiles);
      text("profile-" + slot + "-feedback", "Сохранено. Скопируй ссылку M3U для телевизора.");
    } catch (error) {
      text("profile-" + slot + "-feedback", error.message || "Не удалось сохранить профиль.");
      button.disabled = false;
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
      text("profiles-page-message", "Ссылка скопирована. Вставь её в настройки плеера.");
    } catch (_error) {
      input.focus();
      input.select();
      text("profiles-page-message", "Выдели адрес и скопируй вручную.");
    }
    setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1800);
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
  async function exportIncidents() {
    const button = $("incident-export");
    const days = $("incident-days").value;
    const kind = $("incident-filter").value;
    button.disabled = true;
    text("incident-action-status", "Подготавливаем CSV…");
    try {
      const path = "incidents/export?days=" + encodeURIComponent(days) +
                   "&kind=" + encodeURIComponent(kind);
      const response = await fetch("/admin/api/" + path,
                                   {credentials:"same-origin", cache:"no-store"});
      if (response.status === 401) { showLogin(); return; }
      if (!response.ok) throw new Error("export_failed");
      const blob = await response.blob();
      const link = document.createElement("a");
      const downloadUrl = URL.createObjectURL(blob);
      link.href = downloadUrl;
      link.download = "tv-incidents-" + days + "d-" + kind + ".csv";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      text("incident-action-status", "CSV экспортирован. Можно сохранить его перед очисткой журнала.");
    } catch (_error) {
      text("incident-action-status", "Не удалось экспортировать журнал. Повтори попытку.");
    } finally {
      button.disabled = false;
    }
  }
  async function clearIncidents() {
    const confirmText = "Удалить ВСЕ накопленные ошибки и зависания без возможности восстановления?\n" +
      "История просмотров останется. Рекомендуем сначала экспортировать CSV.";
    if (!window.confirm(confirmText)) return;
    const button = $("incident-clear");
    button.disabled = true;
    text("incident-action-status", "Очищаем журнал…");
    try {
      const data = await api("incidents/clear", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
        body: JSON.stringify({confirm:"DELETE_INCIDENTS"})
      });
      text("incident-action-status", "Удалено записей: " + n(data.deleted) +
        ". Новые ошибки продолжат фиксироваться.");
      await loadIncidents();
    } catch (_error) {
      text("incident-action-status", "Очистка не удалась. Данные сохранены.");
    } finally {
      button.disabled = false;
    }
  }

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
  $("cdn-cabin-switch").addEventListener("click", event => {
    const button = event.target.closest("button[data-slot]");
    if (!button) return;
    state.cdnHome = Number(button.dataset.slot);
    if (state.overview) {
      renderCdnMonitor(state.overview);
      renderMultiCdn(state.overview.multi_cdn);
    }
  });
  $("cdn-channel-select").addEventListener("change", event => {
    state.cdnChannelByHome[state.cdnHome] = event.target.value;
    if (state.overview) renderCdnMonitor(state.overview);
  });
  $("refresh-button").addEventListener("click", () => refresh(true));
  $("profiles-container").addEventListener("click", event => {
    const button = event.target.closest("button[data-copy]");
    if (button) copyTvLink(button);
  });
  $("history-days").addEventListener("change", loadHistory);
  $("incident-days").addEventListener("change", loadIncidents);
  $("incident-filter").addEventListener("change", loadIncidents);
  $("incident-refresh").addEventListener("click", loadIncidents);
  $("incident-export").addEventListener("click", exportIncidents);
  $("incident-clear").addEventListener("click", clearIncidents);
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
