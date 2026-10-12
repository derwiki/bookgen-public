# bookgen — illustration pipeline
#
# Wraps bookgen.py so the multi-slot invocations are one command instead of a
# hand-typed shell loop. Run `make` for the target list.
#
# Dependencies are pinned in pyproject.toml and resolved into uv.lock, so
# everything goes through plain `uv run` and picks up that exact environment.
# It used to be `uv run --with pyyaml`, which floats whatever pyyaml is newest
# on the day — fine on one laptop, a parity bug the moment a second machine has
# to produce the same prompts.

BOOKGEN := ./bookgen.py
UV      := uv run $(OFFLINE)

# Which book. bookgen requires --book on every invocation with no fallback, so
# this default is the only thing making `make pdf` short. Override per command:
#   make pdf BOOK=some-other-book
BOOK  ?= sample-book

# Slots to act on. Override: make gen PAGES="4 5 6"
PAGES ?= 1 2 3
# Candidates per slot at draft size. Ignored by `final` (always 1).
N     ?= 3
# Diagrams to act on. Empty means all of them. Override: make diagrams D="D1 D2"
D     ?=
# Flags appended verbatim to the bookgen invocation, for the options no make
# variable models: make gen PAGES=4 N=4 EXTRA=--strict,
# make pdf EXTRA="--no-diagrams --out /tmp/proof.pdf"
EXTRA ?=

# One shared run directory for every slot in a single `make gen`, so the whole
# run stays in one place for review. Assigned with := inside ifndef, NOT `?=`:
# `?=` makes a recursively-expanded variable, so $(shell date) would re-run on
# every reference and gen would write to a different directory than lock
# records.
ifndef RUN
RUN := $(shell date +%Y%m%d-%H%M%S)
endif

# Empty unless RUN was passed on the command line. `lock` acts on an existing
# run, where the auto-generated timestamp above would be meaningless.
RUN_GIVEN := $(filter command line,$(origin RUN))

.DEFAULT_GOAL := help
.PHONY: help compile compile-pages gen gen-all diagrams final dry-run pdf html cover preview lock clean-empty-runs publish publish-index review

