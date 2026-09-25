#!/usr/bin/env python3
"""
Smoke test for Library Organizer.

Not a full test suite — a guard against the two failure modes that have
actually bitten this project before:

  1. The Windows and Docker editions drifting apart (core.py / app.py /
     templates/index.html must stay byte-identical).
  2. A parser "fix" quietly regressing an earlier real-world case (each
     case below came from an actual messy library — see docs/CHANGELOG.md).

Run:  python tests/test_smoke.py
Exits non-zero (and prints what failed) on any problem.
"""
import filecmp
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "docker"))

failures = []


def check(label, condition):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def mk(root, rel, files):
    d = os.path.join(root, rel)
    os.makedirs(d, exist_ok=True)
    for name in files:
        open(os.path.join(d, name), "wb").write(b"x" * 20)


def section(title):
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
section("Edition parity (Windows == Docker)")
# ---------------------------------------------------------------------------
for fname in ("core.py", "app.py", "enrich.py", "ai.py", "dedupe.py", "authors.py", "library_db.py",
              "requirements.txt", os.path.join("templates", "index.html")):
    d = os.path.join(REPO_ROOT, "docker", fname)
    w = os.path.join(REPO_ROOT, "windows", fname)
    check(f"{fname} identical between docker/ and windows/",
          os.path.isfile(d) and os.path.isfile(w) and filecmp.cmp(d, w, shallow=False))


# ---------------------------------------------------------------------------
section("Both files at least import cleanly")
# ---------------------------------------------------------------------------
try:
    import core  # noqa: E402
    import enrich, ai, dedupe, authors, library_db  # noqa: E401,E402,F401
    check("docker/core.py and the v2 modules import", True)
except Exception as e:
    check(f"docker/core.py imports ({e})", False)
    print("\nCannot continue without core.py - stopping.")
    sys.exit(1)


# ---------------------------------------------------------------------------
section("Real-world parsing regressions")
# ---------------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="lo-smoke-")
src = os.path.join(tmp, "source")
os.makedirs(src)
try:
    # Box set ranges must be preserved and kept distinct (v1.9.1)
    mk(src, "Aer-ki Jyr/Star Force Universe (Jyr)/02 - Star Force Origin Box Set (5-8)", ["book.m4b"])
    mk(src, "Aer-ki Jyr/Star Force Universe (Jyr)/03 - Star Force Origin Box Set (9-12)", ["book.m4b"])

    # Disc subfolders merge into one book, natural sort (v1.7)
    for d in (1, 2, 10):
        mk(src, f"Stephen King/The Stand/Disc {d}", ["01.mp3", "02.mp3"])

    # Numbered prefix codes must not fragment one book into many (v1.7.1)
    mk(src, "Larry Niven-Saturns Race",
       [f"LNSR{i:02d}-95 Larry Niven - Saturn's Race.mp3" for i in range(1, 11)])

    # "Last, First" with a two-word surname, via a Calibre-style sidecar (v1.4)
    leguin_dir = os.path.join(src, "temp", "unsorted_042")
    os.makedirs(leguin_dir, exist_ok=True)
    open(os.path.join(leguin_dir, "book.epub"), "wb").write(b"x" * 20)
    open(os.path.join(leguin_dir, "metadata.opf"), "w").write(
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>A Wizard of Earthsea</dc:title>"
        "<dc:creator>Le Guin, Ursula K.</dc:creator>"
        "</metadata></package>")

    # Junk folders must never become an author (multiple versions)
    mk(src, "New folder (3)/wise mans fear book2", ["a.m4b"])

    books = list(core.scan_library(src, True, True, log=lambda m: None))
    by_title = {}
    for b in books:
        for f in (b.title,):
            by_title.setdefault(f, []).append(b)

    sf = [b for b in books if "Star Force" in b.title]
    check("two distinct Star Force box-set titles found",
          len({b.title for b in sf}) == 2)
    check("box-set titles keep their (N-N) ranges",
          all("(" in b.title and ")" in b.title for b in sf))
    check("box sets are at different series positions (not duplicates)",
          len({b.series_index for b in sf}) == 2)

    stand = next((b for b in books if b.title == "The Stand"), None)
    check("3 disc folders (incl. Disc 10) merge into ONE 6-file book",
          stand is not None and len(stand.files) == 6)
    if stand:
        order = [os.path.basename(os.path.dirname(p)) for p in stand.files]
        check("natural sort: Disc 2 before Disc 10",
              order.index("Disc 2") < order.index("Disc 10"))

    saturn = next((b for b in books if "Saturn" in b.title), None)
    check("10 numbered-prefix files merge into ONE book",
          saturn is not None and len(saturn.files) == 10)
    check("numbered-prefix book gets the right author",
          saturn is not None and saturn.author == "Larry Niven")

    leguin = next((b for b in books if "Wizard of Earthsea" in b.title), None)
    check("two-word surname 'Le Guin, Ursula K.' flips correctly",
          leguin is not None and leguin.author == "Ursula K. Le Guin")

    junk = next((b for b in books if "wise mans fear" in b.title.lower()
                or "Wise Mans Fear" in b.title), None)
    check("book in a junk-named folder does not inherit a fake author",
          junk is not None and junk.author == "")

    dup_keys = set()
    dupes = 0
    for b in books:
        if b.author and b.title:
            key = (b.kind, b.author.lower(), b.title.lower(),
                   core.fmt_series_index(b.series_index) if b.series else "")
            dupes += key in dup_keys
            dup_keys.add(key)
    check("no false duplicates across the whole fixture library", dupes == 0)

finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
if failures:
    print(f"\n{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll smoke checks passed.")
