"use strict";
/* Dashboard del agente. Todo el texto dinámico entra con textContent (nunca innerHTML). */
const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return sessionStorage.getItem(k) || localStorage.getItem(k) || ""; } catch (e) { return ""; } },
  set(k, v, persist) { try { (persist ? localStorage : sessionStorage).setItem(k, v); } catch (e) { /* sin almacenamiento */ } },
  del(k) { try { sessionStorage.removeItem(k); localStorage.removeItem(k); } catch (e) { /* nada */ } },
};
const state = { token: store.get("agent_token"), cfg: null, tasks: new Map(), selected: null, tab: "reasoning", timer: null, openFile: null };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { Authorization: "Bearer " + state.token, "Content-Type": "application/json" } });
  let data = {};
  try { data = await res.json(); } catch (e) { /* cuerpo vacío */ }
  if (res.status === 401) { logout("Token incorrecto o caducado."); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error(data.error || "Error " + res.status);
  return data;
}

/* ---------- acceso ---------- */
function logout(msg) {
  store.del("agent_token"); state.token = ""; clearInterval(state.timer);
  $("app").hidden = true; $("login").hidden = false; $("login-error").textContent = msg || "";
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.token = $("token").value.trim();
  try { await boot(); store.set("agent_token", state.token, $("remember").checked); $("token").value = ""; }
  catch (err) { if (err.message !== "unauthorized") $("login-error").textContent = "No se pudo conectar: " + err.message; }
});
$("logout").addEventListener("click", () => logout(""));

/* ---------- arranque ---------- */
async function boot() {
  state.cfg = await api("/agent/config");
  $("login").hidden = true; $("app").hidden = false;
  renderPills(); renderSelectors(); renderExamples(); updateComposer(); renderTabs(); renderPanel();
  const { tasks } = await api("/agent/tasks");
  for (const t of tasks.reverse()) { const full = await api("/agent/task/" + t.id); state.tasks.set(t.id, full); }
  renderFeed();
  if (!state.selected && state.tasks.size) state.selected = [...state.tasks.keys()].pop();
  renderPanel(); startPolling();
}

function pill(text, tone) {
  const color = tone === "ok" ? "var(--ok)" : tone === "bad" ? "var(--bad)" : "var(--mute)";
  return h("span", { class: "inline-flex items-center gap-1.5 rounded-full border border-[var(--line)] px-2.5 py-1" },
    h("span", { class: "inline-block w-2 h-2 rounded-full", style: "background:" + color }), text);
}
function renderPills() {
  const c = state.cfg, missing = c.env.filter((v) => v.required && !v.set).length;
  $("pills").replaceChildren(
    pill(c.ollama_ok ? "Ollama conectado" : "Ollama sin respuesta", c.ollama_ok ? "ok" : "bad"),
    pill("Modelo base: " + c.model, "mute"),
    pill(missing ? missing + " variable(s) por definir" : "Variables listas", missing ? "bad" : "ok"));
}
function renderSelectors() {
  const c = state.cfg, inst = new Set(c.models.installed);
  const names = [...new Set([c.model, ...c.models.installed, ...c.models.suggested])];
  $("model").replaceChildren(...names.map((n) => h("option", { value: n }, inst.has(n) ? n : n + " (no instalado)")));
  $("model").value = c.model;
  $("repo").replaceChildren(...(c.repos.length ? c.repos : [c.repo]).map((r) => h("option", { value: r }, r)));
  $("repo").value = c.repo;
}
const EXAMPLES = [
  ["Landing en React", "Crea una landing page en React con TailwindCSS para una cafetería: hero, menú, ubicación y formulario de contacto.", "project"],
  ["API en Python", "Hazme una API REST en FastAPI para gestionar clientes con PostgreSQL (DATABASE_URL), validación y Dockerfile.", "project"],
  ["Script de scraping", "Crea scripts/scrape_precios.py que lea una URL de la variable PRODUCT_URL y guarde título y precio en un CSV.", "task"],
];
function renderExamples() {
  $("examples").replaceChildren(...EXAMPLES.map(([label, text, mode]) => h("button", {
    type: "button", class: "rounded-full border border-[var(--line)] px-3 py-1 text-xs text-[var(--mute)] hover:text-[var(--ink)]",
    onclick: () => { $("instruction").value = text; document.querySelector(`input[name=mode][value=${mode}]`).checked = true; updateComposer(); $("instruction").focus(); },
  }, label)));
}
function updateComposer() {
  const len = $("instruction").value.length, project = document.querySelector("input[name=mode]:checked").value === "project";
  $("count").textContent = len + "/4000";
  $("mode-hint").textContent = project
    ? "Proyecto completo: planifica la arquitectura, genera hasta " + state.cfg.limits.project_max_files + " archivos por lotes, los valida, agrega README y .gitignore, y publica."
    : "Tarea: genera o modifica hasta " + state.cfg.limits.max_files + " archivos, los valida y los publica.";
}
$("instruction").addEventListener("input", updateComposer);
document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", updateComposer));
$("instruction").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("composer").requestSubmit(); });

