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

.PHONY: refresh-quality build test dogfood dogfood-real

refresh-quality:
	@command -v node >/dev/null || { echo "node required" >&2; exit 1; }
	@test -f "$(FETCHER)" || { echo "fetcher not found: $(FETCHER)" >&2; exit 1; }
	node "$(FETCHER)" --quality-out "$(OUT)"
	@echo "wrote $(OUT) — check quality.json.unmappedSlugs for new slugs to add to SLUG_TO_OR_ID in orc"
	@jq -r '.unmappedSlugs[]' "$(OUT)" 2>/dev/null | sort -u | head -20

build:
	./build.sh

# A suite that ends in "OK (skipped=N)" still exits 0, so coverage can fall away
# without anyone noticing. Require a bare OK; a deliberate skip gets stated here.
test:
	@output=$$(PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test -p '*_test.py' 2>&1); \
	status=$$?; \
	printf '%s\n' "$$output"; \
	[ $$status -eq 0 ] || exit $$status; \
	printf '%s\n' "$$output" | grep -qx 'OK' || { \
	  echo "" >&2; \
	  echo "make test: the suite passed but did not end in a bare OK, so a test skipped or" >&2; \
	  echo "expectedly failed and is hiding coverage. Fix it, or record the skip in the Makefile." >&2; \
	  exit 1; }

dogfood:
	./test/fusion_dogfood.sh

dogfood-real:
	FUSION_REAL=$${FUSION_REAL:-0} ./test/fusion_real_smoke.sh
