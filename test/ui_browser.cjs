// npm install --prefix /tmp/orc-ui-tools @playwright/test
// NODE_PATH=/tmp/orc-ui-tools/node_modules node test/ui_browser.cjs
const { chromium, expect } = require("@playwright/test");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const root = path.resolve(__dirname, "..");

async function checkActivityScrolling(page) {
  let revision = 0;
  await page.route("**/api/workflow?*", async (route) => {
    const response = await route.fetch();
    const report = await response.json();
    for (const node of report.live_nodes) {
      node.messages = Array.from(
        { length: 60 + revision },
        (_, i) => `Worker update ${i + 1} · revision ${revision}`,
      );
    }
    report.events = Array.from({ length: 40 }, (_, i) => ({
      ts: Date.now() - i * 1000,
      type: `Workflow event ${i}`,
      node_id: "plan",
    }));
    await route.fulfill({ response, json: report });
  });
  const feed = page.locator(".activity-feed").first();
  const events = page.locator(".activity-feed").nth(1);
  const atBottom = () =>
    feed.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop < 1);
  await expect(feed).toContainText("Worker update 60");
  await expect.poll(atBottom).toBe(true);
  await feed.evaluate((el) => (el.scrollTop = 130));
  await events.evaluate((el) => (el.scrollTop = 240));
  revision++;
  await expect(feed).toContainText("Worker update 61");
  expect(await feed.evaluate((el) => el.scrollTop)).toBe(130);
  expect(await events.evaluate((el) => el.scrollTop)).toBe(240);

  // Follow only after returning to the bottom; new content scrolls smoothly.
  await feed.evaluate((el) => (el.scrollTop = el.scrollHeight));
  await page.evaluate(() => {
    window.scrollBehaviors = [];
    const original = Element.prototype.scrollTo;
    Element.prototype.scrollTo = function (options, ...args) {
      if (this.matches(".activity-feed"))
        window.scrollBehaviors.push(options?.behavior);
      return original.call(this, options, ...args);
    };
  });
  revision++;
  await expect(feed).toContainText("Worker update 62");
  await expect.poll(atBottom).toBe(true);
  expect(await page.evaluate(() => window.scrollBehaviors)).toContain("smooth");
  expect(await events.evaluate((el) => el.scrollTop)).toBe(240);

  await feed.evaluate((el) => (el.scrollTop = 180));
  revision++;
  await expect(feed).toContainText("Worker update 63");
  expect(await feed.evaluate((el) => el.scrollTop)).toBe(180);

  // A different stage starts at its own latest output, not the old position.
  await page.locator('.stage[data-node="explore"]').click();
  await expect.poll(atBottom).toBe(true);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.evaluate(() => (window.scrollBehaviors = []));
  revision++;
  await expect(feed).toContainText("Worker update 64");
  await expect.poll(atBottom).toBe(true);
  expect(await page.evaluate(() => window.scrollBehaviors)).not.toContain(
    "smooth",
  );
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.unroute("**/api/workflow?*");
}

