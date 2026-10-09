/* Unified Workstation — application window.
   No framework, no build step, no network beyond this computer. Text never enters the page as
   HTML: every node is created with h(), so nothing a server or a file name says can run. */
"use strict";

const TOKEN = document.querySelector('meta[name="session-token"]').content;
const $main = document.getElementById("main");
const $rail = document.getElementById("rail");
const $app = document.getElementById("app");
const $wizard = document.getElementById("wizard");
const $toasts = document.getElementById("toasts");
let boot = null;
let catalog = {};
let current = "dashboard";
let refreshTimer = null;

// ── basics ──────────────────────────────────────────────────────────────────
function t(key, vars) {
  let text = catalog[key];
  if (text === undefined) return key;
  if (vars) for (const [k, v] of Object.entries(vars)) text = text.split("{" + k + "}").join(String(v));
  return text;
}
function has(key) { return catalog[key] !== undefined; }
function tOr(key, fallback) { return has(key) ? catalog[key] : fallback; }

// Appends children, flattening arrays and skipping null/false (the native append would print them).
function put(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style") el.style.cssText = v;  // CSSOM, so the strict style policy holds
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "hidden" || k === "open") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  return put(el, ...children);
}

class ProblemError extends Error {
  constructor(problem) { super(problem.code); this.problem = problem; }
}

async function api(method, params) {
  let reply;
  try {
    const res = await fetch("/api", { method: "POST", headers: { "Content-Type": "application/json", "X-Session-Token": TOKEN }, body: JSON.stringify({ method, params: params || {} }) });
    reply = await res.json();
  } catch (_err) {
    throw new ProblemError({ code: "window_disconnected", actions: ["close"], detail: "", params: {} });
  }
  if (reply.problem) throw new ProblemError(reply.problem);
  return reply.result;
}

function toast(message) {
  const el = h("div", { class: "toast", text: message });
  put($toasts, el);
  setTimeout(() => el.remove(), 4200);
}

function fmtBytes(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return new Intl.NumberFormat(boot.language, { maximumFractionDigits: n < 10 && i > 0 ? 1 : 0 }).format(n) + " " + units[i];
}
function fmtNum(n) { return new Intl.NumberFormat(boot.language).format(Number(n) || 0); }
function fmtTime(value) {
  if (!value) return t("time.never");
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (isNaN(date)) return String(value);
  const seconds = (Date.now() - date.getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(boot.language, { numeric: "auto" });
  if (seconds < 60) return t("time.just_now");
  if (seconds < 3600) return rtf.format(-Math.round(seconds / 60), "minute");
  if (seconds < 86400) return rtf.format(-Math.round(seconds / 3600), "hour");
  if (seconds < 86400 * 14) return rtf.format(-Math.round(seconds / 86400), "day");
  return new Intl.DateTimeFormat(boot.language, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
function osName(id) { return { linux: "Linux", macos: "macOS", windows: "Windows" }[id] || id || "—"; }

// A state is always a symbol and a word; colour only repeats what they already say.
const STATES = {
  SYNCED: ["ok", "●"], CONNECTED: ["ok", "●"], ONLINE: ["ok", "●"], READY: ["ok", "●"], SUPPORTED: ["ok", "●"], pass: ["ok", "✓"], PASS: ["ok", "✓"], granted: ["ok", "✓"],
  SYNCING: ["busy", "◐"], WAITING: ["busy", "◐"], DEGRADED: ["warn", "▲"],
  CONFLICT: ["warn", "▲"], PAUSED: ["warn", "‖"], PARTIALLY_SUPPORTED: ["warn", "◐"], REQUIRES_PERMISSION: ["warn", "▲"], warn: ["warn", "▲"], WARN: ["warn", "▲"], AUTH_REQUIRED: ["warn", "▲"], UNTRUSTED: ["warn", "▲"], missing: ["warn", "▲"],
  ERROR: ["err", "✕"], MISSING: ["err", "✕"], fail: ["err", "✕"], FAIL: ["err", "✕"],
  OFFLINE: ["idle", "○"], NOT_CONFIGURED: ["idle", "○"], NOT_INSTALLED: ["idle", "○"], UNAVAILABLE: ["idle", "—"], UNKNOWN: ["idle", "?"], skip: ["idle", "—"], unknown: ["idle", "?"], not_needed: ["idle", "—"], NONE: ["idle", "—"],
};
function state(word, label) {
  const [cls, symbol] = STATES[word] || ["idle", "?"];
  return h("span", { class: "state " + cls, "data-symbol": symbol }, label || tOr("state." + word, word));
}

function button(label, onclick, cls) {
  const el = h("button", { type: "button", class: "btn " + (cls || ""), text: label });
  el.addEventListener("click", async () => {
    if (el.getAttribute("aria-busy") === "true") return;
    el.setAttribute("aria-busy", "true");
    el.disabled = true;
    try { await onclick(el); } catch (err) { await showProblem(err); } finally { el.removeAttribute("aria-busy"); el.disabled = false; }
  });
  return el;
}

// ── dialogs ─────────────────────────────────────────────────────────────────
function dialog(title, body, actions, opts) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: (opts && opts.wide) ? "wide" : "", "aria-labelledby": "dlg-title" });
    const footer = h("footer");
    const close = (value) => { dlg.close(); dlg.remove(); resolve(value); };
    for (const action of actions) {
      const el = h("button", { type: "button", class: "btn " + (action.cls || ""), text: action.label });
      el.addEventListener("click", async () => {
        if (!action.run) return close(action.value);
        el.disabled = true;
        try { const out = await action.run(); if (out !== false) close(out === undefined ? action.value : out); }
        catch (err) { await showProblem(err); } finally { el.disabled = false; }
      });
      put(footer, el);
    }
    put(dlg, h("div", { class: "body" }, h("h2", { id: "dlg-title", text: title }), body, footer));
    dlg.addEventListener("cancel", (event) => { event.preventDefault(); close(null); });
    put(document.body, dlg);
    dlg.showModal();
  });
}
function confirmDialog(title, message, okLabel, danger) {
  return dialog(title, typeof message === "string" ? h("p", { text: message }) : message, [
    { label: t("action.cancel"), value: false },
    { label: okLabel || t("action.continue"), value: true, cls: danger ? "danger" : "primary" },
  ]);
}

// Every failure answers: what happened, why, what can I do.
async function showProblem(err) {
  if (!(err instanceof ProblemError)) { console.error(err); err = new ProblemError({ code: "unexpected", actions: ["close"], detail: String(err && err.message || err), params: {} }); }
  const p = err.problem;
  const base = "problem." + (has("problem." + p.code + ".what") ? p.code : "unexpected");
  const params = p.params || {};
  if (params.role) params.role = tOr("server." + params.role, params.role);
  const body = h("div", { class: "stack" },
    h("p", { text: t(base + ".what", params) }),
    has(base + ".why") ? h("p", { class: "muted", text: t(base + ".why", params) }) : null,
    has(base + ".fix") ? h("p", { text: t(base + ".fix", params) }) : null,
    p.detail ? h("details", {}, h("summary", { text: t("action.details") }), h("div", { class: "code small", text: p.detail })) : null);
  const actions = [];
  for (const action of p.actions || []) {
    if (action === "retry" || action === "details" || action === "close") continue;
    const page = { open_capabilities: "health", open_setup: "settings", repair: "health", review_fingerprint: "servers" }[action];
    if (page) actions.push({ label: t("action." + action), value: page });
  }
  actions.push({ label: t("action.close"), value: null, cls: "primary" });
  const go = await dialog(t("problem.title"), body, actions);
  if (go) navigate(go);
}

async function pickFolder(start) {
  let data = await api("browse", { path: start || "" });
  const list = h("div", { class: "folderlist", role: "listbox", "aria-label": t("folder.folders") });
  const where = h("div", { class: "code small" });
  const problem = h("p", { class: "note warn", hidden: true });
  const render = () => {
    where.textContent = data.path;
    problem.hidden = !data.problem;
    problem.textContent = data.problem ? t("folder.problem") + " " + data.problem : "";
    list.replaceChildren();
    if (data.parent) put(list, h("button", { type: "button", text: "↑ " + t("folder.up"), onclick: async () => { data = await api("browse", { path: data.parent }); render(); } }));
    for (const name of data.folders) put(list, h("button", { type: "button", text: name, onclick: async () => { data = await api("browse", { path: data.path + "/" + name }); render(); } }));
    if (!data.folders.length) put(list, h("div", { class: "muted", style: "padding:8px 12px", text: t("folder.empty") }));
  };
  render();
  const name = h("input", { type: "text", "aria-label": t("folder.new_name"), placeholder: t("folder.new_name") });
  const make = button(t("folder.create"), async () => { if (!name.value.trim()) return; data = await api("make_folder", { parent: data.path, name: name.value }); name.value = ""; render(); });
  const body = h("div", { class: "stack" }, where, list, h("div", { class: "pathbox" }, name, make), problem);
  return dialog(t("folder.title"), body, [
    { label: t("action.cancel"), value: null },
    { label: t("folder.choose"), cls: "primary", run: () => { if (data.problem) return false; return data.path; } },
  ]);
}

function download(info) {
  const link = h("a", { href: "/download?id=" + encodeURIComponent(info.download) + "&k=" + encodeURIComponent(TOKEN), download: info.name });
  put(document.body, link); link.click(); link.remove();
}

// ── navigation ──────────────────────────────────────────────────────────────
const PAGES = ["dashboard", "workstations", "workspace", "sync", "peripherals", "servers", "assistant", "settings", "health", "recovery", "help"];
const renderers = {};

