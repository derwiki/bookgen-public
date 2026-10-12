#!/usr/bin/env python3
"""
bookgen — deterministic prompt compiler + Nano Banana driver for
illustrated picture books.

One tool, many books. Each lives in books/<slug>/ with its own book.yaml,
refs/ and out/; reference-photos/ at the repo root has one subdir per book
slug (a photo used by more than one book is copied into each subdir).
Every command names its book with --book, always: inferring one from the
working directory would eventually send a `gen` — which costs real money —
into the wrong book's out/.

All guardrails live in book.yaml. Never edit a prompt by hand: edit the spec
and recompile, so a fix applies to every page at once.

  B=sample-book

  ./bookgen.py --book $B compile                 # write prompts, free
  ./bookgen.py --book $B compile --page 14
  ./bookgen.py --book $B gen --n 3               # all slots, 3 candidates, draft
  ./bookgen.py --book $B gen --page 14 --n 4 --strict
  ./bookgen.py --book $B gen --diagrams
  ./bookgen.py --book $B gen --page 14 --final   # 4K render of a locked page
  ./bookgen.py --book $B lock --page 14 --cand 2 # record the pick
  ./bookgen.py --book $B lock --diagram D3 --cand 2
  ./bookgen.py --book $B pdf                     # proof: prose | illustration
  ./bookgen.py --book $B pdf --lulu              # Lulu upload: fonts embedded
  ./bookgen.py --book $B html                    # one page, to paste into a Doc

Nothing here is machine-specific: every path and name that differs between a
laptop and a server comes from the environment. See .env.example.

Env: GEMINI_API_KEY          the model key; required by `gen`
     BOOKGEN_BUCKET          S3 bucket `publish` writes to (default below)
     BOOKGEN_REF_PHOTOS_S3   s3:// prefix holding the photo trees — synced into
                             a temp dir for one command, then deleted
     BOOKGEN_REF_PHOTOS_DIR  local dir holding them instead (default: repo root)
     AWS_*                   the aws CLI's own variables, used by publish
"""
import argparse, base64, contextlib, functools, hashlib, io, json, mimetypes
import os, pathlib, re, shutil, subprocess
import sys, tempfile, time, zlib
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.error
import yaml

ROOT = pathlib.Path(__file__).resolve().parent
BOOKS = ROOT / "books"

# Assigned by resolve_book() once --book is parsed. Module-level globals rather
# than a context object threaded through every command: seventeen call sites
# read these, and rebinding two names is a far smaller change than rewriting
# all of them. One process only ever works on one book.
BOOK = SPEC = OUT = None


def resolve_book(slug):
    """Point the module's paths at one book.

    Explicit --book only. No inferring from the working directory and no
    "if there is exactly one book, use it" fallback: both are conveniences that
    can silently send a `gen` — which costs real money — into the wrong book's
    out/, and neither saves anything the Makefile's BOOK ?= does not.
    """
    global BOOK, SPEC, OUT
    avail = sorted(p.name for p in BOOKS.glob("*") if (p / "book.yaml").is_file())
    if slug not in avail:
        sys.exit(f"no such book: {slug!r}\n"
                 f"available: {', '.join(avail) if avail else '(none in books/)'}")
    BOOK = BOOKS / slug
    SPEC = BOOK / "book.yaml"
    OUT = BOOK / "out"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
MAX_REFS = 4         # model ceiling per request
MAX_REF_PX = 1024    # longest edge of a prepared ref


# ---------------------------------------------------------------- config
#
# Everything that differs between a laptop and a server, and nothing that is a
# secret. Defaults reproduce the laptop exactly, so an unset environment is
# still the old behaviour.

def _env_dir(name):
    v = os.environ.get(name)
    return pathlib.Path(v).expanduser().resolve() if v else None


BUCKET = os.environ.get("BOOKGEN_BUCKET") or "bookgen-books"

# The reference photos — real likenesses of real people — live in two trees,
# `reference-photos/` and `reference-photos-cropped/`, and are deliberately
# NOT in git. On the author's machine they sit at the repo root; on a server
# they are synced out of REF_PHOTOS_S3 into a temp dir for the length of one
# command and deleted on the way out, so they are never at rest on the disk.
REF_PHOTOS_S3 = os.environ.get("BOOKGEN_REF_PHOTOS_S3") or None
REF_PHOTOS_DIR = _env_dir("BOOKGEN_REF_PHOTOS_DIR") or ROOT

# The tree names as they appear inside a book.yaml path, and where the trees
# actually are right now. Rebound per command by photo_root().
PHOTO_TREES = ("reference-photos-cropped", "reference-photos")
PHOTO_ROOT = REF_PHOTOS_DIR


@contextlib.contextmanager
def photo_root():
    """Put the photo trees in place for one command, and clean up after it.

    Without BOOKGEN_REF_PHOTOS_S3 this is just the local directory — the repo
    root, which is where reference-photos/ has always been, so a laptop is
    unchanged. With it, the prefix is synced into a fresh temp dir and that dir
    is removed in a finally, including on a crash or a sys.exit().
    """
    global PHOTO_ROOT
    if not REF_PHOTOS_S3:
        if os.environ.get("BOOKGEN_REF_PHOTOS_DIR"):
            PHOTO_ROOT = REF_PHOTOS_DIR
        yield PHOTO_ROOT
        return
    if os.environ.get("BOOKGEN_REF_PHOTOS_DIR"):
        sys.exit("set only one of BOOKGEN_REF_PHOTOS_S3 and BOOKGEN_REF_PHOTOS_DIR "
                 "— two photo sources would make 'which photos did this render "
                 "use' unanswerable")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bookgen-refs-"))
    try:
        print(f"  fetching reference photos: {REF_PHOTOS_S3} -> {tmp}")
        if not aws("s3", "sync", REF_PHOTOS_S3, str(tmp), "--only-show-errors"):
            sys.exit(f"could not fetch reference photos from {REF_PHOTOS_S3}")
        PHOTO_ROOT = tmp
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def with_photo_root(fn):
    """Run a command with the photo trees in place. Only the commands that
    touch a photo need this: `compile` reports the ref list, `gen` sends it."""
    @functools.wraps(fn)
    def wrapper(spec, args):
        with photo_root():
            return fn(spec, args)
    return wrapper


def load():
    with open(SPEC) as f:
        return yaml.safe_load(f)


def sq(text):
    """Squash a YAML folded block into one clean paragraph."""
    return " ".join(str(text).split())

def dedication_lines(text):
    """The dedication's own line breaks ARE its layout.

    prose_lines joins single newlines into one flowing paragraph, which is
    right for prose and wrong here: the salutation is its own line and the
    sentence breaks where the author broke it. Returned stripped, blanks
    kept so a deliberate blank line still reads as one.
    """
    return [l.strip() for l in str(text).strip("\n").split("\n")]


def dedication_html(text):
    """The dedication for HTML, its line breaks as <br>."""
    return "<br>".join(html_spans(l) for l in dedication_lines(text) if l)



# ---------------------------------------------------------------- compile

def compile_page(spec, pid, strict=False):
    """Assemble one page prompt. Deterministic: same spec -> same bytes."""
    p = spec["pages"][pid]
    cast = p.get("cast", [])
    parts = []

    if cast:
        parts.append(sq(spec["ref_instruction"]))

    # pair rules (e.g. sisters share hair colour) fire automatically
    for rule in spec.get("pair_rules", []):
        if all(c in cast for c in rule["when"]):
            parts.append(sq(rule["text"]))

    parts.append(sq(spec["style"]))
    parts.append(sq(p["scene"]))

    # context rules (e.g. booster seat in car scenes)
    for tag in p.get("tags", []):
        if tag in spec.get("context_rules", {}):
            parts.append(sq(spec["context_rules"][tag]))

    if cast:
        parts.append(
            "The people:\n"
            + "\n".join(f"- {sq(spec['characters'][c]['desc'])}" for c in cast)
        )

    parts.append(sq(spec["light"][p["light"]]))

    if strict and p.get("pitfalls"):
        parts.append("\n".join(f"- {sq(x)}" for x in p["pitfalls"]))

    parts.append(sq(spec["negatives"]))
    return "\n\n".join(parts)


def compile_diagram(spec, did):
    """Assemble one diagram prompt.

    A diagram may name an alternate style from `diagram_styles:`. The schematic
    default (flat shapes, hand-lettered labels) is wrong for a diagram that
    should look like the thing rather than like a chart — a mineral specimen
    that wants to look like the rock in your hand, say.
    """
    d = spec["diagrams"][did]
    name = d.get("style")
    if not name:
        return sq(spec["diagram_style"]) + "\n\n" + sq(d["scene"])
    styles = spec.get("diagram_styles") or {}
    if name not in styles:
        sys.exit(f"{did}: style {name!r} is not in diagram_styles")
    return sq(styles[name]) + "\n\n" + sq(d["scene"])


def _photo_tree_path(fp):
    """Re-hang a book-relative photo path off PHOTO_ROOT.

    book.yaml writes `../../reference-photos/<slug>/x.jpg`, relative to the
    book directory, which lands on the repo root — true on a laptop, false on a
    server, where the trees are in a temp dir. So the path is cut at the tree
    name and rejoined to wherever the trees are now. A path naming no tree (a
    book's own `refs/` anchor, say) comes back untouched.
    """
    parts = fp.parts
    for tree in PHOTO_TREES:
        if tree in parts:
            return PHOTO_ROOT / pathlib.Path(*parts[parts.index(tree):])
    return fp


def ref_label(fp):
    """A ref's name for dry-run output, the same string on any machine.

    Relative to the book when it is a book-local anchor, else relative to the
    photo root, else the bare filename. Absolute paths are deliberately not
    used: they differ between a laptop and a container, and the whole point of
    the manifest is that two machines' outputs diff clean.
    """
    for base in (BOOK, PHOTO_ROOT):
        try:
            return fp.relative_to(base).as_posix()
        except ValueError:
            pass
    return fp.name


def ref_paths(spec, name):
    """Resolve a ref name from either namespace: char_refs first, then refs.

    Relative to the book directory, not the repo root, so `refs/` stays
    book-local while `char_refs` reach up to that book's own
    `reference-photos/<slug>/` — wherever the photo trees happen to be mounted
    this run, which _photo_tree_path() handles.

    A char_refs entry is normally one path, but may be a list — every crop
    the labeler produced for that person (face, fullbody, a second face crop
    from another photo, ...) all attach together rather than picking one.
    `refs` entries stay single-path; there is no per-anchor crop variant.
    """
    for table in ("char_refs", "refs"):
        if name in spec.get(table, {}):
            val = spec[table][name]
            vals = val if isinstance(val, list) else [val]
            return [_photo_tree_path((BOOK / v).resolve()) for v in vals]
    return None


def refs_for(spec, pid):
    """Refs for one page, highest priority first.

    The order IS the truncation order when a page exceeds MAX_REFS: people
    first, since likeness is what actually drifts; then the page's own
    continuity anchors; then the style ref, which is the cheapest to lose.
    Within one person, every crop counts toward that same budget.

    Cast members with no entry in char_refs simply contribute no ref. That is
    deliberate, not a lookup failure — a character whose only photo is of them
    at the wrong age is better carried by the text and a sibling's photo than
    by a photo that fights the written age.
    """
    p = spec["pages"][pid]
    names = [c for c in p.get("cast", []) if c in spec.get("char_refs", {})]
    names += list(p.get("extra_refs", []))
    names += list(spec.get("ref_policy", {}).get(p["distance"], []))

    paths, seen = [], set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        resolved = ref_paths(spec, n)
        if resolved is None:
            print(f"  ! p{pid:02d}: ref {n!r} is in neither char_refs nor refs "
                  f"— skipped", file=sys.stderr)
            continue
        for fp in resolved:
            if not fp.exists():
                print(f"  ! p{pid:02d}: ref {n} missing ({fp.name}) — skipped",
                      file=sys.stderr)
            else:
                paths.append(fp)

    if len(paths) > MAX_REFS:
        dropped = ", ".join(f.name for f in paths[MAX_REFS:])
        print(f"  ! p{pid:02d}: {len(paths)} refs > model limit {MAX_REFS}, "
              f"dropping {dropped}", file=sys.stderr)
        paths = paths[:MAX_REFS]
    return paths


# ---------------------------------------------------------------- ref prep

def imagemagick():
    """Path to ImageMagick's CLI, or None. The non-macOS stand-in for sips."""
    return shutil.which("magick") or shutil.which("convert")


def ref_dims(fp):
    """(width, height) via sips or ImageMagick, or (0, 0) if unreadable."""
    if shutil.which("sips"):
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(fp)],
                           capture_output=True, text=True)
        d = {}
        for line in r.stdout.splitlines():
            k, _, v = line.strip().partition(": ")
            if v.isdigit():
                d[k] = int(v)
        return d.get("pixelWidth", 0), d.get("pixelHeight", 0)
    magick = imagemagick()
    if not magick:
        return 0, 0
    r = subprocess.run([magick, "identify", "-format", "%w %h", str(fp)],
                       capture_output=True, text=True)
    w, _, h = r.stdout.partition(" ")
    if w.isdigit() and h.strip().isdigit():
        return int(w), int(h.strip())
    return 0, 0


