# bookgen

Deterministic illustration pipeline for picture books.

**The rule: never hand-edit a prompt.** Edit `book.yaml`, recompile. A fix to the
character sheet then applies to every page at once. Hand-edited prompts are how
character consistency drifts apart in the first place.

## How it works

`bookgen.py` reads one `books/<slug>/book.yaml`, compiles a prompt per
illustration slot and per diagram, sends them to Gemini's image model with the
book's reference images attached, and writes the results into a timestamped run
directory. No prompt is ever authored by hand — the spec is the single source of
truth, and `compile` prints exactly what `gen` would send.

```
book.yaml ──compile──▶ out/prompts/p01.txt ──gen──▶ out/runs/<ts>/p01_c1.jpg
                                                          │
                        picks.json ──────────────────────▶ │
                                                          ▼
                                          pdf / html / preview / publish
```

The pipeline is stdlib plus the dependencies pinned in `uv.lock`. The proof PDF
is written by hand rather than with a PDF library, so there is no reportlab in
the tree.

## Books

One tool, many books. Each lives in `books/<slug>/` with its own `book.yaml`,
`refs/` and `out/`; `reference-photos/` at the repo root holds one subdir per
book slug (a photo shared by more than one book is copied into each).

```
books/sample-book/          The Sample Book — the template and the smoke-test fixture
```

`sample-book` is a small, complete, runnable spec: two invented characters, five
illustration slots, one diagram, and placeholder reference images. Copy the
directory to start a real book.

**Every command names its book.** There is no default and no inferring one from
the working directory — that convenience would eventually send a `gen`, which
costs real money, into the wrong book's `out/`. `make` carries
`BOOK ?= sample-book` so `make pdf` and `make pdf BOOK=other-book` both work.
The Makefile is the entry point: it wraps `bookgen.py`, supplies the locked `uv`
environment, and keeps one run directory shared across a sweep. Run `make` for
the target list. `bookgen.py` runs on its own too (`uv run ./bookgen.py --book …`)
for anything the Makefile does not cover.

## Setup

    uv sync                            # installs the pinned deps from uv.lock
    cp .env.example .env               # then put GEMINI_API_KEY in it

Dependencies are pinned in `pyproject.toml` and resolved into `uv.lock`, and
every invocation goes through `uv run`, so a laptop, the container and a server
all resolve the same environment — the same prompts, byte for byte. There is no
`pip install` step and nothing to configure system-wide.

`.env` is gitignored and is never baked into the Docker image; `.env.example`
carries the variable names and a line about each, and no values. On a laptop
`export`ing the two or three variables is just as good — nothing reads `.env`
itself, it is there for `--env-file` and for remembering what is needed.

## Dry run

    make dry-run                       # every slot, free, nothing sent
    make dry-run EXTRA="--page 3"      # ... just one slot
    make dry-run EXTRA="--diagrams"

Writes everything `gen` would send to `books/<slug>/out/dryrun/`: each prompt
as plain text, plus a `requests.json` with the model, aspect ratio, size,
thinking level and ref list for every candidate. Nothing is sent and nothing is
uploaded, and no image conversion runs — so it needs neither an API key nor
ImageMagick.

It exists to be **diffed**. The output is deterministic, so running it on two
machines and comparing the two directories is comparing what would have been
sent:

    diff -r books/sample-book/out/dryrun /tmp/dryrun-elsewhere

That is how the container and a server are checked against the laptop. Prompt
files are identical only if the spec, the code and the dependency versions all
match. `publish` is separately dry by default — it prints the upload set and
uploads nothing until `--yes`.

## Running it in a container

    docker build -t bookgen .
    docker run --rm --env-file .env -v "$PWD:/work" \
      --user "$(id -u):$(id -g)" bookgen --book sample-book gen --dry-run

The image holds the tool and its locked dependencies, not the books — the
checkout is mounted at `/work`. Nothing secret is baked in: `.env` is
dockerignored, the image sets no API key, and credentials arrive at run time.
`--user` keeps files written into the mounted checkout from coming out
root-owned.

`pdf --lulu` is the one thing that will not run in the container, or on any
Linux host: the Lulu upload file embeds the macOS system Helvetica the printed
editions were built with, and that face lives at `/System/Library/Fonts`. It
exits with that message rather than quietly substituting a different font into
an upload file. Everything else — `compile`, `gen`, `pdf`, `html`, `cover`,
`preview`, `publish`, `publish-index` — is Linux-clean.