function buildRail(attention) {
  const counts = {};
  for (const item of attention || []) counts[item.page] = (counts[item.page] || 0) + 1;
  $rail.replaceChildren();
  put($rail,
    h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), h("span", { text: boot.product.name })),
    boot.demo ? h("div", { class: "demo-flag", text: t("demo.flag") }) : null,
    PAGES.map((id) => h("button", { type: "button", "aria-current": id === current ? "page" : false, onclick: () => navigate(id) },
      h("span", { text: t("page." + id) }),
      counts[id] ? h("span", { class: "badge", "aria-label": t("nav.attention", { n: counts[id] }), text: counts[id] }) : null)),
    h("div", { class: "gap" }),
    h("div", { class: "foot" }, boot.product.name + " " + boot.product.version, h("br"), t("channel." + boot.product.channel)));
}

async function navigate(id) {
  if (!renderers[id]) id = "dashboard";
  current = id;
  clearInterval(refreshTimer);
  buildRail(lastAttention);
  await draw();
  $main.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}
let lastAttention = [];
async function draw() {
  const page = h("div", { class: "page" });
  try { await renderers[current](page); }
  catch (err) { page.replaceChildren(pageHeader(t("page." + current)), h("div", { class: "note err", text: t("problem.page") })); $main.replaceChildren(page); await showProblem(err); return; }
  $main.replaceChildren(page);
}
function pageHeader(title, lead, ...actions) {
  return h("header", {}, h("div", {}, h("h1", { text: title }), lead ? h("p", { text: lead }) : null), actions.length ? h("div", { class: "row-actions" }, actions) : null);
}
function rows(items, cls) { return h("div", { class: "rows " + (cls || "") }, items); }
function row(name, what, action) { return h("div", { class: "r" }, h("div", { class: "name" }, name), h("div", { class: "what" }, what), h("div", { class: "row-actions" }, action || null)); }
function detail(text) { return h("span", { class: "detail", text }); }

// ── dashboard ───────────────────────────────────────────────────────────────
renderers.dashboard = async (page) => {
  const d = await api("dashboard");
  lastAttention = d.attention;
  buildRail(d.attention);
  const working = d.mode === "workstation";
  const modeButton = d.kind === "workstation"
    ? button(working ? t("mode.leave") : t("mode.enter"), async () => {
        if (working) {
          const choice = await dialog(t("mode.leave_title"), h("p", { text: t("mode.leave_text") }), [
            { label: t("action.cancel"), value: null }, { label: t("mode.keep_open"), value: "keep" }, { label: t("mode.save_close"), value: "close", cls: "primary" }]);
          if (!choice) return;
          await api("set_mode", { mode: "default", save: choice === "close", close: choice === "close" });
        } else { await api("set_mode", { mode: "workstation" }); }
        await draw();
      }, working ? "" : "primary")
    : null;
  put(page, pageHeader(t("page.dashboard"), t("dashboard.lead", { device: d.device }), modeButton));

  const verdict = h("div", { class: "verdict " + (d.ready ? "ok" : "warn") },
    h("div", { class: "headline" }, state(d.ready ? "READY" : "warn", d.ready ? t("dashboard.ready") : t("dashboard.attention"))),
    h("div", { class: "muted", text: working ? t("mode.is_workstation") : t("mode.is_default") }));
  if (d.attention.length) {
    put(verdict, h("ul", {}, d.attention.map((item) => h("li", {},
      h("span", { text: t("attention." + (has("attention." + item.code) ? item.code : "generic"), { name: tOr("server." + item.name, item.name || "") }) }),
      h("button", { type: "button", class: "btn quiet", text: t("action.open_page", { page: t("page." + item.page) }), onclick: () => navigate(item.page) })))));
  }
  put(page, verdict);

  const personal = d.kind !== "workstation";
  const list = [];
  list.push(row(t("dashboard.workspace"), [state(d.workspace.state), detail(d.workspace.path)], h("button", { type: "button", class: "btn quiet", text: t("action.open_folder"), onclick: () => api("open_path", { what: "workspace" }).catch(showProblem) })));
  list.push(row(t("dashboard.sync"), [state(d.sync.state), detail(personal ? t("dashboard.sync_personal") : syncDetail(d.sync))], linkTo("sync")));
  const others = d.workstations.filter((w) => !w.self);
  list.push(row(t("dashboard.workstations"), others.length
    ? others.map((w) => h("span", {}, state(w.online ? "ONLINE" : "OFFLINE", w.name + " — " + t(w.online ? "state.ONLINE" : "state.OFFLINE"))))
    : [state("NOT_CONFIGURED", t("dashboard.no_workstations"))], linkTo("workstations")));
  list.push(row(t("dashboard.peripherals"), [state(d.peripherals.state)], linkTo("peripherals")));
  for (const s of d.servers) list.push(row(t("server." + s.role), [state(s.status), detail(s.configured ? serverDetail(s) : t("dashboard.optional"))], linkTo("servers")));
  list.push(row(t("dashboard.updates"), d.updates.available ? [state("warn", t("updates.available", { version: d.updates.available }))] : [state("READY", t("updates.current", { version: d.updates.current }))], linkTo("settings")));
  put(page, h("section", { "aria-label": t("dashboard.status") }, rows(list)));
  if (personal) put(page, h("div", { class: "note" }, h("p", { text: t("dashboard.personal_note") }), h("div", { class: "row-actions", style: "margin-top:8px" }, button(t("dashboard.setup_workstation"), () => startWizard({ kind: "workstation" })))));
  refreshTimer = setInterval(() => { if (current === "dashboard" && !document.querySelector("dialog[open]") && !document.hidden) draw(); }, 15000);
};
function linkTo(id) { return h("button", { type: "button", class: "btn quiet", text: t("action.manage"), "aria-label": t("action.open_page", { page: t("page." + id) }), onclick: () => navigate(id) }); }
function syncDetail(s) {
  if (s.state === "SYNCING" && s.need_items) return t("sync.items_left", { n: fmtNum(s.need_items) });
  if (s.state === "NOT_CONFIGURED") return tOr("sync.reason." + s.reason, "");
  return s.last_sync ? t("sync.last", { when: fmtTime(s.last_sync) }) : "";
}
function serverDetail(s) {
  const r = s.report || {};
  const parts = [];
  if (r.cpu_pct !== undefined && r.cpu_pct !== null) parts.push("CPU " + Math.round(r.cpu_pct) + "%");
  if (r.ram_used_pct !== undefined && r.ram_used_pct !== null) parts.push("RAM " + Math.round(r.ram_used_pct) + "%");
  if (r.disk_used_pct !== undefined && r.disk_used_pct !== null) parts.push(t("server.disk") + " " + Math.round(r.disk_used_pct) + "%");
  if (s.hardware && s.hardware.gpu) parts.push(s.hardware.gpu);
  return parts.join(" · ");
}

