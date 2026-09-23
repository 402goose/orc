# orc Makefile
#
# Targets:
#   make refresh-quality   re-fetch Artificial Analysis quality data and
#                           copy it into data/quality.json. Run from the
#                           orc source directory. Requires the cmndcntr
#                           fetcher at $CMNDCNTR/scripts/.
#
# Add a new entry to slug_to_or_id_map() in orc for any new entries
# in unmappedSlugs you want surfaced; rerun `make build` after editing
# orc/pricing.jq to assemble a fresh binary.

CMNDCNTR ?= $(HOME)/code/hathbanger/cmndcntr
FETCHER := $(CMNDCNTR)/scripts/fetch-artificial-analysis-leaderboard.mjs
OUT     := data/quality.json

.PHONY: refresh-quality build test test-ui dogfood dogfood-paired dogfood-real

refresh-quality:
	@command -v node >/dev/null || { echo "node required" >&2; exit 1; }
	@test -f "$(FETCHER)" || { echo "fetcher not found: $(FETCHER)" >&2; exit 1; }
	node "$(FETCHER)" --quality-out "$(OUT)"
	@echo "wrote $(OUT) — check quality.json.unmappedSlugs for new slugs to add to SLUG_TO_OR_ID in orc"
	@jq -r '.unmappedSlugs[]' "$(OUT)" 2>/dev/null | sort -u | head -20

build:
	./build.sh

# Unexpected skips and expected failures fail the suite. The only allowed skip
# is the real macOS Codex sandbox probe when that runtime/profile is unavailable;
# run_python.py checks its exact test ID and reason, and reports it explicitly.
test:
	PYTHONDONTWRITEBYTECODE=1 python3 test/run_python.py

# Unit tests for the control room's pure logic (fusion_ui_assets/logic.js).
# No browser, no server: the Playwright checks in test/*_browser.cjs stay the
# end-to-end layer. Dev-only tooling installs outside the repo, like Playwright,
# so the shipped runtime stays npm-free.
UI_TOOLS ?= /tmp/orc-ui-tools
test-ui:
	@test -x $(UI_TOOLS)/node_modules/.bin/vitest || \
	  npm install --prefix $(UI_TOOLS) --no-audit --no-fund vitest@3.2.4
	@$(UI_TOOLS)/node_modules/.bin/vitest run test/logic.test.js --root .

dogfood:
	./test/fusion_dogfood.sh

dogfood-paired:
	FUSION_DOGFOOD_PAIRED=1 ./test/fusion_dogfood.sh

dogfood-real:
	FUSION_REAL=$${FUSION_REAL:-0} ./test/fusion_real_smoke.sh