def to_jpeg(src, dst, max_edge=None):
    """Write a JPEG copy of src to dst, shrinking the longest edge to max_edge.

    sips where it exists — macOS, the path this pipeline was written on, so a
    Mac's output is unchanged — and ImageMagick where it does not, which is the
    hosted sandbox. Both only ever shrink; neither upscales a small ref.
    Returns True on success, and leaves dst absent on failure.
    """
    if shutil.which("sips"):
        cmd = ["sips", "-s", "format", "jpeg"]
        if max_edge:
            cmd += ["-Z", str(max_edge)]
        cmd += [str(src), "--out", str(dst)]
    else:
        magick = imagemagick()
        if not magick:
            return False
        cmd = [magick, str(src)]
        if max_edge:
            cmd += ["-resize", f"{max_edge}x{max_edge}>"]
        cmd += ["-quality", "90", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and dst.exists()


def prepare_ref(fp):
    """Return a model-ready copy of a ref: JPEG, longest edge <= MAX_REF_PX.

    Two reasons this exists. HEIC: the API will not take it, and phones shoot
    it by default. Size: a 4284x5712 phone photo base64s to ~16 MB, so four
    refs is a ~45 MB request body per candidate — 54 of those for one 18x3
    run — and none of that resolution survives being turned into watercolor.

    Uses sips on macOS and ImageMagick elsewhere, so there is no Pillow
    dependency. Cached on mtime, so editing a photo invalidates it. Falls back
    to the original if neither converter is available.
    """
    cache = OUT / "refcache"
    dst = cache / f"{fp.stem}-{int(fp.stat().st_mtime)}-{MAX_REF_PX}.jpg"
    if dst.exists():
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    w, h = ref_dims(fp)
    # Only shrink: both converters upscale when asked to.
    max_edge = MAX_REF_PX if max(w, h) > MAX_REF_PX else None
    if not to_jpeg(fp, dst, max_edge):
        print(f"  ! no working image converter (sips, or ImageMagick) — "
              f"sending {fp.name} unprepared", file=sys.stderr)
        return fp
    return dst


# ---------------------------------------------------------------- api

def call_api(model, prompt, ref_paths, aspect, size, thinking, timeout=300):
    inputs = [{"type": "text", "text": prompt}]
    for src in ref_paths:
        fp = prepare_ref(src)
        mime = mimetypes.guess_type(str(fp))[0] or "image/png"
        if mime in ("image/heic", "image/heif"):
            sys.exit(f"{fp.name}: the API will not accept HEIC and no "
                     f"converter (sips or ImageMagick) could convert it. "
                     f"Convert it to JPEG by hand.")
        inputs.append({
            "type": "image",
            "mime_type": mime,
            "data": base64.b64encode(fp.read_bytes()).decode(),
        })
    body = {
        "model": model,
        "input": inputs,
        "response_format": {
            # JPEG is the only output type the interactions endpoint accepts;
            # asking for image/png is a hard 400, not a silent downgrade.
            "type": "image", "mime_type": "image/jpeg",
            "aspect_ratio": aspect, "image_size": size,
        },
        "generation_config": {"thinking_level": thinking},
    }
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY not set")
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")

    # walk steps for the final image; ignore interim "thought" images
    for step in data.get("steps", []):
        if step.get("type") == "model_output":
            for blk in step.get("content", []):
                if blk.get("type") == "image":
                    return base64.b64decode(blk["data"])
    img = data.get("output_image")
    if img:
        return base64.b64decode(img["data"])
    raise RuntimeError("no image in response: " + json.dumps(data)[:400])


# ---------------------------------------------------------------- pdf writer
#
# The proof PDF is written by hand rather than with reportlab so the pipeline
# stays stdlib + pyyaml. That is cheap here for two reasons: screen proofs can
# reference the base-14 fonts without embedding them, and a JPEG is already a
# legal PDF image stream as-is (/DCTDecode), so nothing below ever decodes a
# pixel. The exception is the Lulu upload file: Lulu requires all fonts
# embedded, and ghostscript's pdfwrite passes base-14 fonts through
# unembedded no matter the flags, so `pdf --lulu` embeds the system Helvetica
# itself (PDF(embed=True) below) — fonttools is needed for that path only.

PDF_TRIM_W = 576.0        # 8 in wide — the fallback page when a book sets no
                          # trim:. Height follows the spec's aspect_ratio, so a
                          # 2:3 book is 8x12 in and a 2:3 render fills the
                          # illustration page edge to edge with no letterboxing.
PDF_MARGIN = 54.0         # 0.75 in
COVER_EDGE = 63.0         # 0.875 in — Lulu's outer-edge allowance on their
                          # wraparound template (24x10.25in for Letter Landscape)
PDF_BODY = (18, 17, 16, 15, 14, 13, 12, 11, 10)   # first size that fits wins;
                                     # picture-book type wants to start large
PDF_LEAD = 1.45           # leading, as a multiple of the font size


def page_size(spec):
    """Trim size in points: the shape of the physical printed page.

    `trim:` sets the physical page size in inches as "WxH" — "11x8.5" is US
    Letter Landscape (792x612 pts), the Lulu trim this series prints at (Book
    Creation Guide p.12 chart). A malformed value exits loudly: silently
    falling back to the default is exactly the wrong-size-pages bug this key
    exists to fix. Books without `trim:` keep the 8-in-wide page whose height
    follows `aspect_ratio:` (a 2:3 book is 8x12 in, so a render fills the art
    page edge to edge). `place_contain`/`draw_art_page` size art from the
    actual image file, never from these numbers, so `trim:` only changes the
    page around the art — it can never stretch or crop a render.
    """
    if spec.get("trim"):
        try:
            w, h = (float(p) for p in str(spec["trim"]).lower().split("x"))
        except ValueError:
            sys.exit(f"malformed trim: {spec['trim']!r} — expected \"WxH\" "
                     "in inches, e.g. trim: \"11x8.5\"")
        if w <= 0 or h <= 0:
            sys.exit(f"malformed trim: {spec['trim']!r} — sizes must be "
                     "positive inches, e.g. trim: \"11x8.5\"")
        return w * 72.0, h * 72.0
    a, b = (float(x) for x in str(spec.get("aspect_ratio", "2:3")).split(":"))
    return PDF_TRIM_W, PDF_TRIM_W * b / a


# Advance widths in units/1000 em, from Adobe's Helvetica AFMs, verified against
# the rendered fonts character by character. These are what all line breaking is
# measured against, and — because a run of bold inside a line is a separate
# positioned Tj — what keeps a bold word from colliding with the roman text
# after it. Both faces need their own table: Helvetica-Bold runs noticeably
# wider than Helvetica on lowercase and digits, so no single fudge factor is
# safe.
_HELV_W, _HELV_B = {}, {}
for _table, _rows in (
    (_HELV_W, [
        (222, "'`‘’ijl"), (260, "|"), (278, " ,./:;\\[]Ift"),
        (333, "()-r"), (334, "{}"), (350, "•"), (355, '"'), (389, "*"),
        (469, "^"), (500, "Jsczkvxy“”"),
        (556, "#$0123456789_bdghnopquL–?ae"), (584, "~+<=>"), (611, "FTZ"),
        (667, "ABEKSVXY&"), (722, "CDHNRUw"), (778, "GOQ"), (833, "Mm"),
        (889, "%"), (944, "W"), (1015, "@"), (1000, "—…"),
    ]),
    (_HELV_B, [
        (278, " ',./\\Iijl’"), (280, "|"), (333, "`:;[]ft()-‘"),
        (350, "•"), (389, "*{}r"), (474, '"'), (500, "z“”"),
        (556, "#$0123456789_–aeJscvxyk"), (584, "^~+<=>"),
        (611, "FTZbdghnopquL?"), (667, "EKSVXY"), (722, "ABCDHNRU&"),
        (778, "GOQw"), (833, "M"), (889, "m%"), (944, "W"), (975, "@"),
        (1000, "—…"),
    ]),
):
    for _w, _chars in _rows:
        for _c in _chars:
            _table[_c] = _w


def text_width(s, size, bold=False):
    table = _HELV_B if bold else _HELV_W
    return sum(table.get(c, 556) for c in s) / 1000.0 * size


def pdf_escape(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def jpeg_info(data):
    """(width, height, components) read straight off the JPEG's SOF marker.

    Saves depending on sips or Pillow just to learn how big an image is, and
    tells us which PDF colour space the stream needs.
    """
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m == 0x01 or 0xD0 <= m <= 0xD9:      # standalone markers, no length
            i += 2
            continue
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):   # any SOFn
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h, data[i + 9]
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("no SOF marker — not a JPEG?")


def jpeg_dims(fp):
    """(width, height) in pixels off the JPEG's SOF marker.

    Reads only a header prefix — preview's every-candidate-across-every-run
    sweep would otherwise read whole 4K finals off disk just to learn their
    shape. Falls back to the full file when the SOF marker sits past the
    prefix (a fat APPn segment can push it there).
    """
    with open(fp, "rb") as f:
        head = f.read(1 << 16)
    try:
        w, h, _ = jpeg_info(head)
    except ValueError:
        w, h, _ = jpeg_info(fp.read_bytes())
    return w, h


def same_orientation(fp, ratio):
    """True when the JPEG at `fp` points the same way as a width/height
    `ratio`: both landscape, both portrait, or both square."""
    w, h = jpeg_dims(fp)
    orient = 1 if w > h else -1 if w < h else 0
    want = 1 if ratio > 1 else -1 if ratio < 1 else 0
    return orient == want


class Page:
    """One page's content stream, in points from the bottom-left corner."""

    def __init__(self, pdf):
        self.pdf, self.ops, self.used = pdf, [], set()

    def text(self, s, x, y, size, bold=False):
        if not s:
            return
        self.ops.append(f"BT {'/F2' if bold else '/F1'} {size:.2f} Tf "
                        f"1 0 0 1 {x:.2f} {y:.2f} Tm ({pdf_escape(s)}) Tj ET")

    def vtext(self, s, x, y, size, bold=False):
        """Vertical text running upward, for cover spines (US convention)."""
        if not s:
            return
        self.ops.append(f"BT {'/F2' if bold else '/F1'} {size:.2f} Tf "
                        f"0 1 -1 0 {x:.2f} {y:.2f} Tm ({pdf_escape(s)}) Tj ET")

    def image(self, path, x, y, w, h):
        name, _, _ = self.pdf.image(path)
        self.used.add(name)
        self.ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")

    def frame(self, x, y, w, h, gray=0.78):
        self.ops.append(f"q {gray} G 0.75 w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S Q")

    def content(self):
        # cp1252 is what /WinAnsiEncoding means. It covers the manuscript's em
        # dashes and curly quotes; anything outside it (Polish diacritics, say)
        # lands as "?" and would need a real embedded font.
        return zlib.compress("\n".join(self.ops).encode("cp1252", "replace"))


def _helvetica_ttf(subfamily):
    """One face of the macOS system Helvetica as standalone TTF bytes, plus
    its 1000-upm metrics for the PDF font descriptor. fsType 0 (installable),
    so embedding is unrestricted. fonttools is imported here, not at module
    level: only the --lulu path needs it.

    This is the one genuinely macOS-bound path in the tool, and it is
    deliberate: `pdf --lulu` has to embed the same Helvetica the printed
    editions were built with, and that face lives at /System/Library/Fonts on
    macOS. It exits loudly anywhere else rather than silently substituting a
    different font into the upload file."""
    try:
        from fontTools.ttLib import TTCollection
    except ImportError:
        sys.exit("pdf --lulu needs fonttools, which is a pinned dependency — "
                 "this environment is broken; re-run `uv sync`")
    ttc = "/System/Library/Fonts/Helvetica.ttc"
    if not pathlib.Path(ttc).exists():
        sys.exit(f"pdf --lulu needs {ttc} (ships with macOS)")
    for f in TTCollection(ttc, lazy=True).fonts:
        if (f["name"].getDebugName(1), f["name"].getDebugName(2)) == \
                ("Helvetica", subfamily):
            head, hhea = f["head"], f["hhea"]
            k = 1000.0 / head.unitsPerEm
            cap = getattr(f["OS/2"], "sCapHeight", 0) or int(hhea.ascent * 0.72)
            buf = io.BytesIO()
            f.save(buf)
            return (buf.getvalue(),
                    (int(head.xMin * k), int(head.yMin * k),
                     int(head.xMax * k), int(head.yMax * k)),
                    int(hhea.ascent * k), int(hhea.descent * k),
                    int(cap * k))
    sys.exit(f"{ttc}: no Helvetica {subfamily} face")


class PDF:
    """Minimal PDF 1.5 writer: base-14 Helvetica, JPEG images, Flate content.
    `embed=True` swaps the base-14 font references for embedded system
    Helvetica — the Lulu upload path (`pdf --lulu`)."""

    def __init__(self, w, h, embed=False):
        self.w, self.h = w, h
        self.embed = embed
        self.objs = [b""]      # 1-indexed; objs[0] is the free-list head
        self.pages = []
        self.images = {}       # path -> (name, object id, px width, px height)

    def _add(self, body=b""):
        self.objs.append(body)
        return len(self.objs) - 1

    def _stream(self, extra, data):
        return self._add(b"<< " + extra.encode() + f" /Length {len(data)} >>".encode()
                         + b"\nstream\n" + data + b"\nendstream")

    def image(self, path):
        """Register a JPEG once; returns (resource name, px width, px height)."""
        if path not in self.images:
            data = pathlib.Path(path).read_bytes()
            w, h, ncomp = jpeg_info(data)
            cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(ncomp)
            if cs is None:
                sys.exit(f"{path.name}: {ncomp}-component JPEG, cannot embed")
            oid = self._stream(
                f"/Type /XObject /Subtype /Image /Width {w} /Height {h} "
                f"/ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode", data)
            self.images[path] = (f"Im{len(self.images)}", oid, w, h)
        name, _, w, h = self.images[path]
        return name, w, h

    def page(self):
        self.pages.append(Page(self))
        return self.pages[-1]

    def _font(self, base, subfamily, widths, stemv):
        """Font object with the program embedded, for the --lulu print file.

        The system Helvetica face (fsType 0, embedding unrestricted) goes in
        as /TrueType with WinAnsi /Widths taken from the same AFM tables all
        line breaking is measured against, so print and screen proofs agree.
        Embedding here — rather than a ghostscript post-pass — keeps the
        JPEGs bit-identical: a gs round-trip decodes every image to raw
        pixels and re-encodes them, a full JPEG generation of loss.
        """
        ttf, bbox, asc, desc, cap = _helvetica_ttf(subfamily)
        ff = self._stream(f"/Length1 {len(ttf)}", ttf)
        fd = self._add(
            f"<< /Type /FontDescriptor /FontName /{base} /Flags 32 "
            f"/FontBBox [{' '.join(str(v) for v in bbox)}] /ItalicAngle 0 "
            f"/Ascent {asc} /Descent {desc} /CapHeight {cap} "
            f"/StemV {stemv} /FontFile2 {ff} 0 R >>".encode())
        ws = " ".join(str(widths.get(chr(c), 556)) for c in range(32, 256))
        return self._add(
            f"<< /Type /Font /Subtype /TrueType /BaseFont /{base} "
            f"/Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255 "
            f"/Widths [{ws}] /FontDescriptor {fd} 0 R >>".encode())

    def write(self, path):
        if self.embed:
            fr = self._font("Helvetica", "Regular", _HELV_W, 88)
            fb = self._font("Helvetica-Bold", "Bold", _HELV_B, 118)
        else:
            fr = self._add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
            fb = self._add(b"<< /Type /Font /Subtype /Type1 "
                           b"/BaseFont /Helvetica-Bold "
                           b"/Encoding /WinAnsiEncoding >>")
        tree = self._add()          # reserved: every page needs /Parent
        kids = []
        for pg in self.pages:
            cid = self._stream("/Filter /FlateDecode", pg.content())
            xo = " ".join(f"/{n} {oid} 0 R" for n, oid, _, _ in self.images.values()
                          if n in pg.used)
            res = f"/Font << /F1 {fr} 0 R /F2 {fb} 0 R >>"
            if xo:
                res += f" /XObject << {xo} >>"
            kids.append(self._add(
                f"<< /Type /Page /Parent {tree} 0 R "
                f"/MediaBox [0 0 {self.w:.2f} {self.h:.2f}] "
                f"/Resources << {res} >> /Contents {cid} 0 R >>".encode()))
        self.objs[tree] = (f"<< /Type /Pages /Count {len(kids)} /Kids ["
                           + " ".join(f"{k} 0 R" for k in kids) + "] >>").encode()
        # TwoPageRight is the whole point of the layout: odd pages on the right,
        # so a reader opens on the book's intended spreads — prose-left by
        # default, art-left for a book with `spread: art-left` (bookgen flips
        # the page order; the catalog setting is the same either way).
        cat = self._add(f"<< /Type /Catalog /Pages {tree} 0 R "
                        f"/PageLayout /TwoPageRight >>".encode())

        buf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * len(self.objs)
        for i in range(1, len(self.objs)):
            offsets[i] = len(buf)
            buf += f"{i} 0 obj\n".encode() + self.objs[i] + b"\nendobj\n"
        xref = len(buf)
        buf += f"xref\n0 {len(self.objs)}\n0000000000 65535 f \n".encode()
        for i in range(1, len(self.objs)):
            buf += f"{offsets[i]:010d} 00000 n \n".encode()
        buf += (f"trailer\n<< /Size {len(self.objs)} /Root {cat} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        path.write_bytes(bytes(buf))


# ---------------------------------------------------------------- pdf layout
#
# A laid-out line is (runs, indent, gap above), where runs is a list of
# (text, bold) pieces. Runs rather than plain strings so that emphasis marked
# in the manuscript survives, since it can start and end mid-word.

def md_spans(text):
    """(text, bold) spans for one paragraph, consuming ** ** emphasis."""
    spans, bold, i = [], False, 0
    while i < len(text):
        if text.startswith("**", i):
            bold = not bold
            i += 2
            continue
        j = text.find("**", i)
        j = len(text) if j < 0 else j
        spans.append((text[i:j], bold))
        i = j
    return spans


def md_words(text):
    """Words as run lists. "**rock**." is one word of two differently set
    pieces, which is why a word is a list rather than a string."""
    words, cur = [], []
    for chunk, bold in md_spans(text):
        for part in re.split(r"(\s+)", chunk):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append((part, bold))
    if cur:
        words.append(cur)
    return words


def plain_words(text, bold=False):
    return [[(w, bold)] for w in text.split()]


def line_width(runs, size):
    return sum(text_width(t, size, b) for t, b in runs)


def wrap_words(words, size, width):
    """Greedy line break over run-list words."""
    space = text_width(" ", size)
    lines, cur, used = [], [], 0.0
    for word in words:
        w = line_width(word, size)
        if cur and used + space + w > width:
            lines.append(cur)
            cur, used = list(word), w
        else:
            if cur:
                cur.append((" ", False))
                used += space
            cur += list(word)
            used += w
    if cur:
        lines.append(cur)
    return lines or [[]]


def prose_blocks(text):
    """Manuscript prose split the way every renderer has to see it: a list of
    paragraphs, each a list of [text, marker] blocks. A line starting "- "
    opens a bullet, a line starting "N. " opens a numbered item; any other
    line continues the block above it. The marker is "- " for a bullet, the
    bare number ("1.") for a numbered item, "" for plain text — the number is
    stripped here and re-supplied by the renderer, so the PDF hangs wrapped
    lines under it and the Doc can emit a real <ol> without double numbering.

    Shared by prose_lines and the HTML writer on purpose. The proof and the Doc
    reading the same page differently is the one thing neither is allowed to do,
    and two copies of this loop is how that would start.
    """
    out = []
    for para in (p for p in text.split("\n\n") if p.strip()):
        blocks = []
        for raw in para.split("\n"):
            s = raw.strip()
            if not s:
                continue
            m = re.match(r"(\d+)\.\s+(.*)", s)
            if s.startswith("- "):
                blocks.append([s[2:], "- "])
            elif m:
                blocks.append([m.group(2), f"{m.group(1)}."])
            elif blocks:
                blocks[-1][0] += " " + s
            else:
                blocks.append([s, ""])
        if blocks:
            out.append(blocks)
    return out


def prose_lines(text, size, width):
    """Manuscript prose: paragraphs split on blank lines, ** ** honoured, and
    "- " / "N. " lines kept as separate hanging-indent items instead of being
    run together into the paragraph above them. A bullet gets the glyph; a
    numbered item keeps its own number."""
    # Gap rules: a list attaches FLUSH to the paragraph
    # before it — no extra gap before the first item — and an all-bold
    # paragraph is a heading, so the block after it attaches flush too.
    # Everything else keeps the full paragraph gap.
    out, prev_item, prev_bold = [], False, False
    text = str(text)   # straight quotes: smartify() was removed 2026-09-13
    for i, blocks in enumerate(prose_blocks(text)):
        for k, (btext, marker) in enumerate(blocks):
            prefix = "•  " if marker == "- " else (marker + " " if marker else "")
            inset = text_width(prefix, size) if marker else 0.0
            lines = wrap_words(md_words(btext), size, width - inset)
            for j, runs in enumerate(lines):
                gap = 0.0
                if i or k:
                    if marker:
                        gap = size * 0.35 if prev_item else 0.0
                    else:
                        gap = 0.0 if prev_bold else size * 0.75
                if j:
                    gap = 0.0
                elif marker:
                    runs = [(prefix, False)] + runs
                out.append((runs, 0.0 if not j else inset, gap))
            prev_item = bool(marker)
            if not marker:
                spans = [s for s, b in md_spans(btext) if s]
                prev_bold = bool(spans) and all(b for s, b in md_spans(btext))
            else:
                prev_bold = False
    return out


def summary_lines(block, size, width):
    """Preformatted indented text, for anything the spec writes as a listing.

    Its shape carries meaning (numbered sections, dashed examples), so the
    source indentation is honoured and wrapped lines hang under it.
    """
    out = []
    for n, raw in enumerate(block.rstrip("\n").split("\n")):
        if not raw.strip():
            out.append(([], 0.0, 0.0))
            continue
        pad = len(raw) - len(raw.lstrip())
        indent = pad * size * (_HELV_W[" "] / 1000.0)
        bold = n == 0 or bool(re.match(r"\s*\d+\.\s", raw))
        hang = size * 0.75 if raw.lstrip().startswith("- ") else 0.0
        words = plain_words(raw.strip(), bold)
        for j, runs in enumerate(wrap_words(words, size, width - indent - hang)):
            out.append((runs, indent + (hang if j else 0.0), 0.0))
    return out


def lines_height(lines, size):
    return sum(gap + size * PDF_LEAD for _, _, gap in lines)


def draw_lines(page, lines, x, top, size, width, center=False):
    """Draw from baseline `top` downwards; returns the y left over."""
    y = top
    for runs, indent, gap in lines:
        y -= gap + size * PDF_LEAD
        if not runs:
            continue
        merged = []
        for text, bold in runs:            # one Tj per font change, not per word
            if merged and merged[-1][1] == bold:
                merged[-1][0] += text
            else:
                merged.append([text, bold])
        tx = x + (width - line_width(runs, size)) / 2 if center else x + indent
        for text, bold in merged:
            page.text(text, tx, y, size, bold)
            tx += text_width(text, size, bold)
    return y


def fit_prose(text, width, avail):
    """Biggest body size at which a page's prose still fits, and its lines.

    A page that overflows even the smallest size is reported and clipped rather
    than reflowed onto a second page: spilling would push the illustration off
    the facing page and desynchronise every spread after it. The fix is to cut
    prose, which is the same signal `compile` gives with <-- LONG.
    """
    for size in PDF_BODY:
        lines = prose_lines(text, size, width)
        if lines_height(lines, size) <= avail:
            return size, lines, True
    size = PDF_BODY[-1]
    return size, prose_lines(text, size, width), False


def paginate(lines, size, avail):
    """Split a long line list into page-sized chunks."""
    pages, cur, used = [], [], 0.0
    for ln in lines:
        h = ln[2] + size * PDF_LEAD
        if cur and used + h > avail:
            pages.append(cur)
            cur, used = [], 0.0
        cur.append(ln)
        used += h
    if cur:
        pages.append(cur)
    return pages


def place_contain(page, fp, x, y, w, h):
    """Largest centred placement of an image inside a box, aspect preserved."""
    _, iw, ih = page.pdf.image(fp)
    s = min(w / iw, h / ih)
    page.image(fp, x + (w - iw * s) / 2, y + (h - ih * s) / 2, iw * s, ih * s)


def resolve_run(name):
    """The run to draw images from: the named one, else the newest, else None.

    Newest by mtime, not by name. Run directories are usually timestamps but not
    always — a hand-named one like `smoketest` sorts after every `2026...` name,
    so sorting lexically handed the whole proof to whichever throwaway run
    happened to sort last. Directories with no JPEGs are skipped for the same
    reason `clean-empty-runs` deletes them: a gen that died before its first API
    call still leaves a directory behind, and it should not win.
    """
    if name:
        r = OUT / "runs" / name
        if not r.is_dir():
            sys.exit(f"no such run: {name}")
        return r
    runs = [p for p in (OUT / "runs").glob("*")
            if p.is_dir() and any(p.glob("*.jpg"))]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def pick_file(run, picks, name, cand=None):
    """Which render to lay out for `name`.

    A locked pick in picks.json wins outright — that is the point of locking.
    Nothing locked (the normal case mid-review, when every slot still has three
    candidates) falls back to `cand` if asked for, else the lowest-numbered one,
    so a draft sweep can be proofed without locking anything first.
    """
    p = picks.get(name)
    if p:
        fp = OUT / "runs" / str(p["run"]) / f"{name}_c{p['cand']}.jpg"
        if fp.exists():
            return fp
        print(f"  ! {name}: locked pick missing ({fp}) — falling back",
              file=sys.stderr)
    if not run:
        return None
    if cand:
        fp = run / f"{name}_c{cand}.jpg"
        if fp.exists():
            return fp
    cands = sorted(run.glob(f"{name}_c*.jpg"))
    return cands[0] if cands else None


def cand_num(fp):
    """The candidate number out of a `<name>_c<N>.jpg` path, as an int — c10
    sorts lexically before c2, so anything ordering candidates has to key on
    this rather than the filename string."""
    return int(fp.stem.rsplit("_c", 1)[-1])


def candidates_for(name):
    """Every rendered candidate for `name`, across every run, oldest run
    first and candidates in numeric order within a run. Used by `preview`'s
    click-to-cycle — a slot re-rolled across several runs (the usual case
    once a re-roll happens) should still show every candidate, not just the
    newest run's three."""
    hits = []
    for run_dir in sorted((OUT / "runs").glob("*")):
        hits.extend(sorted(run_dir.glob(f"{name}_c*.jpg"), key=cand_num))
    return hits


def aspect_of(spec, name):
    """Declared width/height for a slot or diagram, used to lay out a page
    whose art does not exist yet the way the render will lay out."""
    d = (spec.get("diagrams") or {}).get(name) or {}
    a, b = (float(x) for x in
            str(d.get("aspect_ratio") or spec.get("aspect_ratio", "2:3")).split(":"))
    return a / b

def page_diagrams(spec, bp, placed):
    """This manuscript page's diagrams, first-page-wins, in display order.

    Order is `inset_order:` (default 0) then id. sorted() alone would always
    put D10 left of D11, and the manuscript can ask for the reverse — a page
    can want D11 on the left and D10 on the right.
    """
    dias = spec.get("diagrams", {})
    hits = [d for d in dias
            if bp in (dias[d].get("pages") or []) and d not in placed]
    return sorted(hits, key=lambda d: (dias[d].get("inset_order", 0), d))


def draw_art_page(page, items, W, H, inset=0.0):
    """Lay out one page's art. items is [(label, path or None, aspect)].

    A single image fills the trim. Several share the page: in a row when they
    are portrait, since two 2:3 renders side by side read as a diptych, and in
    a column when they are landscape, which is how the 3:2 diagrams come out.
    `inset` shrinks the layout box by that many points on every side, so the
    art is centred inside the inset box and can never touch a page edge —
    that keeps a trimmed book in Lulu's exact-trim, no-bleed-needed category.
    Returns the labels that had no file, which are drawn as placeholders so the
    proof paginates the same before and after a render.
    """
    W, H = W - 2 * inset, H - 2 * inset
    shapes = []
    for _, fp, hint in items:
        if fp:
            _, iw, ih = page.pdf.image(fp)
            shapes.append(iw / ih)
        else:
            shapes.append(hint)
    row = sum(shapes) / len(shapes) < 1.0
    n = len(items)
    gut = 0.0 if n == 1 else 14.0
    cw = (W - gut * (n - 1)) / n if row else W
    ch = H if row else (H - gut * (n - 1)) / n
    absent = []
    for i, (label, fp, _) in enumerate(items):
        x = inset + (i * (cw + gut) if row else 0.0)
        y = inset + (0.0 if row else H - (i + 1) * ch - i * gut)
        if fp:
            place_contain(page, fp, x, y, cw, ch)
        else:
            absent.append(label)
            page.frame(x + 24, y + 24, cw - 48, ch - 48)
            note = f"{label} — not rendered yet"
            page.text(note, x + (cw - text_width(note, 11)) / 2, y + ch / 2, 11)
    return absent


def draw_diagram_strip(page, files, x, bottom, width, max_h):
    """Diagram insets in a row along `bottom`. Returns the height consumed."""
    gap = 12.0
    each = (width - gap * (len(files) - 1)) / len(files)
    tall = 0.0
    for fp in files:
        _, iw, ih = page.pdf.image(fp)
        tall = max(tall, each * ih / iw)
    used = min(tall, max_h)
    for i, fp in enumerate(files):
        place_contain(page, fp, x + i * (each + gap), bottom, each, used)
    return used


# ---------------------------------------------------------------- commands

@with_photo_root
def cmd_compile(spec, args):
    d = OUT / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    ids = [args.page] if args.page else sorted(spec["pages"])
    for pid in ids:
        txt = compile_page(spec, pid, strict=args.strict)
        (d / f"p{pid:02d}.txt").write_text(txt)
        w = len(txt.split())
        flag = "  <-- LONG, expect degraded geometry" if w > 400 else ""
        refs = refs_for(spec, pid)
        names = ",".join(f.stem.split("-")[0] for f in refs) or "none"
        print(f"p{pid:02d}  {w:>4} words  "
              f"{len(refs)} refs [{names}]  "
              f"{spec['pages'][pid]['distance']}{flag}")
    if not args.page:
        for did in sorted(spec["diagrams"]):
            (d / f"{did}.txt").write_text(compile_diagram(spec, did))
        print(f"\n{len(spec['diagrams'])} diagram prompts written")
    print(f"\n-> {d}")


def gen_jobs(spec, args):
    """(name, prompt, refs, aspect, size) for every render this invocation
    would ask for, and how many candidates of each.

    Shared by cmd_gen and its dry run on purpose: the dry run is only worth
    anything if it cannot disagree with the real thing about what was going to
    be sent.
    """
    size = spec["final_size"] if args.final else spec["draft_size"]
    n = 1 if args.final else args.n

    jobs = []
    if args.diagrams or args.diagram:
        # --diagram narrows to named ids; bare --diagrams is still all of them.
        # Worth having because a re-roll is usually one or two diagrams, and the
        # full sweep is 14 x N images.
        ids = args.diagram or sorted(spec["diagrams"])
        unknown = [d for d in ids if d not in spec["diagrams"]]
        if unknown:
            sys.exit(f"no such diagram(s): {', '.join(unknown)}")
        for did in ids:
            d = spec["diagrams"][did]
            jobs.append((did, compile_diagram(spec, did), [],
                         d.get("aspect_ratio", spec["aspect_ratio"]),
                         d.get("size", size)))
    else:
        ids = [args.page] if args.page else sorted(spec["pages"])
        for pid in ids:
            jobs.append((f"p{pid:02d}", compile_page(spec, pid, strict=args.strict),
                         refs_for(spec, pid), spec["aspect_ratio"], size))
    return jobs, n


def dry_run_gen(spec, args):
    """`gen` right up to the API call, written to disk instead of sent.

    Same prompt, same ref list, same model / aspect / size / thinking level:
    the whole request bar the image bytes and the POST. Writes each prompt as
    plain text plus one requests.json, both deterministic, so diffing two
    machines' output is diffing what would have been sent.

    Ref bytes are never written — they are the reference photos — only their
    names, via ref_label(). No image conversion happens either, so a dry run
    needs neither sips nor ImageMagick.
    """
    jobs, n = gen_jobs(spec, args)
    dest = pathlib.Path(args.dry_run_out).expanduser() if args.dry_run_out \
        else OUT / "dryrun"
    dest.mkdir(parents=True, exist_ok=True)

    requests = []
    for name, prompt, refs, aspect, sz in jobs:
        (dest / f"{name}.txt").write_text(prompt)
        for i in range(1, n + 1):
            requests.append({
                "name": name,
                "cand": i,
                "aspect_ratio": aspect,
                "size": sz,
                "thinking_level": spec["thinking_level"],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "refs": [ref_label(r) for r in refs],
            })

    manifest = {
        "book": BOOK.name,
        "command": "gen",
        "dry_run": True,
        "endpoint": ENDPOINT,
        "model": spec["model"],
        "requests": sorted(requests, key=lambda r: (r["name"], r["cand"])),
    }
    (dest / "requests.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"\n{len(requests)} request(s) over {len(jobs)} prompt(s) — "
          f"nothing sent, nothing uploaded")
    print(f"-> {dest}")


@with_photo_root
def cmd_gen(spec, args):
    if args.dry_run:
        return dry_run_gen(spec, args)

    run = OUT / "runs" / (args.run or time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, exist_ok=True)
    jobs, n = gen_jobs(spec, args)

    calls = []
    for name, prompt, refs, aspect, sz in jobs:
        (run / f"{name}.prompt.txt").write_text(prompt)
        for i in range(1, n + 1):
            out = run / f"{name}_c{i}.jpg"
            if out.exists():
                print(f"{name} c{i}  skip (exists)")
                continue
            calls.append((name, i, prompt, refs, aspect, sz, out))

    manifest = []
    manifest_lock = threading.Lock()

    def do_call(call):
        name, i, prompt, refs, aspect, sz, out = call
        try:
            jpg = call_api(spec["model"], prompt, refs, aspect, sz,
                           spec["thinking_level"])
            out.write_bytes(jpg)
            print(f"{name} c{i}  ok  {len(jpg)//1024} KB  {sz}")
            with manifest_lock:
                manifest.append({"name": name, "cand": i, "file": out.name,
                                 "size": sz, "refs": [r.name for r in refs]})
        except Exception as e:
            print(f"{name} c{i}  FAIL  {e}", file=sys.stderr)
        time.sleep(args.sleep)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(do_call, calls))

    (run / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n-> {run}")


def cmd_lock(spec, args):
    """Record the chosen render for a slot or a diagram.

    `pick_file` has always keyed picks.json by render name, so a diagram id is
    as lockable as a slot; only this command could not say one. The render is
    checked for existence here because the alternative is a typo that stays
    silent until `pdf` warns and quietly falls back to candidate 1.
    """
    if args.diagram:
        name = args.diagram
        if name not in (spec.get("diagrams") or {}):
            sys.exit(f"no such diagram: {name}")
    else:
        if args.page not in spec["pages"]:
            sys.exit(f"no such slot: {args.page}")
        name = f"p{args.page:02d}"

    fp = OUT / "runs" / args.run / f"{name}_c{args.cand}.jpg"
    if not fp.exists():
        sys.exit(f"no such render: {fp}")

    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}
    picks[name] = {"run": args.run, "cand": args.cand}
    picks_f.write_text(json.dumps(picks, indent=2, sort_keys=True))
    print(f"locked {name} -> run {args.run} candidate {args.cand}")

    # Computed, not hardcoded: which slots are anchors is a property of the
    # spec (whatever any other slot names in extra_refs), and it differs per
    # book.
    anchors = {pid for p in spec["pages"].values()
               for pid in p.get("extra_refs", [])}
    if args.page is not None and anchors:
        print(f"  (continuity anchors referenced by other slots: "
              f"{', '.join(sorted(anchors))} — if this slot feeds one, copy the "
              f"JPEG into refs/ under the name book.yaml expects)")


def cmd_pdf(spec, args):
    """Assemble the proof book: each manuscript page's prose and the art it
    calls for as a facing spread.

    The unit here is a MANUSCRIPT page, not an illustration slot — slots are
    numbered separately and several share a page (`book_page:`), so pages 5, 9
    and 12 put two renders on one recto while 2, 16 and 17 have no slot at all
    and face their diagrams instead. Everything stays two PDF pages per
    manuscript page, which is what keeps every spread's text and art paired.
    Which side is which is per-book (`spread:`): the default is prose verso /
    art recto; `spread: art-left` mirrors it — art verso, prose recto.

    Images come from the newest run unless --run says otherwise, and a locked
    pick in picks.json always beats a loose candidate.
    """
    W, H = page_size(spec)
    run = resolve_run(args.run)
    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}
    print(f"images: {'run ' + run.name if run else 'none yet — placeholders only'}"
          f"   locked: {len(picks)}"
          f"   unlocked: candidate {args.cand if args.cand else 1}")

    inner = W - 2 * PDF_MARGIN
    top = H - PDF_MARGIN
    floor = PDF_MARGIN + 20        # leaves room for the folio
    pdf = PDF(W, H, embed=args.lulu)
    fm = spec.get("front_matter") or {}

    # Title page: text only, title and byline. A render above the title read
    # as a second cover, not a title page, so nothing is placed above it —
    # `front_matter.cover:` now feeds only the wraparound cover (cmd_cover)
    # and the published index thumbnail (cover_file). A dedicated cover slot
    # still wants `book_page: 0` so the story loop below never also places it
    # on a recto — manuscript pages are 1-based, so nothing claims it. The
    # title page is also structural: one page ahead of the story is what puts
    # the FIRST story page on a verso, so each spread stays paired whichever
    # side the art is on (`spread:`).
    tp = pdf.page()
    title = fm.get("title") or "Untitled"
    title_top = H * 0.62
    y = draw_lines(tp, [(runs, 0.0, 0.0) for runs in
                        wrap_words(plain_words(title, True), 30, inner)],
                   PDF_MARGIN, title_top, 30, inner, center=True)
    if fm.get("byline"):
        draw_lines(tp, [(runs, 0.0, 0.0) for runs in
                        wrap_words(plain_words(fm["byline"]), 14, inner)],
                   PDF_MARGIN, y - 36, 14, inner, center=True)
    if fm.get("dedication"):
        dp = pdf.page()
        # Line breaks preserved: a blank source line becomes a gap, every
        # other line is its own line here (dedication_lines, not prose_lines).
        dlines = []
        for raw in dedication_lines(fm["dedication"]):
            if not raw:
                dlines.append(([], 0.0, 13 * 0.9))
                continue
            for runs in wrap_words(md_words(raw), 13, inner):
                dlines.append((runs, 0.0, 0.0))
        draw_lines(dp, dlines, PDF_MARGIN, H * 0.6, 13, inner, center=True)
    while len(pdf.pages) % 2 == 0:      # first story page starts a fresh spread
        pdf.page()

    slots = {}                     # manuscript page -> [slot id, ...]
    for pid in sorted(spec["pages"]):
        slots.setdefault(spec["pages"][pid]["book_page"], []).append(pid)

    # Which side of the spread carries the art. Default is prose verso / art
    # recto; art-left is per-book opt-in.
    art_left = spec.get("spread") == "art-left"

    # `text_spreads:` pairs text-only pages that FACE each other as one
    # spread (a numbered answer list continuing across the gutter) instead
    # of each facing a blank page. A pair only fires when BOTH pages are
    # text-only; every other text-only page keeps its blank partner, so the
    # spread parity of the pages after it cannot shift.
    text_pairs = dict(spec.get("text_spreads") or [])
    text_only, scratch = {}, set()
    for bp in sorted(spec["manuscript"]):
        d = page_diagrams(spec, bp, scratch)
        scratch.update(d)
        text_only[bp] = not slots.get(bp) and not d
    consumed = set()

    def prose_page(p):
        """One PDF page of centered prose plus folio. Shared by the normal
        one-prose-per-spread path and the text_spreads pair path."""
        pg = pdf.page()
        size, lines, fits = fit_prose(str(spec["manuscript"][p]), inner,
                                      top - floor)
        slack = max(0.0, top - floor - lines_height(lines, size)) / 2
        draw_lines(pg, lines, PDF_MARGIN, top - slack, size, inner)
        pg.text(str(p), (W - text_width(str(p), 10)) / 2, PDF_MARGIN * 0.7, 10)
        if not fits:
            tight.append(p)
            print(f"  ! manuscript p{p}: prose overflows the page even at "
                  f"{size}pt — cut text or split the page", file=sys.stderr)

    placed = set()                 # a diagram lands once, on its first page
    missing, tight = [], []
    for bp in sorted(spec["manuscript"]):
        if bp in consumed:         # the second half of a text_spreads pair
            continue
        dias = page_diagrams(spec, bp, placed)
        placed.update(dias)
        art = [f"p{pid:02d}" for pid in slots.get(bp, [])]
        # A diagram normally sits as an inset under the prose it explains. On a
        # page with no illustration slot it becomes the facing art instead —
        # otherwise the prose would face a blank sheet. A diagram with
        # share_recto: true joins the illustration on the recto page rather
        # than being a small prose-page inset.
        dias_spec = spec.get("diagrams", {})
        dias_share = [d for d in dias if dias_spec.get(d, {}).get("share_recto")]
        dias_inset = [d for d in dias if d not in set(dias_share)]
        recto = (art + dias_share) if art else (dias if dias else [])
        insets = [] if (not art or args.no_diagrams) else dias_inset

        pair = text_pairs.get(bp)
        if not recto and pair and pair > bp and text_only.get(pair):
            consumed.add(pair)
            prose_page(bp)
            prose_page(pair)       # one spread: two facing text pages
            continue

        if art_left:
            ap = pdf.page()      # art verso: emitted before the prose page
        pg = pdf.page()          # the prose page (verso by default)
        strip = 0.0
        files = [fp for fp in (pick_file(run, picks, d, args.cand)
                               for d in insets) if fp]
        if files:
            # A page's inset strip may ask for more or less than the default
            # 40% of the text column: `inset_max:` per diagram, largest wins.
            cap = max((dias_spec[d].get("inset_max", 0.40) for d in insets),
                      default=0.40)
            strip = draw_diagram_strip(pg, files, PDF_MARGIN, floor, inner,
                                       cap * (top - floor)) + 20
        avail = top - floor - strip
        # NOT sq() — prose_lines is what honours paragraph breaks, "- " bullets
        # and **emphasis**, and squashing the newlines first made all three of
        # them unreachable. Every manuscript page came out as one block
        # paragraph, which the activity pages (numbered step lists) cannot
        # survive. Folded `>` entries are unaffected: they carry no interior
        # blank line, so they still lay out as the single paragraph they are.
        size, lines, fits = fit_prose(str(spec["manuscript"][bp]), inner, avail)
        # centre the block in what is left over, so a short page reads as
        # deliberate white space rather than a page that stopped early
        slack = max(0.0, avail - lines_height(lines, size)) / 2
        draw_lines(pg, lines, PDF_MARGIN, top - slack, size, inner)
        pg.text(str(bp), (W - text_width(str(bp), 10)) / 2, PDF_MARGIN * 0.7, 10)
        if not fits:
            tight.append(bp)
            print(f"  ! manuscript p{bp}: prose overflows the page even at "
                  f"{size}pt — cut text or split the page", file=sys.stderr)

        if not art_left:
            ap = pdf.page()      # art recto: emitted after the prose page
        if recto:
            missing += draw_art_page(
                ap, [(n, pick_file(run, picks, n, args.cand), aspect_of(spec, n))
                     for n in recto], W, H,
                inset=PDF_MARGIN if spec.get("trim") else 0.0)

    for block in (spec.get("back_matter") or {}).values():
        lines = summary_lines(str(block), 11, inner)
        for chunk in paginate(lines, 11, top - floor):
            draw_lines(pdf.page(), chunk, PDF_MARGIN, top, 11, inner)

    suffix = f"-c{args.cand}" if args.cand else ""
    if args.lulu:
        suffix += "-lulu"
    out = pathlib.Path(args.out) if args.out else \
        OUT / (f"book-{run.name}{suffix}.pdf" if run else "book.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.write(out)
    print(f"{len(pdf.pages)} pages, {out.stat().st_size // 1024} KB, "
          f"{W / 72:.0f}x{H / 72:.0f} in trim")
    if missing:
        print(f"{len(missing)} placeholders: " + " ".join(missing))
    if tight:
        print("overflowing prose: " + " ".join(f"p{p}" for p in tight))
    print(f"\n-> {out}")
    if args.lulu:
        print("fonts embedded — the Lulu upload file")


def cmd_cover(spec, args):
    """The wraparound cover as one Lulu-ready page: back cover, spine, front
    cover, sized to the book's trim plus Lulu's edge allowance (0.875in on
    every outer edge — their template for a Letter Landscape book lands at
    24x10.25in). Lulu's four upload checks fall out by construction: one page,
    exact dimensions, no layers (this PDF has never had any), and --lulu
    embeds the fonts.

    Layout: the cover slot's art matted on the front, the title above it and
    the byline below it (the interior title page, outside), both rotated up
    the spine, and front_matter.description wrapped on the back — that field
    exists for this page.
    """
    run = resolve_run(args.run)
    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}
    fm = spec.get("front_matter") or {}
    title = fm.get("title") or "Untitled"
    byline = fm.get("byline") or ""
    desc = sq(fm.get("description") or "")

    cw, ch = page_size(spec)                 # one cover leaf, points
    spine = args.spine * 72
    W, H = 2 * cw + spine + 2 * COVER_EDGE, ch + 2 * COVER_EDGE
    back_x = COVER_EDGE
    spine_x = back_x + cw
    front_x = spine_x + spine
    leaf_y = COVER_EDGE

    pdf = PDF(W, H, embed=args.lulu)
    pg = pdf.page()
    missing = []

    # ---- front cover: title above, art matted, byline below — the interior
    # title page's arrangement, minus the page number. The whole block is
    # nudged 18pt (1/4in) toward the outer edge: on the physical case the
    # cover curves around the board's edge, so a block dead-centred on the
    # flat leaf reads a touch too close to the spine. Confirmed against Lulu's
    # wraparound previewer, whose guide band sits slightly outer-of-centre.
    title_h, byline_h = 90.0, 60.0           # reserved bands inside the leaf
    bx, bw = front_x + PDF_MARGIN + 18, cw - 2 * PDF_MARGIN
    art_box = (bx, leaf_y + PDF_MARGIN + byline_h, bw,
               ch - 2 * PDF_MARGIN - title_h - byline_h)
    fp = pick_file(run, picks, fm.get("cover") or "", args.cand)
    if fp:
        _, iw, ih = pdf.image(fp)
        ax, ay, aw, ah = art_box
        s = min(aw / iw, ah / ih)
        art_w, art_h = iw * s, ih * s
        pg.image(fp, ax + (aw - art_w) / 2, ay + (ah - art_h) / 2, art_w, art_h)
        ppi = iw / ((art_w or 1) / 72)
        if ppi < 250:
            print(f"  ! cover art at {int(ppi)} PPI — Lulu wants 300; "
                  f"render {fm.get('cover')} with --final and re-lock")
        art_top = ay + (ah - art_h) / 2 + art_h
        art_bot = ay + (ah - art_h) / 2
    else:
        missing.append(fm.get("cover") or "(no front_matter.cover)")
        ax, ay, aw, ah = art_box
        pg.frame(ax, ay, aw, ah)
        msg = f"{missing[-1]} — not rendered yet"
        pg.text(msg, ax + (aw - text_width(msg, 11)) / 2, ay + ah / 2, 11)
        art_top = ay + ah
        art_bot = ay
    draw_lines(pg, [(runs, 0.0, 0.0) for runs in
                    wrap_words(plain_words(title, True), 30, bw)],
               bx, art_top + 30, 30, bw, center=True)
    if byline:
        draw_lines(pg, [(runs, 0.0, 0.0) for runs in
                        wrap_words(plain_words(byline), 16, bw)],
                   bx, art_bot - 18, 16, bw, center=True)

    # ---- spine: title then author, rotated to read bottom to top. On a 0.25
    # spine Lulu may not print text at all, so this stays off if it won't fit.
    if spine >= 11:
        pg.vtext(title, spine_x + spine / 2 - 3, leaf_y + PDF_MARGIN, 10, True)
        bt = f"· {byline}" if byline else ""
        pg.vtext(bt, spine_x + spine / 2 - 3, leaf_y + ch - PDF_MARGIN
                 - text_width(bt, 8), 8)

    # ---- back cover: the description, wrapped and centred in the upper half.
    if desc:
        wrap_w = (back_x + cw - 2 * PDF_MARGIN - back_x) * 0.62
        lines = [(runs, 0.0, 0.0) for runs in wrap_words(plain_words(desc), 12, wrap_w)]
        draw_lines(pg, lines, back_x + (cw - wrap_w) / 2,
                   leaf_y + ch * 0.62, 12, wrap_w, center=True)

    out = pathlib.Path(args.out) if args.out else \
        OUT / ("cover-lulu.pdf" if args.lulu else "cover.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.write(out)
    letter = (round(W), round(H)) == (1728, 738)
    print(f"1 page, {out.stat().st_size // 1024} KB, "
          f"{W / 72:.2f}x{H / 72:.2f} in (spine {args.spine:.2f} in)"
          + (" — matches Lulu's 24x10.25in Letter Landscape template"
             if letter else
             " — check this against Lulu's per-book cover template"))
    if missing:
        print(f"placeholder: {missing[0]}")
    print(f"\n-> {out}")
    if args.lulu:
        print("fonts embedded — the Lulu upload file")


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_spans(text):
    """One run of text as HTML, honouring the same ** ** emphasis the PDF does."""
    return "".join(f"<strong>{html_escape(t)}</strong>" if bold else html_escape(t)
                   for t, bold in md_spans(text) if t)


def html_prose(text):
    """One manuscript page as HTML, structured the way prose_lines structures it
    for the PDF: paragraphs split on blank lines, a run of "- " lines as a
    bullet list, a run of "N. " lines as a numbered list, ** ** as bold. Both
    go through prose_blocks, so the Doc and the proof cannot disagree about
    where a paragraph ends or what is a list item. The number itself is
    stripped by prose_blocks, so <ol> supplies it — except across a paragraph
    break, where a fresh <ol> has to be told where to start.
    """
    out, run, kind, ol_start = [], [], None, 1

    def flush():
        nonlocal kind, ol_start
        if run:
            if kind == "ul":
                out.append("<ul>"
                           + "".join(f"<li>{html_spans(t)}</li>" for t in run)
                           + "</ul>")
            elif kind == "ol":
                out.append(f'<ol start="{ol_start}">'
                           + "".join(f"<li>{html_spans(t)}</li>" for t in run)
                           + "</ol>")
            run.clear()
        kind, ol_start = None, 1

    for blocks in prose_blocks(text):
        for btext, marker in blocks:
            if marker == "- ":
                if kind != "ul":
                    flush()
                    kind = "ul"
                run.append(btext)
            elif marker:
                if kind != "ol":
                    flush()
                    kind, ol_start = "ol", int(marker[:-1])
                run.append(btext)
            else:
                flush()
                # An all-bold paragraph is a heading (Activity titles, "Make
                # a Hypothesis"): the stylesheet attaches it flush to the
                # block after it, matching prose_lines' prev_bold gap rule.
                spans = [b for s, b in md_spans(btext) if s.strip()]
                cls = ' class="t"' if spans and all(spans) else ""
                out.append(f"<p{cls}>{html_spans(btext)}</p>")
        flush()
    return "\n".join(out)


def art_label(name, fp, picks):
    """Names the render under a figure. Which candidate and run it came from is
    the whole point: a comment in the Doc has to be traceable back to a file,
    and mid-review most slots are still showing an arbitrary candidate 1."""
    cand = fp.stem.rsplit("_c", 1)[-1]
    locked = " · locked" if name in picks else ""
    return f"{name} · candidate {cand} · run {fp.parent.name}{locked}"


# Geometry of the reference layout: US Letter landscape, 1in margins, Arial
# 18pt at a 24pt pitch, one beat per page, art centred on its own page. The
# paste below reproduces that: every section carries an inline page-break
# (Docs maps break-before onto its own paragraph property), and every image
# carries explicit width/height in px, which is what Docs reads to size a
# pasted image (96px = 1in, so 468pt tall = 624px).
DOC_W_PT, DOC_H_PT = 11 * 72, 8.5 * 72        # US Letter landscape
DOC_MARGIN_PT = 72                            # 1in all around
DOC_CW = DOC_W_PT - 2 * DOC_MARGIN_PT         # 648pt content width
DOC_CH = DOC_H_PT - 2 * DOC_MARGIN_PT         # 468pt content height
DOC_LINE_PT = 24                              # 18pt body at a 24pt pitch
DOC_GAP_PT = 12                               # paragraph space-after
DOC_DIAG_H_PT = 312                           # diagrams cap at 4 1/3in tall
DOC_DIAG_MIN_PT = 180                         # below this a diagram is clearer on
                                              # its own page than shrunk beside prose
PT_TO_PX = 96 / 72                            # CSS px per point at 96dpi


def doc_prose_height(text):
    """Points the page's prose occupies in the Docs layout: 24pt lines, 12pt
    between paragraphs, lists tight. wrap_words measures with the Helvetica
    widths, which are Arial's by design, so the line count is the one the Doc
    itself will arrive at. The caller adds a small slack against edge-case
    wraps that disagree by a line."""
    lines, paras = 0, 0
    hang = text_width("•  ", 18)
    for blocks in prose_blocks(text):
        paras += 1
        for btext, is_bullet in blocks:
            inset = hang if is_bullet else 0.0
            lines += len(wrap_words(md_words(btext), 18, DOC_CW - inset))
    return lines * DOC_LINE_PT + (paras - 1) * DOC_GAP_PT


def doc_img(fp, alt, out_dir, max_w_pt=DOC_CW, max_h_pt=DOC_CH):
    """An <img> that pastes into Docs at a fixed physical size: the render's
    aspect fitted inside (max_w_pt, max_h_pt), then converted to the px
    width/height attributes Docs sizes pasted images from."""
    nw, nh = jpeg_dims(fp)
    w_pt, h_pt = nw / PT_TO_PX, nh / PT_TO_PX
    s = min(max_w_pt / w_pt, max_h_pt / h_pt)
    w_px, h_px = round(w_pt * s * PT_TO_PX), round(h_pt * s * PT_TO_PX)
    src = html_escape(os.path.relpath(fp, out_dir))
    return (f'<img src="{src}" alt="{html_escape(alt)}" '
            f'width="{w_px}" height="{h_px}">', h_pt * s)


def cmd_html(spec, args):
    """The whole book as one browser page, for pasting into Google Docs.

    Docs' Markdown paste carries headings and emphasis but not images, and a Doc
    cannot reach a local file, so the way in is: open this in a browser, select
    all, copy, paste. The browser hands Docs the decoded bitmaps along with the
    markup and Docs uploads each one. Those uploads are snapshots — re-rolling a
    slot means re-pasting that page.

    The default output is the PRINT manuscript: no tool chrome (no "Page N"
    headings, no candidate/run captions), one beat per Doc page via hard page
    breaks, laid out to match the reference geometry — see the DOC_*
    constants. --review gives the old labelled output for mid-review Docs
    where a comment has to be traceable back to a render file.

    Gathering matches `pdf` exactly: slots grouped by book_page:, each diagram
    on the first page that calls for it. So the Doc and the proof agree about
    which art belongs to which beat, and a slot that is missing here is missing
    there. Prose goes through the same prose_blocks() the PDF lays out, so the
    Doc is not quietly a different text.
    """
    run = resolve_run(args.run)
    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}
    out = pathlib.Path(args.out) if args.out else OUT / "manuscript.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"images: {'run ' + run.name if run else 'none yet — placeholders only'}"
          f"   locked: {len(picks)}"
          f"   unlocked: candidate {args.cand if args.cand else 1}")

    slots = {}                     # manuscript page -> [slot id, ...]
    for pid in sorted(spec["pages"]):
        slots.setdefault(spec["pages"][pid]["book_page"], []).append(pid)

    fm = spec.get("front_matter") or {}
    if getattr(args, "review", False):
        return cmd_html_review(spec, args, run, picks, out, slots, fm)

    def page(blocks, first=False):
        """One Doc page. The inline break is what turns into a hard page break
        on paste; it is inline because the clipboard serialises element style
        attributes verbatim, while a <style> rule only reaches the Doc if the
        browser inlines it at copy time."""
        brk = '' if first else ' style="page-break-before:always;break-before:page"'
        body.append(f"<div class=page{brk}>" + "\n".join(blocks) + "</div>")

    def blank(n=1):
        """n empty paragraphs — the only vertical spacing that survives a
        paste, since Docs keeps empty paragraphs but not arbitrary margins."""
        return ['<p class=blank>&nbsp;</p>'] * n

    def figure(name, max_h_pt=DOC_CH, max_w_pt=DOC_CW):
        """A centred image paragraph for one rendered slot/diagram, or the
        missing-art placeholder. Returns (html, height_pt)."""
        fp = pick_file(run, picks, name, args.cand)
        if not fp:
            missing.append(name)
            return (f'<p class="c absent">{html_escape(name)}'
                    " — not rendered yet</p>", max_h_pt / 2)
        tag, h_pt = doc_img(fp, name, out.parent, max_w_pt, max_h_pt)
        return (f'<p class=c>{tag}</p>', h_pt)

    body, missing = [], []

    # ---- title page: title at the top, byline under it. Text-only, matching
    # the pdf title page — no cover image.
    tp = [f'<p class=title><strong>{html_escape(sq(fm.get("title") or "Untitled"))}'
          "</strong></p>"] + blank()
    if sq(fm.get("byline") or ""):
        tp.append(f'<p class=c>{html_escape(sq(fm["byline"]))}</p>')
    page(tp, first=True)

    if sq(fm.get("dedication") or ""):
        page([f'<p class=c>{dedication_html(fm["dedication"])}</p>'])

    placed = set()                 # a diagram lands once, on its first page
    for bp in sorted(spec["manuscript"]):
        dias = page_diagrams(spec, bp, placed)
        placed.update(dias)
        art = [f"p{pid:02d}" for pid in slots.get(bp, [])]
        # share_recto diagrams go on the art page, not as prose insets.
        dias_spec = spec.get("diagrams", {})
        dias_share = [d for d in dias
                      if dias_spec.get(d, {}).get("share_recto")]
        dias_inset = [d for d in dias if d not in set(dias_share)]

        # Prose page: the text, with this page's diagram(s) centred under it —
        # the same pairing pdf draws as prose-plus-inset on the verso. The
        # diagram is sized to share the page with the prose when it can stay
        # legible (the usual arrangement); when the prose
        # leaves less than DOC_DIAG_MIN_PT, the image keeps its full size and
        # flows onto the next page on its own, which is also acceptable.
        pp = [html_prose(str(spec["manuscript"][bp]))]
        prose_h = doc_prose_height(str(spec["manuscript"][bp])) + DOC_GAP_PT
        fit_h = DOC_CH - prose_h - DOC_GAP_PT
        if not args.no_diagrams and len(dias_inset) > 1:
            # Several diagrams on one page go side by side in a borderless
            # table — the only side-by-side structure a paste keeps. A
            # diagram's optional `label:` sits above its image inside the same
            # cell: one row is atomic, so labels can never be stranded on the
            # wrong side of a page break (the triptych
            # arrangement). The image width must leave room for the cells'
            # horizontal padding: a table even a point wider than the content
            # box makes Chrome scale the WHOLE printed document to fit.
            each_w = (DOC_CW - 2 * DOC_GAP_PT * len(dias_inset)) / len(dias_inset) - 2
            row_h = min(DOC_DIAG_H_PT, each_w) + DOC_LINE_PT
            cells = []
            for d in dias_inset:
                fp = pick_file(run, picks, d, args.cand)
                label = sq((spec["diagrams"][d].get("label") or ""))
                cap = f'<div class=c>{html_escape(label)}</div>' if label else ""
                if fp:
                    tag, _ = doc_img(fp, d, out.parent, each_w, each_w)
                    cells.append(f"<td>{cap}{tag}</td>")
                else:
                    missing.append(d)
                    cells.append(f'<td class=absent>{cap}{html_escape(d)}'
                                 " — not rendered yet</td>")
            tbl = (f'<table class=row style="page-break-inside:avoid;'
                   f'break-inside:avoid"><tr>{"".join(cells)}</tr></table>')
            if row_h <= fit_h:
                pp.append(tbl)
                page(pp)
            else:
                page(pp)
                page([tbl])
        elif not args.no_diagrams and dias_inset:
            max_h = min(DOC_DIAG_H_PT, fit_h) if fit_h >= DOC_DIAG_MIN_PT \
                else DOC_DIAG_H_PT
            fig, _ = figure(dias_inset[0], max_h_pt=max_h)
            pp.append(fig)
            page(pp)
        else:
            page(pp)

        # Art page: the slot's render alone, centred. The blank paragraphs
        # above it approximate vertical centring — a paste has no other
        # mechanism for it, and a full-height image needs none.
        for name in art:
            fig, h_pt = figure(name)
            pad = max(0, round((DOC_CH - h_pt) / 2 / DOC_LINE_PT))
            page(blank(pad) + [fig])
        # share_recto diagrams get their own full page, like the art.
        for d in dias_share:
            fig, h_pt = figure(d)
            pad = max(0, round((DOC_CH - h_pt) / 2 / DOC_LINE_PT))
            page(blank(pad) + [fig])

    for block in (spec.get("back_matter") or {}).values():
        page([f"<pre>{html_escape(str(block).rstrip())}</pre>"])

    title_text = html_escape(sq(fm.get("title") or "Untitled"))
    out.write_text(f"""<!doctype html><meta charset=utf-8>
<title>{title_text}</title>
<style>
 /* The reference geometry, set once here: the browser inlines these
    rules into the copied markup, which is how they follow the text into the
    Doc. Arial 18pt at a 24pt pitch, 12pt between paragraphs. */
 body{{font-family:Arial,sans-serif;font-size:18pt;line-height:24pt;margin:1in}}
 p{{margin:0 0 {DOC_GAP_PT}pt}}
 /* A list attaches flush to the paragraph before it, and
    an all-bold heading (class "t") attaches flush to the block after it. */
 p.t{{margin:0}}
 p:has(+ ul),p:has(+ ol){{margin:0}}
 ul{{margin:0 0 {DOC_GAP_PT}pt;padding-left:24pt}}
 li{{margin:0}}
 .c,.title{{text-align:center}}
 .blank{{margin:0}}
 img{{vertical-align:top}}
 .row{{margin:0 auto {DOC_GAP_PT}pt;border:none;border-collapse:collapse}}
 .row td{{border:none;text-align:center;padding:0 {DOC_GAP_PT}pt;vertical-align:top}}
 .absent{{border:1px dashed #999;padding:2em}}
 pre{{white-space:pre-wrap;margin:0}}
 /* Browser print preview shares the Doc's page shape, so File > Print in the
    browser paginates the way the pasted Doc will. */
 @page{{size:{DOC_W_PT / 72:g}in {DOC_H_PT / 72:g}in;margin:{DOC_MARGIN_PT // 72:.0f}in}}
 @media print{{body{{margin:0}}}}
</style>
{chr(10).join(body)}
""")
    kb = out.stat().st_size // 1024
    print(f"{len(spec['manuscript'])} manuscript pages, "
          f"{len(placed)} diagrams, {kb} KB of markup")
    if missing:
        print(f"{len(missing)} placeholders: " + " ".join(missing))
    print(f"\n-> {out}")
    print("Open in a browser, select all, copy, paste into the Doc.")
    print("The Doc carries the page shape, not the paste: File > Page setup > "
          "Landscape, Letter, 1in margins. Page numbers are Insert > Page\n"
          "numbers (page numbers bottom-right, title page unnumbered).")