// ── workstations and pairing ────────────────────────────────────────────────
renderers.workstations = async (page) => {
  const data = await api("workstations");
  put(page, pageHeader(t("page.workstations"), t("workstations.lead"),
    button(t("pair.add"), addWorkstation, "primary"), button(t("pair.join"), joinWorkstation)));
  put(page, rows(data.rows.map((w) => row(
    h("span", {}, w.name, w.self ? h("span", { class: "muted", text: " · " + t("workstations.this") }) : null),
    [state(w.online ? "ONLINE" : "OFFLINE"), detail([osName(w.platform), w.version ? t("workstations.version", { v: w.version }) : "", !w.self && w.completion !== null && w.completion !== undefined ? t("workstations.completion", { n: Math.round(w.completion) }) : "", !w.self && !w.online ? t("workstations.last_seen", { when: fmtTime(w.last_seen) }) : ""].filter(Boolean).join(" · "))],
    [button(t("action.rename"), () => renameDevice(w.name)), w.self ? null : button(t("pair.unpair"), () => unpair(w.name), "danger")]))));
  put(page, h("section", {}, h("h2", { text: t("workstations.caps") }), h("p", { text: t("workstations.caps_lead") }), featureTable(data.capabilities)));
};
function featureTable(features) {
  return h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: t("health.feature") }), h("th", { text: t("health.status") }), h("th", { text: t("health.explanation") }))),
    h("tbody", {}, features.map((f) => h("tr", {}, h("td", { text: tOr("feature." + f.id, f.id) }), h("td", {}, state(f.status)), h("td", {}, f.reason, f.action ? h("div", { class: "muted small", text: f.action }) : null))))));
}
async function addWorkstation() {
  const offer = await api("pair_offer");
  const body = h("div", { class: "stack" }, h("p", { text: t("pair.add_text") }), h("div", { class: "code", text: offer.code, tabindex: "0", "aria-label": t("pair.code") }),
    h("div", { class: "row-actions" }, button(t("action.copy"), async () => { await navigator.clipboard.writeText(offer.code); toast(t("toast.copied")); })),
    h("p", { class: "muted", text: t("pair.add_next") }));
  const next = await dialog(t("pair.add"), body, [{ label: t("action.close"), value: null }, { label: t("pair.enter_other"), value: "join", cls: "primary" }]);
  if (next === "join") await joinWorkstation();
}
async function joinWorkstation() {
  const input = h("textarea", { "aria-label": t("pair.code"), placeholder: "UW1-…", spellcheck: "false", autocomplete: "off" });
  const review = await dialog(t("pair.join"), h("div", { class: "stack" }, h("p", { text: t("pair.join_text") }), input), [
    { label: t("action.cancel"), value: null },
    { label: t("action.continue"), cls: "primary", run: async () => { if (!input.value.trim()) return false; return { code: input.value, review: await api("pair_review", { code: input.value }) }; } }]);
  if (!review) return;
  const r = review.review;
  if (r.problems.length) { await dialog(t("pair.blocked"), h("div", { class: "note warn", text: r.problems[0] }), [{ label: t("action.close"), value: null, cls: "primary" }]); return; }
  const number = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", maxlength: "6", "aria-label": t("pair.number_label"), class: "mono" });
  const optSync = h("input", { type: "checkbox", checked: r.sync, disabled: !r.sync });
  const optInput = h("input", { type: "checkbox", checked: r.peripherals, disabled: !r.peripherals });
  const merge = h("input", { type: "checkbox" });
  const body = h("div", { class: "stack" },
    h("dl", { class: "kv" }, h("dt", { text: t("pair.other") }), h("dd", { text: r.theirs.name + " · " + osName(r.theirs.platform) }),
      h("dt", { text: t("pair.files_there") }), h("dd", { text: t("pair.files", { n: fmtNum(r.theirs.files), size: fmtBytes(r.theirs.bytes) }) }),
      h("dt", { text: t("pair.files_here") }), h("dd", { text: t("pair.files", { n: fmtNum(r.mine.files), size: fmtBytes(r.mine.bytes) }) })),
    h("div", { class: "note" + (r.both_have_files ? " warn" : ""), text: r.both_have_files ? t("pair.merge_both") : t("pair.merge_one") }),
    h("p", { text: t("pair.number_text") }), h("div", { class: "bignum", text: r.confirmation, "aria-label": t("pair.number_here") }),
    h("p", { class: "muted", text: t("pair.my_code_text") }), h("div", { class: "code small", text: r.my_code }),
    h("label", { class: "check" }, optSync, h("span", {}, t("pair.opt_sync"))),
    h("label", { class: "check" }, optInput, h("span", {}, t("pair.opt_input"))),
    r.both_have_files ? h("label", { class: "check" }, merge, h("span", {}, t("pair.merge_confirm"))) : null,
    h("label", {}, t("pair.number_label") + " ", number));
  const done = await dialog(t("pair.confirm_title", { name: r.theirs.name }), body, [
    { label: t("action.cancel"), value: null },
    { label: t("pair.trust"), cls: "primary", run: () => api("pair_accept", { code: review.code, confirmation: number.value, sync: optSync.checked, share_input: optInput.checked, merge_confirmed: merge.checked || !r.both_have_files }) }], { wide: true });
  if (done) { toast(t("toast.paired", { name: done.device })); await draw(); }
}
async function unpair(name) {
  if (!(await confirmDialog(t("pair.unpair_title", { name }), t("pair.unpair_text"), t("pair.unpair"), true))) return;
  await api("unpair", { name }); toast(t("toast.unpaired", { name })); await draw();
}
async function renameDevice(old) {
  const input = h("input", { type: "text", value: old, "aria-label": t("workstations.new_name") });
  const ok = await dialog(t("action.rename"), h("div", { class: "stack" }, h("p", { class: "muted", text: t("workstations.name_rule") }), input), [
    { label: t("action.cancel"), value: null }, { label: t("action.save"), cls: "primary", run: async () => { await api("rename_device", { old, new: input.value }); return true; } }]);
  if (ok) await draw();
}

// ── workspace ───────────────────────────────────────────────────────────────
renderers.workspace = async (page) => {
  const w = await api("workspace");
  put(page, pageHeader(t("page.workspace"), t("workspace.lead"), button(t("action.open_folder"), () => api("open_path", { what: "workspace" })), button(t("workspace.change"), changeWorkspace)));
  put(page, rows([
    row(t("workspace.location"), [state(w.exists ? "READY" : "MISSING"), h("span", { class: "detail mono", text: w.path })], w.exists ? null : button(t("workspace.create"), async () => { await api("repair", { what: "create_workspace" }); await draw(); }, "primary")),
    row(t("workspace.contents"), [detail(t("pair.files", { n: fmtNum(w.files), size: fmtBytes(w.bytes) }) + (w.partial ? " · " + t("workspace.partial") : ""))]),
    row(t("workspace.repos"), [detail(w.repos.length ? w.repos.join(", ") : t("workspace.none"))]),
  ]));
  put(page, h("section", {}, h("h2", { text: t("workspace.boundary") }), h("p", { text: t("workspace.boundary_lead") }),
    h("div", { class: "choice" },
      h("div", { class: "note" }, h("strong", { text: t("workspace.shared") }), h("p", { class: "small", text: t("workspace.shared_text") })),
      h("div", { class: "note" }, h("strong", { text: t("workspace.local") }), h("p", { class: "small", text: t("workspace.local_text") })))));
  const large = h("section", {}, h("h2", { text: t("workspace.large") }), h("p", { text: t("workspace.large_lead", { mb: fmtNum(w.large_threshold_mb) }) }));
  put(large, w.large.length ? rows(w.large.map((f) => row(h("span", { class: "mono", text: f.path }), [state(f.held ? "PAUSED" : "SYNCED", f.held ? t("workspace.held") : t("workspace.synced_anyway")), detail(fmtBytes(f.bytes))])), "") : h("div", { class: "empty", text: t("workspace.no_large") }));
  put(page, large);
  put(page, h("section", {}, h("h2", { text: t("workspace.skipped") }), h("p", { text: t("workspace.skipped_lead") }), h("div", { class: "code small", text: w.rules.join("   ") || "—" })));
};
async function changeWorkspace() {
  const chosen = await pickFolder();
  if (!chosen) return;
  if (!(await confirmDialog(t("workspace.change"), t("workspace.change_text", { path: chosen }), t("workspace.use")))) return;
  const out = await api("settings_apply", { changes: { "work.path": chosen } });
  if (out.errors && out.errors["work.path"]) { await dialog(t("problem.title"), h("p", { text: out.errors["work.path"] }), [{ label: t("action.close"), value: null, cls: "primary" }]); return; }
  await draw();
}

// ── sync ────────────────────────────────────────────────────────────────────
renderers.sync = async (page) => {
  const s = await api("sync_status");
  const act = (action) => async () => { await api("sync_action", { action }); await draw(); };
  put(page, pageHeader(t("page.sync"), t("sync.lead"),
    button(t("sync.now"), act("sync_now"), "primary"), s.paused ? button(t("sync.resume"), act("resume")) : button(t("sync.pause"), act("pause")), button(t("sync.scan"), act("scan"))));
  if (!s.installed || !s.enabled || s.reason === "not_configured") {
    put(page, h("div", { class: "empty" }, h("p", { text: tOr("sync.reason." + s.reason, t("sync.reason.not_configured")) }), h("div", { class: "row-actions" }, button(t("action.set_up"), async () => { await api("repair", { what: "sync" }); await draw(); }, "primary"), linkToHealth())));
    return;
  }
  put(page, rows([
    row(t("health.status"), [state(s.state), detail(tOr("sync.reason." + s.reason, ""))], s.state === "ERROR" ? button(t("action.repair"), async () => { await api("repair", { what: "sync" }); await draw(); }, "primary") : null),
    row(t("sync.folder"), [h("span", { class: "detail mono", text: s.path }), detail(t("pair.files", { n: fmtNum(s.files), size: fmtBytes(s.bytes) }))]),
    row(t("sync.pending"), [detail(s.need_items || s.outgoing_items ? t("sync.pending_n", { incoming: fmtNum(s.need_items), outgoing: fmtNum(s.outgoing_items) }) : t("sync.nothing_pending"))]),
    row(t("sync.last_ok"), [detail(fmtTime(s.last_sync))]),
  ]));
  const peers = h("section", {}, h("h2", { text: t("sync.peers") }));
  put(peers, s.peers.length ? rows(s.peers.map((p) => row(p.name, [state(p.connected ? "ONLINE" : "OFFLINE"), detail(p.completion !== null && p.completion !== undefined ? t("workstations.completion", { n: Math.round(p.completion) }) : "")])))
    : h("div", { class: "empty" }, h("p", { text: t("sync.no_peers") }), button(t("pair.add"), () => navigate("workstations"), "primary")));
  put(page, peers);
  if (s.errors.length) put(page, h("section", {}, h("h2", { text: t("sync.errors") }), rows(s.errors.map((e) => row(h("span", { class: "mono", text: e.path || "—" }), [state("ERROR", e.error)])), "")));
  const conflicts = h("section", {}, h("h2", { text: t("sync.conflicts") }), h("p", { text: t("sync.conflicts_lead") }));
  put(conflicts, s.conflicts.length ? rows(s.conflicts.map((c) => row(h("span", { class: "mono small", text: c }), [state("CONFLICT", t("sync.two_versions"))], [
    button(t("sync.keep_current"), async () => { await api("sync_resolve", { conflict: c, keep: "current" }); toast(t("toast.conflict_kept")); await draw(); }),
    button(t("sync.keep_other"), async () => { await api("sync_resolve", { conflict: c, keep: "conflict" }); toast(t("toast.conflict_kept")); await draw(); })])))
    : h("div", { class: "empty", text: t("sync.no_conflicts") }));
  put(page, conflicts);
  put(page, h("section", {}, h("h2", { text: t("sync.versions") }), h("p", { text: t("sync.versions_lead", { n: fmtNum(s.versions) }) }), h("div", { class: "row-actions" }, button(t("sync.view_versions"), showVersions))));
};
function linkToHealth() { return button(t("page.health"), () => navigate("health")); }
async function showVersions() {
  const data = await api("sync_versions", {});
  const body = data.versions.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: t("sync.file") }), h("th", { text: t("sync.saved") }), h("th", { text: t("sync.size") }), h("th", {}))),
    h("tbody", {}, data.versions.map((v) => h("tr", {}, h("td", { class: "mono small", text: v.path }), h("td", { text: fmtTime(v.mtime) }), h("td", { class: "num", text: fmtBytes(v.bytes) }),
      h("td", {}, button(t("sync.restore"), async () => { const out = await api("sync_restore", { path: v.path }); toast(t("toast.restored", { path: out.restored_as })); })))))))
    : h("div", { class: "empty", text: t("sync.no_versions") });
  await dialog(t("sync.versions"), h("div", { class: "stack" }, h("p", { class: "muted", text: t("sync.restore_note") }), body), [{ label: t("action.close"), value: null, cls: "primary" }], { wide: true });
}

