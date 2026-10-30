/* Oral Argument Calendar — static front end. Reads data/*.json built by the daily GitHub Action. */
(() => {
  "use strict";
  const ET = "America/New_York";
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- time helpers (court times are Eastern) ----------
  const etParts = (d) => Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(d).map((p) => [p.type, p.value]));
  const todayET = () => { const p = etParts(new Date()); return `${p.year}-${p.month}-${p.day}`; };
  const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const daysBetween = (a, b) => Math.round((new Date(b + "T12:00:00Z") - new Date(a + "T12:00:00Z")) / 864e5);
  // Convert a wall-clock time in a court's time zone to a real instant (handles DST).
  function instantIn(dateIso, hhmm, tz) {
    const guess = new Date(`${dateIso}T${hhmm}:00Z`);
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(guess).map((x) => [x.type, x.value]));
    const asIfUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute);
    return new Date(guess.getTime() + (guess.getTime() - asIfUtc));
  }
  const etInstant = (dateIso, hhmm) => instantIn(dateIso, hhmm, ET);
  const tzAbbr = (d, tz) => new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" }).formatToParts(d).find((x) => x.type === "timeZoneName").value;
  const fmtTime = (d, tz) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz });
  const fmtDay = (iso) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" });
  const fmtShort = (iso) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  const localTZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const sameZoneAsET = new Intl.DateTimeFormat("en-US", { timeZone: localTZ, timeZoneName: "short" }).format(new Date()).slice(-3) ===
    new Intl.DateTimeFormat("en-US", { timeZone: ET, timeZoneName: "short" }).format(new Date()).slice(-3);
  const relLabel = (iso, today) => {
    const n = daysBetween(today, iso);
    if (n === 0) return ["Today", "today"]; if (n === 1) return ["Tomorrow", ""]; if (n === -1) return ["Yesterday", ""];
    return n > 0 ? [`In ${n} days`, ""] : [`${-n} days ago`, ""];
  };

  // ---------- .ics for "add to calendar" ----------
  function icsHref(c) {
    const start = etInstant(c.date, c.time_et || "10:00");
    const end = new Date(start.getTime() + 60 * 60e3);
    const z = (d) => d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
    const lines = [
      "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//oral-argument-calendar//EN", "BEGIN:VEVENT",
      `UID:${c.id}@oral-argument-calendar`, `DTSTAMP:${z(new Date())}`, `DTSTART:${z(start)}`, `DTEND:${z(end)}`,
      `SUMMARY:Oral argument: ${c.title} (No. ${c.docket})`,
      `DESCRIPTION:${(c.question_plain || c.overview || c.question || "").replace(/[,;]/g, (m) => "\\" + m).replace(/\n/g, " ")}\\n\\nListen: ${c.links.live}`,
      `URL:${c.links.docket}`, "END:VEVENT", "END:VCALENDAR",
    ];
    return "data:text/calendar;charset=utf-8," + encodeURIComponent(lines.join("\r\n"));
  }

  // ---------- Supreme Court ----------
  let scotus = { cases: [] };
  let range = "month";

  function inRange(c, today) {
    const n = daysBetween(today, c.date);
    switch (range) {
      case "today": return n === 0;
      case "week": return n >= 0 && n <= 7;
      case "month": return n >= 0 && n <= 30;
      case "upcoming": return n >= 0;
      case "past": return n < 0 || (n === 0 && c.argued);
    }
  }

  function scotusCard(c, today) {
    const node = $("#tpl-case").content.firstElementChild.cloneNode(true);
    const start = etInstant(c.date, c.time_et || "10:00");
    const later = (c.order || 1) > 1;
    $(".time", node).textContent = later ? `${c.time_et} ET (approx.)` : `${c.time_et} ET`;
    $(".local", node).textContent = sameZoneAsET ? (later ? "after first case" : "") :
      start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZoneName: "short" }) + (later ? " approx." : "");
    $(".title", node).textContent = c.title;
    $(".sub", node).textContent = [`No. ${c.docket}`, c.lower_court ? `from the ${c.lower_court.replace(/^United States /, "U.S. ")}` : null]
      .filter(Boolean).join(" · ");
    $(".ask", node).textContent = c.question_plain || "";
    const sum = $(".summary", node);
    const text = c.overview || c.summary;
    if (text) {
      sum.textContent = text;
      sum.insertAdjacentHTML("beforeend", '<span class="tag">· Summary written by Claude (AI)</span>');
    } else sum.textContent = c.question || "";
    const why = c.why_it_matters || [];
    if (why.length) {
      $(".why ul", node).innerHTML = why.map((w) => `<li>${esc(w.text)} <a class="src" href="${esc(w.source.url)}" target="_blank" rel="noopener">${esc(w.source.name)}</a></li>`).join("");
    } else $(".why", node).remove();
    if (text && c.question) $(".qp p", node).textContent = c.question; else $(".qp", node).remove();
    $(".note", node).textContent = c.note || "";

    const a = $(".actions", node);
    const n = daysBetween(today, c.date);
    if (c.argued && c.links.recording) {
      a.insertAdjacentHTML("beforeend", `<a class="oac-btn primary" href="${esc(c.links.recording)}" target="_blank" rel="noopener">▶ Listen to recording</a>`);
    } else if (n === 0) {
      a.insertAdjacentHTML("beforeend", `<a class="oac-btn live" href="${esc(c.links.live)}" target="_blank" rel="noopener">Listen live</a>
        <span class="pending">Recording posted later today</span>`);
    } else if (n > 0) {
      a.insertAdjacentHTML("beforeend", `<a class="oac-btn" href="${icsHref(c)}" download="${esc(c.docket)}.ics">＋ Add to calendar</a>
        <span class="pending">Live audio on supremecourt.gov that morning</span>`);
    } else {
      a.insertAdjacentHTML("beforeend", `<a class="oac-btn primary" href="${esc(c.links.recording_expected)}" target="_blank" rel="noopener">▶ Recording</a>`);
    }
    a.insertAdjacentHTML("beforeend", `<a class="oac-btn quiet" href="${esc(c.links.qp)}" target="_blank" rel="noopener">Question presented (PDF)</a>
      <a class="oac-btn quiet" href="${esc(c.links.docket)}" target="_blank" rel="noopener">Docket</a>`);
    return node;
  }

  function renderScotus() {
    const today = todayET();
    const q = $("#scotus-q").value.trim().toLowerCase();
    let list = scotus.cases.filter((c) => inRange(c, today));
    if (q) list = scotus.cases.filter((c) => [c.title, c.docket, c.overview, c.question_plain, c.question, c.lower_court, ...(c.why_it_matters || []).map((w) => w.text)].join(" ").toLowerCase().includes(q));
    list.sort((x, y) => (range === "past" && !q ? -1 : 1) * (x.date.localeCompare(y.date) || (x.order || 1) - (y.order || 1)));

    const box = $("#scotus-list");
    box.innerHTML = "";
    if (!list.length) {
      const next = scotus.cases.filter((c) => c.date >= today).sort((a, b) => a.date.localeCompare(b.date))[0];
      box.innerHTML = `<p class="empty">No arguments ${q ? "match that search" : "in this window"}.` +
        (next && !q ? ` Next up: <strong>${esc(next.title)}</strong> on ${fmtDay(next.date)}.` : "") + "</p>";
    }
    let day = null, group = null;
    for (const c of list) {
      if (c.date !== day) {
        day = c.date;
        const [rel, cls] = relLabel(day, today);
        group = document.createElement("section");
        group.className = "day";
        group.innerHTML = `<h2>${fmtDay(day)} <span class="rel ${cls}">${rel}</span></h2>`;
        box.appendChild(group);
      }
      group.appendChild(scotusCard(c, today));
    }
    const upcoming = scotus.cases.filter((c) => c.date >= today).length;
    $("#scotus-meta").textContent = `${scotus.term || ""} · ${upcoming} case${upcoming === 1 ? "" : "s"} scheduled · times are Eastern` +
      (sameZoneAsET ? "" : `, with your local time (${localTZ.replace(/_/g, " ")}) below`);
  }

  // ---------- Courts of Appeals ----------
  let circuits = { cases: [] }, upcoming = { cases: [], courts: [] }, courts = [];
  let circuitFilter = "all", shown = 40, cView = "upcoming", cRange = "week", upFilter = "all", upShown = 60;
  const courtShort = (id) => (courts.find((c) => c.id === id) || {}).short || id;
  const dur = (s) => (s ? `${Math.round(s / 60)} min` : "");

  function upRow(c, today) {
    const row = document.createElement("div");
    row.className = "up-row" + (c.no_argument ? " noarg" : "");
    let when = "Time TBA", local = "";
    if (c.time_local) {
      const inst = instantIn(c.date, c.time_local, c.tz);
      when = `${fmtTime(inst, c.tz)} ${tzAbbr(inst, c.tz)}`;
      if (tzAbbr(inst, c.tz) !== tzAbbr(inst, localTZ)) local = `${fmtTime(inst, localTZ)} ${tzAbbr(inst, localTZ)}`;
    }
    const n = daysBetween(today, c.date);
    const sub = [courtShort(c.court), `No. ${c.docket}`, c.origin, c.location, c.room].filter(Boolean).map(esc).join(" · ");
    let act = "";
    if (c.links.recording) act += `<a class="oac-btn primary" href="${esc(c.links.recording)}" target="_blank" rel="noopener">▶ Recording</a>`;
    else if (c.no_argument) act += `<span class="pending">Decided on the briefs, no argument</span>`;
    else if (n === 0) act += `<a class="oac-btn live" href="${esc(c.links.live)}" target="_blank" rel="noopener">Listen live</a>`;
    else if (n > 0) act += `<a class="oac-btn" href="${esc(c.links.live)}" target="_blank" rel="noopener">Live stream page</a>`;
    else act += `<span class="pending">Recording usually posted within a day</span>`;
    act += `<a class="oac-btn quiet" href="${esc(c.links.calendar)}" target="_blank" rel="noopener">Court calendar (PDF)</a>`;
    row.innerHTML = `<div class="when"><span class="time">${esc(when)}</span><span class="local">${esc(local)}</span></div>
      <div class="body"><div class="t">${esc(c.title)}</div><div class="d">${sub}</div>
      ${c.issue ? `<p class="issue"><span class="lbl">From the court's calendar:</span> ${esc(c.issue)}</p>` : ""}
      ${c.notes ? `<p class="d">${esc(c.notes.join(" · "))}</p>` : ""}
      ${c.live_note && n >= 0 ? `<p class="d">${esc(c.live_note)}</p>` : ""}
      <div class="actions">${act}</div></div>`;
    return row;
  }

  function renderUpcoming() {
    const today = todayET();
    const q = $("#up-q").value.trim().toLowerCase();
    const withNoArg = $("#up-noarg").checked;
    let list = upcoming.cases.filter((c) => {
      const n = daysBetween(today, c.date);
      if (cRange === "today" && n !== 0) return false;
      if (cRange === "week" && (n < 0 || n > 7)) return false;
      if (cRange === "month" && (n < 0 || n > 30)) return false;
      if (cRange === "all" && n < 0) return false;
      if (cRange === "past" && n >= 0) return false;
      return (upFilter === "all" || c.court === upFilter) && (withNoArg || !c.no_argument);
    });
    if (q) list = list.filter((c) => [c.title, c.docket, c.issue, c.origin].join(" ").toLowerCase().includes(q));
    const at = (c) => (c.time_local ? instantIn(c.date, c.time_local, c.tz).getTime() : Date.parse(c.date + "T23:59:00Z"));
    list.sort((a, b) => a.date.localeCompare(b.date) || at(a) - at(b) || a.court.localeCompare(b.court));
    if (cRange === "past") list.reverse();
    const box = $("#up-list");
    box.innerHTML = "";
    if (!upcoming.cases.length) {
      box.innerHTML = '<p class="empty">The first calendar update hasn\'t run yet. Upcoming arguments will appear here after the next daily update.</p>';
    } else if (!list.length) {
      const next = upcoming.cases.filter((c) => c.date >= today && (upFilter === "all" || c.court === upFilter)).sort((a, b) => a.date.localeCompare(b.date))[0];
      box.innerHTML = `<p class="empty">No arguments ${q ? "match that search" : "in this window"}.` + (next && !q ? ` Next: ${esc(courtShort(next.court))} on ${fmtDay(next.date)}.` : "") + "</p>";
    }
    let day = null, group = null;
    for (const c of list.slice(0, upShown)) {
      if (c.date !== day) {
        day = c.date;
        const [rel, cls] = relLabel(day, today);
        group = document.createElement("section");
        group.className = "day";
        group.innerHTML = `<h2>${fmtDay(day)} <span class="rel ${cls}">${rel}</span></h2>`;
        box.appendChild(group);
      }
      group.appendChild(upRow(c, today));
    }
    if (list.length > upShown) {
      const more = document.createElement("button");
      more.className = "oac-btn more"; more.textContent = `Show more (${list.length - upShown})`;
      more.onclick = () => { upShown += 60; renderUpcoming(); };
      box.appendChild(more);
    }
    const covered = (upcoming.courts || []).map(courtShort).join(", ");
    $("#up-meta").textContent = `${list.length} argument${list.length === 1 ? "" : "s"} · calendars from ${covered || "—"} · times shown in each court's local time` +
      (withNoArg ? "" : " · cases decided without argument hidden");
  }

  function renderUpChips() {
    const present = new Set(upcoming.cases.map((c) => c.court));
    const ids = ["all", ...courts.filter((c) => c.tier === 2 && present.has(c.id)).map((c) => c.id)];
    $("#up-filter").innerHTML = ids.map((id) =>
      `<button data-c="${id}" class="${id === upFilter ? "on" : ""}">${id === "all" ? "All" : esc(courtShort(id))}</button>`).join("");
  }

  function renderCircuits() {
    const q = $("#circuit-q").value.trim().toLowerCase();
    let list = circuits.cases.filter((c) => circuitFilter === "all" || c.court === circuitFilter);
    if (q) list = list.filter((c) => (c.title + " " + c.docket).toLowerCase().includes(q));
    const box = $("#circuit-list");
    box.innerHTML = list.length ? "" : '<p class="empty">No recordings match.</p>';
    for (const c of list.slice(0, shown)) {
      const row = document.createElement("div");
      row.className = "oa-row";
      row.innerHTML = `<span class="badge">${esc(courtShort(c.court))}</span>
        <div><div class="t">${esc(c.title)}</div><div class="d">${fmtShort(c.date)} · No. ${esc(c.docket)}${c.duration_sec ? " · " + dur(c.duration_sec) : ""} · <a href="${esc(c.page)}" target="_blank" rel="noopener">details</a></div></div>
        <button class="oac-btn primary">▶ Play</button>`;
      $("button", row).addEventListener("click", (e) => {
        if (!c.audio_mp3) return;
        e.currentTarget.remove();
        const au = document.createElement("audio");
        au.controls = true; au.preload = "none"; au.src = c.audio_mp3;
        au.addEventListener("error", () => au.replaceWith(Object.assign(document.createElement("a"), {
          href: c.page, target: "_blank", rel: "noopener", className: "oac-btn", textContent: "Open recording on CourtListener ↗" })));
        row.appendChild(au); au.play().catch(() => {});
      });
      box.appendChild(row);
    }
    if (list.length > shown) {
      const more = document.createElement("button");
      more.className = "oac-btn more"; more.textContent = `Show more (${list.length - shown})`;
      more.onclick = () => { shown += 40; renderCircuits(); };
      box.appendChild(more);
    }
    $("#circuit-meta").textContent = `${list.length} recording${list.length === 1 ? "" : "s"} from the last ${circuits.window_days || 7} days · via CourtListener`;
  }

  function renderCircuitChips() {
    const present = new Set(circuits.cases.map((c) => c.court));
    const ids = ["all", ...courts.filter((c) => c.tier === 2 && present.has(c.id)).map((c) => c.id)];
    $("#circuit-filter").innerHTML = ids.map((id) =>
      `<button data-c="${id}" class="${id === circuitFilter ? "on" : ""}">${id === "all" ? "All" : esc(courtShort(id))}</button>`).join("");
  }

  function setCView(v) {
    cView = v;
    document.querySelectorAll("#cview button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
    $("#c-upcoming").hidden = v !== "upcoming";
    $("#c-recent").hidden = v !== "recent";
  }

  function renderDirectory() {
    $("#court-directory").innerHTML = courts.filter((c) => c.tier === 2).map((c) => `
      <div class="court"><h3>${esc(c.name.replace("U.S. Court of Appeals for the ", ""))}</h3>
      <p>${esc(c.listen_note || "")}</p>
      <a href="${esc(c.live)}" target="_blank" rel="noopener">Live</a>
      <a href="${esc(c.recordings)}" target="_blank" rel="noopener">Recordings</a>
      <a href="${esc(c.calendar || c.home)}" target="_blank" rel="noopener">${c.calendar ? "Calendar" : "Court site"}</a></div>`).join("");
  }

  // ---------- wiring ----------
  function selectTab(name) {
    document.querySelectorAll(".oac-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    document.querySelectorAll(".oac .panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
    history.replaceState(null, "", `#${name}`);
  }
  document.querySelectorAll(".oac-tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("#scotus-range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    range = b.dataset.range;
    document.querySelectorAll("#scotus-range button").forEach((x) => x.classList.toggle("on", x === b));
    renderScotus();
  });
  $("#scotus-q").addEventListener("input", renderScotus);
  $("#circuit-q").addEventListener("input", () => { shown = 40; renderCircuits(); });
  $("#cview").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setCView(b.dataset.v); });
  $("#up-range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    cRange = b.dataset.range; upShown = 60;
    document.querySelectorAll("#up-range button").forEach((x) => x.classList.toggle("on", x === b));
    renderUpcoming();
  });
  $("#up-filter").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    upFilter = b.dataset.c; upShown = 60; renderUpChips(); renderUpcoming();
  });
  $("#up-q").addEventListener("input", () => { upShown = 60; renderUpcoming(); });
  $("#up-noarg").addEventListener("change", renderUpcoming);
  $("#circuit-filter").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    circuitFilter = b.dataset.c; shown = 40; renderCircuitChips(); renderCircuits();
  });

  const load = (p) => fetch(p, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : Promise.reject(p)));
  Promise.allSettled([load("data/scotus.json"), load("data/circuits_recent.json"), load("data/courts.json"),
    load("data/circuits_upcoming.json")]).then(([s, ci, co, up]) => {
    if (s.status === "fulfilled") scotus = s.value;
    if (ci.status === "fulfilled") circuits = ci.value;
    if (co.status === "fulfilled") courts = co.value.courts;
    if (up.status === "fulfilled") upcoming = up.value;
    renderUpChips(); renderUpcoming();
    // Default view: today if the Court sits today, otherwise the next 30 days.
    if (scotus.cases.some((c) => c.date === todayET())) $('#scotus-range [data-range="today"]').click(); else renderScotus();
    renderCircuitChips(); renderCircuits(); renderDirectory();
    const when = scotus.generated_at && new Date(scotus.generated_at);
    if (when) $("#updated").textContent = `Last updated ${when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}.`;
    const h = location.hash.slice(1); if (["scotus", "circuits", "district"].includes(h)) selectTab(h);
  });
})();