def cmd_html_review(spec, args, run, picks, out, slots, fm):
    """The pre-print manuscript: every page headed "Page N" and every render
    captioned with its slot, candidate and run, so a comment in the review Doc
    is traceable back to a file. This was the only `html` output until the
    print layout became the default; --review keeps it available."""
    body = []
    # Text-only title page, matching the pdf. `front_matter.cover:` feeds the
    # wraparound cover and the published index thumbnail, nothing here.
    body.append(f"<h1>{html_escape(sq(fm.get('title') or 'Untitled'))}</h1>")
    if sq(fm.get("byline") or ""):
        body.append(f'<p class="byline">{html_escape(sq(fm["byline"]))}</p>')
    if sq(fm.get("dedication") or ""):
        body.append(f'<p class="dedication">{dedication_html(fm["dedication"])}</p>')

    placed = set()                 # a diagram lands once, on its first page
    missing = []
    for bp in sorted(spec["manuscript"]):
        dias = page_diagrams(spec, bp, placed)
        placed.update(dias)
        art = [f"p{pid:02d}" for pid in slots.get(bp, [])]

        body.append("<section>")
        body.append(f"<h2>Page {bp}</h2>")
        body.append(html_prose(str(spec["manuscript"][bp])))
        for name in art + ([] if args.no_diagrams else dias):
            fp = pick_file(run, picks, name, args.cand)
            if fp:
                src = html_escape(os.path.relpath(fp, out.parent))
                body.append(f'<figure><img src="{src}" alt="{name}">'
                            f"<figcaption>{art_label(name, fp, picks)}"
                            "</figcaption></figure>")
            else:
                missing.append(name)
                body.append('<figure class="absent"><figcaption>'
                            f"{name} — not rendered yet</figcaption></figure>")
        body.append("</section>")

    for block in (spec.get("back_matter") or {}).values():
        body.append(f"<pre>{html_escape(str(block).rstrip())}</pre>")

    title_text = html_escape(sq(fm.get("title") or "Untitled"))
    out.write_text(f"""<!doctype html><meta charset=utf-8>
<title>{title_text}</title>
<style>
 /* Layout only, and deliberately no font-family: whatever is set here follows
    the text into the Doc as an explicit override, and the Doc should keep its
    own styles. h1/h2 map onto Docs' own heading styles on paste. */
 body{{max-width:46em;margin:3em auto;padding:0 1.5em;line-height:1.6}}
 h1{{font-size:1.9em}}
 h2{{font-size:1.2em;margin-top:2.2em}}
 .byline,.dedication{{font-style:italic}}
 ul{{padding-left:1.4em}}
 figure{{margin:1.4em 0}}
 img{{width:100%;display:block}}
 figcaption{{color:#666;font-size:.8em;padding-top:.4em}}
 .absent figcaption{{border:1px dashed #bbb;padding:2.5em 1em;text-align:center}}
 p.t{{margin:0}}
 p:has(+ ul),p:has(+ ol){{margin:0}}
 pre{{white-space:pre-wrap}}
</style>
{chr(10).join(body)}
""")
    kb = out.stat().st_size // 1024
    print(f"{len(spec['manuscript'])} manuscript pages, "
          f"{len(placed)} diagrams, {kb} KB of markup (review labels)")
    if missing:
        print(f"{len(missing)} placeholders: " + " ".join(missing))
    print(f"\n-> {out}")
    print("Open in a browser, select all, copy, paste into the Doc.")


