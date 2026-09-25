# Contributing

Thanks for looking at this. Library Organizer's parser has been shaped
almost entirely by real, messy libraries — that's the most valuable kind
of contribution it can get.

## The single most useful thing you can do

Open an issue with **a real (anonymized) folder/file naming pattern the
parser gets wrong**. Concretely:

1. What the folder and file names actually looked like (rename any
   private details, but keep the *shape* — number placement, separators,
   capitalization, extra words — exactly as it was)
2. What the tool produced (author / series / # / title, or "flagged as
   duplicate", etc.)
3. What it should have produced

A screenshot of the review table is great; a plain-text listing (e.g.
`find /path -type d` output) is even better because it can go straight
into a test fixture. This is exactly how most of the parser's edge cases
(box sets, disc folders, roman-numeral parts, numbered prefix codes,
hyphenated `Author-Title` folders, bad sidecar metadata...) were found
and fixed — see `docs/CHANGELOG.md` for the running list.

## Code contributions

- `docker/core.py` is the engine (scanning, metadata sources, filename
  parsing, copying). `docker/app.py` is the Flask API. `docker/
  templates/index.html` is the whole browser UI in one file.
- **The Windows and Docker editions must stay byte-identical** for
  `core.py`, `app.py`, and `templates/index.html` — a fix goes in once
  and gets copied to both folders. Please don't patch only one.
- Please include a test (even an ad-hoc script that builds a small fake
  folder tree and asserts the scan result) for any parser change — this
  codebase has been bitten before by fixes that "obviously" work but
  regress an earlier case. Copy-paste the pattern of existing fixture
  tests referenced in the changelog if you're not sure how.
- Run `python -m py_compile core.py app.py` at minimum before opening a
  PR; the CI workflow does this plus a scripted smoke test automatically.

## What NOT to send

- Changes that make embedded tags, folder names, or online lookups
  *more* authoritative than a sidecar `metadata.opf`/`metadata.json`
  without a very good reason — that priority order was deliberate and
  hard-won (see the v1.5/v1.9.2 changelog entries).
- Anything that would make Copy able to modify or delete a source file.
  Copy-never-move is a hard invariant of this project.

## Questions / ideas

Open an issue. If it's a feature idea rather than a bug, a short
description of the real workflow it would help with is more useful than
a spec — this project grows from actual library-organizing pain, not
speculative features.
