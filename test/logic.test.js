/* Unit tests for the control room's pure decision logic.
 *
 * These run in milliseconds with no browser, no server and no worker
 * subprocesses. The Playwright checks in test/*_browser.cjs stay as the thin
 * end-to-end layer; this is where the branches get covered.
 */
import { describe, it, expect } from "vitest";
import "../fusion_ui_assets/logic.js";

const L = globalThis.ORCLogic;

describe("esc", () => {
  it("neutralizes every character that can break out of an HTML context", () => {
    expect(L.esc(`<script>alert("x")</script>`)).toBe(
      "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;",
    );
    expect(L.esc("a & b")).toBe("a &amp; b");
    expect(L.esc("it's")).toBe("it&#39;s");
  });

  it("escapes the ampersand so an escape cannot be smuggled through", () => {
    // If & were not escaped first, "&lt;" in user input would render as "<".
    expect(L.esc("&lt;img onerror=x&gt;")).toBe("&amp;lt;img onerror=x&amp;gt;");
  });

  it("renders null and undefined as empty, not as the words", () => {
    expect(L.esc(null)).toBe("");
    expect(L.esc(undefined)).toBe("");
  });

  it("stringifies non-strings rather than throwing", () => {
    expect(L.esc(0)).toBe("0");
    expect(L.esc(false)).toBe("false");
  });
});

describe("safeGithubURL", () => {
  it("passes real GitHub URLs through", () => {
    expect(L.safeGithubURL("https://github.com/o/r/pull/1")).toBe("https://github.com/o/r/pull/1");
  });

  it("refuses look-alike hosts", () => {
    for (const url of [
      "https://github.com.evil.test/x",
      "https://notgithub.com/x",
      "https://evil.test/github.com",
      "https://raw.github.com/x",
    ]) {
      expect(L.safeGithubURL(url)).toBe("#");
    }
  });

  it("refuses non-https schemes, including javascript:", () => {
    expect(L.safeGithubURL("javascript:alert(1)")).toBe("#");
    expect(L.safeGithubURL("http://github.com/o/r")).toBe("#");
    expect(L.safeGithubURL("data:text/html,<script>alert(1)</script>")).toBe("#");
  });

  it("returns an inert href for unparseable or missing input", () => {
    expect(L.safeGithubURL("not a url")).toBe("#");
    expect(L.safeGithubURL(undefined)).toBe("#");
  });
});

describe("withoutDerived", () => {
  it("drops graph, which is regenerated on every validate", () => {
    expect(L.withoutDerived({ nodes: [1], graph: { edges: [] } })).toEqual({ nodes: [1] });
  });

  it("leaves a spec without graph untouched", () => {
    expect(L.withoutDerived({ nodes: [] })).toEqual({ nodes: [] });
  });

  it("does not mutate its argument", () => {
    const spec = { nodes: [], graph: {} };
    L.withoutDerived(spec);
    expect(spec.graph).toBeDefined();
  });

  it("passes non-objects through unchanged", () => {
    expect(L.withoutDerived(null)).toBe(null);
    expect(L.withoutDerived("x")).toBe("x");
  });
});

describe("canApproveLabels", () => {
  const draft = (answers, evidence) => ({ answers, evidence });

  it("requires at least one answer and non-blank evidence", () => {
    expect(L.canApproveLabels(draft({ q: "yes" }, "saw it in run 3"))).toBe(true);
  });

  it("refuses when every answer is blank", () => {
    expect(L.canApproveLabels(draft({ q: "", r: "" }, "evidence"))).toBe(false);
  });

  it("refuses whitespace-only evidence", () => {
    // Approved labels enter training exports; blank evidence poisons the set.
    expect(L.canApproveLabels(draft({ q: "yes" }, "   \n\t "))).toBe(false);
  });

  it("refuses missing fields instead of throwing", () => {
    expect(L.canApproveLabels({})).toBe(false);
    expect(L.canApproveLabels(null)).toBe(false);
  });
});

describe("commandPreview", () => {
  it("peels off shell -c wrappers", () => {
    expect(L.commandPreview({ command: `bash -c 'make test'` })).toBe("make test");
    expect(L.commandPreview({ command: `/bin/sh -c "pytest -q"` })).toBe("pytest -q");
    expect(L.commandPreview({ command: `zsh -lc 'git status'` })).toBe("git status");
  });

  it("keeps only the first line", () => {
    expect(L.commandPreview({ command: "make test\nmake lint" })).toBe("make test");
  });

  it("leaves a plain command alone", () => {
    expect(L.commandPreview({ command: "git status" })).toBe("git status");
  });

  it("falls back to name, then to a placeholder", () => {
    expect(L.commandPreview({ name: "probe" })).toBe("probe");
    expect(L.commandPreview({})).toBe("Command");
    expect(L.commandPreview(null)).toBe("Command");
  });
});