## References

Two kinds, and the distinction matters.

**Photos** — `char_refs:` in `book.yaml`, pointing at
`../../reference-photos/<slug>/`. Books that use the same person each keep their
own copy of that photo rather than pointing at a shared file. These attach
automatically from each page's `cast`, in cast order, up to the model's 4-ref
ceiling. They are **likeness only**: `ref_instruction` tells the model the text's
age beats the photo's, so a book can write a character older than the photo
shows without the model splitting the difference and drawing something in
between.

A character can also be left out of `char_refs` on purpose. Handing the model a
photo at the wrong age next to a written age makes it average the two; a
character with no photo is carried by the text plus the `pair_rules`
resemblance clause, and a close-up slot can list a sibling in `extra_refs` so
there is still a face to work from.

Refs are converted to JPEG and downscaled to 1024px longest edge before sending
(`sips` on macOS, ImageMagick elsewhere, cached under `out/refcache/`). HEIC in,
JPEG out — no need to convert phone photos by hand.

### Where the photos live

**Real photos are not in git.** They are people's likenesses; they are inputs,
and `reference-photos/` and `reference-photos-cropped/` are both gitignored.
`book.yaml` still names them by the same relative path it always did — the
pipeline re-hangs that path off wherever the trees actually are.

- **Local:** they sit at the repo root, which is the default. Nothing to set.
- **Server:** set `BOOKGEN_REF_PHOTOS_S3` to a prefix whose immediate children
  are `reference-photos/` and `reference-photos-cropped/`. It is synced into a
  temp directory for the length of one command and deleted afterwards —
  including on a crash — so the photos are never at rest on the server's disk.
  Only `compile` and `gen` fetch them; `pdf`, `html`, `preview` and `publish`
  read already-rendered JPEGs and never touch a photo.

`BOOKGEN_REF_PHOTOS_DIR` points at a local directory instead, for a checkout
where the trees are somewhere other than the repo root. Set at most one of the
two: two photo sources would make "which photos did this render use"
unanswerable, so setting both is a hard error rather than a silent precedence.

The prefix must be a **private** bucket. The publishing bucket is world-readable
by policy, so a photo prefix inside it would be a public URL to a real person's
face.

One deliberate exception: the two placeholder `.png` files under
`reference-photos/sample-book/` **are** tracked (see the negation in
`.gitignore`). They are synthetic shapes, not photographs of anyone, and the
sample has to compile on a fresh clone.

**Style and continuity anchors** — `refs:` in `book.yaml`, attached only via
`ref_policy` (one per camera distance) or a page's `extra_refs`. Missing files are
skipped with a warning, so the pipeline runs before any exist. Drop them in as:

    refs/close_porch.jpg     # the tight, close-camera plate
    refs/mid_street.jpg      # the medium plate
    refs/wide_valley.jpg     # the wide establishing plate

The 4-ref ceiling is budgeted as **3 photo refs + 1 style ref**. `compile` prints
the refs each page will send, and `refs_for` drops from the tail — style ref
first — if a page ever goes over.

## Workflow

    make compile                     # free. read the prompts before spending money
    make gen-all N=3                 # every slot x 3 candidates, 1K draft
    make lock P=3 C=2 RUN=20260726-141500
    make final P=3                   # 4K print render of the locked page
    make pdf                         # assemble the proof book
    make html                        # one page to paste into Google Docs
    make preview                     # browser mock-up of the print spread
    make publish YES=1               # PUBLIC — upload to S3

Single page, harder:

    make gen PAGES=3 N=4 EXTRA=--strict

Generation calls go out in parallel — `--concurrency 3` is the default, and
`make gen ... EXTRA=--concurrency 4` raises it. It is network/API-bound, not
CPU-bound, so at least 3 is always safe.

## The proof PDF

`make pdf` lays the book out one spread at a time: a manuscript page's prose on
one side and the art it calls for facing it on the other.

    make pdf                                   # newest run -> out/book-<run>.pdf
    make pdf C=2                               # ... using candidate 2 throughout
    make pdf RUN=20260726-141500 EXTRA="--out /tmp/proof.pdf"
    make pdf EXTRA=--no-diagrams