help:
	@echo 'bookgen — illustration pipeline'
	@echo
	@echo 'Acting on BOOK=$(BOOK).  Books available:'
	@for d in books/*/; do [ -f "$$d/book.yaml" ] && echo "    $$(basename $$d)"; done
	@echo
	@echo '  make compile                     compile every slot (free, no API calls)'
	@echo '  make compile-pages PAGES="1 2 3" compile just those slots (free)'
	@echo '  make pdf                         proof book, prose | art (free)'
	@echo '  make pdf C=2 RUN=20260728-185746 ... from candidate 2 of that run'
	@echo '  make html                        print-clean page to paste into Google Docs (free)'
	@echo '  make cover                       one-page wraparound cover PDF for Lulu (free)'
	@echo '  make html C=2 RUN=20260728-185746 ... from candidate 2 of that run'
	@echo '  make html EXTRA=--review         ... labelled review copy (Page N + captions)'
	@echo '  make preview                     print-spread mock-up, clickable (free)'
	@echo
	@echo '  make gen PAGES="1 2 3" N=3       COSTS MONEY — draft renders'
	@echo '  make gen-all N=3                 COSTS MONEY — every slot'
	@echo '  make diagrams                    COSTS MONEY — every diagram'
	@echo '  make diagrams D="D1 D2" RUN=...  COSTS MONEY — just those, into that run'
	@echo '  make final P=9                   COSTS MONEY — one 4K print render'
	@echo
	@echo '  make dry-run                     free — everything gen would send,'
	@echo '                                   written to out/dryrun/ instead'
	@echo '  make dry-run EXTRA="--page 9"    ... just one slot'
	@echo
	@echo '  make publish                     dry run: what would go public'
	@echo '  make publish YES=1               PUBLIC — upload this book to S3'
	@echo '  make publish-index YES=1         PUBLIC — the index across all books'
	@echo
	@echo '  make review                      dry run: refresh the reviewer link'
	@echo '  make review YES=1                PUBLIC — refresh preview.html only,'
	@echo '                                   leaving the book itself alone'
	@echo
	@echo '  make lock P=9 C=2 RUN=20260726-141500   record the chosen slot candidate'
	@echo '  make lock D=D15 C=1 RUN=20260726-141500 ... or a diagram candidate'
	@echo '  make clean-empty-runs            delete run dirs that produced no JPEGs'
	@echo
	@echo 'Vars: BOOK=$(BOOK)  PAGES=$(PAGES)  N=$(N)  RUN=$(RUN)'
	@echo 'Every target acts on BOOK. Switch with: make pdf BOOK=other-book'
	@echo 'Add OFFLINE=--offline if pypi is unreachable but uv has the locked deps cached.'
	@echo 'EXTRA appends flags verbatim: make gen PAGES=4 N=4 EXTRA=--strict'
	@echo
	@echo 'Read books/$(BOOK)/out/prompts/ before spending. Never sweep unasked.'

# ---- free ------------------------------------------------------------------

compile:
	$(UV) $(BOOKGEN) --book $(BOOK) compile $(EXTRA)

compile-pages:
	@for p in $(PAGES); do $(UV) $(BOOKGEN) --book $(BOOK) compile --page $$p $(EXTRA); done

# Locked picks win; unlocked slots take candidate 1 unless C says otherwise, so
# this is useful mid-review, before anything has been locked. Nothing rendered
# yet is drawn as a labelled placeholder rather than dropped, so the pagination
# is the same before and after a gen.
pdf:
	$(UV) $(BOOKGEN) --book $(BOOK) pdf $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(EXTRA)

# Same gathering and the same pick rules as `pdf`, emitted as one browser page.
# Open it, select all, copy, paste into a Google Doc: the browser carries the
# images across, which a Markdown paste cannot do.
html:
	$(UV) $(BOOKGEN) --book $(BOOK) html $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(EXTRA)

# Same gathering and pick rules as `pdf` again, as a browser mock-up of the
# print spread: every image clickable, cycling through every rendered
# candidate for that slot across all runs. Display only — no picks.json edit.
cover:
	$(UV) $(BOOKGEN) --book $(BOOK) cover $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(EXTRA)

preview:
	$(UV) $(BOOKGEN) --book $(BOOK) preview $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(EXTRA)

# ---- goes public -------------------------------------------------------------

# Free in money, not in consequence: the bucket is world-readable, so anything
# uploaded is on the open internet and may be cached or indexed even if deleted
# later. Both targets are a dry run until YES=1, which is the whole point.
publish:
	$(UV) $(BOOKGEN) --book $(BOOK) publish $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(if $(YES),--yes,) $(EXTRA)

# The reviewer's link. Refreshes preview.html and the runs it cycles through
# and leaves the book itself alone — so a draft can be handed out, and a
# re-roll can be pushed, without republishing the manuscript. Same YES=1 gate
# as publish, because the renders land in the same world-readable bucket.
review:
	$(UV) $(BOOKGEN) --book $(BOOK) publish --review-only $(if $(RUN_GIVEN),--run $(RUN),) $(if $(C),--cand $(C),) $(if $(YES),--yes,) $(EXTRA)

# The one cross-book target, so it takes no BOOK.
publish-index:
	$(UV) $(BOOKGEN) publish-index $(if $(YES),--yes,)

# ---- costs money -----------------------------------------------------------

# bookgen's --page takes a single slot, so loop. Sharing --run keeps all slots in
# one directory; note manifest.json is rewritten per invocation and so ends up
# describing only the last slot. lock works off the .jpg files rather than the
# manifest, so nothing downstream breaks.
gen:
	@echo "==> $(words $(PAGES)) slot(s) x $(N) candidate(s) into run $(RUN)"
	@for p in $(PAGES); do $(UV) $(BOOKGEN) --book $(BOOK) gen --page $$p --n $(N) --run $(RUN) $(EXTRA); done

gen-all:
	@echo "==> every slot of $(BOOK) x $(N) candidate(s) into run $(RUN)"
	$(UV) $(BOOKGEN) --book $(BOOK) gen --n $(N) --run $(RUN) $(EXTRA)

# A re-roll is usually one or two diagrams; the full sweep is every diagram x N.
# Pass RUN of an existing run to drop the new candidates in beside the slots
# already rendered there — gen skips files that exist, so nothing is re-paid for.
diagrams:
	$(UV) $(BOOKGEN) --book $(BOOK) gen $(if $(D),--diagram $(D),--diagrams) --n $(N) --run $(RUN) $(EXTRA)

final:
ifndef P
	$(error final needs a slot: make final P=9)
endif
	$(UV) $(BOOKGEN) --book $(BOOK) gen --page $(P) --final $(EXTRA)

# Everything `gen` would send, written to books/$(BOOK)/out/dryrun/ instead of
# sent: every prompt as text, plus requests.json with the model, aspect, size,
# thinking level and ref list for each candidate. Free, and touches no API.
#
# One invocation over every slot on purpose — a per-slot loop would rewrite
# requests.json each time and leave it describing only the last slot (the same
# trap `gen` documents above). Narrow it with EXTRA:
#   make dry-run EXTRA="--page 9"
#   make dry-run EXTRA="--diagrams --n 1"
dry-run:
	$(UV) $(BOOKGEN) --book $(BOOK) gen --dry-run $(EXTRA)

# ---- bookkeeping -----------------------------------------------------------

# A slot (P=9) or a single diagram (D=D15). picks.json has always been keyed by
# render name, so a diagram locks the same way a slot does.
lock:
ifeq ($(and $(or $(P),$(D)),$(C),$(RUN_GIVEN)),)
	$(error lock needs a target, a candidate and a run: make lock P=9 C=2 RUN=20260726-141500, or make lock D=D15 C=1 RUN=...)
endif
	$(UV) $(BOOKGEN) --book $(BOOK) lock $(if $(P),--page $(P),--diagram $(D)) --cand $(C) --run $(RUN)

# A run that failed before any API call still leaves prompts and an empty
# directory behind, and the newest-run default that `pdf`, `preview` and
# friends use would pick that empty one. This clears those out.
clean-empty-runs:
	@for d in books/$(BOOK)/out/runs/*/; do \
	  [ -d "$$d" ] || continue; \
	  if ! ls "$$d"*.jpg >/dev/null 2>&1; then echo "rm $$d"; rm -rf "$$d"; fi; \
	done