describe("age", () => {
  const now = 1_000_000_000_000;

  it("renders seconds, minutes, then hours", () => {
    expect(L.age(now - 5_000, now)).toBe("5s");
    expect(L.age(now - 90_000, now)).toBe("1m 30s");
    expect(L.age(now - 7_260_000, now)).toBe("2h 1m");
  });

  it("switches unit exactly at the boundary", () => {
    expect(L.age(now - 59_000, now)).toBe("59s");
    expect(L.age(now - 60_000, now)).toBe("1m 0s");
    expect(L.age(now - 3_599_000, now)).toBe("59m 59s");
    expect(L.age(now - 3_600_000, now)).toBe("1h 0m");
  });

  it("clamps a future timestamp to zero rather than showing negative time", () => {
    expect(L.age(now + 60_000, now)).toBe("0s");
  });

  it("shows a dash for a missing timestamp", () => {
    expect(L.age(0, now)).toBe("—");
    expect(L.age(undefined, now)).toBe("—");
  });
});

describe("matchesWorkflowFilter", () => {
  const run = { task: "Add CSV export", id: "wf-123", agents: "claude,codex", status: "running" };

  it("matches everything when the filter is all and search is empty", () => {
    expect(L.matchesWorkflowFilter(run, "all", "")).toBe(true);
  });

  it("filters on exact status", () => {
    expect(L.matchesWorkflowFilter(run, "running", "")).toBe(true);
    expect(L.matchesWorkflowFilter(run, "failed", "")).toBe(false);
  });

  it("searches task, id and agents, case-insensitively", () => {
    expect(L.matchesWorkflowFilter(run, "all", "csv")).toBe(true);
    expect(L.matchesWorkflowFilter(run, "all", "WF-123")).toBe(true);
    expect(L.matchesWorkflowFilter(run, "all", "codex")).toBe(true);
    expect(L.matchesWorkflowFilter(run, "all", "nope")).toBe(false);
  });

  it("requires status AND search to match", () => {
    expect(L.matchesWorkflowFilter(run, "failed", "csv")).toBe(false);
  });
});

describe("tokenFromURL", () => {
  const origin = "http://127.0.0.1:8765";

  it("extracts the capability from a matching origin", () => {
    expect(L.tokenFromURL(`${origin}/#token=abc123`, origin)).toBe("abc123");
  });

  it("refuses a URL from another origin", () => {
    // The token is a credential; handing it to another origin would leak it.
    expect(L.tokenFromURL("http://evil.test/#token=abc123", origin)).toBe("");
    expect(L.tokenFromURL("http://127.0.0.1:9999/#token=abc123", origin)).toBe("");
    expect(L.tokenFromURL("https://127.0.0.1:8765/#token=abc123", origin)).toBe("");
  });

  it("returns empty when there is no token in the fragment", () => {
    expect(L.tokenFromURL(`${origin}/#overview`, origin)).toBe("");
    expect(L.tokenFromURL(`${origin}/`, origin)).toBe("");
  });

  it("returns empty for unparseable input rather than throwing", () => {
    expect(L.tokenFromURL("paste me", origin)).toBe("");
    expect(L.tokenFromURL("", origin)).toBe("");
  });
});

describe("storedCredential", () => {
  it("reads the value", () => {
    expect(L.storedCredential({ getItem: () => "tok" })).toBe("tok");
  });

  it("returns empty when storage is unavailable, as in a private window", () => {
    expect(
      L.storedCredential({
        getItem() {
          throw new Error("SecurityError");
        },
      }),
    ).toBe("");
  });

  it("normalizes a missing key to empty string", () => {
    expect(L.storedCredential({ getItem: () => null })).toBe("");
  });
});

describe("active", () => {
  it("covers exactly the statuses the server may still move", () => {
    for (const s of ["running", "queued", "stopping"]) expect(L.active(s)).toBe(true);
    for (const s of ["success", "failed", "blocked", "cache_hit", undefined]) {
      expect(L.active(s)).toBe(false);
    }
  });
});