It is free, offline, and safe to run at any point — including before anything
has been generated, when every art page comes out as a labelled placeholder.
Details worth knowing:

- **Which render it uses.** A locked pick in `out/picks.json` wins outright.
  With nothing locked — the normal case mid-review — an unlocked slot takes
  candidate 1, or whatever `--cand` says, so a draft sweep can be proofed
  without locking first. `--cand` is recorded in the default filename
  (`book-<run>-c2.pdf`) so two proofs don't overwrite each other.
- **The unit is a manuscript page, not a slot.** Slots are gathered by their
  `book_page:`, so several slots sharing a page put their renders on one art
  page, and a page with no slot faces its diagrams instead. Either way it is
  two PDF pages per manuscript page, a prose page facing its art page. The side
  order is per-book: prose-left by default, art-left when the book sets
  `spread: art-left`.
- **Diagrams** are insets under the prose they explain, placed once, on the
  first page that lists them (a `diagrams:` entry can span a range of pages).
- **Trim** is 8 in wide by whatever `trim:` says, falling back to
  `aspect_ratio:` if the book has no `trim:` — 8x12 for 2:3, which is why a
  full-page render has no letterboxing when the two match. Set `trim:`
  separately when they don't: a book can render square (1:1) art but print a
  landscape page, `trim: "11x8.5"`, with the art centred and matted rather than
  filling the recto edge to edge.
- **Type** auto-fits: the largest size from 18pt down that still fits wins, and
  the block is centred in the leftover space. A page that overflows even at
  10pt is reported and clipped rather than spilling onto a second page, which
  would push the art off the facing page and desynchronise every spread after
  it. Cut prose in `manuscript:` instead.
- **`**emphasis**` and `- ` bullet lines** in the prose render as bold and as
  hanging-indent items, in case the manuscript mirror grows them.
- **`front_matter:`** supplies the title page. It is load-bearing, not
  decorative: one page ahead of the story is what puts the first story page
  on a verso, whichever side the art is on. A `dedication:` is padded to
  preserve that parity.

No new dependency — the PDF is written directly (base-14 Times, JPEGs embedded
as-is via `/DCTDecode`), so the script is still stdlib + pyyaml. `/PageLayout`
is set to `TwoPageRight`, so a viewer opens on the intended spreads. One local
quirk: `pdftoppm` here has no Times-Bold substitute and renders bold as roman.
Preview and Ghostscript are correct.

## The wraparound cover PDF

`cover` writes the one-page cover Lulu's uploader demands — back cover, spine,
front cover, sized to the book's `trim:` plus Lulu's outer-edge allowance
(0.875in per edge: a Letter Landscape book comes out 24x10.25in, matching
their template). Lulu's four upload checks fall out by construction: one page,
exact dimensions, no layers (the writer has never emitted any), and `--lulu`
embeds the fonts.

    make cover                       # proof, base-14 fonts -> out/cover.pdf
    make cover EXTRA=--lulu          # fonts embedded -> out/cover-lulu.pdf
    make cover EXTRA="--spine 0.31"  # spine width from Lulu's per-book template