/* ---------- envío ---------- */
$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const instruction = $("instruction").value.trim();
  if (instruction.length < 10) { $("mode-hint").textContent = "Escribe al menos 10 caracteres para que el agente entienda el requerimiento."; return; }
  $("send").disabled = true;
  const body = { instruction, mode: document.querySelector("input[name=mode]:checked").value, model: $("model").value, repo: $("repo").value, dry_run: $("dry").checked };
  try {
    const { id } = await api("/agent/task", { method: "POST", body: JSON.stringify(body) });
    state.tasks.set(id, { id, status: "queued", steps: [], ...body });
    state.selected = id; $("instruction").value = ""; updateComposer();
    renderFeed(); renderPanel(); startPolling();
  } catch (err) {
    if (err.message !== "unauthorized") $("mode-hint").textContent = "No se pudo enviar: " + err.message;
  } finally { $("send").disabled = false; }
});

/* ---------- sondeo ---------- */
const ACTIVE = new Set(["queued", "running"]);
function startPolling() {
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    const live = [...state.tasks.values()].filter((t) => ACTIVE.has(t.status));
    if (!live.length) { clearInterval(state.timer); return; }
    for (const t of live) { try { state.tasks.set(t.id, await api("/agent/task/" + t.id)); } catch (e) { if (e.message === "unauthorized") return; } }
    renderFeed(); renderPanel();
  }, 1500);
}

/* ---------- pipeline de verificación ---------- */
const STAGES = [
  ["Sincronizar", /sincronizando/], ["Razonar", /planific|generando/], ["Validar", /validación estática/],
  ["Revisar", /revisión lógica|corrigiendo/], ["Commit", /autogit|dry_run/],
];
function stageStates(t) {
  const msgs = (t.steps || []).map((s) => s.msg);
  let reached = -1;
  STAGES.forEach(([, re], i) => { if (msgs.some((m) => re.test(m))) reached = i; });
  const finished = !ACTIVE.has(t.status), ok = t.status === "done" || t.status === "no_changes";
  return STAGES.map((_, i) => {
    if (i < reached) return "done";
    if (i === reached) return finished ? (ok ? "done" : "fail") : "active";
    return ok && finished ? "done" : "idle";
  });
}
function pipeline(t) {
  const st = stageStates(t);
  return h("ol", { class: "grid grid-cols-5 gap-2", "aria-label": "Etapas de verificación" },
    STAGES.map(([name], i) => h("li", { class: "stage-" + st[i] },
      h("div", { class: "stage-bar" }),
      h("span", { class: "mt-1 block text-xs " + (st[i] === "idle" ? "text-[var(--mute)]" : "") }, name,
        h("span", { class: "sr-only" }, " - " + ({ done: "completada", active: "en curso", fail: "con error", idle: "pendiente" })[st[i]])))));
}

