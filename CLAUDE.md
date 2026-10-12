# bookgen — illustration pipeline

A deterministic prompt compiler and image-generation driver for illustrated
picture books. Each book is one `book.yaml`; the tool turns it into a prompt per
illustration slot and per diagram, sends those to Gemini's image model with the
book's reference images attached, and writes the renders into a timestamped run
directory. It also writes the proof PDF, the Doc-pasteable manuscript, the
print-preview page and the static site.

This repository is the **pipeline**, not the manuscripts. Each book's prose is
authored elsewhere, but every `book.yaml` carries a `manuscript:` mirror of it,
keyed by manuscript page, so a scene description can't quietly drift out of
agreement with the beat it illustrates. **When the prose changes, update the
mirror in the same commit.**

## The books

| Slug | Title | Read first |
|---|---|---|
| `sample-book` | The Sample Book | [books/sample-book/CLAUDE.md](books/sample-book/CLAUDE.md) |

`sample-book` is a template and the smoke-test fixture: two invented characters,
five illustration slots, one diagram, placeholder reference images. Copy the
directory to start a real book.

## Why the pipeline exists

An early book was generated ad hoc and **character consistency collapsed** — the
same character came out with different hair colours page to page. Everything
here exists to prevent that recurrence. The whole design follows from one rule:

> **Never hand-edit a prompt.** Edit `book.yaml` and recompile. A fix to the
> character sheet then applies to every page at once. Hand-edited prompts are how
> character drift starts.

If you are tempted to tweak a file in `out/prompts/`, you are doing it wrong —
those are build artifacts (gitignored) and get overwritten.

## Layout

```
bookgen.py       prompt compiler + Gemini "Nano Banana 2" driver + proof-PDF
                 writer (stdlib + the deps pinned in uv.lock — the PDF is
                 written by hand)
Makefile         wraps the multi-slot invocations; `make` lists the targets
README.md        operator's manual — workflow, model notes, rationale
pyproject.toml   pinned deps: pyyaml, fonttools (fonttools only for pdf --lulu)
uv.lock          the resolved lockfile; `uv sync` builds from it
Dockerfile       the tool in a slim image; the checkout is mounted at /work
.env.example     every environment variable, names and one-line notes only
reference-photos/        one subdir per book slug, holding that book's photos.
                         NOT in git — real photos are people's likenesses. See
                         "Where the photos live" in README.md. On a server they
                         are synced from BOOKGEN_REF_PHOTOS_S3 into a temp dir
                         and deleted afterwards. The one tracked exception is
                         reference-photos/sample-book/, whose two files are
                         synthetic placeholders, not photographs.
reference-photos-cropped/  what tools/crop-labeler.html cuts out of them.
                         Likewise not in git.
books/<slug>/
  book.yaml      SINGLE SOURCE OF TRUTH for that book — style, characters,
                 refs, manuscript mirror, front matter, slots, diagrams
  CLAUDE.md      that book's own invariants and state
  refs/          style and continuity anchors harvested from locked pages
  out/           gitignored EXCEPT picks.json: prompts/, runs/<timestamp>/,
                 refcache/, dryrun/, book-<run>.pdf, manuscript.html,
                 preview.html
```

`out/picks.json` is deliberately **not** ignored. It records which candidate was
chosen for each slot; regenerating it means paying for and re-reviewing every
candidate again. It is the one thing in `out/` that is a decision, not output.

## Running it

Dependencies are pinned in `pyproject.toml` / `uv.lock`; `uv run` builds and
uses that environment, so there is nothing to install by hand. **Every command
names its book** — there is no default and no inference from the working
directory, because a stale `cd` would eventually send a paid `gen` into the
wrong book's `out/`:

```bash
B=sample-book

uv run ./bookgen.py --book $B compile   # free — always do this first
uv run ./bookgen.py --book $B gen --dry-run  # free — everything gen would send, written to out/dryrun/
uv run ./bookgen.py --book $B gen --n 3 --concurrency 3 # every slot x 3 at 1K; ALWAYS concurrency >= 3 (default 3)
uv run ./bookgen.py --book $B lock --page 3 --cand 2 --run 20260726-141500
uv run ./bookgen.py --book $B lock --diagram D1 --cand 2 --run ...
uv run ./bookgen.py --book $B gen --page 3 --final --concurrency 3   # 4K print render
uv run ./bookgen.py --book $B pdf       # free — proof, prose | art
uv run ./bookgen.py --book $B html      # free — page to paste into Docs
uv run ./bookgen.py --book $B preview   # free — browser mock-up of the print spread
uv run ./bookgen.py --book $B cover     # free — one-page wraparound cover PDF
uv run ./bookgen.py --book $B publish   # dry run; --yes GOES PUBLIC
uv run ./bookgen.py --book $B publish --review-only --yes  # refresh the reviewer's link only
uv run ./bookgen.py publish-index --yes # the one cross-book command
```

`make` wraps all of it and carries `BOOK ?= sample-book`, so `make pdf` and
`make pdf BOOK=other-book` both work.

`GEMINI_API_KEY` must be set in the environment for `gen` (see `.env.example`
for every variable the pipeline reads). **`gen` costs real money** — compile and
read the prompts before spending, or run `gen --dry-run` and read what it would
have sent. Never run a full `gen` sweep without the user asking for it.

**`gen` runs `--concurrency 3` by default** — candidate renders go out in
parallel; the work is network-bound, not CPU-bound. Keep it at least 3 when you
invoke it.

## Design invariants — do not "fix" these

- **4 refs is the model ceiling.** Budgeted as 3 photo refs + 1 style ref, which
  lands exactly on it. `refs_for()` drops from the tail (style ref first) if a
  page goes over.
- **`pitfalls:` are NOT in the base prompt.** They're escalation only, appended
  by `--strict`. Preloading every correction produced a 500-word checklist and
  the model satisfied the checklist instead of building a coherent space — it
  rendered a car with two windshields and no floor. Start loose; escalate only
  on the page that actually fails.
- **Past ~400 words geometry degrades.** The compiler prints `<-- LONG`. If a
  long page fails, cut scene text rather than adding to it. `pitfalls:` is the
  pressure valve — detail belongs there, not in `scene:`.
- **A slot whose rolls fail on a different axis each time is a composition at
  the model's edge — rewrite the scene, don't buy more candidates.** After two
  failed batches (6 rolls) whose failures scatter across age, count, geometry
  and placement, a third batch only shuffles the failure. Rewrite `scene:`
  toward the geometry the model is reliably good at — faces toward the camera,
  figures in natural rows or groupings — and never stage a person as a sliver
  at the frame edge; partial people come back as phantom extra heads or
  wrong-aged children. Worked example: a three-quarter car-interior crop with a
  frame-edge sleeve failed 15 rolls, each differently (toddler sliver,
  30-year-old teenager, incoherent cabin, phantom fifth head), and the head-on
  windshield view passed 2 of 3 on the first re-roll. Multi-figure car cabins
  in perspective are beyond this model; the windshield view is not.
- **Model is `gemini-3.1-flash-image`.** Don't switch to `-lite`: it isn't
  optimized for multiple reference inputs, which is the entire mechanism here.
  `thinking_level: high` is why hard geometry has any chance.
- **Output is JPEG only.** Asking the interactions endpoint for `image/png` is a
  hard 400, not a silent downgrade.
- **Dependencies are locked and run through plain `uv run`.** `pyproject.toml` +
  `uv.lock`, not `uv run --with pyyaml`. A floating dependency means two
  machines can compile different prompts from the same `book.yaml`, which is
  the whole failure this pipeline exists to prevent.
- **The dry run is the parity check, so its output must stay deterministic.**
  `gen --dry-run` writes prompts plus `requests.json`; anything machine-specific
  in there — an absolute path, a timestamp, a hostname — makes two machines'
  output undiffable. That is why refs are recorded through `ref_label()`
  (relative to the book, else to the photo root) and never as absolute paths.
  Verify a change with `diff -r` of two dry runs, not by eyeballing.
- **Nothing machine-specific in a prompt or a path.** The bucket comes from
  `BOOKGEN_BUCKET`, the photo trees from `BOOKGEN_REF_PHOTOS_S3` /
  `BOOKGEN_REF_PHOTOS_DIR`. `book.yaml` keeps writing
  `../../reference-photos/<slug>/…` and `_photo_tree_path()` re-hangs it,
  because the spec is shared by every machine and the mount point is not.
