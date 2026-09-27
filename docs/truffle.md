# Truffle pig: issues to reviewed fixes

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Map the backlog](#map-the-backlog)
- [Recovery and field guide](#recovery-and-field-guide)

## Map the backlog

Open **Truffle pig → Map every open issue** to give Snout the whole backlog.
The woodland follows every page of the repository’s open issues. Epics, trackers,
linked-issue checklists, and native GitHub parents become **patches**; their linked
open issues are the **truffles** inside. Standalone issues appear in **the wilds**.
Parent links and tracker references remain inspectable; closed parent issues can
still provide a patch for open children. Linking is inferred from explicit issue
references, so inspect tracker scope before choosing overlapping fixes.

Mapping alone makes no coding-worker calls. **Grade all issues** inspects source
in batches of eight using your configured worker account. Results appear live;
stopping or a worker failure preserves completed batches. **Resume grading** only
assesses remaining issues. A changed checkout or changed pending issue requires a
fresh sync; previous snapshots remain in **Field journals**.

| Grade | Meaning |
| --- | --- |
| A · Ripe | Small, low-risk fix with checked source quotes and a concrete regression plan |
| B · Promising | Bounded small/medium work with evidence and a verification plan |
| C · Needs digging | More evidence or clearer acceptance criteria needed |
| D · Leave for now | Too broad, blocked, assigned (unless included), or already covered |
| P · Patch | Epic/tracker container; assess and queue the child issues |
| U · Unassessed | Still waiting for source investigation |

Every issue stays visible, including C/D grades and policy exclusions. Grades are
suitability judgments, not success probabilities. Click a truffle for the evidence
and proposed implementation. Select A/B candidates into the basket and use **Queue
selected fixes**. Implementation still needs its own tests and independent review.
Woodland/All issues/Field journals, patch, grade and search filters use shallow URL
routing and survive reload. The map uses the active theme and honors reduced motion.

```sh
# Inventory only: no coding-worker calls.
orc fusion --progress truffle survey --sync-only
# Inventory and grade the whole backlog in saved batches.
orc fusion --progress truffle survey --agent codex
# Resume only unassessed issues in a saved survey.
orc fusion --progress truffle survey --resume truffle-0123456789ab --agent codex
```

For a smaller targeted expedition, use **Overview → Send in the Truffle pig**,
or **Truffle pig → Quick hunt**.
Choose a target count (default 5), pool size (default 40), worker, and optional
GitHub search filter such as `label:bug sort:updated-desc`. The repository comes
from the selected Git remote, using your authenticated `gh` CLI. Scouting uses a
coding worker for investigation; it does not edit code or post to GitHub.

The shortlist shows why each issue is tractable, checked source quotations,
effort/risk, reproduction evidence, implementation steps, test commands and
acceptance criteria. It can return fewer issues than requested, including zero.
Source citations are checked against the files; feasibility is a worker judgment,
not a calibrated success probability. Skip reasons remain visible. Assigned issues
(unless included), issues linked to open PRs, and issues already queued by another
hunt are excluded. The PR check covers GitHub's closing-issue links, not every
informal mention in a PR; the scout also checks related source and history.

**Queue selected fixes** runs one issue at a time in its own worktree from the
chosen target branch. Manual PR mode is the default; automatic mode commits,
pushes and opens a PR only after implementation and independent review pass.
Open/updated status and linked PRs are checked again before each launch. A changed
issue needs a fresh hunt. Failure or quota pauses the queue: open the linked workflow,
resume that workflow, then queue the remaining selection again. Successful workflows
are reused, not repeated. Jobs survive closing the browser, and records live under
`.fusion/truffle/` alongside the usual workflow artifacts.

```sh
orc fusion --progress truffle hunt --count 5 --scan-limit 40 --search 'label:bug'
# Scout on a free OpenRouter route; with arms and rank_by_outcomes the model is
# picked inside the route by verified outcomes. --model pins one instead.
orc fusion --progress truffle hunt --count 3 --route orc-free
orc fusion --progress truffle hunt --count 3 --agent claude --model claude-haiku-4-5-20251001
orc fusion truffle show truffle-0123456789ab
# Use the saved hunt ID and only the issue numbers you chose from its shortlist:
orc fusion --progress truffle run truffle-0123456789ab --issues 123 456 --base staging --publish manual
```

Use `--publish auto` to publish accepted fixes automatically; PRs default to drafts.
Each stage has an attempt limit (`--max-attempts`, default 2). Queue execution pauses
at the first unresolved workflow instead of consuming more attempts across the pool.

## Recovery and field guide

Snout’s field guide lives beside Truffle pig hunts, with a theme-colored journey from
issue inspection to accepted fixes and PRs. Hunt status follows linked workflow recovery:
completed selected fixes clear a stale pause, a running recovery shows waiting, and a
recovered queue with unstarted issues offers **Continue queue**. Viewing a hunt never
starts additional work. Published fixes link directly to their PRs.

See [workflow acceptance, reports and publishing](workflows.md) for the
implementation/review stages behind each selected fix, and [routing](routing.md)
for native and OpenRouter lanes.