(async () => {
  const fixture = spawn(
    "python3",
    ["-u", path.join(__dirname, "ui_browser_fixture.py")],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        FUSION_TELEMETRY: "0",
      },
    },
  );
  let stderr = "";
  fixture.stderr.on("data", (chunk) => (stderr += chunk));
  let browser, page;
  try {
    const info = await new Promise((resolve, reject) => {
      let output = "";
      const timeout = setTimeout(
        () => reject(new Error("Fixture startup timed out: " + stderr)),
        20000,
      );
      fixture.stdout.on("data", (chunk) => {
        output += chunk;
        const line = output
          .split("\n")
          .find((line) => line.startsWith('{"url"'));
        if (line) {
          clearTimeout(timeout);
          resolve(JSON.parse(line));
        }
      });
      fixture.on("exit", (code) => {
        clearTimeout(timeout);
        reject(new Error("Fixture exited " + code + ": " + stderr));
      });
    });
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const external = [];
    await page.route("**/*", (route) => {
      if (!route.request().url().startsWith(new URL(info.url).origin)) {
        external.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(info.url);
    await expect(
      page.getByRole("heading", { name: "Everything in motion." }),
    ).toBeVisible();
    await expect(page.locator(".run-row")).toHaveCount(1);
    await page.screenshot({ path: "/tmp/orc-ui-overview.png", fullPage: true });
    await page.locator(".run-row").first().click();
    await expect(page.locator(".markdown table")).toBeVisible();
    await expect(page.locator(".markdown pre code")).toContainText(
      "python -m unittest",
    );
    await expect(
      page.locator(".markdown script,.markdown img,.markdown [data-action]"),
    ).toHaveCount(0);
    expect(await page.evaluate(() => window.PWNED)).toBeUndefined();
    await expect(
      page.getByRole("button", { name: "Implement this →" }),
    ).toHaveCount(2);
    await page.screenshot({ path: "/tmp/orc-ui-workflow.png", fullPage: true });
    await page
      .getByRole("button", { name: "Implement this →" })
      .first()
      .click();
    await expect(page.locator("#launch-kind")).toHaveValue("build");
    await expect(page.locator("#allow-write")).not.toBeChecked();
    await expect(page.locator("[name=from_workflow]")).toHaveValue(
      info.workflow_id,
    );
    await page.getByRole("button", { name: "Close dialog" }).click();
    await page.getByRole("button", { name: "Activity", exact: true }).click();
    await expect(page.locator(".activity-feed").first()).toContainText(
      "Inspecting fixture source files",
    );
    await checkActivityScrolling(page);

    await page.locator("[data-view=decisions]").click();
    await expect(
      page.getByRole("heading", { name: "Meet the decision layer." }),
    ).toBeVisible();
    await page.selectOption("#label-worker", "codex");
    await page.getByRole("button", { name: "Suggest labels", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Suggested assessment" })).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#label-form select")).toHaveValue("false");
    await expect(page.locator("#label-evidence")).toHaveValue(/input reports no work/);
    const eventsPath = path.join(info.workspace, ".fusion/decisions/events.jsonl");
    const decisionEvents = () => fs.readFileSync(eventsPath, "utf8").trim().split("\n").map(JSON.parse);
    expect(decisionEvents().filter(e => e.event === "label")).toHaveLength(0);
    await page.screenshot({ path: "/tmp/orc-ui-labels.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.getByText("Evidence used · 1 sources", { exact: true }).click();
    await expect(page.locator(".label-source")).toContainText('"summary":"Did nothing"');
    await page.fill(
      "#label-evidence",
      "The fixture states that no work was done, so it cannot satisfy the requested change.",
    );
    // A regeneration must not replace the human's in-progress assessment.
    await page.getByRole("button", { name: "Regenerate labels", exact: true }).click();
    await expect(page.getByRole("button", { name: "Use latest draft", exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#label-evidence")).toHaveValue(/^The fixture states/);
    await page.locator("[data-view=overview]").click();
    await page.locator("[data-view=decisions]").click();
    await expect(page.locator("#label-evidence")).toHaveValue(/^The fixture states/);
    await page.getByRole("button", { name: "Approve labels", exact: true }).click();
    await expect(page.locator("#label-form")).toContainText(
      "1 review(s) saved",
    );
    const label = decisionEvents().filter(e => e.event === "label").at(-1);
    expect(label.source).toBe("human_approved_suggestion");
    expect(label.verified).toBe(true);
    expect(label.evidence).toMatch(/^The fixture states/);
    await expect(page.locator("#label-evidence")).toHaveValue(/^The fixture states/);
    await page.reload();
    await expect(page.locator("#label-evidence")).toHaveValue(/^The fixture states/);
    await page.getByRole("button", { name: "Learning tools" }).click();
    await page.getByRole("button", { name: "Start learning job →" }).click();
    await expect(page.locator("#job-content .status")).toHaveText("success", {
      timeout: 15000,
    });
    await expect(page.locator("#job-content")).toContainText('"examples": 1');
    await page.getByRole("button", { name: "Close dialog" }).click();

    // An all-abstention draft remains usable: approve a supported question only.
    await page.locator('[data-action="decision"][data-id="abstained-review"]').click();
    await expect(page.locator(".launch-note")).toContainText("No labels were suggested");
    const approve = page.getByRole("button", { name: "Approve labels", exact: true });
    await expect(approve).toBeDisabled();
    await page.fill("#label-evidence", "E1 explicitly requests independent review.");
    await expect(approve).toBeDisabled();
    await page.selectOption('#label-form [name="needs_review"]', "true");
    await expect(approve).toBeEnabled();
    await expect(page.locator('#label-form [name="specialty"]')).toHaveValue("");
    await page.fill("#label-evidence", "   ");
    await expect(approve).toBeDisabled();
    await page.fill("#label-evidence", "E1 explicitly requests independent review.");
    await expect(approve).toBeEnabled();
    expect(decisionEvents().filter(e => e.event === "label" && e.id === "abstained-review")).toHaveLength(0);
    await page.screenshot({ path: "/tmp/orc-ui-partial-label.png", fullPage: true });
    await approve.click();
    await expect(page.locator("#label-form")).toContainText("1 review(s) saved");
    const partial = decisionEvents().filter(e => e.event === "label" && e.id === "abstained-review").at(-1);
    expect(partial.answers).toEqual({ needs_review: "true" });
    expect(partial.answers_edited).toBe(true);
    expect(partial.source).toBe("human_approved_suggestion");

    await page.locator("[data-view=settings]").click();
    await page.selectOption("#setting-worker", "codex");
    await page.selectOption("#setting-execution", "yolo");
    const original = JSON.parse(
      await page.locator("#fusion-config").inputValue(),
    );
    original.custom_ui_test = { preserved: true };
    await page.fill("#fusion-config", JSON.stringify(original, null, 2));
    await page.getByRole("button", { name: "Save Fusion settings" }).click();
    await expect(page.locator("#toast")).toContainText(
      "Workspace settings saved",
    );
    expect(
      JSON.parse(fs.readFileSync(path.join(info.workspace, ".fusion.json")))
        .custom_ui_test.preserved,
    ).toBe(true);
    await expect(page.locator("#execution-chip")).toHaveText(
      "YOLO · full access",
    );
    expect(
      JSON.parse(fs.readFileSync(path.join(info.workspace, ".fusion.json")))
        .execution_mode,
    ).toBe("yolo");

    await page.getByRole("button", { name: /New run/ }).click();
    await page.selectOption("#launch-kind", "discovery");
    await page.fill(
      "#launch-text",
      "Inspect fixture modules and report evidence.",
    );
    await page.check("#prepare-only");
    await page.getByRole("button", { name: "Prepare workflow →" }).click();
    await expect(page.locator("#job-content .status")).toHaveText("success", {
      timeout: 15000,
    });
    await page
      .getByRole("button", { name: "Inspect prepared workflow" })
      .click();
    await expect(page.locator("#launch-kind")).toHaveValue("workflow");
    await page.getByRole("button", { name: "Start run →" }).click();
    await expect(
      page.getByRole("button", { name: "Open workflow →" }),
    ).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#job-content .status")).toHaveText("success", {
      timeout: 15000,
    });
    await page.getByRole("button", { name: "Open workflow →" }).click();
    await expect(page.locator(".detail-heading .status")).toHaveText("success");

    await page.getByRole("button", { name: /New run/ }).click();
    await page.selectOption("#launch-kind", "delegate");
    await page.selectOption("[name=agent]", "codex");
    await page.fill("#launch-text", "wait fixture");
    await page.getByRole("button", { name: "Start run →" }).click();
    await expect(page.locator(".job-console")).toContainText("worker started", {
      timeout: 10000,
    });
    await page.getByRole("button", { name: "Stop job", exact: true }).click();
    await expect(page.locator("#job-content .status")).toHaveText("cancelled", {
      timeout: 15000,
    });
    await page.getByRole("button", { name: "Close dialog" }).click();

    await page.locator("[data-view=overview]").click();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(
      page.getByRole("heading", { name: "Everything in motion." }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.getByRole("button", { name: /New run/ }).click();
    await expect(page.locator("#launch-text")).toBeVisible();
    await page.screenshot({ path: "/tmp/orc-ui-mobile.png", fullPage: true });
    expect(external).toEqual([]);
    expect(errors).toEqual([]);
    console.log(
      "Browser checks passed: existing runs, Markdown/XSS, findings, activity scroll preservation and smooth following, labels/export, settings, prepare/execute, cancellation, mobile.",
    );
  } catch (error) {
    if (page) {
      console.error(
        "UI state:",
        await page
          .evaluate(() => ({
            view: state.view,
            modal: state.modal,
            polling: state.polling,
            hidden: document.hidden,
            error: document.querySelector("#error-banner").textContent,
            jobs: state.overview?.jobs.map((j) => ({
              id: j.id,
              status: j.status,
            })),
          }))
          .catch(() => null),
      );
      await page
        .screenshot({ path: "/tmp/orc-ui-failure.png", fullPage: true })
        .catch(() => {});
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    fixture.kill("SIGINT");
    await new Promise((resolve) => {
      const timeout = setTimeout(resolve, 12000);
      fixture.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
    if (stderr) process.stderr.write(stderr);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
