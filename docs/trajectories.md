# Trajectory export

[Start here](../README.md) · [CLI reference](cli.md) · [Learning](learning.md)

- [Command](#command)
- [Layout](#layout)
- [Run rows](#run-rows)
- [Step rows](#step-rows)
- [Provenance](#provenance)
- [What is exported](#what-is-exported)
- [Why the label is the review outcome](#why-the-label-is-the-review-outcome)

A run leaves its harness's raw `stdout.log` (Claude Code stream JSON, Codex
exec JSON, OpenCode JSON, Grok or agy output) beside `task.json`,
`result.json` and `trace.json`. Its verdict is in the decision log
(`.fusion/decisions/events.jsonl`), where the latest measured verdict wins.
`fusion trajectories export` joins them into a dataset a training or analysis
job can read without knowing any harness's format.

## Command

```sh
fusion trajectories export --out DIR --for personal|employer \
  [--since DAYS] [--role ROLE] [--include-prerelease] [--dry-run]
fusion trajectories parquet DIR
```

`DIR` must not exist or must be empty. `--dry-run` prints the counts per
filter and exclusion reason, and writes nothing. The export reads the control
workspace's runs, including runs recorded in workflow worktrees.

`fusion trajectories parquet DIR` writes a `.parquet` file beside each JSONL file
of an export. It needs `pyarrow`. Without it, the command exits 1 with a message
and writes nothing. The runtime itself needs only the Python standard library.
In parquet, structured fields whose shape varies by harness (`arguments`,
`task`, `outcome`, `usage`, `provenance`) are stored as JSON text.

## Layout

The layout follows the LeRobot dataset convention. Every file is JSONL or
JSON, and readable only by its owner:

```
DIR/meta/info.json          schema, counts, field lists, filters, exclusions
DIR/meta/runs.jsonl         one row per exported run, ordered by run_id
DIR/steps/chunk-000.jsonl   one row per step, ordered by (run_id, index)
DIR/steps/chunk-001.jsonl   ...
```

A chunk holds about 10,000 steps. Each run's steps stay in one chunk, so a new
chunk starts at the first run boundary after 10,000.

`info.json`:

| Field | Meaning |
|-------|---------|
| `schema`, `schema_version` | `fusion.trajectories.v1`, `1` |
| `created_at` | UTC time of the export |
| `orc_sha` | the exporting ORC commit, or null when unknown |
| `totals` | `runs`, `steps`, `chunks`, `splits` (`train`/`heldout` counts), `accepted`, `rejected` |
| `fields` | the run and step field names |
| `filters` | `for`, `since_days`, `role`, `include_prerelease` |
| `excluded` | runs left out, counted by reason (below) |
| `redactions` | how many secrets the redactor masked |

## Run rows

| Field | Meaning |
|-------|---------|
| `run_id`, `issue`, `role`, `write`, `agent`, `model`, `reasoning_effort`, `repo` | as recorded for the run; `issue` from its verdict, else its delegation |
| `lane_key` | the lane, with any account path cut to its last component |
| `host` | the endpoint host the lane named, or null for the harness's own provider |
| `task` | `text` (the task as sent), `success_criteria`, `constraints` |
| `outcome` | the effective verdict: `accepted`, `stage`, `rejection_class`, `reporter`, `reason` |
| `diff`, `diff_truncated` | the writer's final diff, capped at 200,000 characters |
| `usage`, `cost_usd`, `cost_estimated` | the run's token usage and cost; `cost_estimated` when a declared price filled it in |
| `split` | `train` or `heldout`, by the sha256 of the issue |
| `provenance` | below |
| `steps` | how many step rows the run has |

The diff is recorded when a writer finishes, as `diff.patch` in its run
directory. It is taken against the start commit a workflow gave the worker,
else against HEAD at dispatch. Untracked new files are included and Fusion's
own `.fusion` directory is not. Readers have no diff. Runs from before the diff
was recorded export `diff: null`.

The split puts about 10% of issues in `heldout`. It depends only on the issue
(owner and name compared case-insensitively), so every run of one issue lands
on the same side, on every export. A run without an issue is split by its
workflow group.

## Step rows

```
{run_id, index, kind, role, text, tool, arguments, result, is_error, time_ms, raw_type}
```

| `kind` | Meaning |
|--------|---------|
| `message` | text from the model (`role` `assistant`), its reasoning (`reasoning`), the user (`user`) or an error (`error`, `is_error` true) |
| `tool_call` | the model called `tool` with `arguments` |
| `tool_result` | what `tool` returned, in `result`; `is_error` when it failed or was denied |
| `other` | an event the normaliser does not know: `raw_type` names it and `text` holds it as JSON |

An unknown event is never dropped. A harness's new event type shows up as
`other` until a normaliser learns it. Events that only frame others add no
step: a session's init, a step boundary, an in-progress tool update, usage, and
the final result event when the stream already carried the answer.
`time_ms` is the event's own time when the harness gives one (OpenCode), else
null. Text, results and large arguments are capped at 20,000 characters each.

The normalisers live beside the harness output parsers in `fusion_core.py`
(`claude_steps`, `codex_steps`, `opencode_steps`, `grok_steps`, `agy_steps`).

## Provenance

Every dispatch writes `provenance` into `result.json`:

| Field | Meaning |
|-------|---------|
| `schema_version` | `1` |
| `orc_sha` | the installed ORC commit, from `orc.sha` (written by `install.sh`) or the checkout ORC runs from; null when unknown |
| `repo_head` | the workspace HEAD at dispatch |
| `prompt_sha256` | the sha256 of the exact brief sent to the worker |
| `lane_config_sha256` | the sha256 of the resolved lane settings with secret-named keys, environment values and paths removed |
| `suite_sha256` | present when the task pins its check inputs: a digest of the pinned digests and the check commands |

The export copies it. A run recorded before provenance existed exports with
`derived: true` and what its `task.json` still determines: `prompt_sha256`
(the brief this ORC would build from the task) and `suite_sha256`. The other
fields are null.

## What is exported

Exports fail closed. Each run is counted under the first reason that
excludes it. A run dir with no result is never exported and is counted by
its [state](learning.md#run-states-and-tombstones): `running`, `abandoned`
or `unknown`. A tombstoned run (`status: "abandoned"`) counts as `abandoned`.

1. **Filters.** `filter_since` and `filter_role` count runs that `--since` or
   `--role` left out.
2. **Settled only.** A run needs an effective accepted or rejected verdict
   (`fusion_policy.effective_outcomes`). Excluded:
   - `no_outcome`: no verdict;
   - `withdrawn`: the verdict was withdrawn and none replaced it;
   - `unmeasured`: only an unmeasured outcome;
   - `check_inputs_changed`: the run changed its own check inputs;
   - `eval_unmeasured`: rejected with class `eval_unmeasured`;
   - `quota`, `refusal`: the run hit a quota or the model declined;
   - `harness_failure`: a permission denial, auth failure, missing
     executable, coordinator error or missing review verdict, or a rejection
     classed `land_conflict` or `other`.
3. **Repo exclusions.** `export.exclude_repos` applies as it does to
   `fusion decisions export` (`excluded_repo`). A run whose repo is unknown is
   excluded (`unknown_repo`). This export has no include flags.
4. **Data walls.** A new `export.walls` block in `.fusion.json`:

   ```json
   {"export": {"walls": {"personal": ["owner/*"],
                         "employer": ["org/*"],
                         "prerelease": ["owner/name"]}}}
   ```

   Patterns are shell-style (`*`, `?`, `[...]`) and match `owner/name`
   case-insensitively. `--for personal` exports only repos on the personal
   wall, and `--for employer` only repos on the employer wall
   (`wall_other_side`). A repo on both walls (`wall_both`) or neither
   (`wall_neither`) is excluded, so with no walls configured nothing is
   exported. A `prerelease` repo is excluded (`prerelease`) unless
   `--include-prerelease`.
5. **Secrets.** Every exported string passes a redactor: the step text,
   arguments and results, the task, the verdict's reason and the diff. It
   masks `sk-…`, GitHub (`ghp_…` and its siblings) and AWS (`AKIA…`) key
   shapes, PEM blocks, `Authorization:` header values, values under
   secret-named fields, and the value of every environment variable whose name
   contains `KEY`, `TOKEN` or `SECRET` at export time (values of 8 characters
   or more). Each mask becomes `[REDACTED]`, and the count is `redactions` in
   `info.json`.

## Why the label is the review outcome

A run's label is its effective verdict, not whether its tests passed. A suite
can be passed by a fake: a hard-coded answer, a weakened assertion, a check
input edited to agree (see [grader integrity](learning.md#grader-integrity)).
Review is what caught those runs. A rejection at stage `review` carries its
`rejection_class` (`review_changes`, `missed_gap`, …), so a training job can
learn what review rejected and why. Runs whose verdict says nothing about the
work (`eval_unmeasured`, quota, refusal, harness failures) carry no label and
stay out.