// ── keyboard, mouse, clipboard ──────────────────────────────────────────────
renderers.peripherals = async (page) => {
  const p = await api("peripherals_status");
  const act = (action) => async () => { await api("peripherals_action", { action }); await draw(); };
  put(page, pageHeader(t("page.peripherals"), t("peripherals.lead")));
  put(page, h("div", { class: "note" + (p.feature.status === "SUPPORTED" ? "" : " warn") }, state(p.feature.status), h("p", { text: p.feature.reason }), p.feature.action ? h("p", { class: "small muted", text: p.feature.action }) : null));
  if (!p.installed) return;
  if (!p.enabled || p.state === "NOT_CONFIGURED" || p.state === "DISABLED") {
    put(page, h("div", { class: "empty" }, h("p", { text: t("peripherals.not_set_up") }), button(t("action.set_up"), act("setup"), "primary")));
    return;
  }
  put(page, rows([
    row(t("health.status"), [state({ CONNECTED: "CONNECTED", WAITING: "WAITING", UNPAIRED: "NOT_CONFIGURED", STOPPED: "ERROR" }[p.state] || "UNKNOWN"), detail(tOr("peripherals.state." + p.state, ""))],
      [p.state === "STOPPED" ? button(t("action.start"), act("start"), "primary") : button(t("action.restart"), act("restart")), p.state !== "STOPPED" ? button(t("action.stop"), act("stop")) : null]),
    row(t("peripherals.role"), [detail(t("peripherals.role_" + p.role))]),
    row(t("peripherals.clipboard"), [state(p.clipboard ? p.clipboard_feature.status : "PAUSED", p.clipboard ? null : t("state.off")), detail(p.clipboard_feature.reason)]),
    row(t("peripherals.network"), [state(p.exposed ? "warn" : "READY", p.exposed ? t("peripherals.exposed") : t("peripherals.private"))]),
  ]));
  const screens = p.screens.length ? p.screens : [p.screen];
  const ordered = [p.server_device || screens[0], ...screens.filter((n) => n !== (p.server_device || screens[0]))];
  const layout = p.peer_side === "left" ? [...ordered].reverse() : ordered;
  put(page, h("section", {}, h("h2", { text: t("peripherals.arrangement") }), h("p", { text: t("peripherals.arrangement_lead") }),
    h("div", { class: "screens", role: "img", "aria-label": t("peripherals.arrangement") }, layout.map((name) => h("div", { class: "screen" + (name === p.server_device ? " main" : "") }, name, h("small", { text: name === p.server_device ? t("peripherals.has_keyboard") : t("peripherals.controlled") })))),
    h("div", { class: "row-actions" }, button(t("peripherals.change_arrangement"), () => openSettings("peripherals")))));
  if (p.permissions.length) put(page, h("section", {}, h("h2", { text: t("health.permissions") }), permissionList(p.permissions)));
};
function permissionList(list) {
  return rows(list.map((perm) => h("div", { class: "r" }, h("div", { class: "name" }, perm.title, h("div", {}, state(perm.state, tOr("perm.state." + perm.state, perm.state)))),
    h("div", { class: "what" }, h("dl", { class: "kv small" }, h("dt", { text: t("perm.what") }), h("dd", { text: perm.what }), h("dt", { text: t("perm.why") }), h("dd", { text: perm.why }), h("dt", { text: t("perm.effect") }), h("dd", { text: perm.effect }), h("dt", { text: t("perm.revoke") }), h("dd", { text: perm.revoke }))),
    h("div", { class: "row-actions" }, perm.settings_url ? button(t("perm.open"), () => api("open_permission", { permission: perm.id })) : null))));
}

// ── servers ─────────────────────────────────────────────────────────────────
renderers.servers = async (page) => {
  const data = await api("servers");
  put(page, pageHeader(t("page.servers"), t("servers.lead")));
  if (!data.ssh) put(page, h("div", { class: "note warn", text: t("servers.no_ssh") }));
  for (const s of data.rows) {
    const section = h("section", {}, h("h2", { text: t("server." + s.role) }), h("p", { text: t("servers." + s.role + "_lead") }));
    if (!s.configured) {
      put(section, h("div", { class: "empty" }, h("p", { text: t("servers.optional") }), button(t("servers.add_" + s.role), () => editServer(s.role, data, null), "primary")));
    } else {
      put(section, rows([
        row(t("health.status"), [state(s.status), detail(serverDetail(s))], [button(t("action.test"), async () => { await api("server_test", { role: s.role }); toast(t("toast.server_ok", { name: t("server." + s.role) })); await draw(); }), button(t("servers.connect"), () => api("server_connect", { role: s.role }), "primary")]),
        row(t("servers.endpoint"), [h("span", { class: "detail mono", text: (s.user ? s.user + "@" : "") + s.host + (s.port && s.port !== 22 ? ":" + s.port : "") })], [button(s.role === "cloud" ? t("servers.replace") : t("servers.edit"), () => editServer(s.role, data, s)), button(t("action.remove"), () => removeServer(s.role), "danger")]),
      ]));
      if (s.status === "DEGRADED") put(section, h("div", { class: "note warn", text: t("servers.degraded") }));
    }
    put(page, section);
  }
  const projects = await api("cloud_projects");
  const cloud = h("section", {}, h("h2", { text: t("cloud.projects") }), h("p", { text: t("cloud.projects_lead") }));
  if (!projects.configured) put(cloud, h("div", { class: "empty", text: t("cloud.needs_cloud") }));
  else if (!projects.candidates.length) put(cloud, h("div", { class: "empty", text: t("cloud.no_projects") }));
  else put(cloud, rows(projects.candidates.map((name) => row(name, [detail(projects.sent.find((x) => x.name === name) ? t("cloud.sent_before") : t("cloud.not_sent"))], [button(t("cloud.review_send"), () => sendProject(name), "primary"), projects.sent.find((x) => x.name === name) ? button(t("cloud.bring_back"), async () => { await api("cloud_pull", { name }); toast(t("toast.pulled", { name })); }) : null]))));
  put(page, cloud);
};
async function editServer(role, data, existing) {
  const host = h("input", { type: "text", value: existing ? existing.host : "", placeholder: role === "home" ? "home-server.local" : "203.0.113.5", autocomplete: "off", spellcheck: "false", id: "srv-host" });
  const user = h("input", { type: "text", value: existing ? existing.user : (data.user || ""), autocomplete: "off", spellcheck: "false", id: "srv-user" });
  const port = h("input", { type: "number", value: existing ? existing.port : 22, min: "1", max: "65535", id: "srv-port" });
  const key = h("select", { id: "srv-key" }, h("option", { value: "", text: t("servers.key_default") }), data.keys.map((k) => h("option", { value: k, text: k })));
  const form = h("div", { class: "stack" }, h("p", { text: t("servers.form_lead") }),
    h("label", { for: "srv-host", text: t("servers.host") }), host, h("label", { for: "srv-user", text: t("servers.user") }), user, h("label", { for: "srv-port", text: t("servers.port") }), port,
    role === "home" ? [h("label", { for: "srv-key", text: t("servers.key") }), key] : null,
    role === "cloud" && existing ? h("div", { class: "note", text: t("servers.replace_note") }) : null);
  const scanned = await dialog(t(existing ? (role === "cloud" ? "servers.replace" : "servers.edit") : "servers.add_" + role), form, [
    { label: t("action.cancel"), value: null },
    { label: t("servers.check_identity"), cls: "primary", run: async () => { if (!host.value.trim() || !user.value.trim()) return false; return api("server_scan", { host: host.value, port: Number(port.value) || 22 }); } }]);
  if (!scanned) return;
  const body = h("div", { class: "stack" },
    scanned.identity_changed ? h("div", { class: "note err", text: t("servers.identity_changed") }) : null,
    h("p", { text: t("servers.fingerprint_text") }), h("div", { class: "code", text: scanned.fingerprint }),
    h("p", { class: "muted small", text: t("servers.fingerprint_how") }),
    scanned.already_trusted ? h("div", { class: "note", text: t("servers.already_trusted") }) : null);
  const trusted = await dialog(t("servers.fingerprint_title"), body, [{ label: t("action.cancel"), value: false }, { label: t("servers.fingerprint_match"), value: true, cls: scanned.identity_changed ? "danger" : "primary" }]);
  if (!trusted) return;
  if (role === "home") {
    await api("home_save", { host: host.value, user: user.value, port: Number(port.value) || 22, identity: key.value, fingerprint: scanned.fingerprint });
    toast(t("toast.server_ok", { name: t("server.home") }));
  } else {
    const out = await api("cloud_enroll", { host: host.value, user: user.value, port: Number(port.value) || 22, fingerprint: scanned.fingerprint });
    await dialog(t("cloud.active_title"), h("div", { class: "stack" }, h("p", { text: t("cloud.active_text") }),
      rows(out.stages.map((st) => row(st.note || t("cloud.stage." + st.id), [state(st.ok ? "pass" : "warn")])), ""),
      out.retired ? h("div", { class: "note", text: t("cloud.retired", { label: out.retired }) }) : null), [{ label: t("action.close"), value: null, cls: "primary" }]);
  }
  await draw();
}
async function removeServer(role) {
  if (!(await confirmDialog(t("servers.remove_title", { name: t("server." + role) }), t("servers.remove_text"), t("action.remove"), true))) return;
  await api(role === "home" ? "home_remove" : "cloud_remove"); await draw();
}
async function sendProject(name) {
  const plan = await api("cloud_plan", { name });
  const confirm = h("input", { type: "checkbox" });
  const body = h("div", { class: "stack" },
    h("dl", { class: "kv" }, h("dt", { text: t("cloud.project") }), h("dd", { text: plan.name || name }), h("dt", { text: t("cloud.files") }), h("dd", { text: fmtNum(plan.files) }), h("dt", { text: t("sync.size") }), h("dd", { text: fmtBytes(plan.bytes) }),
      h("dt", { text: t("cloud.target") }), h("dd", { class: "mono", text: plan.target || "cloud" }), h("dt", { text: t("cloud.excluded") }), h("dd", { text: (plan.excluded || plan.excludes || []).join(", ") || "—" }),
      h("dt", { text: t("cloud.credentials") }), h("dd", { text: (plan.sensitive || []).length ? (plan.sensitive || []).join(", ") : t("cloud.no_credentials") })),
    (plan.needs_confirmation || []).map((line) => h("div", { class: "note warn", text: line })),
    (plan.needs_confirmation || []).length ? h("label", { class: "check" }, confirm, h("span", {}, t("cloud.confirm"))) : null);
  const out = await dialog(t("cloud.review_title"), body, [{ label: t("action.cancel"), value: null }, { label: t("cloud.send"), cls: "primary", run: () => api("cloud_push", { name, confirmed: confirm.checked || !(plan.needs_confirmation || []).length }) }], { wide: true });
  if (out) { toast(t("toast.sent", { name })); await draw(); }
}

