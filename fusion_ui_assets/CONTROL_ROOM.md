# Canonical local control room

The existing ORC UI owns local observation. TENET recipe receipts and saved state
observations attach to the same workflow; there is no second frontend service or
executor. Control room and Runs are the primary navigation. Existing Truffle,
Laya, models, and settings remain under Tools & learning.

## Evidence semantics

- The workspace and run lists come from the registered local workspace APIs.
- Active work takes focus; otherwise the latest saved outcome does. Worker
  messages and command receipts provide the activity, not fabricated progress.
- Workflow outcome, coordinator checks, and TENET executor receipts are separate
  facts. Worker-reported test prose does not count as coordinator acceptance.
- A saved TENET projection is an observation at its recorded time. It is not a
  fresh shared-state read. Missing receipts and liveness remain unknown.
- Artifact preview, download, and copy-path use declared opaque artifact IDs
  through the existing authenticated server. The browser cannot request an
  arbitrary filesystem path through that endpoint.

The homepage no longer repeats the previous guild hero, cost statistics, and
template gallery. New run and the existing functional tools remain available.

## Design references and shell consolidation

The functional base is ORC's workspace navigation, worker activity timeline,
Evidence tab, local appearance system, and supplied tusked logo. The restrained
project heading follows the forest/bone/ink and editorial-serif direction in
the platform's `src/lib/tenet/workspace-brand.ts` and `src/app/cockpit/page.tsx`
(inspected at platform revision `2713e16`). No hosted fonts or assets were added.

The legacy CLI dashboard (`dashboard/src/main.tsx`, its Sidebar and TheDaily
pages) is a separate shell. Stop opening it for this pilot; retain its
authenticated run projection APIs, receipt store, CLI, and MCP capabilities.
Its frontend entrypoints can be retired after any remaining functional views
have an explicit destination. The platform cockpit uses cloud snapshot data;
its backend capabilities should not be deleted as part of local shell cleanup.
The local canonical destination is ORC, not another standalone TENET dashboard.

## Validation

`vitest run test/logic.test.js --root .` checks acceptance/status separation and
active-run selection alongside existing UI decision logic. The asset sources
also pass `node --check` and `git diff --check`.

The pilot browser check uses the actual local server, Oasis workflow and TENET
receipts. It checks desktop/mobile overflow, saved worker activity, artifact
preview, exact downloaded content, and copied absolute path. It rejects external
requests and records any POST or page error. It does not launch workers or mock
responses. Diagnostic script, results, and screenshots are locally retained in
`.fusion/evaluations/control-room-ui/`; they are not product source.