def cmd_preview(spec, args):
    """A browser mock-up of the print spread, one row per manuscript page —
    the same pairing `pdf` prints, without needing a PDF viewer to check it.
    Side order is per-book (`spread:`), matching cmd_pdf: prose-left by
    default, art on the left for `spread: art-left`.

    Gathering matches `pdf` and `html` exactly: slots grouped by book_page:,
    each diagram on the first page that calls for it, an inset under the art
    on a page that already has slot art, the full recto on a page that does
    not. All three can never quietly disagree about which art belongs to
    which beat.

    One notes row rides under each spread — a feedback textarea for the
    page's text and one per art piece, kept in localStorage. "Email feedback"
    opens a compose window with the notes and the un-locked picks as one
    block, and "Copy feedback" puts the same block on the clipboard for a
    reviewer whose mail client won't take a long mailto — that block is what
    a Claude session acts on, and it carries the `id: feedback` list and the
    picks.json fragment together, since a review is one thing and not two.
    The notes live here and not in `html` on purpose: the manuscript is for
    select-all-copy-paste into Docs and must stay clean, while this page is
    the review surface. Cycling an image likewise records the candidate left
    on screen as the browser's pick for that slot, and "Copy picks" exports
    just the picks.json fragment — the static page can't write picks.json
    itself, so applying it stays a deliberate step.
    """
    run = resolve_run(args.run)
    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}
    out = pathlib.Path(args.out) if args.out else OUT / "preview.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"images: {'run ' + run.name if run else 'none yet — placeholders only'}"
          f"   locked: {len(picks)}"
          f"   unlocked: candidate {args.cand if args.cand else 1}")

    # The recto box takes the printed page's shape from the same source the
    # PDF proof uses, so it can never disagree with the printed page.
    w, h = page_size(spec)
    trim_ratio = w / h   # width/height

    # Same per-book spread order the PDF prints (see cmd_pdf).
    art_left = spec.get("spread") == "art-left"
    divider = "left" if art_left else "right"   # the dashed seam between the pair

    slots = {}                     # manuscript page -> [slot id, ...]
    for pid in sorted(spec["pages"]):
        slots.setdefault(spec["pages"][pid]["book_page"], []).append(pid)

    fm = spec.get("front_matter") or {}
    body = ['<section class="titlepage">']
    body.append(f"<h1>{html_escape(sq(fm.get('title') or 'Untitled'))}</h1>")
    if sq(fm.get("byline") or ""):
        body.append(f'<p class="byline">{html_escape(sq(fm["byline"]))}</p>')
    if sq(fm.get("dedication") or ""):
        body.append(f'<p class="dedication">{dedication_html(fm["dedication"])}</p>')
    body.append("</section>")

    def cand_label(name, fp):
        """Like art_label, but checks THIS file against the lock rather than
        just whether the slot has one — art_label's "in picks" test is only
        correct when a single candidate is ever shown, and cycling shows
        every candidate for a slot side by side. Returns the label and the
        locked flag together, since the page's pick-recording script needs
        the flag per candidate to keep exports down to not-yet-locked picks.
        """
        cand = fp.stem.rsplit("_c", 1)[-1]
        p = picks.get(name)
        is_locked = bool(p) and str(p.get("run")) == fp.parent.name \
            and str(p.get("cand")) == cand
        return (f"{name} · candidate {cand} · run {fp.parent.name}"
                + (" · locked" if is_locked else ""), is_locked)

    def figure(name):
        """One <figure>, showing the current pick but carrying every rendered
        candidate for `name` in a data attribute so the page can cycle
        through them on tap/click — see the page's own <script>, which wires
        up pointer events rather than a plain onclick, so a scroll that
        happens to start on the image doesn't get mistaken for a tap. Each
        candidate also carries its run/cand/locked triple so the script can
        record a cycled-to candidate as the browser's pick for this slot and
        later export the picks.json fragment ("Copy picks").

        Only candidates matching the slot's declared orientation ride along:
        a book that changed aspect_ratio: (say 2:3 -> 3:2) keeps the old-shape
        renders on disk, and cycling onto one would offer — and now record —
        art that can never ship in this slot. Per-slot, not per-book: a 1:1
        diagram in a 3:2 book still filters against square.
        """
        locked_fp = pick_file(run, picks, name, args.cand)
        want = aspect_of(spec, name)
        rendered = candidates_for(name)
        all_fps = [fp for fp in rendered if same_orientation(fp, want)]
        if not all_fps:
            which = "landscape" if want > 1 else \
                "portrait" if want < 1 else "square"
            why = f"no {which} renders yet" if rendered else "not rendered yet"
            return None, (f'<figure class="absent"><figcaption>'
                          f"{name} — {why}</figcaption></figure>")
        if locked_fp and locked_fp in rendered and locked_fp not in all_fps:
            print(f"  ! {name}: locked pick {locked_fp.parent.name}/"
                  f"{locked_fp.name} has the wrong orientation for this slot"
                  f" — preview starts on the first matching candidate",
                  file=sys.stderr)
        # Start on the current pick, but every candidate stays one click away
        # in run/candidate order, wrapping back around to it.
        start = all_fps.index(locked_fp) if locked_fp in all_fps else 0
        ordered = all_fps[start:] + all_fps[:start]
        cands = []
        for fp in ordered:
            label, is_locked = cand_label(name, fp)
            cands.append({"src": html_escape(os.path.relpath(fp, out.parent)),
                          "label": label, "run": fp.parent.name,
                          "cand": int(fp.stem.rsplit("_c", 1)[-1]),
                          "locked": is_locked})
        n = len(cands)
        counter = f" · 1/{n} (click to cycle)" if n > 1 else ""
        return ordered[0], (
            f'<figure><img src="{cands[0]["src"]}" alt="{name}" '
            f'class="cyclable" data-name="{name}" '
            f"data-cands='{json.dumps(cands)}' "
            f'data-idx="0">'
            f'<figcaption>{cands[0]["label"]}{counter}</figcaption></figure>')

    def note_block(ident, label):
        """One feedback textarea, identified by the same name `--strict`,
        `lock` and the CLAUDE.md tables already use (a manuscript page number
        for text, a slot/diagram id for art) — so a note copied out of here
        can be pasted straight into a Claude session and acted on without a
        translation step."""
        return (f'<div class="note-block"><label>Notes — {label}</label>'
                f'<textarea class="note" data-id="{html_escape(ident)}" '
                'placeholder="Feedback on this one..."></textarea></div>')

    placed = set()                 # a diagram lands once, on its first page
    missing = []
    # Same text_spreads pairing the PDF prints (see cmd_pdf): the pair
    # renders as ONE spread row of two facing text panels.
    text_pairs = dict(spec.get("text_spreads") or [])
    text_only, scratch = {}, set()
    for bp in sorted(spec["manuscript"]):
        d = page_diagrams(spec, bp, scratch)
        scratch.update(d)
        text_only[bp] = not slots.get(bp) and not d
    consumed = set()
    for bp in sorted(spec["manuscript"]):
        if bp in consumed:         # the second half of a text_spreads pair
            continue
        dias = page_diagrams(spec, bp, placed)
        placed.update(dias)
        art = [f"p{pid:02d}" for pid in slots.get(bp, [])]
        dias_spec = spec.get("diagrams", {})
        dias_share = [d for d in dias if dias_spec.get(d, {}).get("share_recto")]
        dias_inset = [d for d in dias if d not in set(dias_share)]
        recto = (art + dias_share) if art else (dias if dias else [])
        insets = [] if (not art or args.no_diagrams) else dias_inset

        pair = text_pairs.get(bp)
        if not recto and pair and pair > bp and text_only.get(pair):
            consumed.add(pair)
            def text_panel(p):
                return (f'<div class="verso"><div class="folio">{p}</div>'
                        f'{html_prose(str(spec["manuscript"][p]))}</div>')
            body.append('<section class="spread text-pair">')
            body.append(text_panel(bp) + text_panel(pair))
            body.append("</section>")
            notes = [note_block(f"page{bp}", f"page {bp} text"),
                     note_block(f"page{pair}", f"page {pair} text")]
            body.append(f'<div class="spread-notes">{"".join(notes)}</div>')
            continue

        main_html, aspects = [], []
        for name in recto:
            fp, html_bit = figure(name)
            if not fp:
                missing.append(name)
            aspects.append(aspect_of(spec, name))
            main_html.append(html_bit)
        # Portrait renders side by side (a diptych), landscape ones stacked —
        # the same row/column call draw_art_page makes for the PDF.
        direction = "row" if (sum(aspects) / len(aspects) if aspects else 1) < 1 \
            else "column"

        inset_html = []
        for name in insets:
            fp, html_bit = figure(name)
            if not fp:
                missing.append(name)
            inset_html.append(html_bit)

        verso_html = (f'<div class="verso"><div class="folio">{bp}</div>'
                      f'{html_prose(str(spec["manuscript"][bp]))}</div>')
        recto_html = ('<div class="recto">'
                      f'<div class="main" style="flex-direction:{direction}">'
                      f'{"".join(main_html)}</div>')
        if inset_html:
            # Same `inset_max:` knob the PDF strip uses, mapped onto the
            # recto mock-up: the 0.40 default is the 22% the stylesheet
            # gives insets, so the default look is unchanged.
            cap = max((dias_spec[d].get("inset_max", 0.40) for d in insets),
                      default=0.40)
            recto_html += (f'<div class="insets" style="height:{cap * 55:.0f}%">'
                           f'{"".join(inset_html)}</div>')
        recto_html += "</div>"
        body.append('<section class="spread">')
        body.append(recto_html + verso_html if art_left
                    else verso_html + recto_html)
        body.append("</section>")
        # One notes row per spread, outside it — textareas inside the flex
        # spread would distort the page mock-up. Identifiers match what the
        # manuscript used to emit: page text first, then art in recto+inset
        # order (same set and order as html's art + dias).
        notes = [note_block(f"page{bp}", f"page {bp} text")]
        notes += [note_block(name, name) for name in recto + insets]
        body.append(f'<div class="spread-notes">{"".join(notes)}</div>')

    # The review checklist from book.yaml, printed at the top of the page so
    # the human comparing candidates sees what to check.
    checklist = (f"<p class=checklist>{sq(spec['review_checklist'])}</p>"
                 if spec.get("review_checklist") else "")

    # What the page's own script needs to send a review back: which book it
    # was (so a pasted-back review says so without being asked), the title for
    # the subject line, and the address "Email feedback" opens a compose
    # window for. `publish.feedback_email` is optional and empty by default —
    # the page is served from a world-readable bucket, so an address in it is
    # an address for a scraper. Empty is still usable: the button fills in the
    # subject and body and leaves the recipient for the reviewer to pick from
    # their own contacts. When it is set, the address is emitted split so the
    # page's HTML never contains it whole.
    feedback_email = str((spec.get("publish") or {}).get("feedback_email") or "")
    fb_user, _, fb_host = feedback_email.partition("@")
    js_title = json.dumps(sq(fm.get("title") or ""))
    js_slug = json.dumps(publish_slug(spec) or BOOK.name)
    js_mailto = "[" + ", ".join(json.dumps(p) for p in (fb_user, fb_host)
                                if p) + "].join(\"@\")"

    out.write_text(f"""<!doctype html><meta charset=utf-8>
<title>{html_escape(sq(fm.get("title") or "Untitled"))} — preview</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{{font-family:Helvetica,Arial,sans-serif;max-width:64em;margin:2em auto;
      padding:0 1.5em;line-height:1.5;color:#222;background:#eee}}
 .titlepage{{text-align:center;margin-bottom:3em}}
 .titlepage h1{{font-weight:normal;font-size:2em;margin:.6em 0 .2em}}
 .byline,.dedication{{font-style:italic;color:#555}}
 .spread{{display:flex;margin:1.5em 0;background:#fff;
         box-shadow:0 2px 10px rgba(0,0,0,.15)}}
 .verso,.recto{{flex:1 1 50%;padding:2em;box-sizing:border-box;position:relative}}
 /* Prose centers vertically, matching the PDF's slack/2 — a two-line page
    like ms 18 must not hug the top of the spread. */
 .verso{{border-{divider}:1px dashed #ccc;display:flex;flex-direction:column;
        justify-content:center}}
 .folio{{position:absolute;bottom:.6em;left:0;right:0;text-align:center;
        color:#999;font-size:.75em}}
 .recto{{display:flex;flex-direction:column;justify-content:center;
        aspect-ratio:{trim_ratio};padding:1em}}
 .recto .main{{display:flex;gap:8px;flex:1;min-height:0}}
 .recto .main figure{{flex:1;margin:0;display:flex;flex-direction:column;
                      min-height:0}}
 .recto .main img{{width:100%;height:100%;object-fit:contain;flex:1;min-height:0}}
 .recto .insets{{display:flex;gap:8px;margin-top:8px;height:22%}}
 .recto .insets figure{{flex:1;margin:0}}
 .recto .insets img{{width:100%;height:100%;object-fit:contain}}
 figcaption{{color:#888;font-size:.7em;text-align:center;padding-top:.3em}}
 .absent{{border:1px dashed #bbb;display:flex;align-items:center;
         justify-content:center;color:#999;font-size:.8em;min-height:6em}}
 img.cyclable{{cursor:pointer}}
 img.cyclable:hover{{outline:3px solid #ffb300;outline-offset:-3px}}
 .verso p{{margin:0 0 .9em}}
 .verso p.t{{margin:0}}
 .verso ul,.verso ol{{margin:0 0 .9em;padding-left:1.4em}}
 .verso p:has(+ ul),.verso p:has(+ ol){{margin:0}}
 .spread.text-pair .verso{{border:none}}
 .spread.text-pair .verso + .verso{{border-left:1px dashed #ccc}}
 @media (max-width:800px){{.spread{{flex-direction:column}}
   .verso{{border-{divider}:none;border-bottom:1px dashed #ccc}}}}
 @media (max-width:800px){{.spread.text-pair .verso + .verso
   {{border-left:none;border-top:1px dashed #ccc}}}}
 /* Review notes — one row under each spread, never inside it, so the mock-up
    keeps its printed-page shape. Display only; nothing here is published
    content. */
 #notes-bar{{position:sticky;top:0;background:#eee;padding:.6em 1em;
            margin:0 -1.5em 1.5em;border-bottom:1px solid #ccc;
            display:flex;flex-wrap:wrap;gap:.6em 1em;align-items:center;
            font-size:.85em;z-index:10}}
 #notes-bar button{{padding:.4em .9em;cursor:pointer}}
 .spread-notes{{display:flex;gap:1em;flex-wrap:wrap;margin:-.9em 0 1.5em}}
 .note-block{{flex:1;min-width:16em;font-size:.85em}}
 .note-block label{{display:block;color:#888;margin-bottom:.2em}}
 textarea.note{{width:100%;min-height:3.2em;font:inherit;font-size:.95em;
               padding:.4em;border:1px dashed #bbb;box-sizing:border-box;
               background:#fff}}
 .checklist{{background:#fff;padding:.8em 1.2em;font-size:.9em;margin:1.5em 0 0;
           box-shadow:0 2px 10px rgba(0,0,0,.15)}}
</style>
<div id="notes-bar">
 <label><input type=checkbox id=notes-toggle checked> Show notes</label>
 <button onclick="emailFeedback()">Email feedback</button>
 <button onclick="copyFeedback()">Copy feedback</button>
 <button onclick="exportPicks()">Copy picks</button>
<button onclick="clearNotes()">Clear notes</button>
 <span id="notes-status"></span>
</div>
{checklist}
{chr(10).join(body)}
<script>
 // Every rendered candidate rides along in data-cands; a tap/click advances
 // through them and wraps around. Cycling also RECORDS the choice: the
 // candidate left on
 // screen is kept in localStorage as this browser's pick for the slot and
 // restored on the next visit. "Copy picks" exports the recorded picks as a
 // picks.json fragment for a Claude session to merge in — a static page
 // can't write picks.json itself. The key drops the " — preview" title
 // suffix for no deeper reason than keeping one namespace per book.
 const PICKS_KEY = "bookgen-picks:" + document.title.replace(/ — preview$/, "");
 // What this page is, and where a review goes. Substituted at build time.
 // MAILTO is empty unless book.yaml sets publish.feedback_email, in which
 // case "Email feedback" opens a compose window already addressed; empty
 // still works, it just leaves the recipient for the reviewer to pick.
 const TITLE = {js_title};
 const SLUG = {js_slug};
 const MAILTO = {js_mailto};
 function savedPicks() {{
   try {{ return JSON.parse(localStorage.getItem(PICKS_KEY) || "{{}}"); }}
   catch (e) {{ return {{}}; }}
 }}
 function showCand(img, i) {{
   const cands = JSON.parse(img.dataset.cands);
   img.dataset.idx = i;
   img.src = cands[i].src;
   const cap = img.closest("figure").querySelector("figcaption");
   if (cap) cap.textContent = cands[i].label +
     (cands.length > 1 ? " · " + (i + 1) + "/" + cands.length +
      " (click to cycle)" : "");
 }}
 function recordCand(img) {{
   const cands = JSON.parse(img.dataset.cands);
   const c = cands[parseInt(img.dataset.idx, 10)];
   const picks = savedPicks();
   picks[img.dataset.name] = {{run: c.run, cand: c.cand}};
   localStorage.setItem(PICKS_KEY, JSON.stringify(picks));
 }}
 function cycleCand(img) {{
   const cands = JSON.parse(img.dataset.cands);
   if (cands.length < 2) return;
   showCand(img, (parseInt(img.dataset.idx, 10) + 1) % cands.length);
   recordCand(img);
 }}
 // Only picks that differ from what's already locked — re-exporting an
 // applied pick would just re-lock it, so the fragment stays minimal. Shared
 // with the feedback payload below, which carries the identical fragment.
 function chosenPicks() {{
   const picks = savedPicks();
   const chosen = {{}};
   document.querySelectorAll("img.cyclable").forEach(img => {{
     const sel = picks[img.dataset.name];
     if (!sel) return;
     const c = JSON.parse(img.dataset.cands)
       .find(x => x.run === sel.run && x.cand === sel.cand);
     if (c && !c.locked) chosen[img.dataset.name] = sel;
   }});
   const ordered = {{}};
   Object.keys(chosen).sort().forEach(n => {{ ordered[n] = chosen[n]; }});
   return ordered;
 }}
 function exportPicks() {{
   const status = document.getElementById("notes-status");
   const ordered = chosenPicks();
   const names = Object.keys(ordered);
   if (!names.length) {{
     status.textContent = "No new picks — nothing cycled, or every pick already locked";
     return;
   }}
   const text = JSON.stringify(ordered, null, 2);
   navigator.clipboard.writeText(text).then(
     () => {{ status.textContent = "Copied " + names.length + " pick(s) — paste into Claude"; }},
     () => {{ window.prompt("Copy this and paste into Claude:", text); }});
 }}
 // Pointer events, not onclick: on a touch screen a scroll that happens to
 // start on an image still delivers a click in some browsers once the
 // finger lifts, which was cycling images out from under someone just
 // scrolling the page. A tap only counts if the pointer barely moved and
 // came up quickly; a scroll/drag moves it, or the browser cancels the
 // pointer outright once it takes over for scrolling — either way, no cycle.
 document.querySelectorAll("img.cyclable").forEach(img => {{
   let start = null;
   img.addEventListener("pointerdown", e => {{
     start = {{x: e.clientX, y: e.clientY, t: Date.now()}};
   }});
   img.addEventListener("pointerup", e => {{
     if (!start) return;
     const moved = Math.hypot(e.clientX - start.x, e.clientY - start.y);
     const elapsed = Date.now() - start.t;
     start = null;
     if (moved < 10 && elapsed < 500) cycleCand(img);
   }});
   img.addEventListener("pointercancel", () => {{ start = null; }});
 }});
 // Restore this browser's picks: jump each image to the recorded candidate.
 // A recorded pick whose run/candidate is gone (the run was re-rolled away)
 // is dropped — it can't be shown, so it can't be the pick.
 const restored = savedPicks();
 let pruned = false;
 document.querySelectorAll("img.cyclable").forEach(img => {{
   const sel = restored[img.dataset.name];
   if (!sel) return;
   const cands = JSON.parse(img.dataset.cands);
   const i = cands.findIndex(x => x.run === sel.run && x.cand === sel.cand);
   if (i < 0) {{ delete restored[img.dataset.name]; pruned = true; return; }}
   if (i > 0) showCand(img, i);
 }});
 if (pruned) localStorage.setItem(PICKS_KEY, JSON.stringify(restored));

 // Review notes. The key drops this page's " — preview" title suffix so it
 // matches the one the manuscript page used — localStorage is per-origin, so
 // notes already saved from the manuscript carry over when both are served
 // from the same site.
 const NOTES_KEY = "bookgen-notes:" + document.title.replace(/ — preview$/, "");
 function notes() {{ return document.querySelectorAll("textarea.note"); }}
 function saveNotes() {{
   const data = {{}};
   notes().forEach(a => {{ if (a.value.trim()) data[a.dataset.id] = a.value; }});
   localStorage.setItem(NOTES_KEY, JSON.stringify(data));
 }}
 function loadNotes() {{
   let saved = {{}};
   try {{ saved = JSON.parse(localStorage.getItem(NOTES_KEY) || "{{}}"); }} catch (e) {{}}
   notes().forEach(a => {{ if (saved[a.dataset.id]) a.value = saved[a.dataset.id]; }});
 }}
 // The feedback payload: one block, not two. A re-roll request ("p03 needs a
 // wider shot") and a pick ("p03 -> run/cand 2") are the same review, and both
 // are addressed by the identifier the rest of the tool already uses — a
 // manuscript page number for the prose, a slot or diagram id for the art — so
 // nothing has to be translated before it can be acted on. The picks half is
 // byte-identical to what "Copy picks" exports, so it still merges into
 // picks.json verbatim; the notes half is the `id: feedback` list a Claude
 // session reads.
 function feedbackText(compact) {{
   const lines = [];
   notes().forEach(a => {{
     const v = a.value.trim();
     if (v) lines.push(a.dataset.id + ": " + v);
   }});
   const picks = chosenPicks();
   return ["Book: " + SLUG, "Title: " + TITLE, "",
           "--- notes ---",
           lines.length ? lines.join("\\n\\n") : "(none)",
           "", "--- picks ---",
           Object.keys(picks).length
             ? JSON.stringify(picks, null, compact ? 0 : 2) : "(none)"
          ].join("\\n");
 }}
 // A mailto: body rides in the URL, so an over-long review is TRUNCATED by the
 // mail client rather than refused — silently, which is the bad kind of
 // failure, and a 30-slot book's worth of notes can reach it. Past this many
 // characters the button declines and points at "Copy feedback" instead.
 const MAILTO_MAX = 1800;
 function feedbackMailto() {{
   return "mailto:" + MAILTO + "?subject=" +
     encodeURIComponent("Book review — " + TITLE) +
     "&body=" + encodeURIComponent(feedbackText(true));
 }}
 function emailFeedback() {{
   const status = document.getElementById("notes-status");
   const url = feedbackMailto();
   if (url.length > MAILTO_MAX) {{
     status.textContent = "Too long for an email link — use Copy feedback";
     return;
   }}
   window.location.href = url;
   status.textContent = "Opening your mail app…";
 }}
 function copyFeedback() {{
   const status = document.getElementById("notes-status");
   const text = feedbackText(false);
   navigator.clipboard.writeText(text).then(
     () => {{ status.textContent = "Copied — paste into an email or a Claude session"; }},
     () => {{ window.prompt("Copy this:", text); }});
 }}
function clearNotes() {{
  notes().forEach(a => a.value = "");
  localStorage.removeItem(NOTES_KEY);
  document.getElementById("notes-status").textContent = "Notes cleared";
}}
 notes().forEach(a => a.addEventListener("input", saveNotes));
 document.getElementById("notes-toggle").addEventListener("change", e => {{
   document.body.classList.toggle("hide-notes", !e.target.checked);
 }});
 loadNotes();
</script>
""")
    kb = out.stat().st_size // 1024
    print(f"{len(spec['manuscript'])} spreads, {kb} KB of markup")
    if missing:
        print(f"{len(missing)} placeholders: " + " ".join(missing))
    print(f"\n-> {out}")
    print("Per-page/per-image note fields are built in — \"Email feedback\" "
          "sends the notes and any un-locked picks as one block; \"Copy "
          "feedback\" puts the same block on the clipboard. Cycling an image "
          "records the pick in the browser; \"Copy picks\" exports just the "
          "picks.json fragment.")