// ── assistant ───────────────────────────────────────────────────────────────
renderers.assistant = async (page) => {
  const [data, git] = await Promise.all([api("assistants"), api("git_projects")]);
  put(page, pageHeader(t("page.assistant"), t("assistant.lead")));
  for (const a of data.assistants) {
    const section = h("section", {}, h("h2", { text: a.name }));
    put(section, h("div", { class: "note" }, state(a.installed ? "READY" : "NOT_INSTALLED", a.installed ? t("assistant.installed", { v: a.version || "" }) : t("state.NOT_INSTALLED")), h("p", { text: a.installed ? t("assistant.installed_text") : t("assistant.missing_text") })));
    put(section, h("h3", { text: t("assistant.projects") }));
    put(section, a.projects.length ? rows(a.projects.map((p) => row(p.project,
      [h("div", { class: "stack", style: "gap:4px" }, p.state.map((item) => h("span", {}, state(item.portable ? "SYNCED" : "PAUSED", t("assistant.kind." + item.kind)), " ", h("span", { class: "mono small", text: item.path }), " ", detail(tOr("assistant.item." + item.what, "")))),
        p.state.length ? null : detail(t("assistant.no_state")), p.error ? h("span", { class: "state err", "data-symbol": "✕", text: p.error }) : null)],
      p.shared ? state("SYNCED", t("assistant.memory_shared")) : button(t("assistant.share_memory"), () => shareMemory(p.project)))))
      : h("div", { class: "empty", text: t("assistant.no_projects") }));
    put(section, h("h3", { text: t("assistant.global") }), h("p", { class: "muted", text: t("assistant.global_lead") }),
      rows(a.global_state.filter((g) => g.present).map((g) => row(h("span", { class: "mono small", text: g.path }), [state("PAUSED", t("assistant.kind." + g.kind)), detail(tOr("assistant.item." + g.what, ""))])), "two"));
    put(page, section);
  }
  const repos = h("section", {}, h("h2", { text: t("git.title") }), h("p", { text: t("git.lead") }));
  if (!git.installed) put(repos, h("div", { class: "empty", text: t("git.missing") }));
  else if (!git.rows.length) put(repos, h("div", { class: "empty", text: t("git.none") }));
  else put(repos, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: t("git.repo") }), h("th", { text: t("git.branch") }), h("th", { text: t("health.status") }), h("th", { text: t("git.changes") }))),
    h("tbody", {}, git.rows.map((r) => h("tr", {}, h("td", { text: r.name }), h("td", { class: "mono small", text: r.branch }), h("td", {}, state(r.dirty ? "warn" : "READY", r.dirty ? t("git.uncommitted") : t("git.clean"))), h("td", { text: [r.dirty ? t("git.dirty_n", { n: r.dirty }) : "", r.ahead ? t("git.ahead_n", { n: r.ahead }) : "", r.behind ? t("git.behind_n", { n: r.behind }) : ""].filter(Boolean).join(" · ") || "—" })))))));
  put(page, repos);
};
async function shareMemory(project) {
  const preview = await api("assistant_share_memory", { project, dry: true });
  if (!(await confirmDialog(t("assistant.share_memory"), h("div", { class: "stack" }, h("p", { text: t("assistant.share_text") }), h("div", { class: "code small", text: JSON.stringify(preview, null, 1) })), t("assistant.share_memory")))) return;
  await api("assistant_share_memory", { project, dry: false }); await draw();
}