- **`pdf --lulu` is macOS-only, on purpose.** The Lulu upload file must embed
  the system Helvetica, and that face is
  `/System/Library/Fonts/Helvetica.ttc`. It exits loudly rather than
  substituting a font. Everything else runs anywhere.
- **Real photos are never in git and never at rest on a server.** They live in
  `reference-photos/` + `reference-photos-cropped/`, both gitignored; on a
  server they are synced from a private prefix into a temp dir for one command
  and deleted. Never upload them to the publishing bucket, which is
  world-readable by policy. The only tracked files under `reference-photos/` are
  the sample book's synthetic placeholders.
- Refs are auto-converted to JPEG and downscaled to 1024px longest edge via
  `sips` on macOS or ImageMagick elsewhere, cached in `out/refcache/`. HEIC in,
  JPEG out — no hand conversion.
- **`pdf` is two PDF pages per MANUSCRIPT page, always: a prose page facing
  its art page.** Which side is which is per-book: the default is prose
  verso / art recto; `spread: art-left` mirrors it — art verso, prose recto.
  Slots are gathered by `book_page:`, and a diagram-only page faces its
  diagrams. The title page is partly structural — it is what puts the first
  story page on a verso (with `/PageLayout /TwoPageRight`), and a dedication is
  padded to keep that parity.
  Prose that will not fit is shrunk to 10pt and then clipped with a warning
  rather than reflowed onto a second page, since spilling would push the art off
  the facing page and desynchronise every later spread. Cut prose, or split the
  manuscript page, instead.
- **Manuscript prose is laid out with `prose_blocks()`, not `sq()`.** Paragraphs
  split on blank lines, `- ` lines become hanging-indent bullets, `N. ` lines
  become hanging-indent numbered items (a real `<ol>` in the html export, with
  the literal number in the PDF), `**emphasis**` becomes bold. `pdf` and `html`
  both go through the same function on purpose: the proof and the Google Doc
  reading the same page differently is the one thing neither is allowed to do.
  A folded `>` YAML block has no interior blank line and so still lays out as
  the single paragraph it is; use `|` when the page has real structure.
- **`front_matter.cover:`** names a slot painted above the title. That slot wants
  `book_page: 0` so the story loop never also places it on a recto.
- **`publish` is the only outward-facing command, so it dry-runs by default.**
  `--yes` is the commitment. The bucket is world-readable, so anything uploaded
  is on the open internet and may be cached or indexed even if deleted later.
  Credentials come from the `aws` CLI's environment — `AWS_*` or a profile in
  `~/.aws` on a laptop, an instance role on a server. No static keys on a server.
  `publish.slug` in `book.yaml` names the top-level prefix and is spelled out,
  never derived from the title: the title is prose and may change, the prefix is
  a URL that has already been shared. Never pass `--acl public-read` — the
  bucket is `BucketOwnerEnforced` and ACLs are disabled, so it is a hard error.
  `--review-only` rides the same command and the same `--yes` gate, uploading
  `preview.html` and the runs it cycles through but not the manuscript.
- **Every upload sets `Cache-Control`, and it must.** The objects are rewritten
  in place and a reviewer holds the URL. With none at all the browser applies
  heuristic freshness (~10% of the object's age, so days for a book up a few
  weeks) and a reviewer reports on the spread from before the re-roll. HTML keys
  go up `no-cache`; run directories go up
  `public,max-age=31536000,immutable`, since a run name is a timestamp and a
  render never changes once written.
- **`publish` uploads every run the manuscript references, not just the newest.**
  Picks can span runs. `resolve_run()` alone would silently break every image
  that came from an older one.
- **`publish-index` is the single cross-book command** and so the only one that
  takes no `--book`. That exception is checked explicitly in `main()`; the rule
  everywhere else — never guess the book, because a wrong guess can send a paid
  `gen` into the wrong `out/` — is unchanged. It indexes every book with a
  `publish.slug`, ordered by `sequence:`.
- **`diagram_styles:`** lets a diagram opt out of the schematic default with
  `style:`. Used where a diagram should look like the thing rather than like a
  chart — a mineral specimen, for instance.
