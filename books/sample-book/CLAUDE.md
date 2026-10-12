# sample-book — the template book

The one book in this repository. It is not a real book: it is a small, complete,
runnable `book.yaml` that exercises every part of the pipeline, so the tool has
something to compile, dry-run, proof and publish on a fresh clone.

**Copy this directory to start a real book.** Change `publish.slug`, replace
`characters:`, `manuscript:` and `pages:`, point `char_refs:` at your own photos
in `reference-photos/<slug>/`, and delete `refs/` — its style anchors are
placeholders.

## Cast

| Id | Who | Reference |
|---|---|---|
| `nora` | 7-year-old girl, yellow raincoat | `reference-photos/sample-book/nora.png` |
| `milo` | 4-year-old boy, Nora's brother, blue raincoat | `reference-photos/sample-book/milo.png` |

Both are invented. The two `.png` files are **synthetic placeholders** drawn
with flat shapes — not photographs of anyone — and they are the one thing under
`reference-photos/` that is tracked in git (see the exception in `.gitignore`).
Real photos stay out of git: they are people's likenesses.

## State

- **Nothing rendered yet.** `books/sample-book/out/` is empty, so `pdf`, `html`
  and `preview` all draw labelled placeholders and paginate the same way they
  will after a render. That is deliberate: it means the sample costs nothing to
  run and the placeholder path is exercised on every clone.
- `refs/` holds three placeholder style plates (`style-close`, `style-mid`,
  `style-wide`), one per camera distance, wired through `ref_policy:`.
- One diagram, `D1` (the water cycle), inset on manuscript page 3.
- `trim: "11x8.5"` is set, so art pages are matted inside the page margin.

## What this book is for

It is the fixture the pipeline is smoke-tested against, and the worked example
a reader copies. Keep it small, keep it compiling, and keep every key in it
meaning something:

```bash
uv run ./bookgen.py --book sample-book compile
uv run ./bookgen.py --book sample-book gen --dry-run
uv run ./bookgen.py --book sample-book pdf
uv run ./bookgen.py --book sample-book preview
```

`gen` (the real one) costs money; the sample's placeholder refs will produce
placeholder art. Re-roll it only if you mean to spend.