// ── settings ────────────────────────────────────────────────────────────────
let settingsSection = "general";
let showAdvanced = false;
function openSettings(section) { settingsSection = section; navigate("settings"); }
renderers.settings = async (page) => {
  const data = await api("settings_get");
  const pending = {};
  const errors = {};
  put(page, pageHeader(t("page.settings"), t("settings.lead")));
  const visible = data.sections.filter((s) => s !== "advanced" || showAdvanced);
  if (!visible.includes(settingsSection)) settingsSection = "general";
  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": t("page.settings") }, visible.map((s) => h("button", { type: "button", role: "tab", "aria-selected": String(s === settingsSection), text: t("settings.section." + s), onclick: () => { settingsSection = s; draw(); } })));
  const body = h("section", { role: "tabpanel", "aria-label": t("settings.section." + settingsSection) });
  const bar = h("div", { class: "savebar", hidden: true });
  const refreshBar = () => { const n = Object.keys(pending).length; bar.hidden = !n; count.textContent = t("settings.unsaved", { n }); };
  const count = h("span", {});
  const save = button(t("action.save"), async () => {
    const out = await api("settings_apply", { changes: pending });
    if (Object.keys(out.errors || {}).length) { Object.assign(errors, out.errors); for (const [k, v] of Object.entries(out.errors)) { const el = document.getElementById("err-" + k); if (el) { el.textContent = v; el.hidden = false; } } return; }
    toast(t("toast.saved"));
    if ("general.theme" in pending) applyTheme(pending["general.theme"]);
    if ("general.language" in pending) { const lang = await api("set_language", { language: pending["general.language"] }); catalog = lang.catalog; boot.language = lang.language; document.documentElement.lang = lang.language; }
    buildRail(lastAttention); await draw();
  }, "primary");
  put(bar, count, h("div", { class: "row-actions" }, button(t("action.discard"), () => draw()), save));

  const fields = data.fields.filter((f) => f.section === settingsSection && (showAdvanced || !f.advanced));
  if (settingsSection === "privacy") put(body, h("div", { class: "note" }, h("strong", { text: t("privacy.none_title") }), h("p", { text: t("privacy.none_text") })));
  if (settingsSection === "security") put(body, rows([row(t("security.store"), [state(data.credential_store === "none" ? "UNAVAILABLE" : "READY", data.credential_store === "none" ? t("security.no_store") : data.credential_store)]), row(t("security.host_check"), [state("READY", t("security.host_check_on"))]), row(t("security.listen"), [state("READY", t("security.listen_text"))])]));
  if (settingsSection === "network") put(body, h("div", { class: "note" }, h("p", { text: t("network.text") })), h("div", { class: "row-actions" }, button(t("page.health"), () => navigate("health"))));
  if (settingsSection === "workstations") put(body, h("div", { class: "row-actions" }, button(t("page.workstations"), () => navigate("workstations"), "primary")));
  if (settingsSection === "servers") put(body, h("div", { class: "note" }, h("p", { text: t("servers.settings_text") })), h("div", { class: "row-actions" }, button(t("page.servers"), () => navigate("servers"), "primary")));
  if (settingsSection === "assistant") put(body, h("div", { class: "row-actions" }, button(t("page.assistant"), () => navigate("assistant"), "primary")));
  if (settingsSection === "updates") put(body, await updatesBlock());
  if (settingsSection === "advanced") put(body, h("dl", { class: "kv small" }, h("dt", { text: t("advanced.config") }), h("dd", { class: "mono", text: data.locations.config }), h("dt", { text: t("advanced.state") }), h("dd", { class: "mono", text: data.locations.state }), h("dt", { text: t("advanced.cache") }), h("dd", { class: "mono", text: data.locations.cache })),
    h("div", { class: "row-actions" }, button(t("advanced.open_logs"), () => api("open_path", { what: "logs" })), button(t("advanced.rerun_setup"), async () => { await api("onboarding_reset"); location.reload(); })));

  for (const f of fields) put(body, settingField(f, data, pending, errors, refreshBar));
  if (!body.children.length) put(body, h("div", { class: "empty", text: t("settings.nothing") }));
  const adv = h("label", { class: "check" }, h("input", { type: "checkbox", checked: showAdvanced, onchange: (e) => { showAdvanced = e.target.checked; draw(); } }), h("span", {}, t("settings.show_advanced"), h("span", { class: "muted small", text: t("settings.show_advanced_help") })));
  put(page, tabs, body, adv, bar);
};
function settingField(f, data, pending, errors, changed) {
  const id = "set-" + f.key;
  const set = (value) => { if (JSON.stringify(value) === JSON.stringify(f.value)) delete pending[f.key]; else pending[f.key] = value; const el = document.getElementById("err-" + f.key); if (el) el.hidden = true; changed(); };
  let control;
  let choices = f.choices;
  if (f.key === "workstation.editor") choices = ["auto", ...data.editors.map((e) => e.id)];
  if (f.key === "workstation.terminal") choices = ["auto", ...data.terminals.map((e) => e.id)];
  if (f.key === "peripherals.server") choices = data.devices;
  if (choices && !choices.includes(f.value) && f.value !== "" && f.value !== null) choices = [f.value, ...choices];
  if (f.readonly) control = h("output", { id, class: f.kind === "bool" ? "" : "mono", text: f.kind === "bool" ? t(f.value ? "state.on" : "state.off") : String(f.value) });
  else if (f.kind === "bool") control = h("input", { type: "checkbox", id, checked: !!f.value, onchange: (e) => set(e.target.checked) });
  else if (choices) control = h("select", { id, onchange: (e) => set(e.target.value) }, choices.map((c) => h("option", { value: c, selected: c === f.value, text: tOr("choice." + f.key + "." + c, tOr("choice." + c, (data.editors.concat(data.terminals).find((x) => x.id === c) || {}).name || c)) })));
  else if (f.kind === "int" || f.kind === "number") control = h("input", { type: "number", id, value: f.value, min: f.minimum, max: f.maximum, step: f.kind === "int" ? "1" : "any", oninput: (e) => set(e.target.value === "" ? "" : Number(e.target.value)) });
  else if (f.kind === "list") control = h("textarea", { id, rows: "3", value: (f.value || []).join("\n"), spellcheck: "false", oninput: (e) => set(e.target.value.split("\n").map((x) => x.trim()).filter(Boolean)) });
  else if (f.kind === "path") { const out = h("output", { id, class: "mono small", text: f.value }); control = h("div", { class: "pathbox" }, out, button(t("action.choose"), async () => { const p = await pickFolder(); if (p) { out.textContent = p; set(p); } })); }
  else control = h("input", { type: "text", id, value: f.value === null || f.value === undefined ? "" : f.value, spellcheck: "false", oninput: (e) => set(e.target.value) });
  const inline = f.kind === "bool" || f.unit;
  return h("div", { class: "field" },
    h("div", {}, h("label", { for: id, text: tOr("settings." + f.key + ".label", f.key) }), has("settings." + f.key + ".help") ? h("span", { class: "help", text: t("settings." + f.key + ".help") }) : null,
      f.scope === "shared" ? h("span", { class: "help", text: t("settings.shared_scope") }) : null),
    h("div", { class: "control" + (inline ? " inline" : "") }, control, f.unit ? h("span", { class: "unit", text: tOr("unit." + f.unit, f.unit) }) : null,
      f.readonly || JSON.stringify(f.value) === JSON.stringify(f.default) ? null : h("button", { type: "button", class: "btn quiet small", text: t("settings.reset_one"), onclick: async () => { await api("settings_reset", { keys: [f.key] }); await draw(); } })),
    h("div", { class: "error", id: "err-" + f.key, role: "alert", hidden: !errors[f.key], text: errors[f.key] || "" }));
}
async function updatesBlock() {
  const u = await api("update_status");
  const block = h("div", { class: "stack" });
  put(block, rows([
    row(t("updates.installed"), [state("READY", u.current), detail(t("channel." + u.channel_running))]),
    row(t("updates.latest"), u.error ? [state("warn", u.error)] : u.offer ? [state("warn", t("updates.available", { version: u.offer.version })), detail(u.offer.managed_by ? t("updates.managed", { by: u.offer.managed_by }) : fmtBytes(u.offer.size))] : [state("READY", t("updates.none")), detail(u.checked ? t("updates.checked", { when: fmtTime(u.checked) }) : t("updates.never_checked"))],
      [button(t("updates.check"), async () => { await api("update_check"); await draw(); }), u.offer && !u.offer.managed_by ? button(t("updates.download"), async () => { const out = await api("update_download"); await dialog(t("updates.downloaded_title"), h("div", { class: "stack" }, h("p", { text: t("updates.downloaded_text") }), h("div", { class: "code small", text: out.file })), [{ label: t("action.close"), value: null, cls: "primary" }]); }, "primary") : null]),
  ]));
  if (u.offer && u.offer.notes) put(block, h("details", {}, h("summary", { text: t("updates.notes") }), h("div", { class: "note small", style: "white-space:pre-wrap", text: u.offer.notes })));
  put(block, h("p", { class: "muted small", text: t("updates.trust") }));
  return block;
}

// ── health ──────────────────────────────────────────────────────────────────
renderers.health = async (page) => {
  put(page, pageHeader(t("page.health"), t("health.lead"), button(t("health.run"), () => draw(), "primary"), button(t("support.export"), async () => { if (await confirmDialog(t("support.export"), t("support.text"), t("support.export"))) download(await api("support_bundle")); })));
  const [test, caps] = await Promise.all([api("selftest"), api("capabilities")]);
  put(page, h("div", { class: "verdict " + (test.verdict === "pass" ? "ok" : test.verdict === "warn" ? "warn" : "") }, h("div", { class: "headline" }, state(test.verdict, t("health.verdict." + test.verdict)))));
  put(page, h("section", {}, h("h2", { text: t("health.check") }), rows(test.rows.map((r) => row(tOr("selftest." + r.id, r.id), [state(r.status, t("health.level." + r.status)), detail(r.detail)],
    r.fix && r.fix !== "install" ? button(t("health.fix"), async () => { await api("repair", { what: { create_workspace: "create_workspace", repair_sync: "sync" }[r.fix] || r.fix }); toast(t("toast.repaired")); await draw(); }, "primary") : null)))));
  put(page, h("section", {}, h("h2", { text: t("health.repair") }), h("p", { text: t("health.repair_lead") }),
    rows(["sync", "peripherals", "service", "integration", "config", "state", "background"].map((what) => row(t("repair." + what), [detail(t("repair." + what + ".text"))], button(t("action.repair"), async () => { const out = await api("repair", { what }); await dialog(t("health.repair_done"), h("div", { class: "code small", style: "white-space:pre-wrap", text: out.done.join("\n") }), [{ label: t("action.close"), value: null, cls: "primary" }]); await draw(); }))))));
  put(page, h("section", {}, h("h2", { text: t("health.features") }), h("p", { text: t("health.features_lead", { os: osName(caps.platform.os), desktop: [caps.platform.desktop, caps.platform.session].filter(Boolean).join(" · ") || "—" }) }), featureTable(caps.features)));
  put(page, h("section", {}, h("h2", { text: t("health.components") }), h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: t("health.component") }), h("th", { text: t("health.status") }), h("th", { text: t("health.used_for") }))),
    h("tbody", {}, caps.components.map((c) => h("tr", {}, h("td", { text: c.name }), h("td", {}, state(c.installed ? "READY" : "NOT_INSTALLED", c.installed ? (c.version ? t("health.version", { v: c.version }) : t("health.installed")) : t("state.NOT_INSTALLED"))), h("td", {}, c.purpose, !c.installed && c.install_hint ? h("div", { class: "muted small", text: c.install_hint }) : null))))))));
  if (caps.permissions.length) put(page, h("section", {}, h("h2", { text: t("health.permissions") }), h("p", { text: t("health.permissions_lead") }), permissionList(caps.permissions)));
  const details = h("details", {}, h("summary", { text: t("health.raw") }));
  details.addEventListener("toggle", async () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "1";
    const [hl, log] = await Promise.all([api("health"), api("history", { limit: 200 })]);
    const search = h("input", { type: "search", placeholder: t("health.search"), "aria-label": t("health.search") });
    const tbody = h("tbody");
    const fill = (items) => tbody.replaceChildren(...items.map((r) => h("tr", {}, h("td", { class: "small", text: fmtTime(r.timestamp) }), h("td", {}, state(r.level === "error" ? "ERROR" : r.level === "warn" ? "warn" : "READY", r.event)), h("td", { text: r.message }))));
    fill(log.rows);
    search.addEventListener("input", async () => fill((await api("history", { search: search.value, limit: 200 })).rows));
    put(details, h("div", { class: "stack" }, hl.checks.length ? rows(hl.checks.map((c) => row(c.section, [state(c.level, c.title), detail(c.detail)])), "") : null, h("h3", { text: t("health.events") }), search, h("div", { class: "tablewrap" }, h("table", {}, tbody))));
  });
  put(page, details);
};

