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
      node.activity_entries = node.messages.map((text, i) => ({id: `update-${i}`, kind: 'message', text}));
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
  // Scroll metrics round to CSS pixels; smooth scrolling may settle within 1px.
  const atBottom = () =>
    feed.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop <= 2);
  await expect(feed).toContainText("Worker update 60");
  await page.locator('.activity-diagnostics > summary').click();
  await expect.poll(atBottom).toBe(true);
  await feed.evaluate((el) => (el.scrollTop = 130));
  await events.evaluate((el) => (el.scrollTop = 240));
  revision++;
  await expect(feed).toContainText("Worker update 61");
  expect(await feed.evaluate((el) => el.scrollTop)).toBe(130);
  expect(await events.evaluate((el) => el.scrollTop)).toBe(240);
  await expect(page.locator('.activity-diagnostics')).toHaveAttribute('open', '');

  // Follow only after returning to the bottom; new content scrolls smoothly.
  await page.getByRole("button", {name: "Jump to latest activity"}).click();
  await expect.poll(atBottom).toBe(true);
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
  // In-flight smooth scrolling and CSS-pixel rounding may settle one pixel away.
  expect(Math.abs(await feed.evaluate((el) => el.scrollTop) - 180)).toBeLessThanOrEqual(2);

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

async function checkActivityLayout(page) {
  await page.route('**/api/workflow?*', async route => {
    const response = await route.fetch(), report = await response.json();
    for (const node of report.live_nodes) {
      node.status = 'running';
      node.activity_entries = [
        {id:'note-1', kind:'message', text:'I found the retry boundary in `src/payments.ts`.\n\nThe request can be replayed after a timeout. I’m checking the existing tests before changing it.'},
        ...Array.from({length:6}, (_, i) => ({id:`cmd-${i}`, kind:'command', status:'finished', command:i === 5 ? 'pnpm test -- payment-retries' : `rg -n "retry" src/payments.ts`, exit_code:0, output:'PASS payment-retries.test.ts\n12 tests passed\n<script>window.PWNED=true</script>'})),
        {id:'note-2', kind:'message', text:'**The reproduction is confirmed.** All 12 existing tests pass, but none covers the timeout between authorization and settlement.\n\nNext: add a regression test for that gap.'},
        {id:'active-1', kind:'command', status:'running', command:'pnpm test -- payment-timeout', exit_code:null},
      ];
    }
    await route.fulfill({response, json:report});
  });
  await page.evaluate(() => refresh(true));
  await expect(page.locator('.worker-update')).toHaveCount(2);
  await expect(page.locator('.command-group')).toHaveCount(2);
  await expect(page.locator('.command-group').first()).toContainText('6 commands');
  await expect(page.locator('.command-group').last()).toContainText('Running');
  await expect(page.locator('.activity-diagnostics')).not.toHaveAttribute('open', '');
  await expect(page.getByRole('heading', {name:'Workflow events', exact:true})).toHaveCount(0);
  await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {}))));
  await page.screenshot({path:'/tmp/orc-activity-light.png',fullPage:true});
  await page.locator('.command-group > summary').first().click();
  await page.locator('.command-detail > summary').first().click();
  await expect(page.locator('.command-output').first()).toBeVisible();
  await page.evaluate(() => refresh(true));
  await expect(page.locator('.command-output').first()).toBeVisible();
  expect(await page.evaluate(() => window.PWNED)).toBeUndefined();
  await page.locator('.command-group > summary').first().click();
  await page.evaluate(() => ORCAppearance.set({mode:'dark'}));
  await page.screenshot({path:'/tmp/orc-activity-dark.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:'/tmp/orc-activity-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1050});
  await page.evaluate(() => ORCAppearance.set({mode:'light'}));
  await page.unroute('**/api/workflow?*');
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
      page.getByRole("heading", { name: "Good work. Forged here." }),
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
    await checkActivityLayout(page);
    await checkActivityScrolling(page);

    await page.route('**/api/workflow?*', async route => {
      const response = await route.fetch();
      const report = await response.json();
      report.live_nodes.find(n => n.id === 'explore').coordinator_failure = {
        phase: 'snapshot_before_review', message: 'Review did not start: Fusion could not snapshot the repository', detail: 'Git snapshot fixture failure',
      };
      report.live_nodes.find(n => n.id === 'explore').result.command_evidence = ['command exited 1: cat corrected-path'];
      await route.fulfill({response, json: report});
    });
    await page.getByRole('button', {name: 'Deliverable', exact: true}).click();
    await page.evaluate(() => refresh(true));
    await expect(page.getByRole('heading', {name: 'Review did not start: Fusion could not snapshot the repository'})).toBeVisible();
    await expect(page.locator('.detail-grid')).toContainText('No reviewer was launched');
    await page.getByRole('button', {name: 'Evidence', exact: true}).click();
    await expect(page.getByRole('heading', {name: 'Command observations'})).toBeVisible();
    await expect(page.locator('.console').filter({hasText: 'command exited 1: cat corrected-path'})).toBeVisible();
    await page.unroute('**/api/workflow?*');

    await page.locator("[data-view=decisions]").click();
    await expect(
      page.getByRole("heading", { name: "Laya lab." }),
    ).toBeVisible();
    await page.getByRole("tab", {name: /^Review/}).click();
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
    await expect(page.locator('.label-saved-status')).toContainText('No further approval is required.');
    await expect(page.locator('.review-form')).not.toContainText('Waiting for human approval');
    await expect(page.getByRole('button', {name:'Save label changes',exact:true})).toBeDisabled();
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
      page.getByRole("heading", { name: "Good work. Forged here." }),
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
