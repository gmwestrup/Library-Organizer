#!/usr/bin/env python3
"""
Move mode and "already in the destination" handling.

Moving deletes from the source, so this suite checks every way that could
lose data: only approved books move, a failure mid-book puts everything
back, cross-drive moves verify before deleting, leftovers follow the book,
folders other books still use are left alone, a read-only source is
refused, and each conflict option does what it says.

Run:  python tests/test_move.py
"""
import os, shutil, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="lo-move-")
SRC, DEST, DATA = (os.path.join(TMP, d) for d in ("source", "dest", "config"))
os.environ.update(SOURCE_DIR=SRC, DEST_DIR=DEST, DATA_DIR=DATA)
sys.path.insert(0, os.path.join(ROOT, "docker"))
failures = []


def check(label, ok, got=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   -> {got}"))
    if not ok:
        failures.append(label)


def put(rel, data=None):
    p = os.path.join(SRC, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data if data is not None else os.urandom(5000))
    return p


def build():
    shutil.rmtree(SRC, ignore_errors=True); shutil.rmtree(DEST, ignore_errors=True)
    put("Andy Weir/The Martian/The Martian.m4b")
    put("Andy Weir/The Martian/folder.jpg", b"\xff\xd8" + os.urandom(3000))
    put("Andy Weir/The Martian/desc.txt", b"A man alone on Mars.")
    put("Andy Weir/The Martian/Thumbs.db", b"junk")
    put("Blake Crouch/Dark Matter/Dark Matter.m4b")
    put("Loose/Lee Child - Killing Floor.m4b")                  # two books in one folder
    put("Loose/Martha Wells - All Systems Red.m4b")
    put("Unsure/track01.mp3")                                   # can't be identified: not approved


import app, core  # noqa: E402
c = app.app.test_client()


def wait():
    while c.get("/api/state").json["busy"]:
        time.sleep(0.05)
    return c.get("/api/state").json


def scan():
    c.post("/api/scan", json={"source": SRC}); wait()
    return {b["title"]: b for b in c.get("/api/books?per_page=500").json["books"]}


def exists(rel, root=SRC):
    return os.path.exists(os.path.join(root, rel))


try:
    print("\n== Move: only approved books leave the source ==")
    build()
    books = scan()
    c.post("/api/settings", json={"threshold": 99})           # nothing is confident enough on its own...
    ids = [books[t]["id"] for t in ("The Martian", "Killing Floor")]
    c.post("/api/bulk", json={"ids": ids, "action": "approve"})   # ...so approve two by hand
    r = c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "write_cover": False}); wait()
    check("move accepted", r.status_code == 200, r.json)
    check("approved book moved into the clean structure",
          exists("Andy Weir/The Martian/The Martian.m4b", DEST) and not exists("Andy Weir/The Martian/The Martian.m4b"))
    check("leftovers followed the book (folder.jpg, desc.txt)",
          exists("Andy Weir/The Martian/folder.jpg", DEST) and exists("Andy Weir/The Martian/desc.txt", DEST))
    check("emptied source folders removed (Thumbs.db ignored)", not exists("Andy Weir"))
    check("un-approved books stay in the source",
          exists("Blake Crouch/Dark Matter/Dark Matter.m4b") and exists("Unsure/track01.mp3"))
    check("loose folder: moved book gone, the other book and its folder stay",
          not exists("Loose/Lee Child - Killing Floor.m4b") and exists("Loose/Martha Wells - All Systems Red.m4b"))
    st = {b["title"]: b["status"] for b in c.get("/api/books?per_page=500").json["books"]}
    check("status 'moved' for moved books", st["The Martian"] == "moved" and st["Killing Floor"] == "moved", st)
    rows = [r for r in app.DB.journal_rows() if r[1] == "move"]
    check("every move is in the journal", len(rows) >= 4, len(rows))

    print("\n== A failure mid-book puts everything back ==")
    build(); c.post("/api/settings", json={"threshold": 0})
    put("Multi/Book One/01.mp3"); put("Multi/Book One/02.mp3"); put("Multi/Book One/03.mp3")
    books = scan()
    real, calls = core._transfer, {"n": 0}

    def flaky(src, target, move, done_ops, delete_after):
        calls["n"] += 1
        if "03.mp3" in src:
            raise IOError("simulated disk error")
        return real(src, target, move, done_ops, delete_after)
    core._transfer = flaky
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "ids": [books["Book One"]["id"]]}); wait()
    core._transfer = real
    check("all three parts back in the source after the error",
          all(exists(f"Multi/Book One/0{i}.mp3") for i in (1, 2, 3)))
    check("nothing half-moved left in the destination",
          not any(f.endswith(".mp3") for _, _, fs in os.walk(DEST) for f in fs))

    print("\n== Across drives: copy, verify, THEN delete ==")
    build(); books = scan()
    real_same = core._same_drive
    core._same_drive = lambda a, b: False
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "ids": [books["Dark Matter"]["id"]]}); wait()
    core._same_drive = real_same
    check("cross-drive move: at destination, gone from source",
          exists("Blake Crouch/Dark Matter/Dark Matter.m4b", DEST) and not exists("Blake Crouch"))

    print("\n== Read-only source is refused ==")
    build(); scan()
    real_w = app._source_writable
    app._source_writable = lambda p: False
    r = c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move"})
    app._source_writable = real_w
    check("move refused with a clear message", r.status_code == 400 and "read-only" in r.json["error"], r.json)
    check("nothing touched", exists("Andy Weir/The Martian/The Martian.m4b"))

    print("\n== Already in the destination ==")
    def prep(dest_bytes):
        build()
        p = os.path.join(DEST, "Blake Crouch/Dark Matter/Dark Matter.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(dest_bytes)
        return scan(), p

    src_bytes = os.urandom(5000)
    books, p = prep(src_bytes)
    with open(os.path.join(SRC, "Blake Crouch/Dark Matter/Dark Matter.m4b"), "wb") as fh:
        fh.write(src_bytes)                                           # identical copy
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "ids": [books["Dark Matter"]["id"]]}); wait()
    check("identical file at destination: source copy simply removed", not exists("Blake Crouch"))

    books, p = prep(os.urandom(3000))
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "conflict": "skip",
                              "ids": [books["Dark Matter"]["id"]]}); wait()
    b = [x for x in c.get("/api/books?per_page=500").json["books"] if x["title"] == "Dark Matter"][0]
    check("skip: different copy there -> left in the source, flagged",
          exists("Blake Crouch/Dark Matter/Dark Matter.m4b") and b["status"] == "in destination", b["status"])
    check("skip: destination copy untouched", os.path.getsize(p) == 3000)

    books, p = prep(os.urandom(3000))
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "conflict": "keep_better",
                              "ids": [books["Dark Matter"]["id"]]}); wait()
    check("keep better: bigger source copy replaced the smaller one",
          os.path.getsize(p) == 5000 and not exists("Blake Crouch"))
    aside = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(DEST, ".replaced")) for f in fs]
    check("keep better: the old copy was set aside, not deleted", any(os.path.getsize(a) == 3000 for a in aside), aside)

    books, p = prep(os.urandom(9000))
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "conflict": "keep_better",
                              "ids": [books["Dark Matter"]["id"]]}); wait()
    check("keep better: destination copy better -> source left in place",
          os.path.getsize(p) == 9000 and exists("Blake Crouch/Dark Matter/Dark Matter.m4b"))

    books, p = prep(os.urandom(3000))
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "move", "conflict": "replace",
                              "ids": [books["Dark Matter"]["id"]]}); wait()
    check("replace: new copy in place", os.path.getsize(p) == 5000)
    aside = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(DEST, ".replaced")) for f in fs]
    check("replace: old copy kept in .replaced", any(os.path.getsize(a) == 3000 for a in aside))

    books, p = prep(os.urandom(3000))
    c.post("/api/copy", json={"dest": DEST, "source": SRC, "mode": "copy", "conflict": "keep_both",
                              "ids": [books["Dark Matter"]["id"]]}); wait()
    check("keep both: numbered folder, original kept",
          os.path.getsize(p) == 3000 and exists("Blake Crouch/Dark Matter (2)/Dark Matter.m4b", DEST))
    check("copy mode never deletes from the source", exists("Blake Crouch/Dark Matter/Dark Matter.m4b"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} check(s) FAILED")
    sys.exit(1)
print("\nAll move / conflict checks passed.")