/* ---------- chat ---------- */
const STATUS = { queued: "En cola", running: "Trabajando", done: "Listo", failed: "Falló", no_changes: "Sin cambios" };
function summary(t) {
  const r = t.result || {};
  if (t.status === "failed") return [h("p", { class: "text-[var(--bad)]" }, t.error || "La tarea falló."), (r.errors || []).slice(0, 4).map((x) => h("p", { class: "mono text-xs text-[var(--mute)]" }, x))];
  if (t.status === "no_changes") return [h("p", {}, "El código generado ya coincide con el repositorio: no hubo nada que subir.")];
  if (t.status !== "done") return [h("p", { class: "text-[var(--mute)]" }, (t.steps || []).slice(-1)[0]?.msg || "Esperando turno…")];
  const n = (r.files || []).length;
  if (r.dry_run) return [h("p", {}, n + " archivo(s) validados. No se subió nada (modo solo validar). Revisa el código en la pestaña Archivos.")];
  const link = r.url && r.url.startsWith("https://github.com/") ? h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", class: "underline text-[var(--signal)]" }, r.branch) : r.branch;
  return [h("p", {}, n + " archivo(s) publicados en ", link, " con el commit ", h("span", { class: "mono" }, (r.commit || "").slice(0, 8)), r.verified ? ". GitHub confirmó la recepción." : "."),
    r.base && r.branch !== r.base ? h("p", { class: "text-xs text-[var(--mute)]" }, "Abre un Pull Request hacia " + r.base + " cuando quieras desplegarlo.") : null];
}
function renderFeed() {
  const feed = $("feed"), stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  if (!state.tasks.size) {
    feed.replaceChildren(h("div", { class: "max-w-xl mx-auto mt-10 text-center space-y-2" },
      h("h2", { class: "text-lg font-semibold" }, "Dile al agente qué construir"),
      h("p", { class: "text-[var(--mute)]" }, "Elige un modelo y el repositorio destino, describe el requerimiento y observa cómo razona, valida el código y lo sube a GitHub.")));
    return;
  }
  feed.replaceChildren(...[...state.tasks.values()].map((t) => {
    const sel = state.selected === t.id;
    return h("article", { class: "space-y-2" },
      h("div", { class: "ml-auto max-w-[85%] rounded-lg bg-[var(--signal)] text-[var(--signal-ink)] px-3 py-2" },
        h("p", { class: "whitespace-pre-wrap" }, t.instruction || ""),
        h("p", { class: "mt-1 text-xs opacity-80" }, (t.mode === "project" ? "Proyecto completo" : "Tarea") + " · " + (t.model || "") + " · " + (t.repo || "") + (t.dry_run ? " · solo validar" : ""))),
      h("div", { class: "max-w-[92%] rounded-lg border bg-[var(--panel)] p-3 space-y-3 " + (sel ? "border-[var(--signal)]" : "border-[var(--line)]") },
        h("div", { class: "flex items-center justify-between gap-2" },
          h("span", { class: "font-medium" }, STATUS[t.status] || t.status),
          h("button", { type: "button", class: "text-xs underline underline-offset-2 text-[var(--mute)]", onclick: () => { state.selected = t.id; renderFeed(); renderPanel(); } }, sel ? "En el panel" : "Ver detalle")),
        pipeline(t), ...summary(t)));
  }));
  if (stick) feed.scrollTop = feed.scrollHeight;
}

/* ---------- panel lateral ---------- */
const TABS = [["reasoning", "Razonamiento"], ["validation", "Validación"], ["files", "Archivos"], ["connect", "Conexión"]];
function renderTabs() {
  $("tabs").replaceChildren(...TABS.map(([id, label]) => h("button", {
    type: "button", role: "tab", "aria-selected": String(state.tab === id),
    class: "px-4 py-2.5 whitespace-nowrap border-b-2 " + (state.tab === id ? "border-[var(--signal)] font-medium" : "border-transparent text-[var(--mute)]"),
    onclick: () => { state.tab = id; renderTabs(); renderPanel(); },
  }, label)));
}
const empty = (text) => h("p", { class: "text-[var(--mute)]" }, text);
function block(title, ...kids) { return h("section", { class: "space-y-1.5" }, h("h3", { class: "font-medium" }, title), ...kids); }