# ------------------------------------------------------------
# Publishing to S3.
#
# The bucket is world-readable by policy and holds no secrets, so this is a
# plain static-site upload: no signing, no CloudFront, no build step. Two
# things about the target are load-bearing and easy to get wrong:
#
#  - Object ownership is BucketOwnerEnforced, which DISABLES ACLs. Passing
#    --acl public-read is a hard error, not a no-op. Public access comes from
#    the bucket policy and nothing needs to ask for it per object.
#  - There is no website configuration, so <Slug>/ on its own is a 404. The
#    manuscript goes up as index.html to make a clean URL work, and again as
#    manuscript.html because links to that key have already been shared.
#
# Shells out to the aws CLI rather than importing boto3, for the same reason
# prepare_ref() shells out to sips or ImageMagick: this file is stdlib + pyyaml
# and adding a dependency to move some files is a bad trade.
# ------------------------------------------------------------
S3_BASE = f"https://{BUCKET}.s3.us-west-1.amazonaws.com"
COVER_PX = 600       # longest edge of a root-index thumbnail

# Cache-Control on upload, which matters more here than on a normal static
# site because the objects are REWRITTEN IN PLACE and a reviewer holds the URL.
# With no Cache-Control at all S3 sends none, and the browser falls back to
# heuristic freshness — roughly 10% of the object's age, which is days for a
# book that has been up a few weeks. That is a reviewer clicking through a
# spread from before the re-roll and reporting on art that no longer exists.
# Run directories are the opposite case: a run name is a timestamp and a render
# never changes once written, so those are safe to cache forever, which is also
# what makes cycling candidates instant on a second visit.
CACHE_HTML = "no-cache"
CACHE_RUNS = "public,max-age=31536000,immutable"


