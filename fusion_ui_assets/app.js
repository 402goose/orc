/* ORC control room: no hosted assets, no bundler, no separate frontend process. */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pretty = (value) => JSON.stringify(value, null, 2);
const active = (status) => ["running", "queued", "stopping"].includes(status);
const badge = (value) =>
  `<span class="status ${esc(value)}">${esc((value || "unknown").replaceAll("_", " "))}</span>`;
const date = (ms) =>
  ms
    ? new Date(ms).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Unknown start";
const age = (ms) => {
  if (!ms) return "—";
  const n = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  return n < 60
    ? `${n}s`
    : n < 3600
      ? `${Math.floor(n / 60)}m ${n % 60}s`
      : `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
};
const number = (n) =>
  new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n || 0);
const state = {
  workspace: null,
  workspaces: [],
  view: "overview",
  id: null,
  node: null,
  tab: "report",
  overview: null,
  config: null,
  report: null,
  decisions: [],
  decision: null,
  labelDrafts: {},
  search: "",
  filter: "all",
  epoch: 0,
  modal: null,
  polling: false,
  signature: "",
};
// localStorage, not sessionStorage: the capability is stable per machine, so a
// second tab and a later restart should both just work. The fragment is only
// cleared once it has been stored, so a bookmarked URL keeps working if not.
let token = new URLSearchParams(location.hash.slice(1)).get("token");
if (token) {
  let stored = false;
  try {
    localStorage.setItem("fusion-token", token);
    stored = true;
  } catch (error) {
    /* private window or blocked storage: keep the fragment as the only copy */
  }
  if (stored) history.replaceState(null, "", "#overview");
}
try {
  token ||= localStorage.getItem("fusion-token") || "";
} catch (error) {
  token ||= "";
}

async function api(path, body, workspace = state.workspace) {
  const url = new URL("/api/" + path, location.origin);
  if (workspace) url.searchParams.set("w", workspace);
  const response = await fetch(url, {
    method: body ? "POST" : "GET",
    headers: {
      "X-Fusion-Token": token,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify({ ...body, workspace }) : undefined,
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("visible"), 3800);
}
function error(message) {
  $("#error-banner").textContent = message || "";
  $("#error-banner").hidden = !message;
}
function intro(eyebrow, title, description, action = "") {
  return `<div class="page-intro"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${action || `<div class="date">${new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}</div>`}</div>`;
}
function empty(title, description, button = "") {
  return `<div class="empty"><div class="empty-symbol">◫</div><h2>${title}</h2><p>${description}</p>${button}</div>`;
}
function button(text, action, extra = "", type = "") {
  return `<button class="button ${type}" data-action="${action}" ${extra}>${text}</button>`;
}
function markdown(text) {
  return DOMPurify.sanitize(
    marked.parse(text || "", { gfm: true, breaks: false }),
    {
      ALLOWED_TAGS: [
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "del",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "a",
        "input",
      ],
      ALLOWED_ATTR: [
        "href",
        "title",
        "class",
        "type",
        "checked",
        "disabled",
        "start",
        "align",
      ],
      ALLOW_DATA_ATTR: false,
    },
  );
}
function enhanceMarkdown(root = $("#content")) {
  $$(".markdown pre", root).forEach((pre) => {
    const b = document.createElement("button");
    b.className = "code-copy";
    b.textContent = "Copy";
    b.addEventListener("click", () =>
      copy($("code", pre)?.textContent || pre.textContent.replace(/Copy$/, "")),
    );
    pre.append(b);
  });
  $$(".markdown a", root).forEach((link) => {
    const href = link.getAttribute("href") || "";
    if (/^https?:\/\//i.test(href)) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        let path = href.replace(/:\d+(?:-\d+)?$/, "");
        try {
          path = decodeURIComponent(path);
        } catch {}
        if (path && !path.startsWith("#")) openFile(path);
      });
    }
  });
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard");
  } catch {
    toast("Clipboard unavailable. Select and copy the text.");
  }
}
function scrollPanes(root) {
  return $$(".console,.activity-feed", root).map((el, index) => ({
    el,
    key: el.dataset.scrollKey || `${el.className}:${index}`,
  }));
}
function activityItems(el) {
  const items = $$(".activity-item", el);
  return el.hasAttribute("data-newest-first") ? items.reverse() : items;
}
const activityKey = (el) => el.dataset.activityKey || el.textContent;
function animateNewActivity(el, previous) {
  const items = activityItems(el);
  const keys = items.map(activityKey);
  // Match the retained tail, including repeated messages and capped log windows.
  let overlap = Math.min(previous.length, keys.length);
  while (
    overlap > 0 &&
    !previous.slice(-overlap).every((key, index) => key === keys[index])
  )
    overlap--;
  items.slice(overlap).forEach((item, index) =>
    item.animate(
      [
        { opacity: 0, transform: "translateY(4px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      {
        duration: 220,
        delay: Math.min(index, 4) * 25,
        easing: "ease-out",
        fill: "backwards",
      },
    ),
  );
}
function replaceContent(root, html, context) {
  const scrolls = new Map(
    root.dataset.scrollContext === context
      ? scrollPanes(root).map(({ el, key }) => [
          key,
          {
            top: el.scrollTop,
            left: el.scrollLeft,
            atBottom: el.scrollHeight - el.clientHeight - el.scrollTop <= 4,
            items: activityItems(el).map(activityKey),
          },
        ])
      : [],
  );
  root.innerHTML = html;
  root.dataset.scrollContext = context;
  enhanceMarkdown(root);
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  scrollPanes(root).forEach(({ el, key }) => {
    const previous = scrolls.get(key);
    if (previous && !reducedMotion) animateNewActivity(el, previous.items);
    el.scrollTo({
      top: previous?.top || 0,
      left: previous?.left || 0,
      behavior: "instant",
    });
    // Scrolling up pauses following. Returning to the bottom resumes it.
    if (el.hasAttribute("data-follow-tail") && (!previous || previous.atBottom))
      el.scrollTo({
        top: el.scrollHeight,
        behavior: previous && !reducedMotion ? "smooth" : "instant",
      });
  });
}
function mount(html) {
  replaceContent(
    $("#content"),
    html,
    JSON.stringify([
      state.workspace,
      state.view,
      state.id,
      state.node,
      state.tab,
    ]),
  );
}
function runRows(runs) {
  return runs
    .map(
      (run) =>
        `<div class="run-row" role="button" tabindex="0" data-action="run" data-id="${esc(run.id)}"><div><div class="run-title">${esc(run.task)}</div><div class="run-meta"><span class="mono">${esc(run.id)}</span><span>${run.read_only ? "Investigation" : "Implementation"}</span><span>${esc(run.agents?.join(" + "))}</span><span>${date(run.started_at_ms)}</span></div></div><div class="run-side">${badge(run.status)}<div class="mini-progress" title="${run.completed}/${run.total} stages accepted">${Array.from({ length: Math.min(12, run.total) }, (_, i) => `<i class="${i < run.completed ? "done" : ""}"></i>`).join("")}</div></div></div>`,
    )
    .join("");
}
function jobRows(jobs) {
  return jobs
    .map(
      (job) =>
        `<div class="job-row" role="button" tabindex="0" data-action="job" data-id="${esc(job.id)}"><div><h3>${esc(job.action)} <span class="muted">·</span> ${esc(job.title).slice(0, 130)}</h3><p>${date(job.started_at_ms)} ${active(job.status) ? "· " + age(job.started_at_ms) + " elapsed" : ""}</p></div>${badge(job.status)}</div>`,
    )
    .join("");
}
function overview() {
  const data = state.overview,
    runs = data.workflows,
    usage = data.usage,
    workerGroups = usage.by_route.filter((g) => g.agent !== "gate");
  const running = runs.filter((r) => r.status === "running").length,
    finished = runs.filter((r) => r.status === "success").length;
  const tokens =
    (usage.total.input_tokens || 0) + (usage.total.output_tokens || 0);
  const configured = state.config,
    qualified = configured?.qualified_buckets || 0;
  mount(
    intro(
      "YOUR AGENT WORKSPACE",
      "Everything in motion.",
      "From the first idea to the final review. Coordinate the work. See the evidence.",
    ) +
      `<div class="stats"><div class="stat"><div class="stat-label">Active workflows <span>↗</span></div><div class="stat-number">${running}<span class="unit">running</span></div><div class="stat-detail">${runs.length} saved workflows in this workspace</div></div><div class="stat"><div class="stat-label">Completed <span>✓</span></div><div class="stat-number">${finished}</div><div class="stat-detail">All required stages accepted</div></div><div class="stat"><div class="stat-label">Worker calls <span>⌘</span></div><div class="stat-number">${number(workerGroups.reduce((n, g) => n + g.calls - (g.cache_hit || 0), 0))}</div><div class="stat-detail">${number(tokens)} reported input + output tokens</div></div><div class="stat"><div class="stat-label">Reported spend <span>＄</span></div><div class="stat-number">${data.cost.reported_calls ? "$" + Number(usage.total.cost_usd || usage.total.cost || 0).toFixed(2) : "—"}</div><div class="stat-detail">${data.cost.reported_calls} / ${data.cost.calls} calls reported a cost</div></div></div>` +
      `<div class="grid-main"><div><div class="section-bar"><h2>Recent workflows <small>${runs.length}</small></h2><button class="subtle" data-view="workflows">View all →</button></div>${runs.length ? `<div class="panel">${runRows(runs.slice(0, 6))}</div>` : empty("Your next idea starts here.", "Launch a discovery run to map the code and surface useful improvements.", button("Start discovery", "template", 'data-template="audit"', "primary"))}
    ${data.jobs.length ? `<div class="section-bar"><h2>Launch activity</h2><small>Jobs keep running when you close this tab</small></div><div class="panel">${jobRows(data.jobs.slice(0, 5))}</div>` : ""}
    ${data.builds
      .filter((b) => b.status === "running")
      .map(
        (b) =>
          `<div class="info-box"><h3>Preparing a workflow · ${esc(b.phase)}</h3><p>${esc(b.message)} · ${age(b.started_at_ms)}</p></div>`,
      )
      .join("")}</div>
    <aside><div class="section-bar"><h2>Start with a direction</h2></div><div class="panel">${[
      [
        "audit",
        "◈",
        "Find the next improvement",
        "Evidence, ranked findings, and a concrete plan.",
      ],
      [
        "feature",
        "↗",
        "Build something useful",
        "Explore, plan, implement, independently review.",
      ],
      [
        "debug",
        "⌁",
        "Hunt down a bug",
        "Reproduce the failure, fix it, prove it.",
      ],
    ]
      .map(
        ([key, icon, title, desc]) =>
          `<div class="template" role="button" tabindex="0" data-action="template" data-template="${key}"><div class="template-top"><span class="template-icon">${icon}</span><span class="template-arrow">↗</span></div><h3>${title}</h3><p>${desc}</p></div>`,
      )
      .join(
        "",
      )}</div><div class="info-box"><h3>◇ Laya is ${esc(configured?.mode || "unavailable")}</h3><p>${configured?.mode === "shadow" ? "Local decisions are recorded as advice. Your workflow rules remain in control." : configured?.mode === "active" ? `${qualified} qualified calibration buckets. Only qualified, permitted actions can apply.` : "Classification is disabled. Existing orchestration still works."}</p><div class="worker-strip">${(configured?.workers || []).map((w) => `<span class="worker-chip"><b>${w.available ? "●" : "○"}</b>${esc(w.agent)}</span>`).join("")}</div></div></aside></div>`,
  );
}
function workflows() {
  const runs = state.overview.workflows.filter(
    (run) =>
      (state.filter === "all" || run.status === state.filter) &&
      `${run.task} ${run.id} ${run.agents}`
        .toLowerCase()
        .includes(state.search.toLowerCase()),
  );
  mount(
    intro(
      "PERSISTED WORKFLOWS",
      "The work, end to end.",
      "Terminal and browser runs share the same history. Open any run to inspect its plan, activity, and results.",
    ) +
      `<div class="filter-bar"><input id="run-search" placeholder="Search tasks, run IDs, or agents…" aria-label="Search workflows" value="${esc(state.search)}"><select id="run-filter" aria-label="Filter status">${["all", "running", "success", "failed", "blocked", "interrupted", "paused_quota", "paused_budget"].map((s) => `<option value="${s}" ${s === state.filter ? "selected" : ""}>${s === "all" ? "All statuses" : s.replaceAll("_", " ")}</option>`).join("")}</select><small>${runs.length} runs</small></div><div id="run-results">${runs.length ? `<div class="panel">${runRows(runs)}</div>` : empty("No matching workflows.", "Try another filter or start a new run.")}</div>`,
  );
}
function recommendationCards(output) {
  return (output?.findings || [])
    .map(
      (f) =>
        `<div class="finding-card"><h3><span class="number">${f.number.toString().padStart(2, "0")}</span>${esc(f.title)}</h3><div class="actions">${button("Read finding", "finding", `data-number="${f.number}"`, "small ghost")}${state.report.status === "success" && output.status === "success" ? button("Implement this →", "implement", `data-number="${f.number}" data-node="${esc(output.node_id)}"`, "small primary") : ""}</div></div>`,
    )
    .join("");
}
function publishFields(prefix, value = state.config?.publish || {}, modes = true) {
  return `<div class="form-grid">${modes ? `<div class="field"><label for="${prefix}-publish-mode">PR publication</label><select id="${prefix}-publish-mode">${[["off", "Off · current workspace"], ["manual", "Manual · isolated worktree"], ["auto", "Automatic after review"]].map(([v, label]) => `<option value="${v}" ${value.mode === v ? "selected" : ""}>${label}</option>`).join("")}</select></div>` : ""}<div class="field"><label for="${prefix}-publish-base">Target branch</label><input id="${prefix}-publish-base" list="${prefix}-branches" value="${esc(value.base || "staging")}" required><datalist id="${prefix}-branches">${(state.config?.publish?.branches || []).map(b => `<option value="${esc(b)}">`).join("")}</datalist></div><div class="field"><label for="${prefix}-publish-remote">Remote</label><input id="${prefix}-publish-remote" value="${esc(value.remote || "origin")}" required></div><div class="field"><label for="${prefix}-publish-draft">PR type</label><select id="${prefix}-publish-draft"><option value="draft" ${value.draft !== false ? "selected" : ""}>Draft</option><option value="ready" ${value.draft === false ? "selected" : ""}>Ready for review</option></select></div></div>`;
}
function publishValues(prefix) {
  return { mode: $(`#${prefix}-publish-mode`)?.value || "manual", base: $(`#${prefix}-publish-base`).value,
    remote: $(`#${prefix}-publish-remote`).value, draft: $(`#${prefix}-publish-draft`).value === "draft" };
}
function publicationCard(report) {
  const p = report.publication || {}, g = report.git || {};
  if (!p.status && !g.branch) return "";
  const checks = p.pr?.statusCheckRollup || [];
  return `<section class="panel publication-panel"><div class="panel-header"><h3>Pull request</h3>${badge(p.status || "pending")}</div><div class="panel-body"><div class="facts"><span>Target</span><span>${esc(p.base || g.base)}</span><span>Branch</span><span class="mono">${esc(p.branch || g.branch)}</span>${p.commit ? `<span>Commit</span><span class="mono">${esc(p.commit.slice(0, 12))}</span>` : ""}</div>${p.error ? `<p class="blocker">${esc(p.error)}</p>` : ""}${p.url ? `<p><a href="${esc(safeGithubURL(p.url))}" target="_blank" rel="noopener noreferrer">Open PR #${esc(p.number)} ↗</a> · ${esc(p.pr?.state || "Published")}</p>${button("Refresh PR checks", "pr-refresh", "", "small")}<p class="help-copy">${p.checked_at_ms ? "Last checked " + date(p.checked_at_ms) : "CI status has not been fetched yet."}</p>${checks.map(c => `<p class="help-copy">${esc(c.name || c.context)} · ${esc(c.conclusion || c.state || c.status)}</p>`).join("")}` : `<p class="help-copy">${g.mode === "auto" ? "Publishes automatically after the implementation and review are accepted." : "Completed stages stay saved while you publish or retry."}</p>`}${g.workspace ? `<details><summary>Run worktree</summary><p class="mono">${esc(g.workspace)}</p></details>` : ""}</div></section>`;
}
function safeGithubURL(url) {
  try { const u = new URL(url); return u.protocol === "https:" && u.hostname === "github.com" ? u.href : "#"; } catch { return "#"; }
}
function openPublish() {
  const report = state.report;
  const value = { ...state.config.publish, ...report.git, ...report.publication };
  modal("Open a pull request", "Choose the target, then inspect the exact diff and PR description.",
    `<form id="publish-preview-form" data-run="${esc(report.workflow_id)}">${publishFields("pr", value, false)}<p class="help-copy">The preview fetches the target branch and prepares a diff. Publication commits to a feature branch, pushes it, and opens the PR.</p><button class="button primary" type="submit">Preview PR →</button></form>`, "publish");
}
function showPublishPreview(p) {
  modal("Review pull request", `${p.repo} · ${p.branch} → ${p.base}`,
    `<form id="publish-form" data-run="${esc(p.workflow_id)}" data-snapshot="${esc(p.snapshot_id)}"><div class="field"><label for="pr-title">Title</label><input id="pr-title" name="title" value="${esc(p.title)}" maxlength="256" required></div><div class="field"><label for="pr-body">Description · Markdown</label><textarea id="pr-body" name="body" class="editor" required>${esc(p.body)}</textarea></div><label class="check-field"><input type="checkbox" name="draft" ${p.draft ? "checked" : ""}> Create as draft</label><details open><summary>Changes · ${p.files.length} files</summary><pre class="console publish-diff">${esc(p.diff)}</pre></details>${p.legacy ? '<label class="check-field"><input type="checkbox" name="accept_legacy_diff" required> This older run has no saved Git review snapshot. I checked this diff and confirm it is the change to publish.</label>' : '<p class="help-copy">This diff matches the saved tree from the accepted review.</p>'}<div class="dialog-footer"><small>Creates a commit, pushes the feature branch, and opens the PR against ${esc(p.base)}.</small><button class="button primary" type="submit">${p.commit ? "Retry publication →" : "Publish PR →"}</button></div></form>`, "publish");
}
function workflow() {
  const report = state.report,
    nodes = report.live_nodes;
  const task = report.task || "Untitled workflow";
  const normalized = task.replace(/\s+/g, " ").trim();
  const sentence = normalized.match(/^.{1,180}?[.!?](?:\s|$)/)?.[0].trim();
  const title =
    sentence ||
    (normalized.length > 160
      ? normalized.slice(0, 157).replace(/\s+\S*$/, "") + "…"
      : normalized);
  const request =
    task !== title
      ? `<details class="request-detail"><summary>Full request</summary><p>${esc(task)}</p></details>`
      : "";
  if (!nodes.some((n) => n.id === state.node))
    state.node =
      nodes.find((n) => n.status === "running")?.id ||
      report.primary_nodes[0] ||
      nodes[0]?.id;
  const node = nodes.find((n) => n.id === state.node),
    output = report.outputs.find((o) => o.node_id === state.node);
  const decisions =
    report.waves.flatMap((w) => w.nodes).find((n) => n.id === state.node)
      ?.decisions || {};
  const jobs = state.overview.jobs.filter(
      (j) => j.workflow_id === report.workflow_id,
    ),
    job = jobs.find((j) => active(j.status));
  let body = "";
  if (state.tab === "report")
    body = output
      ? `<div class="article-head"><div><h2>${esc(state.node)} · deliverable</h2><small>${esc(output.agent)} · ${output.source === "summary" ? "Saved summary" : "Full worker answer"}</small></div>${button("Copy Markdown", "copy-report", "", "small ghost")}</div>${output.warning ? `<div class="blocker">${esc(output.warning)}</div>` : ""}<article class="markdown">${markdown(output.text)}</article>`
      : empty(
          node?.status === "running"
            ? "The investigation is in progress."
            : "This stage has no deliverable yet.",
          "The complete answer will appear here as soon as the worker finishes. Open Activity to follow its progress.",
          button("Watch activity", "tab", 'data-tab="activity"'),
        );
  if (state.tab === "activity")
    body = `<div class="article-head"><h2>${esc(state.node)} · live activity</h2><span class="pill">Updates every 2 seconds</span></div>${node?.activity ? `<div class="facts"><span>Worker</span><span>${esc(node.agent)} · PID ${node.activity.pid}</span><span>Elapsed</span><span>${age(node.activity.started_at_ms)}</span><span>Captured logs</span><span>${number(node.activity.stdout_bytes)}B stdout / ${number(node.activity.stderr_bytes)}B stderr</span></div><hr>` : ""}<div class="activity-feed" data-scroll-key="worker-${esc(state.node)}-${node?.attempts || 0}" data-follow-tail>${node?.messages?.length ? node.messages.map((m) => `<div class="activity-item"><strong>${esc(m)}</strong></div>`).join("") : `<p class="help-copy">${node?.status === "running" ? "Waiting for the next public worker update. Heartbeats show the coordinator is waiting; they do not guarantee progress." : "No public worker updates were captured. Check the result and stderr below."}</p>`}</div>${node?.stderr ? `<details><summary>Worker stderr</summary><pre class="console">${esc(node.stderr)}</pre></details>` : ""}<hr><h3>Workflow events</h3><div class="activity-feed" data-scroll-key="workflow-events" data-newest-first>${report.events
      .slice(-40)
      .reverse()
      .map(
        (e) =>
          `<div class="activity-item" data-activity-key="${esc(pretty(e))}"><small>${date(e.ts)}</small><br><strong>${esc(e.type)}</strong> ${esc(e.node_id || "")} ${esc(e.status || "")}</div>`,
      )
      .join("")}</div>`;
  if (state.tab === "evidence")
    body = `<h2>Verification &amp; handoff</h2>${(node?.result?.blockers || []).map((b) => `<div class="blocker">${esc(b)}</div>`).join("")}<h3>Reported checks</h3><pre class="console">${esc((node?.result?.tests || []).join("\n") || "No verification reported yet.")}</pre><h3>Changed files</h3><pre class="console">${esc((node?.result?.changed || []).join("\n") || "No changed files reported.")}</pre><p class="help-copy">These are worker receipts. Inspect the actual checks and diff before treating a claim as verified.</p>`;
  if (state.tab === "spec")
    body = `<div class="article-head"><h2>Workflow definition</h2>${button("Use as a new workflow", "custom-from-run", "", "small")}</div><pre class="console">${esc(pretty(report.spec))}</pre>`;
  if (node?.quota)
    body =
      `<div class="quota-notice"><h3>${esc(node.quota.agent)} · ${esc(node.quota.route)} route reached its provider limit</h3><p>${esc(node.quota.message)}</p><p>This is the worker provider’s quota, separate from the Fusion budget. An installed CLI can still depend on a remote account with its own limits.</p>${button("Retry with another worker →", "resume", "", "small")}<small>Accepted stages stay cached. Review runs in a separate worker session.</small></div>` +
      body;
  if (node?.permission_failure)
    body =
      `<div class="quota-notice"><h3>${esc(node.permission_failure.agent)} could not run a required tool</h3><p>${state.config?.execution_mode === "yolo" && node?.result?.execution_mode !== "yolo" ? "This attempt used restricted permissions. YOLO is now configured; retry to launch with full runtime access." : esc(node.permission_failure.message)}</p><p>The worker stopped at a permission gate. Retry after configuring that worker, or choose Auto to use another permitted worker. Accepted stages stay saved.</p>${button("Retry this stage →", "resume", "", "small")}</div>` +
      body;
  mount(`<button class="back" data-view="workflows">← All workflows</button><div class="detail-heading"><div class="eyebrow">${report.read_only ? "DISCOVERY & INTELLIGENCE" : "IMPLEMENTATION WORKFLOW"}</div><h1>${esc(title)}</h1>${request}<div class="run-meta">${badge(report.status)}<span class="mono">${esc(report.workflow_id)}</span><span>${date(report.started_at_ms)}</span><span>${report.read_only ? "Investigation task" : "Implementation task"}</span></div><div class="actions">${button("Export report ↓", "export-report", "", "small")}${report.status === "success" && !report.read_only && !report.publication?.url ? button(report.publication?.status === "failed" ? "Retry publication" : "Open PR", "publish", "", "small primary") : ""}${["paused_quota", "paused_budget", "interrupted", "failed"].includes(report.status) ? button("Resume workflow", "resume", "", "small primary") : ""}${job ? button("Stop run", "cancel", `data-id="${esc(job.id)}"`, "small danger") : ""}</div></div>
    <div class="pipeline" aria-label="Workflow stages">${nodes.map((n, i) => `<button class="stage ${n.id === state.node ? "selected" : ""}" data-action="stage" data-node="${esc(n.id)}"><span class="stage-index">${String(i + 1).padStart(2, "0")}</span>${badge(n.status)}<h3>${esc(n.id)}</h3><small>${esc(n.agent)} · attempt ${n.attempts}</small>${n.needs?.length ? `<div><small>after ${esc(n.needs.join(", "))}</small></div>` : ""}</button>`).join("")}</div>
    <div class="detail-grid"><div class="panel"><div class="panel-body"><div class="tabs">${[
      ["report", "Deliverable"],
      ["activity", "Activity"],
      ["evidence", "Evidence"],
      ["spec", "Workflow JSON"],
    ]
      .map(
        ([key, label]) =>
          `<button class="tab ${state.tab === key ? "active" : ""}" data-action="tab" data-tab="${key}">${label}</button>`,
      )
      .join(
        "",
      )}</div>${body}</div></div><aside class="detail-aside">${publicationCard(report)}<div class="panel"><div class="panel-header"><h3>Run at a glance</h3></div><div class="panel-body"><div class="facts"><span>Stages accepted</span><span>${nodes.filter((n) => n.status === "success").length} / ${nodes.length}</span><span>Reported cost</span><span>${report.cost.reported_calls ? "$" + Number(report.spent_usd).toFixed(4) : "Not reported"}</span><span>Cost coverage</span><span>${report.cost.reported_calls} / ${report.cost.calls} calls</span><span>Recorded-spend budget</span><span>${report.budget_usd ? "$" + report.budget_usd : "No limit"}</span></div>${!job && report.status === "running" ? '<p class="help-copy" style="margin:18px 0 0">Started outside this UI. Observe here; interrupt it from its original terminal.</p>' : ""}</div></div>
    ${
      Object.keys(decisions).length
        ? `<div class="panel"><div class="panel-header"><h3>◇ Laya decisions</h3></div>${Object.entries(
            decisions,
          )
            .map(
              ([kind, d]) =>
                `<div class="decision-mini"><div class="mini-title"><span>${esc(kind)}</span><small>${d.applied ? "Applied" : "Advisory"}</small></div>${Object.entries(
                  d.recommendations || {},
                )
                  .map(
                    ([k, v]) =>
                      `<p>${esc(k)} → ${esc(v.value)} · ${(v.probability * 100).toFixed(1)}%</p><div class="probability"><i style="width:${Math.max(0, Math.min(100, v.probability * 100))}%"></i></div>`,
                  )
                  .join(
                    "",
                  )}<p>Policy action: ${esc(d.actual || d.status || "not recorded")}</p></div>`,
            )
            .join("")}</div>`
        : ""
    }
    ${output?.findings?.length ? `<div><div class="section-bar"><h2>Next moves <small>${output.findings.length}</small></h2></div>${recommendationCards(output)}</div>` : ""}${report.blockers.length ? `<div class="panel"><div class="panel-header"><h3>Open blockers</h3></div><div class="panel-body">${report.blockers.map((b) => `<div class="blocker">${esc(b.node_id)}: ${esc(b.blocker)}</div>`).join("")}</div>` : ""}</aside></div>`);
}
function decisionPredictions(record) {
  return Object.entries(record.recommendations || {})
    .map(([k, v]) => `${k} → ${v.value} (${(v.probability * 100).toFixed(1)}%)`)
    .join(" · ");
}
function labelDraft(record) {
  const key = state.workspace + ":" + record.id;
  const approved = record.labels?.at(-1);
  const draft = (state.labelDrafts[key] ||= {
    answers: { ...(approved?.answers || {}) },
    evidence: approved?.evidence || "",
    suggestion_id: approved?.suggestion_id || null,
    seen: approved?.suggestion_id || null,
    worker: "auto",
    dirty: false,
  });
  const suggestion = record.suggestions?.at(-1);
  if (suggestion && suggestion.suggestion_id !== draft.seen && !draft.dirty &&
      (!approved || suggestion.time_ms > approved.time_ms)) {
    draft.suggestion_id = draft.seen = suggestion.suggestion_id;
    draft.answers = Object.fromEntries(Object.entries(suggestion.answers).map(([k, v]) => [k, v.value]));
    draft.evidence = Object.entries(suggestion.answers).map(([k, v]) =>
      `${k} = ${v.value}: ${v.reason} [${v.evidence.join(", ")}]`).join("\n\n");
  }
  return draft;
}
function canApproveLabels(draft) {
  return Object.values(draft.answers).some(value => value !== "") && !!draft.evidence.trim();
}
function labelEditor(record) {
  const draft = labelDraft(record);
  const suggestion = record.suggestions?.find(s => s.suggestion_id === draft.suggestion_id);
  const latest = record.suggestions?.at(-1);
  const job = record.suggestion_job;
  const busy = job && active(job.status);
  const options = q => q.type === "noul" ? ["false", "true"] : q.type === "score" ? Object.keys(q.criteria).map((_, i) => String(i)) : Object.keys(q.criteria || {});
  return `<section class="review-form"><div class="article-head"><div><h3>Teach from verified evidence</h3><p class="help-copy">Generate a draft, check its reasoning, then edit and approve. Only approved labels enter training exports.</p></div></div>
    <div class="label-tools"><div class="field"><label for="label-worker">Labeling worker</label><select id="label-worker"><option value="auto">Auto · available worker</option>${(state.config?.workers || []).map(w => `<option value="${esc(w.agent)}" ${draft.worker === w.agent ? "selected" : ""} ${!w.available ? "disabled" : ""}>${esc(w.agent)}</option>`).join("")}</select></div>${button(busy ? "Drafting labels…" : latest ? "Regenerate labels" : "Suggest labels", "suggest-labels", busy ? "disabled" : "", "primary")}</div>
    <p class="help-copy">Uses a coding worker with your configured account. Laya’s prediction is withheld from the worker; unsupported answers stay unlabeled.</p>
    ${job ? `<div class="label-job" role="status">${badge(job.status)}<span>${busy ? "Reading evidence and drafting labels · " + age(job.started_at_ms) : job.status === "success" ? (latest && !Object.keys(latest.answers).length ? "Assessment saved; no labels suggested." : "Draft saved. Review below before approving.") : "No new draft was saved. Open activity for the error, then retry with another worker."}</span>${button("View activity", "job", `data-id="${esc(job.id)}"`, "small")}${busy ? button("Stop", "cancel", `data-id="${esc(job.id)}"`, "small") : ""}</div>` : ""}
    ${latest && latest.suggestion_id !== draft.suggestion_id ? `<div class="launch-note">A new draft is ready. Your edits have been preserved. ${button("Use latest draft", "use-label-draft", "", "small")}</div>` : ""}
    ${suggestion && !Object.keys(suggestion.answers).length ? `<div class="launch-note">No labels were suggested. Check the reasons below. You can fill in any answer supported by the original input and add your evidence, or leave it unlabeled. Nothing enters training until you approve.</div>` : ""}
    ${suggestion ? `<div class="label-reasoning"><div class="article-head"><h3>Suggested assessment</h3><span class="pill">Draft · ${esc(suggestion.agent)}</span></div>${Object.entries(suggestion.answers).map(([key, item]) => `<p><strong>${esc(key)} → ${esc(item.value)}</strong><br>${esc(item.reason)} <small>[${esc(item.evidence.join(", "))}]</small></p>`).join("")}${Object.entries(suggestion.abstentions).map(([key, reason]) => `<p class="help-copy"><strong>${esc(key)} · needs evidence</strong><br>${esc(reason)}</p>`).join("")}<details><summary>Evidence used · ${suggestion.sources.length} sources</summary>${suggestion.sources.map(s => `<h4>${esc(s.id)} · ${esc(s.title)}</h4>${s.timing ? `<p class="help-copy">${esc(s.timing)}</p>` : ""}<pre class="console label-source">${esc(typeof s.text === "string" ? s.text : pretty(s.text))}${s.truncated ? "\n[Excerpt truncated]" : ""}</pre>`).join("")}</details></div>` : ""}
    <form id="label-form" data-decision="${esc(record.id)}"><div class="form-grid">${Object.entries(record.questions || {}).map(([key, q], i) => `<div class="field"><label for="label-answer-${i}">${esc(key)}</label><select id="label-answer-${i}" name="${esc(key)}"><option value="">Leave unlabeled</option>${options(q).map(v => `<option value="${esc(v)}" ${draft.answers[key] === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select><small>${esc(q.instructions || "")}</small></div>`).join("")}</div><div class="field"><label for="label-evidence">Verification evidence</label><textarea id="label-evidence" placeholder="What did you check? Which reproduction, diff, or test proves the answer?" required>${esc(draft.evidence)}</textarea></div><p class="help-copy">Select at least one answer and add verification evidence to approve. You can leave other questions unlabeled; only selected answers enter training.</p><button class="button primary" type="submit" ${canApproveLabels(draft) ? "" : "disabled"}>${draft.suggestion_id ? "Approve labels" : "Save reviewed labels"}</button>${record.labels?.length ? `<p class="help-copy" style="margin-top:15px">${record.labels.length} review(s) saved for this decision.</p>` : ""}</form></section>`;
}
function captureLabelEdits() {
  const form = $("#label-form");
  const record = state.decisions.find(r => r.id === form?.dataset.decision);
  if (!record) return;
  const draft = labelDraft(record);
  draft.answers = Object.fromEntries(new FormData(form));
  draft.evidence = $("#label-evidence").value;
  draft.dirty = true;
  form.querySelector('button[type="submit"]').disabled = !canApproveLabels(draft);
}
function decisions() {
  const records = state.decisions;
  const record = records.find((r) => r.id === state.decision) || records[0];
  state.decision = record?.id;
  mount(
    intro(
      "LOCAL INTELLIGENCE",
      "Meet the decision layer.",
      "Inspect recommendations, compare probabilities, and teach Laya from outcomes you have verified.",
      `<div class="actions">${button("Learning tools", "learning")}${button("New probe", "probe", "", "primary")}</div>`,
    ) +
      `<div class="lab-layout"><div class="panel decision-list">${records.length ? records.map((r) => `<div class="decision-row ${r.id === state.decision ? "selected" : ""}" role="button" tabindex="0" data-action="decision" data-id="${esc(r.id)}"><div class="decision-kind">◇ ${esc(r.kind)} ${badge(r.status)}</div><div class="prediction">${esc(decisionPredictions(r) || r.error || "No prediction")}</div><div class="run-meta"><span>${esc(r.mode)}</span><span>${number(r.duration_ms)}ms</span>${r.labels?.length ? "<span>✓ reviewed</span>" : ""}</div></div>`).join("") : '<div class="panel-body"><p>No decisions recorded yet. Run a local probe or start a workflow.</p></div>'}</div>
    <div class="panel"><div class="panel-body">${
      record
        ? `<div class="article-head"><div><h2>${esc(record.kind)} decision</h2><small>${esc(record.id)}</small></div><span class="pill">${esc(record.mode)}</span></div>${record.error ? `<div class="blocker">${esc(record.error)}</div>` : ""}${record.truncated ? '<div class="blocker">Input was truncated. This decision cannot qualify for automatic action or reviewed labels.</div>' : ""}${Object.entries(
            record.recommendations || {},
          )
            .map(
              ([key, value]) =>
                `<div class="prob-row"><header><span>${esc(key)} → <strong>${esc(value.value)}</strong></span><span>${(value.probability * 100).toFixed(1)}%</span></header><div class="probability"><i style="width:${Math.max(0, Math.min(100, value.probability * 100))}%"></i></div><small>${esc(record.questions?.[key]?.instructions)}</small></div>`,
            )
            .join(
              "",
            )}<h3 style="margin-top:25px">What actually happened</h3>${record.applications?.length ? record.applications.map((a) => `<p class="help-copy"><strong>${esc(a.actual)}</strong> · ${a.applied ? "Model recommendation applied" : "Policy decision; recommendation advisory"}<br>${esc(a.reason || "")}</p>`).join("") : '<p class="help-copy">This probe has no workflow action attached.</p>'}<details><summary>Input &amp; model evidence</summary><pre class="console">${esc(pretty({ state: record.state, prediction: record.prediction, model: record.model_identity, context: record.context }))}</pre></details>${
            record.status === "ok" && !record.truncated
              ? labelEditor(record)
              : ""
          }`
        : empty(
            "A small model. An inspectable decision.",
            "Probe intake, review, recovery, or acceptance without launching a coding agent.",
            button("Try a probe", "probe", "", "primary"),
          )
    }</div></div></div>`,
  );
}
function appearanceControls() {
  const current = ORCAppearance.get();
  return `<div class="appearance-controls"><div class="appearance-mode" role="group" aria-label="Color mode">${[
    ["light", "☀", "Light"],
    ["dark", "☾", "Dark"],
    ["system", "◐", "System"],
  ]
    .map(
      ([id, icon, label]) =>
        `<button type="button" data-action="appearance-mode" data-mode="${id}" aria-pressed="${current.mode === id}"><span aria-hidden="true">${icon}</span> ${label}</button>`,
    )
    .join(
      "",
    )}</div><div class="theme-grid" role="group" aria-label="Color theme">${ORCAppearance.themes.map((t) => `<button type="button" class="theme-option" data-action="appearance-theme" data-theme="${t.id}" aria-pressed="${current.theme === t.id}" style="--swatch-dark:${t.dark};--swatch-light:${t.light};--swatch-bg:hsl(${t.hue} ${t.saturation}% 12%)"><span class="theme-preview" aria-hidden="true"><i></i><i></i><i></i></span><span>${t.name}</span><span class="theme-check" aria-hidden="true">✓</span></button>`).join("")}</div><p class="help-copy">Saved automatically in this browser. System follows your device’s appearance.</p></div>`;
}
function syncAppearanceControls() {
  const current = ORCAppearance.get();
  $$('[data-action="appearance-mode"]').forEach((el) =>
    el.setAttribute("aria-pressed", String(el.dataset.mode === current.mode)),
  );
  $$('[data-action="appearance-theme"]').forEach((el) =>
    el.setAttribute("aria-pressed", String(el.dataset.theme === current.theme)),
  );
  $("#appearance-name").textContent =
    ORCAppearance.themes.find((t) => t.id === current.theme).name +
    " · " +
    current.mode;
}
document.addEventListener("orc-appearance-change", syncAppearanceControls);
syncAppearanceControls();

function settings() {
  const c = state.config;
  mount(
    intro(
      "WORKSPACE CONFIGURATION",
      "Make it work your way.",
      "Tune workers, routes, model selection, and Laya. Changes apply to future launches in this workspace.",
    ) +
      `<section class="panel appearance-panel"><div class="panel-header"><h2>Appearance</h2><span class="pill">This browser</span></div><div class="panel-body">${appearanceControls()}</div></section>` +
      `<div class="info-box" style="margin:0 0 24px"><h3>Settings source · ${esc(c.source)}</h3><p>Save creates or updates this workspace’s config. Other keys stay intact; masked secrets keep their existing values.${Object.keys(c.environment).length ? ` Environment overrides: ${esc(pretty(c.environment))}` : ""}</p></div><div class="settings-grid"><section class="panel"><div class="panel-header"><h2>Fusion &amp; Laya</h2><span class="pill">${c.qualified_buckets} qualified buckets</span></div><div class="panel-body"><div class="form-grid"><div class="field full"><label for="setting-execution">Runtime access</label><select id="setting-execution">${["yolo", "restricted"].map((m) => `<option value="${m}" ${m === c.execution_mode ? "selected" : ""}>${m === "yolo" ? "YOLO · full access, no approval prompts" : "Restricted · per-worker permissions"}</option>`).join("")}</select><small>YOLO applies to all workers and routes, including reviews. Review-only tasks remain instructions to avoid edits; runtime access is unrestricted.</small></div><div class="field"><label>Laya mode</label><select id="setting-mode">${["off", "shadow", "active"].map((m) => `<option ${m === c.effective.decisions.mode ? "selected" : ""}>${m}</option>`).join("")}</select></div><div class="field"><label>Preferred worker</label><select id="setting-worker">${["codex", "claude", "agy", "grok"].map((m) => `<option ${m === c.effective.sidekick ? "selected" : ""}>${m}</option>`).join("")}</select></div></div><p class="help-copy">Active mode still requires qualified calibration, allowed decision kinds, and every workflow permission check.</p><h3>GitHub publishing</h3>${publishFields("settings", c.publish)}<p class="help-copy">Manual and automatic modes start new implementation runs in a clean worktree from the target branch. Automatic mode authorizes a feature-branch push and PR creation after successful review.</p><div class="field"><label for="fusion-config">Project .fusion.json</label><textarea id="fusion-config" class="editor" spellcheck="false">${esc(pretty(c.local))}</textarea><small>Full control over routes, workers, timeouts, telemetry, and calibration paths.</small></div><div class="setting-footer">${button("Save Fusion settings", "save-settings", 'data-target="fusion"', "primary")}${button("View resolved settings", "effective", "", "ghost")}</div></div></section><section class="panel"><div class="panel-header"><h2>ORC model preferences</h2></div><div class="panel-body"><p class="help-copy">Choose a project model, small model, permission mode, or saved profile. Project preferences leave your global ORC defaults intact.</p><div class="field"><label for="orc-config">Project .orc.json</label><textarea id="orc-config" class="editor" spellcheck="false" placeholder='{"model":"provider/model-id"}'>${esc(pretty(c.orc))}</textarea><small>Browse the ORC models tab for available IDs and tool-fit results.</small></div>${button("Save ORC settings", "save-settings", 'data-target="orc"', "primary")}<hr><h3>Local runtime</h3><p class="help-copy">Install or repair Laya’s optional Python runtime and cached English checkpoint. Normal inference uses cached weights.</p>${button("Set up Laya runtime", "setup")}<hr><h3>Workers on PATH</h3><div class="worker-strip">${c.workers.map((w) => `<span class="worker-chip" title="${esc(w.reason || "")}"><b>${w.available ? "●" : "○"}</b>${esc(w.agent)} · ${!w.available ? "missing" : w.automatic_ready === false ? "headless setup needed" : "installed"}</span>`).join("")}</div>${c.workers
        .filter((w) => w.available && w.automatic_ready === false)
        .map(
          (w) => `<p class="help-copy">${esc(w.agent)}: ${esc(w.reason)}</p>`,
        )
        .join("")}</div></section></div>`,
  );
}
function models() {
  mount(
    intro(
      "OPENROUTER × CLAUDE",
      "Find the right worker.",
      "Inspect your resolved ORC model, tool-capable models, quality rankings, and saved profiles.",
    ) +
      `<div class="model-toolbar">${[
        ["status", "Resolved model"],
        ["models", "Tool-capable models"],
        ["free", "Free models"],
        ["quality", "Quality rankings"],
        ["profiles", "Profiles"],
      ]
        .map(([cmd, label]) =>
          button(
            label,
            "orc",
            `data-command="${cmd}"`,
            cmd === "status" ? "primary" : "",
          ),
        )
        .join(
          "",
        )}</div><div class="panel"><div class="panel-header"><h3 id="orc-title">Resolved model</h3><small>Read through your installed ORC CLI</small></div><div class="panel-body"><pre id="orc-output" class="model-output">Loading…</pre></div></div><div class="info-box"><h3>Tool support ≠ verified tool fit</h3><p>FIT means ORC verified a tool round trip. UNTESTED is not a failure. Manage project model IDs and profiles in Settings.</p></div>`,
  );
  loadOrc("status");
}
async function loadOrc(command) {
  $("#orc-output").textContent = "Reading ORC…";
  try {
    const data = await api("orc?command=" + command);
    if (state.view !== "models") return;
    $("#orc-title").textContent = {
      status: "Resolved model",
      models: "Tool-capable models",
      free: "Free models",
      quality: "Quality rankings",
      profiles: "Saved profiles",
    }[command];
    let text = data.text;
    try {
      text = pretty(JSON.parse(text));
    } catch {}
    $("#orc-output").textContent = text + (data.error ? "\n" + data.error : "");
    $$("[data-action=orc]").forEach((b) =>
      b.classList.toggle("primary", b.dataset.command === command),
    );
  } catch (e) {
    if ($("#orc-output")) $("#orc-output").textContent = e.message;
  }
}
function modal(title, description, body, kind = null) {
  state.modal = kind;
  $("#dialog-content").innerHTML =
    `<div class="dialog-header"><div><h2 id="dialog-title">${esc(title)}</h2><p>${esc(description)}</p></div><button class="icon-button" data-action="close" aria-label="Close dialog">×</button></div><div class="dialog-body"><div id="dialog-error" class="dialog-error" hidden></div>${body}</div>`;
  if (!$("#dialog").open) $("#dialog").showModal();
  enhanceMarkdown($("#dialog"));
}
function modalError(message) {
  const el = $("#dialog-error");
  if (el) {
    el.textContent = message;
    el.hidden = false;
  } else error(message);
}
function closeModal() {
  state.modal = null;
  $("#dialog").close();
}
const templates = {
  audit: {
    kind: "discovery",
    text: "Find the five highest-value bugs and improvements in this repository. Read repository instructions and preserve existing changes. Cite exact files and evidence. Rank by impact, effort, and regression risk. Use numbered bold headings (1. **Title**) for each finding. For the top three, provide reproduction steps, a scoped implementation plan, acceptance criteria, and exact verification commands. Return recommendations only. Use BLOCKERS: none when investigation is complete; list unmeasured risks as caveats.",
  },
  feature: { kind: "build", text: "" },
  debug: { kind: "debug", text: "" },
};
function openLaunch(options = {}) {
  const kind = options.kind || "discovery";
  modal(
    options.from_workflow ? "Implement a finding" : "Start a new run",
    "Give Fusion an outcome. It handles the stages and saves the evidence.",
    `<form id="launch-form"><input type="hidden" name="from_workflow" value="${esc(options.from_workflow || "")}"><input type="hidden" name="from_node" value="${esc(options.from_node || "")}"><input type="hidden" name="finding" value="${esc(options.finding || "")}">${options.from_workflow ? `<div class="launch-note">Recommendation ${esc(options.finding)} · ${esc(options.title || "")}<br>The worker will revalidate the finding against your current checkout.</div>` : ""}<div class="form-grid"><div class="field"><label for="launch-kind">Workflow</label><select id="launch-kind" name="kind">${[
      ["discovery", "Discover & plan"],
      ["build", "Build a feature"],
      ["debug", "Reproduce & fix"],
      ["review", "Independent review"],
      ["delegate", "Single worker"],
      ["workflow", "Custom workflow JSON"],
    ]
      .map(
        ([v, t]) =>
          `<option value="${v}" ${kind === v ? "selected" : ""}>${t}</option>`,
      )
      .join(
        "",
      )}</select></div><div class="field"><label for="launch-mode">Laya mode for this run</label><select id="launch-mode" name="mode">${["shadow", "off", "active"].map((m) => `<option ${m === (state.config?.mode || "shadow") ? "selected" : ""}>${m}</option>`).join("")}</select></div></div><div class="field" id="prompt-field"><label for="launch-text">${options.from_workflow ? "Additional constraints" : "What should happen?"}</label><textarea id="launch-text" name="text" rows="6" placeholder="Describe a feature, a bug, an investigation, or paste a GitHub issue URL…">${esc(options.text || "")}</textarea></div><div id="custom-field" class="field" hidden><label for="launch-spec">fusion.workflow.v1 JSON</label><textarea id="launch-spec" class="editor" name="spec" spellcheck="false">${esc(pretty(options.spec || { schema: "fusion.workflow.v1", task: "Inspect this repository", nodes: [{ id: "explore", agent: "auto", write: false, task: "Inspect the repository and report useful findings. Do not delegate further." }] }))}</textarea></div><div class="form-grid" id="worker-fields" hidden><div class="field"><label>Worker</label><select name="agent">${["auto", "codex", "claude", "agy", "grok"].map((a) => `<option>${a}</option>`).join("")}</select></div><div class="field"><label>Role</label><select name="role">${["review", "discovery", "planning", "implementation"].map((a) => `<option>${a}</option>`).join("")}</select></div><div class="field full"><label>Route</label><select name="route"><option value="">Automatic / native</option>${Object.keys(
      state.config?.effective?.routes || {},
    )
      .map((r) => `<option>${esc(r)}</option>`)
      .join(
        "",
      )}</select></div></div><div id="build-fields"><div class="form-grid"><div class="field"><label for="launch-attempts">Maximum attempts per stage</label><input id="launch-attempts" name="attempts" type="number" min="1" max="5" value="${kind === "discovery" ? 1 : 2}"></div><div class="field"><label for="launch-budget">Reported-spend budget · USD</label><input id="launch-budget" name="budget" type="number" min="0" step="0.5" value="0"><small>0 = no limit. Not a hard cap when providers omit costs.</small></div></div><label class="check-field"><input type="checkbox" name="prepare" id="prepare-only"> Prepare brief and workflow only</label></div><label class="check-field" id="write-field"><input type="checkbox" name="allow_write" id="allow-write"> Allow this run to edit workspace files</label><section id="launch-publish-fields"><h3>Pull request</h3>${publishFields("launch")}<p class="help-copy">Manual and automatic publishing use a separate worktree from the selected target. Automatic mode commits and pushes accepted changes and creates the PR.</p></section><div class="launch-note" id="launch-preview"></div><div class="dialog-footer"><small>${state.config?.execution_mode === "yolo" ? "YOLO · full runtime access. " : "Restricted runtime. "}Runs use your configured agent accounts. Closing this tab leaves them running.</small><button class="button primary" type="submit" id="launch-submit">Start run →</button></div></form>`,
    "launch",
  );
  $("#launch-form").addEventListener("change", updateLaunch);
  updateLaunch();
}
function updateLaunch() {
  const k = $("#launch-kind").value,
    prep = $("#prepare-only").checked && !["delegate", "workflow"].includes(k);
  $("#custom-field").hidden = k !== "workflow";
  $("#prompt-field").hidden = k === "workflow";
  $("#worker-fields").hidden = k !== "delegate";
  $("#build-fields").hidden = ["delegate", "workflow"].includes(k);
  const writes =
    ["build", "debug", "delegate", "workflow"].includes(k) && !prep;
  $("#write-field").hidden = !writes;
  $("#launch-publish-fields").hidden = !["build", "debug"].includes(k);
  if (!writes) $("#allow-write").checked = false;
  $("#launch-submit").textContent =
    prep && !["delegate", "workflow"].includes(k)
      ? "Prepare workflow →"
      : "Start run →";
  $("#launch-preview").textContent =
    k === "discovery"
      ? "Explore → plan. Reads the repository and produces recommendations."
      : k === "review"
        ? "Explore → independent review. Unresolved findings block acceptance."
        : k === "delegate"
          ? "One fresh worker session with the selected route and role."
          : k === "workflow"
            ? "Runs the exact nodes, dependencies, permissions, and checks in this definition."
            : prep
              ? "Creates an inspectable brief and workflow. No coding workers start."
              : "Explore → plan → implement → independent review. Edits require the checkbox above.";
}
async function launch(body) {
  const job = await api("launch", body);
  closeModal();
  await refresh(true);
  openJob(job.id);
  toast("Launch started. Progress is saved locally.");
}
async function openJob(id) {
  try {
    const job = await api("job?id=" + encodeURIComponent(id));
    modal(
      job.title || job.action,
      "Launch activity · " + id,
      `<div id="job-content"></div>`,
      { job: id },
    );
    renderJob(job);
  } catch (e) {
    error(e.message);
  }
}
function renderJob(job) {
  if (!$("#job-content")) return;
  const result = job.result || {};
  const controls = `<div class="actions" style="margin:16px 0">${job.workflow_id ? button("Open workflow →", "job-workflow", `data-id="${esc(job.workflow_id)}"`, "primary") : ""}${active(job.status) ? button(job.status === "stopping" ? "Stopping…" : "Stop job", "cancel", `data-id="${esc(job.id)}" ${job.status === "stopping" ? "disabled" : ""}`, "danger") : ""}${result.workflow && !result.workflow_id ? button("Inspect prepared workflow", "prepared", `data-path="${esc(result.workflow)}"`) : ""}</div>`;
  replaceContent(
    $("#job-content"),
    `<div class="pill-row">${badge(job.status)}<small>${active(job.status) ? age(job.started_at_ms) + " elapsed" : date(job.finished_at_ms)}</small></div>${controls}${job.error ? `<div class="blocker">${esc(job.error)}</div>` : ""}<pre class="console job-console" data-scroll-key="job-console" data-follow-tail>${esc(job.console || (active(job.status) ? "Starting the local coordinator…" : "Job finished. See its result below."))}</pre>${job.output ? `<details ${!active(job.status) && !job.workflow_id ? "open" : ""}><summary>Result</summary><pre class="console">${esc(typeof result === "object" && Object.keys(result).length ? pretty(result) : job.output)}</pre></details>` : ""}<p class="help-copy" style="margin:16px 0 0">You can close this dialog. The job and its logs remain available under Launch activity.</p>`,
    JSON.stringify([state.workspace, job.id]),
  );
}
function openProbe() {
  modal(
    "Ask the local model",
    "Probe a decision without starting a coding worker.",
    `<form id="probe-form"><div class="field"><label>Decision kind</label><select name="kind">${["intake", "review", "acceptance", "recovery"].map((k) => `<option>${k}</option>`).join("")}</select></div><div class="field"><label>Input · plain text or JSON</label><textarea name="text" required rows="7" placeholder='{"task":"Add CSV export with tests","summary":"Did nothing","changed":[],"tests":[]}'>Investigate the highest-impact reliability improvements in this repository. Return recommendations only.</textarea></div><p class="help-copy">The first checkpoint load can take tens of seconds. Subsequent decisions within the same process reuse the loaded model.</p><div class="dialog-footer"><small>Local inference · no coding agents</small><button class="button primary">Run probe →</button></div></form>`,
    "probe",
  );
}
function openLearning() {
  modal(
    "Learn from reviewed outcomes",
    "Export, train, evaluate, and calibrate without automatically promoting a model.",
    `<form id="learning-form"><div class="field"><label>Action</label><select name="action" id="learning-action"><option value="export">Export reviewed labels</option><option value="train">Train a separate candidate</option><option value="evaluate">Evaluate with shuffled-state control</option><option value="calibrate">Calibrate held-out predictions</option></select></div><div class="field"><label>Dataset path · inside this workspace’s .fusion directory</label><input name="dataset" placeholder="ui/jobs/…/dataset.jsonl"><small>Export does not need a dataset. Other actions require a prior export or evaluation output.</small></div><div class="field"><label>Candidate model path · optional, inside .fusion</label><input name="model_path" placeholder="ui/jobs/…/candidate"><small>Used by train/evaluate; leave empty to use the configured checkpoint.</small></div><div class="launch-note">Outputs are written to a new job directory. Training never changes your active model. Calibration needs independent held-out groups to qualify.</div><button class="button primary">Start learning job →</button></form>`,
    "learning",
  );
}
async function openFile(path) {
  try {
    const data = await api("file?path=" + encodeURIComponent(path));
    modal(
      "Source & artifacts",
      data.path,
      `<pre class="source-code">${esc(data.text)}</pre>`,
      null,
    );
  } catch (e) {
    toast(e.message);
  }
}
function download(name, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function openResume() {
  const nodes = state.report.live_nodes.filter((n) => n.status !== "success");
  const selected = nodes.find((n) => n.id === state.node) || nodes[0];
  if (!selected) return toast("All stages are already accepted.");
  const worker =
    selected.agent === "auto" || selected.quota || selected.permission_failure
      ? "auto"
      : "";
  modal(
    "Resume workflow",
    "Keep accepted stages and retry unfinished work with your chosen worker.",
    `<form id="resume-form"><p class="help-copy">${esc(state.report.workflow_id)} · ${state.config.execution_mode === "yolo" ? "YOLO runtime: full access, no permission prompts" : "Restricted runtime"}</p><div class="field"><label for="resume-node">Stage to retry</label><select id="resume-node" name="node">${nodes.map((n) => `<option value="${esc(n.id)}" ${n.id === selected.id ? "selected" : ""}>${esc(n.id)} · ${esc(n.status.replaceAll("_", " "))}</option>`).join("")}</select></div><div class="field"><label for="resume-worker">Worker or route</label><select id="resume-worker" name="worker"><option value="" ${worker === "" ? "selected" : ""}>Keep the configured worker</option><option value="auto" ${worker === "auto" ? "selected" : ""}>Auto · fall back to available workers</option>${(state.config?.workers || []).map((w) => `<option value="${w.agent}" ${!w.available ? "disabled" : ""}>${w.agent} · ${!w.available ? "not on PATH" : w.automatic_ready === false ? "headless setup needed" : "installed"}</option>`).join("")}${Object.entries(
      state.config?.effective?.routes || {},
    )
      .map(
        ([name, route]) =>
          `<option value="route:${esc(name)}">${esc(name)} · ${esc(route.agent)} route</option>`,
      )
      .join(
        "",
      )}</select><small>Auto skips exhausted or currently blocked routes. Selecting a worker explicitly retries it using the current runtime access mode. Local CLIs can still call paid remote providers.</small></div><div class="field"><label for="resume-attempts">Attempt limit per stage</label><input id="resume-attempts" name="max_attempts" type="number" min="${selected.attempts + 1}" max="100" value="${Math.max(state.report.spec.max_attempts, selected.attempts + 1)}" required><small>Attempts already used count toward this limit. Fallback never resets the counter.</small></div>${!state.report.read_only ? '<label class="check-field"><input name="allow_write" type="checkbox" required> Allow this workflow to edit workspace files</label>' : ""}<button class="button primary">Resume →</button></form>`,
    "resume",
  );
}
async function navigate(view, id = null) {
  state.view = view;
  state.id = id;
  state.node = null;
  state.tab = "report";
  state.signature = "";
  state.epoch++;
  history.replaceState(
    null,
    "",
    "#" + view + (id ? "/" + encodeURIComponent(id) : ""),
  );
  $$(".nav-item").forEach((b) =>
    b.classList.toggle(
      "active",
      b.dataset.view === (view === "workflow" ? "workflows" : view),
    ),
  );
  $("#view-name").textContent =
    {
      overview: "Overview",
      workflows: "Workflows",
      workflow: "Workflow",
      decisions: "Laya lab",
      models: "ORC models",
      settings: "Settings",
    }[view] || "Overview";
  $("#content").innerHTML = '<div class="loading">Loading workspace…</div>';
  error("");
  try {
    await refresh(true);
  } catch (e) {
    error(e.message);
  }
}
async function refresh(force = false) {
  const epoch = state.epoch,
    w = state.workspace;
  try {
    const data = await api("overview", undefined, w);
    if (epoch !== state.epoch) return;
    state.overview = data;
    $("#connection").textContent = "live";
    error("");
    if (force || !state.config) {
      state.config = await api("config", undefined, w);
      if (epoch !== state.epoch) return;
      $("#mode-chip").textContent = "Laya · " + state.config.mode;
      $("#execution-chip").textContent =
        state.config.execution_mode === "yolo"
          ? "YOLO · full access"
          : "Restricted runtime";
    }
    let signature = pretty({ ...data, now_ms: 0 });
    if (state.view === "workflow") {
      const report = await api(
        "workflow?id=" + encodeURIComponent(state.id),
        undefined,
        w,
      );
      if (epoch !== state.epoch) return;
      state.report = report;
      signature = pretty(report);
    }
    if (
      state.view === "decisions" &&
      (force || !$("input:focus,textarea:focus,select:focus"))
    ) {
      const d = await api("decisions", undefined, w);
      if (epoch !== state.epoch) return;
      state.decisions = d.records;
      signature = pretty(d);
    }
    const formsActive =
      !!$("input:focus,textarea:focus,select:focus");
    if (
      force ||
      (signature !== state.signature &&
        !formsActive &&
        !["settings", "models"].includes(state.view))
    ) {
      state.signature = signature;
      if (state.view === "overview") overview();
      else if (state.view === "workflows") workflows();
      else if (state.view === "workflow") workflow();
      else if (state.view === "decisions") decisions();
      else if (state.view === "settings") settings();
      else if (state.view === "models") models();
    }
    if (state.modal?.job) {
      const id = state.modal.job,
        job = await api("job?id=" + encodeURIComponent(id), undefined, w);
      if (state.modal?.job === id && epoch === state.epoch) renderJob(job);
    }
  } catch (e) {
    $("#connection").textContent = "offline";
    error(e.message);
    if (force) throw e;
  }
}
async function chooseWorkspace(id) {
  state.workspace = id;
  localStorage.setItem("fusion-workspace", id);
  state.config = null;
  const ws = state.workspaces.find((w) => w.id === id);
  $("#workspace").value = id;
  $("#workspace-path").textContent = ws?.path || "";
  $("#workspace-path").title = ws?.path || "";
  await navigate("overview");
}
function fillWorkspaces() {
  $("#workspace").innerHTML = state.workspaces
    .map((w) => `<option value="${w.id}">${esc(w.name)}</option>`)
    .join("");
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action],[data-view]");
  if (!target) return;
  try {
    if (target.dataset.view) {
      event.preventDefault();
      await navigate(target.dataset.view);
      return;
    }
    const a = target.dataset.action;
    if (a === "appearance")
      modal(
        "Make yourself at home.",
        "Ten palettes. Light, dark, or in sync with your device.",
        appearanceControls(),
        "appearance",
      );
    else if (a === "appearance-theme")
      ORCAppearance.set({ theme: target.dataset.theme });
    else if (a === "appearance-mode")
      ORCAppearance.set({ mode: target.dataset.mode });
    else if (a === "close") closeModal();
    else if (a === "template") openLaunch(templates[target.dataset.template]);
    else if (a === "run") await navigate("workflow", target.dataset.id);
    else if (a === "stage") {
      state.node = target.dataset.node;
      workflow();
    } else if (a === "tab") {
      state.tab = target.dataset.tab;
      workflow();
    } else if (a === "copy-report")
      copy(
        state.report.outputs.find((o) => o.node_id === state.node)?.text || "",
      );
    else if (a === "export-report")
      download(state.report.workflow_id + ".md", state.report.markdown);
    else if (a === "finding") {
      const f = state.report.outputs
        .find((o) => o.node_id === state.node)
        ?.findings.find((f) => f.number === Number(target.dataset.number));
      modal(
        "Recommendation " + f.number,
        f.title,
        `<article class="markdown">${markdown(f.text)}</article>`,
      );
    } else if (a === "implement") {
      const f = state.report.outputs
        .find((o) => o.node_id === target.dataset.node)
        ?.findings.find((f) => f.number === Number(target.dataset.number));
      openLaunch({
        kind: "build",
        from_workflow: state.report.workflow_id,
        from_node: target.dataset.node,
        finding: f.number,
        title: f.title,
        text: "Keep this change scoped. Preserve existing work, add meaningful regression coverage, and run relevant checks. Do not commit, push, or deploy.",
      });
    } else if (a === "custom-from-run")
      openLaunch({ kind: "workflow", spec: state.report.spec });
    else if (a === "decision") {
      state.decision = target.dataset.id;
      decisions();
    } else if (a === "suggest-labels") {
      const record = state.decisions.find(r => r.id === state.decision);
      const workspace = state.workspace;
      const agent = $("#label-worker").value;
      target.disabled = true;
      try {
        await api("launch", { action: "suggest-labels", decision_id: record.id, agent }, workspace);
        // Keep edits while the job runs; a fresh draft only replaces untouched fields.
        toast("Drafting labels. Nothing is approved or added to training yet.");
        if (workspace === state.workspace) await refresh(true);
      } finally { target.disabled = false; }
    } else if (a === "use-label-draft") {
      const record = state.decisions.find(r => r.id === state.decision);
      const draft = labelDraft(record);
      draft.dirty = false;
      draft.seen = null;
      decisions();
    } else if (a === "probe") openProbe();
    else if (a === "learning") openLearning();
    else if (a === "orc") await loadOrc(target.dataset.command);
    else if (a === "job") await openJob(target.dataset.id);
    else if (a === "job-workflow") {
      closeModal();
      await navigate("workflow", target.dataset.id);
    } else if (a === "cancel") {
      target.disabled = true;
      await api("cancel", { id: target.dataset.id });
      toast(
        "Stop requested. Waiting for the coordinator to clean up its workers.",
      );
      await refresh();
    } else if (a === "resume") openResume();
    else if (a === "publish") openPublish();
    else if (a === "pr-refresh") {
      target.disabled = true;
      try { await api("pr-refresh", { run_id: state.report.workflow_id }); await refresh(true); }
      finally { target.disabled = false; }
    }
    else if (a === "prepared") {
      const file = await api(
        "file?path=" + encodeURIComponent(target.dataset.path),
      );
      openLaunch({ kind: "workflow", spec: JSON.parse(file.text) });
    } else if (a === "setup") {
      modal(
        "Set up Laya",
        "Install or repair the local runtime and download the English checkpoint.",
        `<p class="help-copy">Uses uv and the existing Fusion setup command. Progress is saved as a job. No coding workers start.</p><button class="button primary" data-action="setup-start">Install runtime →</button>`,
      );
    } else if (a === "setup-start") await launch({ action: "setup" });
    else if (a === "effective")
      modal(
        "Resolved Fusion settings",
        "Includes defaults and inherited configuration; credentials are masked.",
        `<pre class="console">${esc(pretty(state.config.effective))}</pre>`,
      );
    else if (a === "save-settings") {
      const isOrc = target.dataset.target === "orc";
      const value = JSON.parse(
        $(isOrc ? "#orc-config" : "#fusion-config").value,
      );
      target.disabled = true;
      try {
        state.config = await api("config", {
          target: target.dataset.target,
          value,
          revision: isOrc ? state.config.orc_revision : state.config.revision,
        });
        $("#execution-chip").textContent =
          state.config.execution_mode === "yolo"
            ? "YOLO · full access"
            : "Restricted runtime";
        settings();
        toast("Workspace settings saved. Future launches use these settings.");
      } finally {
        target.disabled = false;
      }
    }
  } catch (e) {
    if ($("#dialog").open) modalError(e.message);
    else error(e.message);
    target.disabled = false;
  }
});
document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    submit = $("[type=submit],button.button.primary", form);
  if (submit) submit.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(form));
    if (form.id === "launch-form") {
      const kind = values.kind;
      await launch({
        ...values,
        action: ["delegate", "workflow"].includes(kind) ? kind : "build",
        allow_write: !!values.allow_write,
        prepare: !!values.prepare,
        spec: kind === "workflow" ? JSON.parse(values.spec) : undefined,
        publish: ["build", "debug"].includes(kind) ? publishValues("launch") : undefined,
      });
    } else if (form.id === "publish-preview-form") {
      const p = await api("publish-preview", { run_id: form.dataset.run, publish: publishValues("pr") });
      if (p.url) { closeModal(); toast("This workflow already has a PR."); await refresh(true); }
      else showPublishPreview(p);
    } else if (form.id === "publish-form") {
      await launch({ action: "publish", run_id: form.dataset.run, snapshot_id: form.dataset.snapshot,
        title: values.title, body: values.body, draft: !!values.draft, accept_legacy_diff: !!values.accept_legacy_diff });
    } else if (form.id === "probe-form")
      await launch({ ...values, action: "probe", mode: "shadow" });
    else if (form.id === "learning-form") await launch(values);
    else if (form.id === "resume-form") {
      const route = values.worker.startsWith("route:")
        ? values.worker.slice(6)
        : undefined;
      await launch({
        action: "resume",
        run_id: state.report.workflow_id,
        allow_write: !!values.allow_write,
        node: values.node,
        agent: route ? "auto" : values.worker || undefined,
        route,
        max_attempts: Number(values.max_attempts),
      });
    } else if (form.id === "workspace-form") {
      const ws = await api("workspaces", { path: values.path });
      if (!state.workspaces.some((w) => w.id === ws.id))
        state.workspaces.push(ws);
      fillWorkspaces();
      closeModal();
      await chooseWorkspace(ws.id);
    } else if (form.id === "label-form") {
      const record = state.decisions.find(r => r.id === form.dataset.decision);
      const draft = labelDraft(record);
      const answers = Object.fromEntries(
        Object.entries(values).filter(([, value]) => value),
      );
      await api("label", {
        id: record.id,
        answers,
        evidence: $("#label-evidence").value,
        suggestion_id: draft.suggestion_id,
        approved: true,
      });
      draft.dirty = false;
      toast("Reviewed labels saved.");
      await refresh(true);
    }
  } catch (e) {
    if ($("#dialog").open) modalError(e.message);
    else error(e.message);
  } finally {
    if (submit) submit.disabled = false;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.closest("#label-form")) captureLabelEdits();
  if (event.target.id === "run-search") {
    state.search = event.target.value;
    const runs = state.overview.workflows.filter(
      (r) =>
        (state.filter === "all" || r.status === state.filter) &&
        `${r.task} ${r.id} ${r.agents}`
          .toLowerCase()
          .includes(state.search.toLowerCase()),
    );
    $("#run-results").innerHTML = runs.length
      ? `<div class="panel">${runRows(runs)}</div>`
      : empty("No matching workflows.", "Try another filter.");
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id.startsWith("settings-publish-")) {
    try {
      const value = JSON.parse($("#fusion-config").value);
      value.publish = publishValues("settings");
      $("#fusion-config").value = pretty(value);
    } catch { error("Correct the JSON before changing publishing defaults."); }
  }
  if (event.target.closest("#label-form")) captureLabelEdits();
  if (event.target.id === "label-worker") {
    const record = state.decisions.find(r => r.id === state.decision);
    labelDraft(record).worker = event.target.value;
  }
  if (event.target.id === "resume-node") {
    const node = state.report.live_nodes.find(
      (n) => n.id === event.target.value,
    );
    const attempts = $("#resume-attempts");
    attempts.min = node.attempts + 1;
    attempts.value = Math.max(Number(attempts.value), node.attempts + 1);
  }
  if (event.target.id === "run-filter") {
    state.filter = event.target.value;
    workflows();
  }
  if (
    ["setting-mode", "setting-worker", "setting-execution"].includes(
      event.target.id,
    )
  ) {
    try {
      const value = JSON.parse($("#fusion-config").value);
      if (event.target.id === "setting-mode")
        value.decisions = { ...value.decisions, mode: event.target.value };
      else if (event.target.id === "setting-execution")
        value.execution_mode = event.target.value;
      else value.sidekick = event.target.value;
      $("#fusion-config").value = pretty(value);
    } catch {
      error("Correct the JSON before changing the quick controls.");
    }
  }
});
$("#launch-top").addEventListener("click", () => openLaunch());
$("#workspace").addEventListener("change", (event) =>
  chooseWorkspace(event.target.value),
);
$("#add-workspace").addEventListener("click", () =>
  modal(
    "Add a workspace",
    "Connect an existing local repository. Its saved runs appear automatically.",
    `<form id="workspace-form"><div class="field"><label for="workspace-directory">Local directory</label><input id="workspace-directory" name="path" placeholder="~/code/visa/visa-mono" required autofocus></div><button class="button primary">Add workspace →</button></form>`,
  ),
);
$("#shortcuts").addEventListener("click", () =>
  modal(
    "Move at your speed",
    "Keyboard shortcuts",
    `<div class="shortcut-grid"><span>New run</span><kbd>N</kbd><span>Navigate sections</span><kbd>1–5</kbd><span>Search workflows</span><kbd>/</kbd><span>Close dialog</span><kbd>Esc</kbd><span>Show shortcuts</span><kbd>?</kbd></div>`,
  ),
);
$("#dialog").addEventListener("close", () => {
  if (!$("#dialog").open) state.modal = null;
});
document.addEventListener("keydown", (event) => {
  if (
    event.target.matches("input,textarea,select") ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey
  )
    return;
  if (event.key === "Enter" && event.target.matches("[role=button]"))
    event.target.click();
  if ($("#dialog").open) return;
  if (event.key.toLowerCase() === "n") {
    event.preventDefault();
    openLaunch();
  }
  if ("12345".includes(event.key) && event.key.length === 1)
    navigate(
      ["overview", "workflows", "decisions", "models", "settings"][
        Number(event.key) - 1
      ],
    );
  if (event.key === "?") $("#shortcuts").click();
  if (event.key === "/") {
    event.preventDefault();
    navigate("workflows").then(() => $("#run-search")?.focus());
  }
});
async function init() {
  try {
    const data = await api("bootstrap");
    state.workspaces = data.workspaces;
    fillWorkspaces();
    const remembered = localStorage.getItem("fusion-workspace");
    const workspace = data.workspaces.some((w) => w.id === remembered)
      ? remembered
      : data.default;
    const route = location.hash.slice(1).split("/");
    await chooseWorkspace(workspace);
    if (
      ["workflows", "workflow", "decisions", "models", "settings"].includes(
        route[0],
      )
    )
      await navigate(route[0], route[1] ? decodeURIComponent(route[1]) : null);
  } catch (e) {
    error(e.message);
    mount(empty("Connect to your control room.", esc(e.message)));
  }
  setInterval(async () => {
    if (state.polling || document.hidden || !state.workspace) return;
    state.polling = true;
    try {
      await refresh();
    } finally {
      state.polling = false;
    }
  }, 2000);
}
init();
