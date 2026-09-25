#!/usr/bin/env python3
"""
End-to-end test of the whole v2 pipeline on a small, deliberately messy
library: scan -> enrich (online calls mocked) -> AI (mocked) -> duplicates ->
authors -> copy, then a RESCAN to prove decisions are remembered.

Real audio is encoded with ffmpeg when it's available (GitHub's Ubuntu
runners have it); without ffmpeg the audio-specific checks are skipped.

Run:  python tests/test_pipeline.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="lo-e2e-")
SRC, DEST, DATA = (os.path.join(TMP, d) for d in ("source", "dest", "config"))
os.environ.update(SOURCE_DIR=SRC, DEST_DIR=DEST, DATA_DIR=DATA)
sys.path.insert(0, os.path.join(ROOT, "docker"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
failures = []


def check(label, ok, got=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   -> {got}"))
    if not ok:
        failures.append(label)


def jpeg(w, h):
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (w, h), (120, 80, 40)).save(b, "JPEG"); return b.getvalue()


_FREQ = [300]


def audio(path, secs=20):
    """Every file gets its own tone so no two are accidentally byte-identical."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _FREQ[0] += 37
    if HAVE_FFMPEG:
        args = ["-c:a", "aac", "-b:a", "48k", "-f", "mp4"] if path.endswith(".m4b") else ["-c:a", "libmp3lame", "-b:a", "48k"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={_FREQ[0]}:duration={secs}",
                        *args, path], check=True)
    else:
        open(path, "wb").write(os.urandom(4000))