def publish_slug(spec):
    """The book's top-level prefix in the bucket, or None if unpublishable.

    Spelled out in book.yaml rather than derived from the title: a title is
    prose and may change, while the prefix is a URL that has already been
    shared, so any derivation rule is a link that breaks silently.
    """
    return ((spec.get("publish") or {}).get("slug") or "").strip() or None


def aws(*argv, dry=False):
    """Run one aws CLI command, streaming its output. Returns success."""
    cmd = ["aws"] + [str(a) for a in argv]
    if dry:
        print("   would run: " + " ".join(cmd))
        return True
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"  ! aws exited {r.returncode}: {' '.join(cmd)}", file=sys.stderr)
    return r.returncode == 0


def cover_image(spec, run, picks, cand):
    """The slot to use as this book's thumbnail on the root index.

    front_matter.cover when set; otherwise the lowest-numbered slot, which is
    the closest thing a book with no cover: key has to one.
    """
    fm = spec.get("front_matter") or {}
    name = sq(fm.get("cover") or "")
    if not name and spec.get("pages"):
        name = f"p{min(spec['pages']):02d}"
    return pick_file(run, picks, name, cand) if name else None


def make_thumb(fp):
    """A COVER_PX-longest-edge JPEG copy of fp, next to the manuscript."""
    dst = OUT / "cover.jpg"
    if not to_jpeg(fp, dst, COVER_PX):
        print(f"  ! no working image converter (sips, or ImageMagick) — "
              f"uploading {fp.name} at full size", file=sys.stderr)
        return fp
    return dst


