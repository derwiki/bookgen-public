# bookgen, in a box.
#
# The image is the TOOL and its locked dependencies — not the books. Mount the
# checkout at /work and run a command:
#
#     docker build -t bookgen .
#     docker run --rm --env-file .env -v "$PWD:/work" \
#       --user "$(id -u):$(id -g)" bookgen --book sample-book gen --dry-run
#
# No secret is baked in at any layer: .env is dockerignored, the image sets no
# API key, and credentials arrive at run time through --env-file (or, on a VPS,
# through the instance role with no keys at all).
#
# Pinned to the Python minor that uv.lock resolves, so the interpreter here and
# the interpreter on the laptop are the same shape.

FROM python:3.13-slim-bookworm

# System tools the pipeline shells out to:
#   imagemagick     — ref downscaling and HEIC->JPEG (sips is macOS-only)
#   awscli          — `publish` and the reference-photo fetch
#   ca-certificates — TLS to the Gemini endpoint and to S3
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      awscli \
      ca-certificates \
      imagemagick \
 && rm -rf /var/lib/apt/lists/*

# uv, pinned to the version the lock was written with.
COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Dependencies first, so editing a book does not invalidate the install layer.
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# The checkout, for a run with no mount. Runs with a mount override this.
COPY . /work
WORKDIR /work

# Matches the uid a fresh Ubuntu VPS hands its first sudo user, which is what
# the repo is cloned as, so files written into a mounted checkout are not left
# root-owned. Override with --user when it does not.
RUN useradd --create-home --uid 1000 --shell /bin/bash bookgen
USER bookgen

ENTRYPOINT ["python", "/work/bookgen.py"]