function panelReasoning(t) {
  if (!t) return [empty("Envía un requerimiento y aquí verás, paso a paso, cómo lo analiza el agente antes de escribir código.")];
  const out = [];
  if (t.plan) out.push(block("Plan de arquitectura", h("p", {}, t.plan.summary), h("p", { class: "text-xs text-[var(--mute)]" }, "Stack: " + (t.plan.stack || []).join(", "))));
  (t.reasoning || []).forEach((r) => out.push(block(r.stage, h("pre", { class: "mono text-xs rounded-md bg-[var(--sunk)] border border-[var(--line)] p-3" }, r.text))));
  out.push(block("Bitácora", h("ol", { class: "space-y-1" }, (t.steps || []).map((s) => h("li", { class: "flex gap-2" }, h("span", { class: "mono text-xs text-[var(--mute)]" }, s.t), h("span", {}, s.msg))))));
  if (!t.reasoning || !t.reasoning.length) out.unshift(empty(ACTIVE.has(t.status) ? "El agente aún está razonando…" : "Este modelo no devolvió bloque de razonamiento."));
  return out;
}
function panelValidation(t) {
  if (!t || !(t.validation || []).length) return [empty("Aquí aparece el resultado de cada validación (sintaxis, imports, seguridad y revisión lógica) antes del commit.")];
  return t.validation.map((v) => block(v.stage,
    h("p", { class: "font-medium " + (v.ok ? "text-[var(--ok)]" : "text-[var(--bad)]") }, v.ok ? "Aprobada" : "Con observaciones"),
    v.errors.map((x) => h("p", { class: "mono text-xs text-[var(--bad)]" }, x)),
    v.review.map((x) => h("p", { class: "mono text-xs text-[var(--warn)]" }, "[revisor] " + x)),
    v.warnings.map((x) => h("p", { class: "mono text-xs text-[var(--warn)]" }, x)),
    h("p", { class: "text-xs text-[var(--mute)]" }, "Herramientas: " + (v.tools.join(", ") || "—"))));
}
function panelFiles(t) {
  if (!t) return [empty("Los archivos del plan y del resultado aparecerán aquí.")];
  const r = t.result || {}, contents = r.contents || {}, out = [];
  const files = r.files || (t.plan ? t.plan.files.map((f) => ({ path: f.path, note: f.purpose })) : []);
  if (!files.length) return [empty("Todavía no hay archivos.")];
  out.push(h("ul", { class: "divide-y divide-[var(--line)] rounded-md border border-[var(--line)]" }, files.map((f) => h("li", {},
    h("button", { type: "button", disabled: !contents[f.path], class: "w-full text-left px-3 py-2 flex justify-between gap-3 disabled:cursor-default", onclick: () => { state.openFile = f.path; renderPanel(); } },
      h("span", { class: "mono text-xs break-all" }, f.path), h("span", { class: "text-xs text-[var(--mute)] shrink-0" }, f.bytes ? f.bytes + " B" : (f.note || "")))))));
  if (state.openFile && contents[state.openFile] != null) out.push(h("pre", { class: "mono text-xs rounded-md bg-[var(--sunk)] border border-[var(--line)] p-3 max-h-[28rem] overflow-auto" }, contents[state.openFile]));
  if ((r.warnings || []).length) out.push(block("Advertencias", r.warnings.slice(0, 10).map((w) => h("p", { class: "mono text-xs text-[var(--warn)]" }, w))));
  return out;
}
function panelConnect() {
  const c = state.cfg;
  const rows = c.env.map((v) => h("li", { class: "py-2 flex gap-3" },
    h("span", { class: "mt-0.5 font-medium shrink-0 w-5 " + (v.set ? "text-[var(--ok)]" : v.required ? "text-[var(--bad)]" : "text-[var(--mute)]"), "aria-label": v.set ? "definida" : "sin definir" }, v.set ? "✓" : v.required ? "✗" : "–"),
    h("div", {}, h("p", { class: "mono text-xs font-medium" }, v.name + (v.required ? " (obligatoria)" : " (opcional)")), h("p", { class: "text-xs text-[var(--mute)]" }, v.help))));
  return [
    block("Variables de entorno en Railway", h("ul", { class: "divide-y divide-[var(--line)]" }, rows)),
    block("Para funcionar al 100%",
      h("ol", { class: "list-decimal pl-5 space-y-1 text-[var(--mute)]" },
        h("li", {}, "Servicio con Dockerfile en docker/Dockerfile, dominio público al puerto 8080 y variable PORT=8080."),
        h("li", {}, "Volumen montado en /root/.ollama para no descargar los modelos en cada arranque."),
        h("li", {}, "Memoria suficiente para el modelo (7B ≈ 8 GB, 14B/16B ≈ 16 GB)."),
        h("li", {}, "Token de GitHub (fine-grained) con Contents: read & write solo en los repos destino."),
        h("li", {}, "Para usar otro modelo, agrégalo en EXTRA_MODELS (p. ej. deepseek-coder-v2:16b) y redespliega."))),
    block("Estado actual", h("p", {}, c.ollama_ok ? "Ollama responde. Modelos instalados: " + (c.models.installed.join(", ") || "ninguno todavía") + "." : "Ollama no responde: puede estar descargando el modelo (revisa los logs)."),
      h("p", {}, "Ramas: " + (c.target_branch ? "push directo a " + c.target_branch : "una rama nueva por tarea (agent/fecha-resumen)") + ".")),
    h("button", { type: "button", class: "rounded-md border border-[var(--line)] px-3 py-1.5", onclick: async () => { state.cfg = await api("/agent/config"); renderPills(); renderSelectors(); renderPanel(); } }, "Verificar de nuevo"),
  ];
}
function renderPanel() {
  const t = state.tasks.get(state.selected);
  const view = { reasoning: panelReasoning, validation: panelValidation, files: panelFiles, connect: panelConnect }[state.tab];
  $("panel").replaceChildren(...view(t).flat());
}

if (state.token) boot().catch(() => logout(""));