def dir_size(d):
    """(file count, bytes) under d, recursively."""
    files = [f for f in d.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def referenced_runs(spec, run, picks, cand, no_diagrams=False):
    """Every run directory the manuscript's images actually live in.

    Not simply resolve_run(). picks.json is keyed per slot and a book can hold
    picks from several runs at once — a single re-rolled slot leaves one book
    with most of its images from one run and one from another. Publishing only
    the newest run would leave those images broken, and broken in the quiet
    way: the HTML is valid, the keys just are not there.

    Gathers exactly what cmd_html() renders, so the two cannot disagree.
    """
    names = [f"p{pid:02d}" for pid in sorted(spec.get("pages") or {})]
    if not no_diagrams:
        names += sorted(spec.get("diagrams") or {})
    dirs = {}
    for name in names:
        fp = pick_file(run, picks, name, cand)
        if fp:
            dirs.setdefault(fp.parent.resolve(), set()).add(name)
    return dirs


def cmd_publish(spec, args):
    """Upload one book to S3 as a publicly readable page.

    Rebuilds the manuscript first, so what goes up can never be stale against
    picks.json. Then the whole run directory goes with it — every candidate
    and the prompts, not just the images the manuscript
    references. That is a few times the bytes, and it is what would let a
    review page offer "choose this one" over the alternatives later without
    re-uploading anything.

    Add-only. No --delete, so re-publishing after a re-lock rewrites the
    manuscript and leaves the run alone.

    --review-only refreshes just the review page (preview.html and the runs it
    cycles through) and leaves the book itself alone. That decouples the
    reviewer's URL from the one command that goes public, which matters twice:
    a book still in draft can have a review link without its manuscript being
    published, and the link can be refreshed after every re-roll instead of
    only when the book is ready. Still --yes gated, because the renders land in
    the same world-readable bucket.
    """
    slug = publish_slug(spec)
    if not slug:
        sys.exit(f"{BOOK.name} has no publish.slug in book.yaml — "
                 "add one to say where in the bucket it goes")

    # --dry-run wins over --yes unconditionally, so a script that carries both
    # can never be the thing that publishes by accident.
    if args.dry_run and args.yes:
        print("--dry-run given with --yes: nothing will be uploaded.")
    if args.dry_run:
        args.yes = False

    review_only = args.review_only

    html = None
    if not review_only:
        cmd_html(spec, args)                  # never publish a stale manuscript
        html = pathlib.Path(args.out) if args.out else OUT / "manuscript.html"
    # preview.html is a separate output from html's --out, always the default
    # path — --out on this command has only ever meant "the manuscript path",
    # and reusing it here would make `publish --out foo.html` silently
    # overwrite foo.html with the preview instead of the manuscript.
    cmd_preview(spec, argparse.Namespace(run=args.run, cand=args.cand, out=None,
                                         no_diagrams=args.no_diagrams))
    preview = OUT / "preview.html"
    run = resolve_run(args.run)
    if not run:
        sys.exit("nothing rendered yet — no run to publish")
    picks_f = OUT / "picks.json"
    picks = json.loads(picks_f.read_text()) if picks_f.exists() else {}

    thumb = None
    if not review_only:
        cover = cover_image(spec, run, picks, args.cand)
        thumb = make_thumb(cover) if cover else None

    dest = f"s3://{args.bucket}/{slug}"
    # In review mode the preview page is the only thing served, so what it can
    # cycle through IS the upload set. Otherwise it is the manuscript's images,
    # widened below.
    rundirs = {} if review_only else \
        referenced_runs(spec, run, picks, args.cand, args.no_diagrams)
    if not review_only and not rundirs:
        sys.exit("the manuscript references no images — nothing to publish")
    # preview.html's click-to-cycle embeds every rendered candidate for every
    # slot, across every run, as data-cands srcs. referenced_runs only covers
    # runs that supply a picked or fallback image, so without this a run that
    # exists only as cycling alternatives (a fresh re-roll awaiting review)
    # would 404 the moment someone clicks past the first candidate. Scanning
    # the just-built artifact — not recomputing — guarantees the sync set and
    # the page can never disagree about what is reachable.
    for m in re.finditer(r"runs/([^/'\"\\]+)/", preview.read_text()):
        d = (OUT / "runs" / m.group(1)).resolve()
        if d.is_dir():
            rundirs.setdefault(d, set())
    if not rundirs:
        sys.exit("the preview references no renders — nothing to review")

    review_url = f"{S3_BASE}/{slug}/preview.html"
    print(f"\n{'review page for' if review_only else 'publishing'} "
          f"{BOOK.name} -> {dest}/")
    if html:
        print(f"  {html.name:<20} -> {slug}/index.html and {slug}/manuscript.html")
    print(f"  {preview.name:<20} -> {slug}/preview.html")
    if thumb:
        print(f"  {'cover':<20} -> {slug}/cover.jpg  (from {cover.name})")
    total = 0
    for d in sorted(rundirs):
        n, nbytes = dir_size(d)
        total += nbytes
        in_use = f"{len(rundirs[d])} in use" if rundirs[d] else "cycle only"
        print(f"  runs/{d.name:<15} -> {slug}/runs/{d.name}/"
              f"  ({n} files, {nbytes / 1e6:.0f} MB, {in_use})")
    print(f"  {total / 1e6:.0f} MB total")

    if not args.yes:
        print("\nDry run. This is public the moment it lands — anyone with the "
              "URL can read it,\nand it may be cached or indexed even if later "
              "deleted. Re-run with --yes.")
        print(f"\nreview: {review_url}")
        return

    ok = True
    for d in sorted(rundirs):
        ok &= aws("s3", "sync", str(d), f"{dest}/runs/{d.name}/",
                  "--cache-control", CACHE_RUNS, "--only-show-errors")
    if html:
        ok &= aws("s3", "cp", str(html), f"{dest}/index.html",
                  "--cache-control", CACHE_HTML, "--only-show-errors")
        ok &= aws("s3", "cp", str(html), f"{dest}/manuscript.html",
                  "--cache-control", CACHE_HTML, "--only-show-errors")
    ok &= aws("s3", "cp", str(preview), f"{dest}/preview.html",
              "--cache-control", CACHE_HTML, "--only-show-errors")
    if thumb:
        ok &= aws("s3", "cp", str(thumb), f"{dest}/cover.jpg",
                  "--cache-control", CACHE_HTML, "--only-show-errors")
    if not ok:
        sys.exit("publish failed — see the aws errors above")
    print(f"\nreview: {review_url}")
    print("  Send that URL. Click an image to cycle candidates, type notes "
          "under a\n  spread, then \"Email feedback\".")
    if not review_only:
        print(f"book:   {S3_BASE}/{slug}/index.html")


# The bucket-root author page. A plain string — not an f-string — so the CSS
# braces need no escaping. __CARDS__ is replaced with the joined book cards.
_AUTHOR_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Books — bookgen</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Illustrated picture books compiled from book.yaml specs by the bookgen pipeline.">
<meta property="og:title" content="Books — bookgen">
<meta property="og:description" content="Illustrated picture books compiled from book.yaml specs by the bookgen pipeline.">
<meta property="og:type" content="website">
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --paper:#faf6ee;--ink:#3d2e1e;--ink-light:#6b5d4a;--ink-muted:#9a8d77;
    --accent:#b5562d;--accent-dark:#8f4220;--rule:#e4ddd0;
  }
  html{scroll-behavior:smooth}
  body{
    font-family:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Georgia,serif;
    background:var(--paper);color:var(--ink);line-height:1.65;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  }
  a{color:inherit}
  .hero{text-align:center;padding:5rem 1.5rem 3.5rem;max-width:40em;margin:0 auto}
  .hero h1{font-size:clamp(2.2rem,6vw,3.4rem);font-weight:400;letter-spacing:-.015em;line-height:1.1}
  .hero .tagline{font-size:1.15rem;color:var(--ink-light);font-style:italic;margin-top:.4rem}
  .hero .divider{width:3rem;height:2px;background:var(--accent);margin:1.8rem auto}
  .hero .intro{font-size:1.05rem;color:var(--ink-light);max-width:32em;margin:0 auto}
  .books{max-width:54em;margin:0 auto;padding:1rem 1.5rem 4rem}
  .section-label{text-align:center;font-size:.78rem;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-muted);margin-bottom:3rem}
  .book-card{display:grid;grid-template-columns:200px 1fr;gap:2.5rem;align-items:start;padding-bottom:4rem;margin-bottom:4rem;border-bottom:1px solid var(--rule)}
  .book-card:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
  .book-card .cover{display:block}
  .book-card .cover img{
    width:100%;height:auto;display:block;border-radius:3px;
    box-shadow:0 8px 28px rgba(61,46,30,.18),0 2px 8px rgba(61,46,30,.1);
    transition:transform .3s ease,box-shadow .3s ease;
  }
  .book-card .cover:hover img{
    transform:translateY(-3px);
    box-shadow:0 12px 36px rgba(61,46,30,.22),0 4px 12px rgba(61,46,30,.12);
  }
  .book-card .info h2{font-size:1.5rem;font-weight:400;line-height:1.25;margin-bottom:.2rem}
  .book-card .info h2 a{color:var(--ink);text-decoration:none}
  .book-card .info h2 a:hover{color:var(--accent)}
  .book-card .info .byline{color:var(--ink-muted);font-size:.95rem;margin-bottom:1rem}
  .book-card .info .description{color:var(--ink-light);margin-bottom:1.2rem}
  .book-card .info .dedication{
    border-left:3px solid var(--accent);padding:.3rem 0 .3rem 1rem;
    font-style:italic;color:var(--ink-light);margin-bottom:1.5rem;font-size:.95rem;
  }
  .book-card .info .links{display:flex;gap:1.8rem}
  .book-card .info .links a{
    color:var(--accent);text-decoration:none;font-size:.95rem;
    border-bottom:1px solid transparent;transition:border-color .2s;
  }
  .book-card .info .links a:hover{border-bottom-color:var(--accent)}
  footer{text-align:center;padding:3rem 1.5rem;color:var(--ink-muted);font-size:.85rem;border-top:1px solid var(--rule)}
  @media(max-width:620px){
    .hero{padding:3.5rem 1.5rem 2.5rem}
    .book-card{grid-template-columns:1fr;gap:1.5rem}
    .book-card .cover{max-width:220px;margin:0 auto}
    .book-card .info{text-align:center}
    .book-card .info .links{justify-content:center}
    .book-card .info .dedication{text-align:left}
  }