- **`trim:` is the printed page's physical size in inches as `"WxH"`;
  `aspect_ratio:` is the art's shape.** `"11x8.5"` is US Letter Landscape
  (792x612 pts), a common Lulu trim, per the Book Creation Guide p.12 chart.
  Without `trim:`, `page_size()` keeps the 8-in-wide page whose height follows
  `aspect_ratio:`, so this is opt-in and doesn't change anything for a book that
  doesn't need it. Setting `trim:` also mats art pages inside `PDF_MARGIN` —
  art never touches a page edge, keeping the file in Lulu's exact-trim,
  no-bleed-needed category. Art placement (`place_contain`/`draw_art_page`)
  always sizes from the actual rendered file, never from either value, so
  `trim:` only ever changes the blank page around the art — it cannot stretch
  or crop a render. `pdf --lulu` writes the upload file with the system
  Helvetica embedded (fontTools on that path only — ghostscript's pdfwrite
  refuses to embed base-14 fonts), so the JPEGs stay bit-identical.

## Getting a book into Google Docs

`bookgen.py --book <slug> html` writes the whole book to `out/manuscript.html`
for pasting into a Doc — open it in a browser, select all, copy, paste. The
browser is what carries the images across; a Markdown paste gets the headings
and emphasis but drops every image. Details in `README.md`. There is no importer
yet, so edits made in the Doc come back into `manuscript:` by hand.

The default output is the **print manuscript**: no tool chrome (no "Page N"
headings, no slot/candidate/run captions), one beat per Doc page via inline
`page-break-before:always`, and US Letter landscape geometry — 1in margins,
Arial 18pt at a 24pt pitch, art centred on its own page, diagrams beside their
prose when they fit. Image width/height attributes are what Docs sizes pasted
images from (96px = 1in), so every `<img>` is sized explicitly. `--review`
restores the labelled output for mid-review Docs where a comment must trace back
to a render file. One layout bug to not reintroduce: a table even a point wider
than the content box makes Chrome scale the ENTIRE printed document to fit —
diagram rows subtract their cells' padding from the image widths.

## Previewing the print layout

`bookgen.py --book <slug> preview` writes `out/preview.html` — the same spread
`pdf` prints (side order per-book via `spread:`), in a browser, free, with no
PDF reader required. Gathering matches `pdf` and `html` exactly. Every image
cycles through all of that slot's rendered candidates on click, across every
run — a re-roll's alternatives are one click away, right in the spread. Cycling
also records the pick: the candidate left on screen persists in the browser's
localStorage, and "Copy picks" exports the not-yet-locked picks as a
`picks.json` fragment — paste it into a Claude session to merge into
`out/picks.json`, since the static page can't write the file itself.

`preview.html` also carries the review checklist (`review_checklist` in
`book.yaml`) at the top of the page, plus the review notes: a field under each
spread for the page's text and one per image, identified the same way the rest
of the tool identifies them (a manuscript page number for text, a slot/diagram id
for art). Notes persist in the browser's localStorage. They live in preview,
not the manuscript, so the Doc-paste flow stays clean.

The notes and the picks leave as ONE payload, not two — "p03 needs a wider
shot" and "p03 → run/cand 2" are the same review, and both are addressed by
the identifier above. "Email feedback" opens a compose window with it;
"Copy feedback" puts it on the clipboard for a mail client that won't take a
long `mailto`. The block is `Book:` / `Title:` / `--- notes ---` (the
`id: feedback` list) / `--- picks ---` (the same fragment "Copy picks"
exports), so a Claude session reads the whole review in one paste and the
picks half still merges into `picks.json` verbatim. The mailto body rides in
the URL and is therefore TRUNCATED, not refused, when long — past 1800
characters the button declines and points at "Copy feedback" rather than
opening a half-empty compose window. `publish.feedback_email` fills in the
recipient and is deliberately unset by default: `preview.html` is served from
a world-readable bucket, and an address in it is an address for a scraper.
Unset still works — the reviewer picks the recipient from their own contacts.

`--review-only` is what makes that link a thing you can send: it uploads
`preview.html` and the runs it cycles through and nothing else, so a re-roll
can be pushed without republishing the manuscript and a draft book can have a
reviewer URL at all. The URL is stable per book, so it is sent once.

## Reviewing candidates

Reading the preview needs a human: check hair colour, height, clothing colour,
apparent age. Ignore braid side, which hand, prop details.