// ── recovery ────────────────────────────────────────────────────────────────
renderers.recovery = async (page) => {
  const r = await api("recovery");
  put(page, pageHeader(t("page.recovery"), t("recovery.lead")));
  if (r.config_warnings.length) put(page, h("div", { class: "note err" }, h("p", { text: t("recovery.config_broken") }), h("div", { class: "row-actions", style: "margin-top:8px" }, button(t("recovery.rebuild"), async () => { await api("repair", { what: "config" }); await draw(); }, "primary"))));
  put(page, h("section", {}, h("h2", { text: t("recovery.files") }), h("p", { text: t("recovery.files_lead", { days: r.trash_days }) }), rows([
    row(t("sync.versions"), [detail(t("recovery.versions_n", { n: fmtNum(r.versions) }))], button(t("sync.view_versions"), showVersions)),
    row(t("sync.conflicts"), [detail(t("recovery.conflicts_n", { n: fmtNum(r.conflicts) }))], button(t("action.manage"), () => navigate("sync"))),
  ])));
  const snaps = h("section", {}, h("h2", { text: t("recovery.settings") }), h("p", { text: t("recovery.settings_lead") }),
    h("div", { class: "row-actions" }, button(t("recovery.snapshot_now"), async () => { await api("snapshot_create"); toast(t("toast.snapshot")); await draw(); }),
      button(t("recovery.export"), exportConfig), button(t("recovery.import"), importConfig),
      button(t("recovery.rebuild"), async () => { const out = await api("repair", { what: "config" }); await dialog(t("health.repair_done"), h("div", { class: "code small", style: "white-space:pre-wrap", text: out.done.join("\n") }), [{ label: t("action.close"), value: null, cls: "primary" }]); }),
      button(t("recovery.reset"), async () => { if (await confirmDialog(t("recovery.reset"), t("recovery.reset_text"), t("recovery.reset"), true)) { await api("config_reset"); toast(t("toast.reset")); await draw(); } }, "danger")));
  put(snaps, r.snapshots.length ? rows(r.snapshots.map((s) => row(fmtTime(s.created), [detail(tOr("snapshot.reason." + s.reason.replace(/-v?\d+$/, ""), s.reason)), detail(s.version ? t("workstations.version", { v: s.version }) : "")],
    button(t("recovery.restore"), async () => { if (await confirmDialog(t("recovery.restore"), t("recovery.restore_text"), t("recovery.restore"))) { await api("snapshot_restore", { snapshot: s.id }); toast(t("toast.restored_settings")); await draw(); } }))))
    : h("div", { class: "empty", text: t("recovery.no_snapshots") }));
  put(page, snaps);
  put(page, h("section", {}, h("h2", { text: t("recovery.app") }), h("p", { text: t("recovery.app_lead") }),
    r.installers.length ? rows(r.installers.map((i) => row(h("span", { class: "mono small", text: i.name }), [detail(fmtBytes(i.bytes)), detail(fmtTime(i.modified))])), "") : h("div", { class: "empty", text: t("recovery.no_installers") })));
  put(page, h("section", {}, h("h2", { text: t("recovery.uninstall") }), h("p", { text: t("recovery.uninstall_lead") }), h("div", { class: "row-actions" }, button(t("recovery.uninstall_preview"), async () => {
    const u = await api("uninstall_preview");
    await dialog(t("recovery.uninstall"), h("div", { class: "stack" }, h("h3", { text: t("recovery.kept") }), h("ul", {}, u.kept.map((k) => h("li", { text: k }))), h("h3", { text: t("recovery.removed") }),
      u.removed.length ? h("div", { class: "code small", style: "white-space:pre-wrap", text: u.removed.join("\n") }) : h("p", { class: "muted", text: t("recovery.nothing_changed") }),
      h("h3", { text: t("recovery.optional") }), h("div", { class: "code small", style: "white-space:pre-wrap", text: u.optional.join("\n") }), h("p", { class: "muted", text: t("recovery.uninstall_how") })), [{ label: t("action.close"), value: null, cls: "primary" }], { wide: true });
  }))));
};
async function exportConfig() {
  const machine = h("input", { type: "checkbox" });
  const devices = h("input", { type: "checkbox" });
  const ok = await confirmDialog(t("recovery.export"), h("div", { class: "stack" }, h("p", { text: t("recovery.export_text") }),
    h("label", { class: "check" }, machine, h("span", {}, t("recovery.export_machine"), h("span", { class: "muted small", text: t("recovery.export_machine_help") }))),
    h("label", { class: "check" }, devices, h("span", {}, t("recovery.export_devices"), h("span", { class: "muted small", text: t("recovery.export_devices_help") }))),
    h("div", { class: "note", text: t("recovery.export_secrets") })), t("recovery.export"));
  if (ok) download(await api("config_export", { include_machine: machine.checked, include_devices: devices.checked }));
}
async function importConfig() {
  const file = h("input", { type: "file", accept: ".zip", "aria-label": t("recovery.import") });
  const picked = await dialog(t("recovery.import"), h("div", { class: "stack" }, h("p", { text: t("recovery.import_text") }), file), [{ label: t("action.cancel"), value: null }, { label: t("action.continue"), cls: "primary", run: async () => {
    if (!file.files.length) return false;
    const bytes = new Uint8Array(await file.files[0].arrayBuffer());
    let binary = ""; for (const b of bytes) binary += String.fromCharCode(b);
    const data = btoa(binary);
    return { data, preview: await api("config_import_preview", { data }) };
  } }]);
  if (!picked) return;
  const p = picked.preview;
  const ok = await confirmDialog(t("recovery.import"), h("div", { class: "stack" }, h("p", { text: t("recovery.import_changes", { n: p.changes.length }) }), p.changes.length ? h("div", { class: "code small", text: p.changes.join("   ") }) : null,
    p.secret_references.length ? h("div", { class: "note warn", text: t("recovery.import_secrets", { n: p.secret_references.length }) }) : null), t("recovery.import_apply"));
  if (ok) { await api("config_import", { data: picked.data }); toast(t("toast.imported")); await draw(); }
}

// ── help ────────────────────────────────────────────────────────────────────
renderers.help = async (page) => {
  const info = await api("help_info");
  put(page, pageHeader(t("page.help"), t("help.lead")));
  const link = (id) => row(t("help.link." + id), [h("span", { class: "detail mono small", text: info.links[id] })], button(t("help.open"), async () => { const out = await api("open_link", { which: id }); if (!out.opened) { try { await navigator.clipboard.writeText(out.url); toast(t("help.copied")); } catch (_e) { toast(out.url); } } }));
  put(page, h("section", {}, h("h2", { text: t("help.fix") }), rows([
    row(t("help.check"), [detail(t("help.check.text"))], button(t("health.run"), () => navigate("health"), "primary")),
    row(t("help.repair"), [detail(t("help.repair.text"))], button(t("page.health"), () => navigate("health"))),
    row(t("help.undo"), [detail(t("help.undo.text"))], button(t("page.recovery"), () => navigate("recovery"))),
  ])));
  put(page, h("section", {}, h("h2", { text: t("help.read") }), rows(["getting_started", "troubleshooting", "platforms", "docs"].map(link))));
  put(page, h("section", {}, h("h2", { text: t("help.tell") }), h("p", { text: t("help.tell.lead") }), rows([
    row(t("support.export"), [detail(t("help.bundle.text"))], button(t("support.export"), async () => { if (await confirmDialog(t("support.export"), t("support.text"), t("support.export"))) download(await api("support_bundle")); })),
    link("report"), link("security"),
  ])));
  put(page, h("section", {}, h("h2", { text: t("help.about") }), h("dl", { class: "kv" },
    h("dt", { text: t("help.version") }), h("dd", { text: info.version + " · " + t("channel." + info.channel) }),
    h("dt", { text: t("help.system") }), h("dd", { text: [osName(info.platform.os), info.platform.os_version, info.platform.arch, info.platform.desktop, info.platform.session].filter(Boolean).join(" · ") }),
    h("dt", { text: t("help.license") }), h("dd", { text: info.license }))), h("div", { class: "row-actions" }, linkTo("health"), button(t("help.link.releases"), async () => { const out = await api("open_link", { which: "releases" }); if (!out.opened) toast(out.url); })));
};