</style>
</head>
<body>
  <header class="hero">
    <h1>Books</h1>
    <p class="tagline">Illustrated picture books, built with bookgen</p>
    <div class="divider"></div>
    <p class="intro">Each book below was compiled from a <code>book.yaml</code> spec and rendered by the pipeline in this repository. Open one to read it, or to see the print spread it came from.</p>
  </header>
  <main class="books">
    <p class="section-label">The Books</p>
__CARDS__
  </main>
  <footer>Built with bookgen</footer>
</body>
</html>
"""


def cmd_publish_index(spec, args):
    """Write and upload the bucket-root index of every publishable book.

    The one command that is deliberately cross-book, and so the one that takes
    no --book. It reads each books/<slug>/book.yaml directly rather than going
    through resolve_book(), which binds the globals to a single book.

    Every book with a publish.slug appears, ordered by its `sequence:` (books
    without one sort last, then by title).
    """
    # --dry-run wins over --yes unconditionally, same as cmd_publish.
    if args.dry_run and args.yes:
        print("--dry-run given with --yes: nothing will be uploaded.")
    if args.dry_run:
        args.yes = False
    books = []
    for d in sorted(BOOKS.glob("*")):
        if not (d / "book.yaml").is_file():
            continue
        s = yaml.safe_load((d / "book.yaml").read_text())
        pslug = publish_slug(s)
        if not pslug:
            print(f"  - {d.name}: no publish.slug, skipped")
            continue
        fm = s.get("front_matter") or {}
        title = sq(fm.get("title") or d.name)
        books.append({
            "slug": pslug,
            "title": title,
            "byline": sq(fm.get("byline") or ""),
            "description": sq(fm.get("description") or ""),
            "dedication": sq(fm.get("dedication") or ""),
            "sequence": s.get("sequence") or 99,
        })

    if not books:
        sys.exit("no book has a publish.slug — nothing to index")

    books.sort(key=lambda b: (b["sequence"], b["title"]))

    cards = []
    for b in books:
        se = html_escape(b["slug"])
        te = html_escape(b["title"])
        by = html_escape(b["byline"])
        de = html_escape(b["description"])
        dd = html_escape(b["dedication"])
        byline_html = f'<p class="byline">{by}</p>' if b["byline"] else ""
        ded_html = (f'<blockquote class="dedication">{dd}</blockquote>'
                    if b["dedication"] else "")
        cards.append(
            f'<article class="book-card">\n'
            f'<a class="cover" href="{se}/index.html">'
            f'<img src="{se}/cover.jpg" alt="{te}" loading="lazy"></a>\n'
            f'<div class="info">\n'
            f'<h2><a href="{se}/index.html">{te}</a></h2>\n'
            f'{byline_html}\n'
            f'<p class="description">{de}</p>\n'
            f'{ded_html}\n'
            f'<p class="links">'
            f'<a href="{se}/index.html">Read the book</a>'
            f'<a href="{se}/preview.html">Print preview</a>'
            f'</p>\n'
            f'</div>\n'
            f'</article>')

    out = ROOT / "out-index.html"
    out.write_text(_AUTHOR_PAGE.replace("__CARDS__", "\n".join(cards)))
    print(f"{len(books)} books indexed -> {out}")
    for b in books:
        print(f"  {b['title']}  ->  {b['slug']}/")

    if not args.yes:
        print(f"\nDry run. Would upload to s3://{args.bucket}/index.html — "
              "public.\nRe-run with --yes.")
        return

    if not aws("s3", "cp", str(out), f"s3://{args.bucket}/index.html",
               "--only-show-errors"):
        sys.exit("publish-index failed")
    print(f"\n-> {S3_BASE}/index.html")


def main():
    ap = argparse.ArgumentParser()
    # Not required=True: publish-index is cross-book by nature and takes no
    # --book. Every other subcommand still demands one — the check just moved
    # below, where it can be stated rather than implied.
    ap.add_argument("--book", metavar="SLUG",
                    help="which books/<slug>/ to act on, e.g. --book sample-book")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile")
    c.add_argument("--page", type=int)
    c.add_argument("--strict", action="store_true")

    g = sub.add_parser("gen")
    g.add_argument("--page", type=int)
    g.add_argument("--n", type=int, default=3)
    g.add_argument("--strict", action="store_true")
    g.add_argument("--final", action="store_true")
    g.add_argument("--diagrams", action="store_true")
    g.add_argument("--diagram", nargs="+", metavar="ID",
                   help="only these diagram ids, e.g. --diagram D1 D2")
    g.add_argument("--run")
    g.add_argument("--dry-run", action="store_true",
                   help="build every prompt and request and write them out, "
                        "but send nothing (see --dry-run-out)")
    g.add_argument("--dry-run-out", metavar="DIR",
                   help="where --dry-run writes (default: books/<slug>/out/dryrun)")
    g.add_argument("--sleep", type=float, default=1.0)
    g.add_argument("--concurrency", type=int, default=3,
                   help="parallel API calls — network/API-cost bound, not "
                        "CPU bound. Default 3: keep gen at at least 3.")

    l = sub.add_parser("lock")
    lg = l.add_mutually_exclusive_group(required=True)
    lg.add_argument("--page", type=int, help="illustration slot, e.g. --page 18")
    lg.add_argument("--diagram", metavar="ID", help="diagram id, e.g. --diagram D15")
    l.add_argument("--cand", type=int, required=True)
    l.add_argument("--run", required=True)

    p = sub.add_parser("pdf")
    p.add_argument("--run", help="run to pull images from (default: newest)")
    p.add_argument("--cand", type=int,
                   help="candidate to use for unlocked slots (default: lowest)")
    p.add_argument("--out", help="output path (default: out/book-<run>.pdf)")
    p.add_argument("--lulu", action="store_true",
                   help="write the Lulu upload file with fonts embedded "
                        "(macOS only: embeds the system Helvetica)")
    p.add_argument("--no-diagrams", action="store_true",
                   help="leave the diagram insets off the prose pages")

    h = sub.add_parser("html")
    h.add_argument("--run", help="run to pull images from (default: newest)")
    h.add_argument("--cand", type=int,
                   help="candidate to use for unlocked slots (default: lowest)")
    h.add_argument("--out", help="output path (default: out/manuscript.html)")
    h.add_argument("--no-diagrams", action="store_true",
                   help="prose and page art only, no diagrams")
    h.add_argument("--review", action="store_true",
                   help="labelled review copy (Page N headings, slot/candidate/run "
                        "captions) instead of the print-clean default")

    cv = sub.add_parser("cover", help="one-page wraparound cover PDF for Lulu")
    cv.add_argument("--run", help="run to pull the cover art from (default: newest)")
    cv.add_argument("--cand", type=int,
                    help="candidate to use if the cover slot is unlocked "
                         "(default: lowest)")
    cv.add_argument("--spine", type=float, default=0.25,
                    help="spine width in inches (default: 0.25 — read it from "
                         "Lulu's per-book cover template)")
    cv.add_argument("--out", help="output path (default: out/cover.pdf)")
    cv.add_argument("--lulu", action="store_true",
                    help="also write cover-lulu.pdf with fonts embedded (the "
                         "upload file Lulu requires)")

    pv = sub.add_parser("preview",
                        help="browser mock-up of the print spread, prose | art")
    pv.add_argument("--run", help="run to pull images from (default: newest)")
    pv.add_argument("--cand", type=int,
                    help="candidate to use for unlocked slots (default: lowest)")
    pv.add_argument("--out", help="output path (default: out/preview.html)")
    pv.add_argument("--no-diagrams", action="store_true",
                    help="leave the diagram insets off pages that have slot art")

    pub = sub.add_parser("publish", help="upload one book to S3, publicly")
    pub.add_argument("--run", help="run to publish (default: newest)")
    pub.add_argument("--cand", type=int,
                     help="candidate to use for unlocked slots (default: lowest)")
    pub.add_argument("--out", help="manuscript path (default: out/manuscript.html)")
    pub.add_argument("--no-diagrams", action="store_true",
                     help="prose and page art only, no diagrams")
    pub.add_argument("--bucket", default=BUCKET, help=f"default: {BUCKET}")
    pub.add_argument("--review-only", action="store_true",
                     help="refresh just the review page (preview.html and the "
                          "runs it cycles through) — leave the book itself "
                          "alone, for a draft or a mid-review re-roll")
    pub.add_argument("--yes", action="store_true",
                     help="actually upload (without this it is a dry run)")
    pub.add_argument("--dry-run", action="store_true",
                     help="force the dry run even if --yes is also given")

    pi = sub.add_parser("publish-index",
                        help="upload the bucket-root index of all books")
    pi.add_argument("--bucket", default=BUCKET, help=f"default: {BUCKET}")
    pi.add_argument("--yes", action="store_true",
                    help="actually upload (without this it is a dry run)")
    pi.add_argument("--dry-run", action="store_true",
                    help="force the dry run even if --yes is also given")

    a = ap.parse_args()

    # publish-index spans every book, so it is the one command with no --book.
    # The rule the rest of the tool relies on — never guess which book, because
    # a wrong guess can send a paid gen into the wrong out/ — is unchanged.
    if a.cmd == "publish-index":
        cmd_publish_index(None, a)
        return
    if not a.book:
        ap.error("--book is required (e.g. --book sample-book)")

    resolve_book(a.book)
    spec = load()
    {"compile": cmd_compile, "gen": cmd_gen,
     "lock": cmd_lock, "pdf": cmd_pdf, "html": cmd_html, "cover": cmd_cover,
     "preview": cmd_preview, "publish": cmd_publish}[a.cmd](spec, a)


if __name__ == "__main__":
    main()