def epub(path, title, author, text, cover=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        man, meta = '<item id="t" href="t.xhtml" media-type="application/xhtml+xml"/>', f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
        if cover:
            man += '<item id="c" href="c.jpg" media-type="image/jpeg"/>'; meta += '<meta name="cover" content="c"/>'
            z.writestr("OEBPS/c.jpg", cover)
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{meta}</metadata><manifest>{man}</manifest><spine><itemref idref="t"/></spine></package>')
        z.writestr("OEBPS/t.xhtml", f"<html><body><p>{text}</p></body></html>")


# ------------------------------------------------------------------ fixture
os.makedirs(SRC)
epub(f"{SRC}/ebooks/Dune - Frank Herbert.epub", "Dune", "Frank Herbert", "A beginning.", cover=jpeg(600, 900))
os.makedirs(f"{SRC}/kindle")
open(f"{SRC}/kindle/Dune - Frank Herbert.mobi", "wb").write(b"x" * 3000)                      # Title - Author swap
epub(f"{SRC}/unsorted/book_00417.epub", "Unknown", "calibre", "Foundation. ISBN 9780553293357")  # junk + ISBN in text
epub(f"{SRC}/ebooks/Good Omens.epub", "Good Omens", "Neil Gaiman &amp; Terry Pratchett", "In the beginning")
audio(f"{SRC}/Kate Reading - The Way of Kings/The Way of Kings.m4b")                          # narrator as author
audio(f"{SRC}/[MAM] Dean Koontz - Watchers (2021) 64k/Watchers.mp3")                          # torrent junk
audio(f"{SRC}/Brandon Sanderson/Elantris/Elantris.m4b")
audio(f"{SRC}/Brandon Sandersen/Warbreaker/Warbreaker.m4b")                                   # author typo
os.makedirs(f"{SRC}/Stephen King/The Stand"); open(f"{SRC}/Stephen King/The Stand/The Stand.mp3", "wb").close()  # damaged
audio(f"{SRC}/misc/Home.mp3")                                                                 # generic title
if HAVE_FFMPEG:
    audio(f"{SRC}/Andy Weir/The Martian/The Martian.m4b", 30)
    os.makedirs(f"{SRC}/Downloads"); shutil.copy(f"{SRC}/Andy Weir/The Martian/The Martian.m4b", f"{SRC}/Downloads/martian_REAL.m4b")

# ------------------------------------------------------------------ run
import mock_online  # noqa: E402
import app  # noqa: E402
import ai  # noqa: E402
mock_online.install()
AI_CALLS = []


def fake_ai(url, headers, body, timeout=90):
    ev = json.loads(body["messages"][0]["content"].split("Evidence:\n", 1)[1])
    AI_CALLS.append(ev["folder_path"])
    ans = ({"title": "Home", "author": "Harlan Coben", "confidence": 85} if "misc" in ev["folder_path"] else
           {"title": ev["current_guess"]["title"], "author": ev["current_guess"]["author"], "confidence": 50})
    return {"content": [{"type": "text", "text": json.dumps(ans)}]}


ai._post = fake_ai
c = app.app.test_client()


def wait():
    while c.get("/api/state").json["busy"]:
        time.sleep(0.1)
    return c.get("/api/state").json


def books():
    return c.get("/api/books?per_page=500").json["books"]


def find(title):
    return [b for b in books() if b["title"] == title]


try:
    print("\n== Scan ==")
    c.post("/api/scan", json={"source": SRC}); wait()
    dune = find("Dune")
    check("'Dune - Frank Herbert.mobi' swapped to author Frank Herbert", all(b["author"] == "Frank Herbert" for b in dune) and len(dune) == 2, dune)
    check("junk author 'calibre' / title 'Unknown' cleared", not any(b["author"].lower() == "calibre" for b in books()))
    check("glued co-authors -> primary author", find("Good Omens")[0]["author"] == "Neil Gaiman", find("Good Omens"))
    w = find("Watchers")
    check("[MAM] ... (2021) 64k -> Dean Koontz / Watchers", w and w[0]["author"] == "Dean Koontz", w)
    check("narrator-as-author flagged", any("narrator" in f for b in find("The Way of Kings") for f in b["flags"]))

    print("\n== Enrich (online mocked) ==")
    c.post("/api/enrich", json={"online": True, "covers": True}); wait()
    wok = find("The Way of Kings")[0]
    check("narrator fixed: author Brandon Sanderson, narrator Kate Reading",
          wok["author"] == "Brandon Sanderson" and wok["narrator"] == "Kate Reading", wok)
    check("Audnexus series -> The Stormlight Archive #01", wok["series"] == "The Stormlight Archive" and wok["series_index"] == "01", wok)
    check("cover found for The Way of Kings", wok["cover"])
    f = find("Foundation")
    check("ISBN in the book text -> identified as Asimov's Foundation", f and f[0]["author"] == "Isaac Asimov", f)
    home = find("Home")[0]
    check("generic title 'Home' NOT matched to a random 'Home'", home["author"] == "", home)
    damaged = [b for b in books() if b["health"]]
    check("0-byte file flagged damaged", len(damaged) == 1 and damaged[0]["title"] == "The Stand", damaged)

    print("\n== AI (mocked) ==")
    c.post("/api/settings", json={"api_key": "sk-test-0000"})
    c.post("/api/ai", json={"which": "low"}); wait()
    home = find("Home")[0]
    check("generic title: 85% AI answer kept as a suggestion, not applied", home["author"] == "" and home["ai"], home)
    n = len(AI_CALLS); c.post("/api/ai", json={"which": "low"}); wait()
    check("AI answers cached (no repeat calls)", len(AI_CALLS) == n, len(AI_CALLS) - n)

    print("\n== Duplicates & authors ==")
    groups = c.get("/api/dupes").json["groups"]
    check("epub + mobi of Dune -> one 'merge formats' group", any(g["merge_formats"] for g in groups), groups)
    if HAVE_FFMPEG:
        check("renamed identical copy of The Martian found",
              any(r["reason"] == "Identical files" for g in groups for r in g["reasons"]), groups)
    auth = c.get("/api/authors").json["groups"]
    check("'Brandon Sandersen' clustered with 'Brandon Sanderson'",
          any({"Brandon Sanderson", "Brandon Sandersen"} <= {v for v, _ in g["variants"]} for g in auth), auth)
    c.post("/api/dedupe", json={"policy": "smart"})
    check("resolved groups disappear", c.get("/api/state").json["counts"]["dups"] == 0)
    c.post("/api/authors/merge", json={"variants": ["Brandon Sandersen", "Brandon Sanderson"], "canonical": "Brandon Sanderson"})
    bid = find("Good Omens")[0]["id"]
    c.post(f"/api/book/{bid}", json={"series": "Test Series", "series_index": "3"})

    print("\n== Copy ==")
    c.post("/api/copy", json={"dest": DEST, "source": SRC}); wait()
    out = {os.path.relpath(os.path.join(d, f), DEST) for d, _, fs in os.walk(DEST) for f in fs}
    check("Dune epub + mobi in ONE folder", {"Frank Herbert/Dune/Dune.epub", "Frank Herbert/Dune/Dune.mobi"} <= out, sorted(out))
    check("Way of Kings filed under its series", "Brandon Sanderson/The Stormlight Archive/The Way of Kings/metadata.opf" in out, sorted(out))
    check("cover.jpg written", "Brandon Sanderson/The Stormlight Archive/The Way of Kings/cover.jpg" in out)
    check("damaged book held back", not any(p.startswith("Stephen King/") for p in out))
    check("author-less book held back (no 'Unknown Author' folder)", not any(p.startswith("Unknown Author/") for p in out))
    opf = open(os.path.join(DEST, "Brandon Sanderson/The Stormlight Archive/The Way of Kings/metadata.opf"), encoding="utf-8").read()
    check("OPF carries narrator, series, ASIN, cover", all(x in opf for x in ('role="nrt">Kate Reading', "The Stormlight Archive", "B003ZWFO7E", 'href="cover.jpg"')))
    if HAVE_FFMPEG:
        from mutagen.mp4 import MP4
        t = MP4(os.path.join(DEST, "Brandon Sanderson/The Stormlight Archive/The Way of Kings/The Way of Kings.m4b")).tags
        check("m4b tags + embedded cover written", t["\xa9ART"] == ["Brandon Sanderson"] and "covr" in t and t["\xa9mvn"] == ["The Stormlight Archive"])
    orig = zipfile.ZipFile(f"{SRC}/unsorted/book_00417.epub").read("OEBPS/content.opf").decode()
    check("original files untouched", "<dc:title>Unknown</dc:title>" in orig)

    print("\n== Rescan remembers everything ==")
    c.post("/api/scan", json={"source": SRC}); wait()
    go = find("Good Omens")[0]
    check("your edit survives a rescan", go["series"] == "Test Series" and go["series_index"] == "03", go)
    check("author merge survives a rescan", not any(b["author"] == "Brandon Sandersen" for b in books()))
    check("format merge survives a rescan", len(find("Dune")) == 1, find("Dune"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} check(s) FAILED")
    sys.exit(1)
print("\nFull pipeline test passed" + ("" if HAVE_FFMPEG else " (audio checks skipped: no ffmpeg)") + ".")