// ── first run ───────────────────────────────────────────────────────────────
const WIZARD_STEPS = ["welcome", "kind", "workspace", "components", "setup", "permissions", "pair", "home", "cloud", "selftest", "finish"];
async function startWizard(preset) {
  const choices = Object.assign({ kind: "personal", workspace: boot.onboarding.suggested_workspace, sync: true, peripherals: false, integrations: ["ssh", "desktop"], autostart: true, language: "auto" }, preset || {});
  let index = preset ? 2 : 0;
  let applied = null;
  const caps = await api("capabilities");
  const comp = Object.fromEntries(caps.components.map((c) => [c.id, c]));
  if (!comp.syncthing.installed) choices.sync = false;
  $app.hidden = true; $wizard.hidden = false;
  const steps = () => WIZARD_STEPS.filter((s) => choices.kind === "workstation" || !["setup", "permissions", "pair", "home", "cloud"].includes(s));

  const render = async () => {
    const list = steps();
    const id = list[index];
    const step = h("div", { class: "step" + (id === "welcome" ? " welcome" : "") });
    let nextLabel = t("action.continue");
    let onNext = null;
    if (id === "welcome") {
      const lang = h("select", { "aria-label": t("settings.general.language.label"), onchange: async (e) => { choices.language = e.target.value; const out = await api("set_language", { language: e.target.value }); catalog = out.catalog; boot.language = out.language; document.documentElement.lang = out.language; render(); } },
        ["auto", ...Object.keys(boot.languages)].map((code) => h("option", { value: code, selected: choices.language === code, text: code === "auto" ? t("choice.auto") : boot.languages[code] })));
      put(step, h("img", { src: "/static/icon.svg", alt: "", width: "56", height: "56" }), h("h1", { text: t("wizard.welcome.title", { name: boot.product.name }) }), h("p", { text: t("wizard.welcome.text") }), h("div", {}, lang),
        boot.demo ? h("div", { class: "note warn", text: t("demo.note") }) : null, boot.onboarding.existing_setup ? h("div", { class: "note", text: t("wizard.welcome.existing") }) : null);
      nextLabel = t("wizard.get_started");
    } else if (id === "kind") {
      const opt = (value) => h("label", {}, h("input", { type: "radio", name: "kind", value, checked: choices.kind === value, onchange: () => { choices.kind = value; } }), h("span", {}, h("strong", { text: t("wizard.kind." + value) }), h("span", { class: "muted", text: t("wizard.kind." + value + ".text") })));
      put(step, h("h1", { text: t("wizard.kind.title") }), h("p", { text: t("wizard.kind.lead") }), h("div", { class: "choice", role: "radiogroup", "aria-label": t("wizard.kind.title") }, opt("personal"), opt("workstation")));
    } else if (id === "workspace") {
      const plan = await api("onboarding_plan", { choices });
      const out = h("output", { class: "code", text: plan.workspace });
      const problem = h("div", { class: "note warn", hidden: !plan.workspace_problem, text: plan.workspace_problem ? t("folder.problem") + " " + plan.workspace_problem : "" });
      const existing = h("div", { class: "note", hidden: !(plan.existing.exists && plan.existing.entries), text: t("wizard.workspace.existing", { n: plan.existing.entries }) });
      put(step, h("h1", { text: t("wizard.workspace.title") }), h("p", { text: t("wizard.workspace.lead") }), h("div", { class: "pathbox" }, out, button(t("wizard.workspace.other"), async () => { const p = await pickFolder(); if (p) { choices.workspace = p; render(); } })), problem, existing,
        h("p", { class: "muted", text: t("wizard.workspace.note") }));
      onNext = () => !plan.workspace_problem;
    } else if (id === "components") {
      put(step, h("h1", { text: t("wizard.components.title") }), h("p", { text: t("wizard.components.lead") }),
        rows(caps.components.map((c) => row(c.name, [state(c.installed ? "READY" : "NOT_INSTALLED", c.installed ? t("health.installed") : t("state.NOT_INSTALLED")), detail(c.installed ? c.purpose : c.purpose + " — " + (c.install_hint || ""))]))),
        h("p", { class: "muted", text: t("wizard.components.note") }));
    } else if (id === "setup") {
      const check = (key, label, help, available, reason) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!choices[key] && available, disabled: !available, onchange: (e) => { choices[key] = e.target.checked; } }), h("span", {}, label, h("span", { class: "muted small", text: available ? help : reason })));
      const integ = (name) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: choices.integrations.includes(name), onchange: (e) => { choices.integrations = e.target.checked ? [...choices.integrations, name] : choices.integrations.filter((x) => x !== name); } }), h("span", {}, t("wizard.integration." + name), h("span", { class: "muted small", text: t("wizard.integration." + name + ".text") })));
      put(step, h("h1", { text: t("wizard.setup.title") }), h("p", { text: t("wizard.setup.lead") }), h("div", { class: "stack" },
        check("sync", t("wizard.setup.sync"), t("wizard.setup.sync.text"), comp.syncthing.installed, t("wizard.setup.missing", { name: "Syncthing" }) + " " + comp.syncthing.install_hint),
        check("peripherals", t("wizard.setup.peripherals"), t("wizard.setup.peripherals.text"), comp.deskflow.installed, t("wizard.setup.missing", { name: "Deskflow" }) + " " + comp.deskflow.install_hint),
        check("autostart", t("wizard.setup.autostart"), t("wizard.setup.autostart.text"), true, "")),
        h("h2", { text: t("wizard.setup.integrations") }), h("div", { class: "stack" }, integ("ssh"), integ("desktop")),
        h("details", {}, h("summary", { text: t("wizard.setup.more") }), h("div", { class: "stack" }, integ("terminal"), integ("shell"), integ("tmux"), integ("git"))));
    } else if (id === "permissions") {
      const plan = await api("onboarding_plan", { choices });
      put(step, h("h1", { text: t("wizard.permissions.title") }), h("p", { text: t("wizard.permissions.lead") }),
        h("h2", { text: t("wizard.permissions.changes") }), rows(plan.steps.map((s) => row(tOr("step." + s.id, s.id), [h("div", { class: "stack", style: "gap:2px" }, s.touches.map((line) => h("span", { class: "detail small", text: line })), s.available ? null : h("span", { class: "state warn", "data-symbol": "▲", text: s.reason }))])), ""),
        caps.permissions.length ? [h("h2", { text: t("health.permissions") }), permissionList(caps.permissions)] : null,
        h("p", { class: "muted", text: t("wizard.permissions.undo") }));
      nextLabel = t("wizard.apply");
      onNext = async () => { applied = await api("onboarding_apply", { choices }); return true; };
    } else if (id === "pair") {
      put(step, h("h1", { text: t("wizard.pair.title") }), h("p", { text: t("wizard.pair.lead") }),
        applied ? rows(applied.steps.map((s) => row(tOr("step." + s.id, s.id), [state(s.status === "ok" ? "pass" : s.status === "skipped" ? "skip" : "fail", t("health.level." + (s.status === "ok" ? "pass" : s.status === "skipped" ? "skip" : "fail"))), detail(s.note)])), "") : null,
        h("div", { class: "row-actions" }, button(t("pair.add"), addWorkstation, "primary"), button(t("pair.join"), joinWorkstation)), h("p", { class: "muted", text: t("wizard.optional") }));
      nextLabel = t("wizard.skip_or_continue");
    } else if (id === "home" || id === "cloud") {
      put(step, h("h1", { text: t("wizard." + id + ".title") }), h("p", { text: t("servers." + id + "_lead") }),
        h("div", { class: "row-actions" }, button(t("servers.add_" + id), async () => { await editServer(id, await api("servers"), null); })), h("p", { class: "muted", text: t("wizard.optional") }));
      nextLabel = t("wizard.skip_or_continue");
    } else if (id === "selftest") {
      if (!applied) applied = await api("onboarding_apply", { choices });
      const test = await api("selftest");
      put(step, h("h1", { text: t("wizard.selftest.title") }), h("div", { class: "verdict " + (test.verdict === "pass" ? "ok" : test.verdict === "warn" ? "warn" : "") }, h("div", { class: "headline" }, state(test.verdict, t("health.verdict." + test.verdict)))),
        rows(test.rows.map((r) => row(tOr("selftest." + r.id, r.id), [state(r.status, t("health.level." + r.status)), detail(r.detail)], r.fix && r.fix !== "install" ? button(t("health.fix"), async () => { await api("repair", { what: { create_workspace: "create_workspace", repair_sync: "sync" }[r.fix] || r.fix }); render(); }) : null))),
        h("div", { class: "row-actions" }, button(t("action.retry"), render)));
    } else if (id === "finish") {
      put(step, h("h1", { text: t("wizard.finish.title") }), h("p", { text: t(choices.kind === "workstation" ? "wizard.finish.workstation" : "wizard.finish.personal") }));
      nextLabel = t("wizard.finish.open");
      onNext = async () => { location.reload(); return false; };
    }
    const back = h("button", { type: "button", class: "btn", text: t("action.back"), disabled: index === 0 || id === "finish" || (applied && ["pair", "selftest"].includes(id) && list[index - 1] === "permissions"), onclick: () => { index = Math.max(0, index - 1); render(); } });
    const next = button(nextLabel, async () => { if (onNext && (await onNext()) === false) return; index = Math.min(list.length - 1, index + 1); await render(); }, "primary");
    $wizard.replaceChildren();
    put($wizard,
      h("div", { class: "top" }, h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "", width: "26", height: "26" }), boot.product.name), h("div", { class: "progress", text: t("wizard.progress", { n: index + 1, total: list.length }) })),
      h("div", {}, h("div", { class: "bar", role: "progressbar", "aria-valuemin": "1", "aria-valuemax": String(list.length), "aria-valuenow": String(index + 1), "aria-label": t("wizard.progress", { n: index + 1, total: list.length }) }, h("i", { style: "width:" + Math.round(((index + 1) / list.length) * 100) + "%" })), step),
      h("div", { class: "nav" }, back, next));
    const heading = step.querySelector("h1"); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    window.scrollTo(0, 0);
  };
  try { await render(); } catch (err) { await showProblem(err); }
}

// ── start ───────────────────────────────────────────────────────────────────
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
async function start() {
  try { boot = await api("bootstrap"); }
  catch (err) { document.getElementById("loading").textContent = "The application window lost its connection. Close it and open it again."; return; }
  catalog = boot.catalog;
  document.documentElement.lang = boot.language;
  document.title = boot.product.name;
  document.getElementById("skip-link").textContent = t("nav.skip");
  $rail.setAttribute("aria-label", t("nav.sections"));
  applyTheme(boot.theme);
  document.getElementById("loading").remove();
  if (!boot.onboarding.done) { await startWizard(); return; }
  $app.hidden = false;
  await navigate("dashboard");
  if (boot.migration && boot.migration.error) toast(t("toast.migration_failed"));
}
window.addEventListener("keydown", (event) => {
  if (!event.altKey || event.ctrlKey || event.metaKey || $app.hidden) return;
  const n = Number(event.key);
  if (n >= 1 && n <= 9 && PAGES[n - 1]) { event.preventDefault(); navigate(PAGES[n - 1]); }
});
start();