The layout: the cover slot's art matted on the front, title above, byline
below, both rotated up the spine (dropped entirely below a ~0.15in spine —
printers won't set text there), and `front_matter.description` wrapped on the
back. That field exists for this page. The interior title page itself is
text-only — a render above the title read as a second cover, not a title
page.

Two things the tool cannot decide:

- **Spine width comes from Lulu's cover template**, generated per book from
  its page count and paper type. `--spine` defaults to 0.25in; if Lulu's
  template says otherwise, regenerate with the right value or the spine fold
  lands in the wrong place.
- **300 PPI** needs the `--final` 4K render of the cover slot; the command
  warns when the locked render comes out under ~250 PPI at print size. A 1K
  draft is fine for reviewing the layout but is not the upload file.

## Getting the book into Google Docs

`html` writes the whole book as one browser page for pasting into a Doc. The
default output is the **print manuscript**: no tool chrome, one beat per Doc
page, laid out for a US Letter landscape page — 1in margins, Arial 18pt at a
24pt pitch, 12pt between paragraphs, art centred on its own page, diagrams
sharing the page with their prose when they fit.

    make html                                # newest run -> out/manuscript.html
    make html C=2 EXTRA=--no-diagrams
    make html EXTRA=--review                 # labelled review copy instead

Open it in a browser, select all, copy, paste. **The browser is the mechanism** —
it hands Docs the decoded bitmaps along with the markup, and Docs uploads each
one. Pasting Markdown instead gets you the headings and the emphasis but not the
art: `![](…)` is not converted, and a Doc cannot reach a local file.

What carries into the Doc, and how:

- **Hard page breaks.** Every page block carries an inline
  `page-break-before:always`, which Docs maps onto its own "page break before"
  paragraph property on paste. Inline, because the clipboard serialises element
  style attributes verbatim; a `<style>` rule only reaches the Doc if the
  browser inlines it at copy time. If a paste ever lands as one continuous
  flow, that is the thing that failed — check for it first.
- **Image sizes.** Every `<img>` carries explicit `width`/`height` in px, which
  is what Docs sizes a pasted image from (96px = 1in). Slot art is fitted to
  the 9x6.5in content box; diagrams cap at 312pt tall and shrink further only
  when that keeps them on the same page as their prose.
- **Type.** The stylesheet sets Arial 18pt, a 24pt line-height and 12pt
  paragraph spacing once; the browser inlines those rules into the copied
  markup, which is how they follow the text into the Doc.
- **The Doc owns the page shape.** Page size, orientation and margins are the
  Doc's Page setup, not the paste's: File > Page setup > Landscape, Letter,
  1in margins. Page numbers are Insert > Page numbers.
- **Several diagrams on one page** go side by side in a borderless table, each
  with its `label:` above its image — the only side-by-side structure a paste
  keeps. Labels and images share a cell so a page break can never strand one
  from the other.
- **The images are snapshots.** Docs copies them in on paste; re-rolling a
  slot means re-pasting that page.
- **`--review`** keeps the old labelled output: `Page N` headings and a
  `slot · candidate · run` caption under every render, so a comment in a
  review Doc is traceable back to a file while `picks.json` is still
  undecided. Nothing about gathering changes between the two modes.
- **Coming back**, `File > Download > Markdown` pulls images out of the flow as
  `[image1]: <url>` references. That importer does not exist yet — edits come
  back into `manuscript:` by hand.

Gathering and pick rules are `pdf`'s, so the Doc and the proof never disagree
about which render illustrates which beat, and prose goes through the same
`prose_blocks()` the PDF lays out. Browser print preview shares the Doc's page
shape (`@page` is set to the trim), so File > Print in the browser paginates
the way the pasted Doc will — that is how the layout is verified without
spending anything.

## Print preview

`preview` is a browser mock-up of what `pdf` prints: the same spread (side
order per-book via `spread:`), one row per manuscript page, but viewable
without a PDF reader and without spending anything. Gathering is identical to
`pdf` and `html` — slots by `book_page:`, diagrams as an inset once a page
already has slot art, the full art page when it doesn't — so all three never
disagree about which art belongs to which beat.

    make preview                             # newest run -> out/preview.html
    make preview C=2 EXTRA=--no-diagrams

**Every image is clickable, and cycles through every rendered candidate for
that slot or diagram** — across every run, not just the newest one, in
run/candidate order, wrapping back to where it started. The figcaption
updates as you click (`p03 · candidate 3 · run 20260804-eyefix · locked ·
2/12`), so this doubles as a review tool for comparing candidates. **Cycling
records the pick.** The candidate you leave on screen is kept in the browser's
`localStorage` as your pick for that slot and restored on the next visit;
"Copy picks" at the top of the page collects every recorded pick that isn't
locked yet into a `picks.json` fragment on the clipboard — paste it into a
Claude session, which merges it into `out/picks.json` and regenerates. A static
page can't write the file itself, so applying stays a deliberate step.
`candidates_for()` in `bookgen.py` is what gathers the list; `pick_file()` still
decides which one loads first (the locked pick, or the lowest-numbered candidate
if nothing's locked yet).

**Tap-vs-scroll uses pointer events, not `onclick`.** On a touch screen, a
scroll that happens to start on an image can still deliver a click once the
finger lifts on some browsers — which was advancing the image out from under
someone just trying to scroll the page. A tap only cycles if the pointer
barely moved (under 10px) and came up within half a second; anything that
reads as a drag or a scroll (including the browser cancelling the pointer
outright to take over scrolling) leaves the image alone.

**Built-in note fields, one per page's text and one per image**, in a row
under each spread, each tagged with the same identifier used everywhere else
in the tool — a manuscript page number for the prose (`page3`), a slot or
diagram id for the art (`p03`, `D1`). Notes persist in the browser's
`localStorage` between visits; the "Show notes" checkbox hides them for a
clean screenshot. They live here and not in `manuscript.html` so the
select-all-copy-paste into Docs can never carry a stray textarea along.

## Sending a review back

A review is one thing, not two — "p03 needs a wider shot" and "p03 → run/cand
2" are the same message, and both are addressed by the identifier above — so
the page has one payload, produced two ways:

|Button|What it does|
|---|---|
|**Email feedback**|Opens a compose window with the notes and the not-yet-locked picks as one block. Subject is `Book review — <title>`.|
|**Copy feedback**|Same block, on the clipboard — for a phone whose mail client won't take it, or to paste straight into a Claude session.|
|**Copy picks**|Just the `picks.json` fragment, which still merges verbatim.|

The block looks like this, and both halves are consumed as-is:

    Book: sample-book
    Title: The Sample Book

    --- notes ---
    page3: the prose here should mention where the water goes next

    p03: the steam reads as smoke — make it thinner

    --- picks ---
    { "p03": { "run": "20260807-landscape", "cand": 2 } }

**The email body rides in the URL, so it is truncated rather than refused
when it gets long** — silently, which is the bad kind of failure. Past 1800
characters the button declines and points at "Copy feedback" instead of
opening a half-empty compose window.

**`publish.feedback_email` in `book.yaml` fills in the recipient.** It is
unset by default and deliberately so: `preview.html` is served from a
world-readable bucket, so an address in it is an address for a scraper. Unset
still works — the button fills in the subject and body and leaves the
recipient for the reviewer to pick out of their own contacts, which on a
phone is two taps. Set it to a forwardable address if you would rather not
ask.

**The review URL is stable per book** — `…/<Slug>/preview.html` — so it is
worth sending once:

    make review YES=1               # refresh the link, leave the book alone
    make review BOOK=other-book     # dry run first, prints the URL

`--review-only` uploads `preview.html` and the runs it cycles through and
nothing else. That is what makes the link refreshable after every re-roll
without republishing the manuscript, and what lets a book still in draft have a
review link at all.

## Publishing to S3

`publish` puts a book on the open internet as a plain static page — a URL you
can text to someone, no download and no Google account.

    make publish                   # dry run: prints what would go up
    make publish YES=1             # actually upload
    make publish-index YES=1       # the index across all books
    make review YES=1              # just the review page — see "Sending a review back"

    https://bookgen-books.s3.us-west-1.amazonaws.com/sample-book/index.html

**A dry run is the default and `--yes` is the commitment.** Unlike `pdf` and
`html`, this one is outward-facing: the bucket is world-readable, so content is
public the moment it lands and may be cached or indexed even if deleted later.

**Credentials come from the `aws` CLI's own environment** — `AWS_ACCESS_KEY_ID`
and friends, or a profile in `~/.aws` on a laptop, or an instance role on a
server, which is the intended setup there: no long-lived keys on the box at all.
No boto3; the script stays stdlib plus the pinned dependencies.

### Layout

    s3://<BOOKGEN_BUCKET>/
      index.html                     all books, with cover thumbnails
      sample-book/
        index.html                   the manuscript
        manuscript.html              same bytes — links to this key predate index.html
        preview.html                 the print-spread mock-up (see "Print preview")
        cover.jpg                    600px thumbnail for the root index
        runs/20260731-sample/        the whole run directory

The top-level name comes from `publish.slug` in `book.yaml`. It is spelled out
rather than derived from the title, because a title is prose and may change
while the prefix is a URL that has already been shared. A book with no
`publish.slug` cannot be published.

Things worth knowing:

- **The manuscript and preview are both rebuilt before every upload**, so what
  is public can never be stale against `picks.json`.
- **The root index links to both** — "Read the book" (`index.html`) and "Print
  preview" (`preview.html`) — per book card, alongside the cover thumbnail.
  Books are ordered by their `sequence:` in `book.yaml`.
- **`cmd_html` needed no changes to make this work.** Its `src` attributes are
  already relative — `runs/<run>/p03_c2.jpg` — which is exactly where the files
  land under the book's prefix.
- **Whole run directories go up**, every candidate and prompt, not just the
  images in use. That is a few times the bytes and it is what would let a review
  page offer "choose this one" later without re-uploading anything.
- **It publishes every run the manuscript references, not just the newest.**
  Picks can span runs, because a single re-rolled slot leaves one book with
  most of its images from one run and one from another. Uploading only
  `resolve_run()`'s answer would leave those images broken, and broken quietly:
  the HTML is fine, the keys just aren't there.
- **Add-only.** No `--delete`, so re-publishing after a re-lock rewrites the
  manuscript and touches nothing else. A second `publish --yes` takes ~2s.
- **`--review-only`** uploads `preview.html` and the runs it cycles through,
  and skips `index.html`, `manuscript.html` and the cover thumbnail. Use it to
  refresh a reviewer's link after a re-roll, or to give a book that is still in
  draft a review link without publishing its manuscript. Same `--yes` gate,
  because the renders land in the same world-readable bucket.
- **Every upload sets `Cache-Control`**, which matters here because the objects
  are rewritten in place and a reviewer holds the URL. With none at all the
  browser applies heuristic freshness (~10% of the object's age, so days for a
  book up a few weeks) and a reviewer ends up clicking through the spread from
  before the re-roll. HTML keys go up `no-cache`; run directories go up
  `public,max-age=31536000,immutable`, since a run name is a timestamp and a
  render never changes once written. That is also what makes cycling candidates
  instant on a second visit.
- **Do not add `--acl public-read`.** Object ownership is `BucketOwnerEnforced`,
  which disables ACLs — that flag is a hard error, not a no-op. Public access
  comes from the bucket policy and no object needs to ask for it.
- **There is no website configuration** on the bucket, so `<Slug>/` on its own
  is a 404. `index.html` is what makes the clean URL work. Enabling website
  hosting would only buy directory URLs, and that endpoint is HTTP-only while
  the REST endpoint above is HTTPS.

To check a publish the way a stranger would see it, force anonymous access:

    aws s3api get-object --no-sign-request --bucket "$BOOKGEN_BUCKET" \
      --key sample-book/index.html /tmp/pub.html

## What `--strict` does

Each page's `pitfalls:` list holds its known failure modes. They are **not** in
the base prompt. `--strict` appends them.

This split is the main lesson from doing pages by hand: preloading every
correction produced a 500-word checklist, and the model satisfied the checklist
instead of building a coherent space — it rendered a car with two windshields and
no floor. Start loose, escalate only on the page that actually fails.

The compiler prints `<-- LONG` past 400 words for the same reason. If a long page
fails, cut scene text rather than adding to it.

## Continuity anchors

When a page must match an earlier one — the same location, the same prop, the
same light — lock the earlier page and copy its chosen JPEG into `refs/`, then
list that ref in the later page's `extra_refs:`. A page can carry at most one
or two of these before the 4-ref ceiling starts dropping character photos,
which is what `refs_for` truncates first.

    cp books/<slug>/out/runs/<run>/p04_c2.jpg books/<slug>/refs/locked_p04_creek.jpg

`book.yaml` then points at `refs/locked_p04_creek.jpg` by name, and the later
pages list that name in `extra_refs`.

## Model notes

- `gemini-3.1-flash-image` (Nano Banana 2). Do **not** switch to `-lite`: it is
  not optimized for multiple reference inputs, which is the whole mechanism here.
- Ceiling is 4 character references. The compiler truncates past 4 and warns.
- `thinking_level: high` is set in the spec. It costs more and is why hard
  compositions have any chance at all.
- 1K drafts for review, 4K for print (3:2 at 4K is 5056x3392, ample for an
  11x8.5in page at 300dpi).
- Every output carries a SynthID watermark.
- For a large re-roll, the Batch API is cheaper with up to 24h turnaround. Not
  wired up here; worth it only if you start re-rolling nightly.

## What still needs a human

- Reading the preview. Check hair colour, height, clothing colour, apparent
  age. Ignore braid side, which hand, prop details.
- The dedication and the title decision.
- Any slot that keeps failing: rewrite `scene:` toward the geometry the model is
  reliably good at — faces toward the camera, figures in natural rows or
  groupings — rather than buying more candidates. After two failed batches whose
  failures scatter across age, count, geometry and placement, a third batch only
  shuffles the failure.
