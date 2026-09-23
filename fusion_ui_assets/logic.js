/* ORC control room: decisions, not rendering.
 *
 * Everything here is a pure function of its arguments. No DOM, no fetch, no
 * storage, no module state, no clock except where a timestamp is passed in.
 * That is the whole point: this is the half of the control room where a wrong
 * answer is a bug rather than a cosmetic glitch, so it is the half worth
 * testing without a browser. app.js keeps the rendering and the wiring.
 *
 * Loaded as a plain script before app.js (no bundler, no npm at runtime) and
 * imported directly by the vitest suite.
 */
"use strict";
(() => {
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  /** Escape text for interpolation into an HTML string. */
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

  /** Only ever link to real GitHub over TLS; anything else becomes inert. */
  const safeGithubURL = (url) => {
    try {
      const u = new URL(url);
      return u.protocol === "https:" && u.hostname === "github.com" ? u.href : "#";
    } catch {
      return "#";
    }
  };

  // `graph` is derived from `nodes` and regenerated on every validate, so
  // showing it in an editor invites edits that are silently discarded.
  const withoutDerived = (spec) => {
    if (!spec || typeof spec !== "object") return spec;
    const { graph, ...rest } = spec;
    return rest;
  };

  /** A run the server may still be moving. */
  const active = (status) => ["running", "queued", "stopping"].includes(status);

  /**
   * A draft may be approved only with at least one answer AND written
   * evidence. Approved labels enter training exports, so a false positive
   * here quietly poisons the dataset.
   */
  const canApproveLabels = (draft) =>
    !!draft &&
    Object.values(draft.answers || {}).some((value) => value !== "") &&
    !!(draft.evidence || "").trim();

  /** First line of a command, with any `sh -c '…'` wrapper peeled off. */
  const commandPreview = (entry) =>
    ((entry && (entry.command || entry.name)) || "Command")
      .replace(/^\S*\b(?:ba|z|fi)?sh\s+-\w*c\s+['"]?/, "")
      .split("\n")[0]
      .replace(/['"]$/, "");

  /** Coarse elapsed label. `now` is injected so this stays pure. */
  const age = (ms, now = Date.now()) => {
    if (!ms) return "—";
    const n = Math.max(0, Math.floor((now - ms) / 1000));
    if (n < 60) return `${n}s`;
    if (n < 3600) return `${Math.floor(n / 60)}m ${n % 60}s`;
    return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
  };

  /** Workflow list predicate: status filter AND case-insensitive substring. */
  const matchesWorkflowFilter = (run, filter = "all", search = "") =>
    (filter === "all" || run.status === filter) &&
    `${run.task} ${run.id} ${run.agents}`.toLowerCase().includes(String(search).toLowerCase());

  /**
   * Pull the access capability out of a pasted control-room URL.
   * Returns "" unless it parses, targets this exact origin, and carries a
   * token — a credential must never be sent to another origin.
   */
  const tokenFromURL = (value, origin) => {
    let url;
    try {
      url = new URL(value);
    } catch {
      return "";
    }
    if (url.origin !== origin) return "";
    return new URLSearchParams(url.hash.slice(1)).get("token") || "";
  };

  /** Read a stored credential from a storage-like object that may throw. */
  const storedCredential = (storage, key = "fusion-token") => {
    try {
      return storage.getItem(key) || "";
    } catch {
      return "";
    }
  };

  const ORCLogic = {
    esc,
    safeGithubURL,
    withoutDerived,
    active,
    canApproveLabels,
    commandPreview,
    age,
    matchesWorkflowFilter,
    tokenFromURL,
    storedCredential,
  };

  globalThis.ORCLogic = ORCLogic;
  if (typeof module !== "undefined" && module.exports) module.exports = ORCLogic;
})();
