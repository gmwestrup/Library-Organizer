#!/usr/bin/env python3
"""
Library Organizer v2 - one tool for a messy audiobook & ebook folder.

  1. Scan      find every book, name it from every clue it carries
  2. Enrich    probe the audio, mine the ebook text, match online
               (duration-aware), gather and rank covers, score confidence
  3. AI        send only the books still uncertain to Claude (or a local
               model), with all the evidence
  4. Clean up  duplicates (identical files, ASIN/ISBN, fuzzy titles, same
               runtime) and author variants, with remembered decisions
  5. Copy      into Author/Series/Title with metadata.opf, metadata.json,
               cover.jpg, and fixed embedded tags + covers in the copies

Originals are never modified. Same code for Docker and Windows.
"""

import csv
import io
import json
import os
import re
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, render_template, Response, send_file, abort

import core
import enrich
import ai
import dedupe
import authors as authors_mod
from library_db import DecisionsDB

VERSION = "2.0.1"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
IS_WINDOWS = os.name == "nt"
DEFAULT_SOURCE = os.environ.get("SOURCE_DIR", "" if IS_WINDOWS else "/source")
DEFAULT_DEST = os.environ.get("DEST_DIR", "" if IS_WINDOWS else "/dest")
if IS_WINDOWS:
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LibraryOrganizer"))
else:
    DATA_DIR = os.environ.get("DATA_DIR", "/config" if os.path.isdir("/config")
                              else os.path.join(DEFAULT_DEST, ".organizer"))
os.makedirs(DATA_DIR, exist_ok=True)
PLAN_FILE = os.environ.get("PLAN_FILE", os.path.join(DATA_DIR, "organizer-plan.json"))
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DB = DecisionsDB(os.path.join(DATA_DIR, "organizer.db"))

# ---------------------------------------------------------------------------
# In-memory state (single-user LAN tool)
# ---------------------------------------------------------------------------
STATE = {
    "books": {}, "next_id": 1, "busy": False, "task": "", "progress": [0, 0],
    "log": [], "lock": threading.RLock(), "ver": 0, "dupes": None, "dupes_ver": -1,
    "source": DEFAULT_SOURCE,
}

BOOK_FIELDS = ("kind", "author", "series", "series_index", "title", "src_display", "status",
               "meta_source", "files", "extras", "narrator", "fingerprint", "year", "publisher",
               "description", "genres", "language", "asin", "isbn", "duration", "bitrate", "codec",
               "chapters", "health", "cover", "cover_info", "cover_candidates", "confidence",
               "match_bonus", "online_best", "ai", "transcript", "locked", "enriched", "dup_skipped", "flags")
EDITABLE = ("author", "series", "series_index", "title", "narrator", "year", "publisher",
            "description", "genres", "language", "asin", "isbn")


def settings():
    return ai.load_settings(SETTINGS_FILE)


def touch():
    STATE["dirty"] = True
    STATE["ver"] += 1


def save_plan():
    try:
        with STATE["lock"]:
            data = [{f: getattr(b, f) for f in BOOK_FIELDS} for b in STATE["books"].values()]
            STATE["dirty"] = False
        tmp = PLAN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": VERSION, "source": STATE["source"], "books": data}, fh)
        os.replace(tmp, PLAN_FILE)
    except OSError as e:
        log(f"Could not save plan file ({e}) - review work won't survive a restart.")


def load_plan():
    for path in (PLAN_FILE, os.path.join(DEFAULT_DEST, ".organizer-plan.json")):   # v1 location
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                STATE["source"] = data.get("source") or DEFAULT_SOURCE
                data = data.get("books", [])
            with STATE["lock"]:
                for rec in data:
                    b = core.Book(kind=rec.get("kind", "audio"))
                    for f in BOOK_FIELDS:
                        if f in rec:
                            setattr(b, f, rec[f])
                    STATE["books"][STATE["next_id"]] = b
                    STATE["next_id"] += 1
            log(f"Restored saved plan: {len(data)} book(s) from {path}")
            return
        except (OSError, ValueError) as e:
            log(f"Could not load saved plan: {e}")


def log(msg):
    print(msg, flush=True)
    with STATE["lock"]:
        STATE["log"].append(str(msg))
        if len(STATE["log"]) > 5000:
            del STATE["log"][:1000]


def _mount_report():
    log(f"Library Organizer v{VERSION} starting...  (data folder: {DATA_DIR})")
    caps = [f"mutagen {'OK' if core.HAVE_MUTAGEN else 'MISSING'}",
            f"ffprobe {'OK' if enrich.FFPROBE else 'not found (mutagen used instead)'}",
            f"Pillow {'OK' if enrich.HAVE_PIL else 'not installed (covers kept as-is)'}",
            f"speech-to-text {'OK' if ai.have_whisper() else 'not installed (optional)'}"]
    s = settings()
    caps.append(f"AI {'configured: ' + s['provider'] + ' / ' + s['model'] if ai.configured(s) else 'not configured (optional)'}")
    log("  " + " | ".join(caps))
    st = DB.stats()
    if st["corrections"] or st["aliases"]:
        log(f"  remembered: {st['corrections']} book correction(s), {st['aliases']} author alias(es), "
            f"{st['not_dupes']} dismissed duplicate group(s)")
    if IS_WINDOWS:
        if "WindowsApps" in (os.sys.executable or "") and "DATA_DIR" not in os.environ:
            log("  NOTE: this is Microsoft Store Python, which redirects AppData. In Explorer the data "
                "folder is really under %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python.3.*"
                "\\LocalCache\\Local\\LibraryOrganizer (or set DATA_DIR to choose your own).")
        log("Windows edition: use the Browse... buttons to pick your library and an output folder.")
        return
    for name, p, hint in (("Source", DEFAULT_SOURCE, "the ':/source:ro' volume line"),
                          ("Destination", DEFAULT_DEST, "the ':/dest' volume line")):
        if os.path.isdir(p):
            try:
                n = len(os.listdir(p))
                log(f"{name} mount {p}: OK - {n} entr{'y' if n == 1 else 'ies'} visible"
                    + ("  <- EMPTY: is the NAS path on the left of " + hint + " correct?" if n == 0 else ""))
            except OSError as e:
                log(f"{name} mount {p}: PERMISSION PROBLEM - {e}")
        else:
            log(f"{name} mount {p}: MISSING inside the container - check {hint} in your compose file")


def _autosaver():
    while True:
        time.sleep(30)
        if STATE.get("dirty"):
            save_plan()


def set_busy(task):
    with STATE["lock"]:
        if STATE["busy"]:
            return False
        STATE.update(busy=True, task=task, progress=[0, 0], task_start=time.time(), cancel=False)
    return True


def clear_busy():
    with STATE["lock"]:
        STATE["busy"], STATE["task"] = False, ""


def run_task(name, fn):
    """Run fn() in a background thread with the busy flag; one task at a time."""
    if not set_busy(name):
        return jsonify({"error": "Another task is running"}), 409

    def worker():
        try:
            fn()
        except Exception:
            log(f"{name} failed:\n" + traceback.format_exc())
        finally:
            save_plan()
            clear_busy()
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


def progress(cur, total):
    with STATE["lock"]:
        STATE["progress"] = [cur, total]


def cancelled():
    return STATE.get("cancel")


def refresh_status(b):
    if b.status in ("copied", "skipped") or b.status.startswith("ERROR"):
        return
    b.status = "ready" if (b.author and b.title) else "needs review"


# ---------------------------------------------------------------------------
# Decisions applied to a book (aliases + your corrections)
# ---------------------------------------------------------------------------
def apply_memory(b):
    if b.author:
        canon = DB.canonical_author(b.author)
        if canon and canon != b.author:
            b.author = canon
    corr = DB.correction(b.fingerprint)
    if corr:
        for k, v in corr.items():
            if k == "_status":
                if v == "skipped" and b.status != "copied":
                    b.status = "skipped"
                continue
            if k == "cover":
                if os.path.isfile(v):
                    b.cover = v
                    b.locked = sorted(set(b.locked) | {"cover"})
                continue
            if k in EDITABLE:
                setattr(b, k, v)
                b.locked = sorted(set(b.locked) | {k})
        if b.meta_source in ("filename", "folders", "tags") and any(k in corr for k in ("title", "author")):
            b.meta_source = "you"
    b.confidence = enrich.confidence(b)


def remember(b, fields: dict):
    """An edit YOU made: lock the fields and store them against the book's content."""
    b.locked = sorted(set(b.locked) | set(k for k in fields if k in EDITABLE or k == "cover"))
    if b.fingerprint:
        DB.save_correction(b.fingerprint, fields)


# ---------------------------------------------------------------------------
# Library-wide pass: things no single folder can tell you
# ---------------------------------------------------------------------------
JUNK_AUTHORS = {"unknown", "unknown author", "calibre", "n a", "none", "author", "admin",
                "administrator", "user", "various artists", "audiobook", "audiobooks"}
JUNK_TITLES = {"unknown", "untitled", "title", "new document", "document", "book", "audiobook"}


def library_pass():
    """1. Junk values ('calibre', 'Unknown') are blanked so a real source can fill them.
    2. 'Dune - Frank Herbert.mobi' parsed as author 'Dune': if the parsed TITLE is an
       author the rest of the library already knows and the parsed author is not,
       swap them."""
    from library_db import author_key
    junk = swapped = 0
    with STATE["lock"]:
        books = [b for b in STATE["books"].values() if b.status != "copied"]
        for b in books:
            if b.author and author_key(b.author) in JUNK_AUTHORS and "author" not in b.locked:
                b.author = ""
                junk += 1
            if b.title and re.sub(r"\W+", " ", b.title.lower()).strip() in JUNK_TITLES and "title" not in b.locked:
                b.title = ""
                junk += 1
        # re-apply remembered ebook format merges (epub + mobi of one book -> one folder)
        by_fp = {b.fingerprint: b for b in books if b.fingerprint}
        merged = 0
        for bid, b in list(STATE["books"].items()):
            corr = DB.correction(b.fingerprint) if b.kind == "ebook" else None
            keeper = by_fp.get((corr or {}).get("_merge_into"))
            if keeper and keeper is not b:
                have = {os.path.splitext(f)[1].lower() for f in keeper.files}
                keeper.files += [f for f in b.files if os.path.splitext(f)[1].lower() not in have]
                STATE["books"].pop(bid)
                merged += 1
        books = [b for b in STATE["books"].values() if b.status != "copied"]
        if merged:
            log(f"  {merged} remembered ebook format merge(s) re-applied")
        known = {author_key(b.author) for b in books
                 if b.author and not (b.meta_source or "").startswith("filename")}
        for b in books:
            if not (b.author and b.title) or {"author", "title"} & set(b.locked):
                continue
            ka, kt = author_key(b.author), author_key(b.title)
            if kt in known and ka not in known:
                b.author, b.title = core.clean_author(b.title), b.author
                b.meta_source = (b.meta_source or "filename") + " (swapped)"
                swapped += 1
        for b in books:
            b.confidence = enrich.confidence(b)
            refresh_status(b)
        touch()
    if junk or swapped:
        log(f"  library pass: {junk} junk value(s) cleared, {swapped} title/author swap(s) fixed "
            "using authors known elsewhere in the library")


def narrator_pass():
    """'Ray Porter - Project Hail Mary': flag books whose AUTHOR is a narrator -
    a famous one, or one this very library's tags name as a narrator - unless
    the same name is also an author of other books here."""
    from library_db import author_key
    n = 0
    with STATE["lock"]:
        books = [b for b in STATE["books"].values() if b.status != "copied"]
        narrators = {author_key(x) for b in books for x in (b.narrator or "").split(",") if x.strip()}
        by_author = {}
        for b in books:
            if b.author:
                by_author.setdefault(author_key(b.author), []).append(b)
        for key, bs in by_author.items():
            also_writes = any(b.narrator and author_key(b.narrator) != key for b in bs)
            suspect = (key in narrators and not also_writes) or enrich.is_known_narrator(bs[0].author)
            for b in bs:
                flag = "author looks like a narrator"
                if suspect and "author" not in b.locked and flag not in b.flags:
                    b.flags = b.flags + [flag]
                    n += 1
                elif not suspect and flag in b.flags:
                    b.flags = [f for f in b.flags if f != flag]
                b.confidence = enrich.confidence(b)
        touch()
    if n:
        log(f"  {n} book(s) have a narrator's name as author - flagged; online match / AI will fix them "
            "(filter 'low confidence').")


# ---------------------------------------------------------------------------
# Duplicate groups (cached per plan version)
# ---------------------------------------------------------------------------
def dup_groups():
    with STATE["lock"]:
        if STATE["dupes_ver"] != STATE["ver"] or STATE["dupes"] is None:
            books = {i: b for i, b in STATE["books"].items() if b.status != "copied"}
            STATE["dupes"] = dedupe.find_groups(books, DB.dismissed_dupes())
            STATE["dupes_ver"] = STATE["ver"]
        return STATE["dupes"]


def dup_ids():
    return {i for g in dup_groups() for i in g["ids"]}


# ---------------------------------------------------------------------------
# JSON views
# ---------------------------------------------------------------------------
def book_json(bid, b, layout="nested_series", dups=None):
    thr = settings()["threshold"]
    return {
        "id": bid, "kind": b.kind, "author": b.author, "series": b.series,
        "series_index": core.fmt_series_index(b.series_index), "title": b.title,
        "narrator": b.narrator, "src": b.src_display, "meta": b.meta_source, "status": b.status,
        "files": len(b.files), "conf": b.confidence, "low": bool(b.confidence < thr),
        "length": enrich.fmt_duration(b.duration), "cover": bool(b.cover),
        "health": b.health, "ai": bool(b.ai and b.ai.get("pending")), "flags": b.flags,
        "dup": bool(dups and bid in dups), "locked": b.locked,
        "dest": b.dest_folder("", layout).lstrip(os.sep) if (b.author or b.title) else "",
    }


@app.get("/")
def index():
    return render_template("index.html", source=STATE["source"] or DEFAULT_SOURCE, dest=DEFAULT_DEST,
                           have_mutagen=core.HAVE_MUTAGEN, version=VERSION)


@app.get("/api/state")
def api_state():
    since = int(request.args.get("log_since", 0))
    thr = settings()["threshold"]
    dups = dup_ids() if not STATE["busy"] else {i for g in (STATE["dupes"] or []) for i in g["ids"]}
    with STATE["lock"]:
        c = {"total": len(STATE["books"]), "ready": 0, "review": 0, "copied": 0, "error": 0,
             "skipped": 0, "low": 0, "damaged": 0, "ai": 0}
        for b in STATE["books"].values():
            if b.status == "copied":
                c["copied"] += 1
            elif b.status == "skipped":
                c["skipped"] += 1
            elif b.status.startswith("ERROR"):
                c["error"] += 1
            elif not b.author or not b.title:
                c["review"] += 1
            else:
                c["ready"] += 1
            if b.status not in ("copied", "skipped"):
                c["low"] += b.confidence < thr
                c["damaged"] += bool(b.health)
                c["ai"] += bool(b.ai and b.ai.get("pending"))
        c["dups"] = len(dups)
        return jsonify({"busy": STATE["busy"], "task": STATE["task"], "progress": STATE["progress"],
                        "counts": c, "log": STATE["log"][since:], "log_next": len(STATE["log"]),
                        "elapsed": (time.time() - STATE.get("task_start", time.time())) if STATE["busy"] else 0})


def _filter(bid, b, status, q, dups, thr):
    if status == "review" and (b.author and b.title):
        return False
    if status == "copied" and b.status != "copied":
        return False
    if status == "pending" and b.status in ("copied", "skipped"):
        return False
    if status == "skipped" and b.status != "skipped":
        return False
    if status == "dups" and bid not in dups:
        return False
    if status == "low" and (b.confidence >= thr or b.status in ("copied", "skipped")):
        return False
    if status == "damaged" and not b.health:
        return False
    if status == "ai" and not (b.ai and b.ai.get("pending")):
        return False
    if status == "nocover" and b.cover:
        return False
    if q and q not in f"{b.author} {b.series} {b.title} {b.narrator} {b.src_display}".lower():
        return False
    return True


@app.get("/api/books")
def api_books():
    q = request.args.get("q", "").lower().strip()
    status = request.args.get("status", "")
    layout = request.args.get("layout", "nested_series")
    sort = request.args.get("sort", "author")
    core.NUMBER_FOLDERS = request.args.get("number_folders", "0") == "1"
    page = max(1, int(request.args.get("page", 1)))
    per = min(2000, max(10, int(request.args.get("per_page", 200))))
    thr = settings()["threshold"]
    dups = dup_ids()
    with STATE["lock"]:
        items = [(i, b) for i, b in STATE["books"].items() if _filter(i, b, status, q, dups, thr)]
        if sort == "conf":
            items.sort(key=lambda x: (x[1].confidence, x[1].author.lower()))
        else:
            items.sort(key=lambda x: (x[1].author.lower(), x[1].series.lower(),
                                      core.fmt_series_index(x[1].series_index), x[1].title.lower()))
        total = len(items)
        out = [book_json(i, b, layout, dups) for i, b in items[(page - 1) * per: page * per]]
    return jsonify({"total": total, "page": page, "pages": max(1, -(-total // per)), "books": out})


@app.get("/api/book/<int:bid>")
def api_book_detail(bid):
    with STATE["lock"]:
        b = STATE["books"].get(bid)
        if not b:
            return jsonify({"error": "not found"}), 404
        d = book_json(bid, b)
        d.update({k: getattr(b, k) for k in ("year", "publisher", "description", "genres", "language",
                                             "asin", "isbn", "bitrate", "codec", "chapters",
                                             "cover_info", "online_best", "ai", "transcript",
                                             "fingerprint")})
        d["duration"] = b.duration
        d["file_list"] = [os.path.relpath(f, STATE["source"]) if STATE["source"] and f.startswith(STATE["source"])
                          else f for f in b.files[:60]]
        d["cover_candidates"] = [dict(c, idx=n) for n, c in enumerate(b.cover_candidates or [])]
        d["cover_path_ok"] = bool(b.cover and os.path.isfile(b.cover))
    return jsonify(d)


@app.post("/api/book/<int:bid>")
def api_edit(bid):
    data = request.get_json(force=True)
    with STATE["lock"]:
        b = STATE["books"].get(bid)
        if not b:
            return jsonify({"error": "not found"}), 404
        changed = {}
        for k in EDITABLE:
            if k in data:
                v = str(data[k]).strip()
                if v != (getattr(b, k) or ""):
                    setattr(b, k, v)
                    changed[k] = v
        if changed:
            remember(b, changed)
            if "title" in changed or "author" in changed:
                b.meta_source = "you"
        b.confidence = enrich.confidence(b)
        refresh_status(b)
        touch()
        return jsonify(book_json(bid, b))


@app.delete("/api/book/<int:bid>")
def api_remove(bid):
    with STATE["lock"]:
        STATE["books"].pop(bid, None)
        touch()
    return jsonify({"ok": True})


@app.post("/api/book/<int:bid>/skip")
def api_skip(bid):
    with STATE["lock"]:
        b = STATE["books"].get(bid)
        if not b:
            return jsonify({"error": "not found"}), 404
        if b.status == "skipped":
            b.status = ""
            refresh_status(b)
            DB.save_correction(b.fingerprint, {"_status": ""})
        elif b.status != "copied":
            b.status = "skipped"
            DB.save_correction(b.fingerprint, {"_status": "skipped"})
        touch()
        return jsonify(book_json(bid, b))


@app.post("/api/book/<int:bid>/ai_accept")
def api_ai_accept(bid):
    with STATE["lock"]:
        b = STATE["books"].get(bid)
        if not b or not b.ai:
            return jsonify({"error": "no AI suggestion for this book"}), 404
        fields = {k: b.ai[k] for k in ("title", "author", "series", "series_index", "narrator", "year")
                  if b.ai.get(k)}
        for k, v in fields.items():
            setattr(b, k, core.clean_author(v) if k == "author" else v)
        remember(b, fields)
        b.meta_source = "AI (accepted)"
        b.ai["pending"] = False
        b.confidence = enrich.confidence(b)
        refresh_status(b)
        touch()
        return jsonify(book_json(bid, b))


@app.post("/api/book/<int:bid>/cover")
def api_choose_cover(bid):
    data = request.get_json(force=True)
    with STATE["lock"]:
        b = STATE["books"].get(bid)
    if not b:
        return jsonify({"error": "not found"}), 404
    if data.get("url"):
        try:
            raw = enrich._get(data["url"].strip(), raw=True, timeout=20)
            w, h = enrich.image_size(raw)
            if not w:
                return jsonify({"error": "That URL did not return a JPEG/PNG image"}), 400
            p = enrich._save_candidate(raw, DATA_DIR)
            b.cover_candidates = [{"path": p, "origin": "pasted URL", "w": w, "h": h,
                                   "score": enrich.cover_score(w, h, b.kind)}] + (b.cover_candidates or [])
            chosen = b.cover_candidates[0]
        except Exception as e:
            return jsonify({"error": f"Could not download the image: {e}"}), 400
    else:
        idx = int(data.get("idx", -1))
        if not (0 <= idx < len(b.cover_candidates or [])):
            return jsonify({"error": "bad cover choice"}), 400
        chosen = b.cover_candidates[idx]
    with STATE["lock"]:
        b.cover = chosen["path"]
        b.cover_info = f"{chosen['w']}x{chosen['h']} {chosen['origin']}"
        remember(b, {"cover": b.cover})
        touch()
    return jsonify({"ok": True, "cover_info": b.cover_info})


def _allowed_image(path: str) -> bool:
    path = os.path.abspath(path)
    if path.startswith(os.path.abspath(os.path.join(DATA_DIR, "covers")) + os.sep):
        return True
    with STATE["lock"]:
        return any(path == os.path.abspath(c["path"]) for b in STATE["books"].values()
                   for c in (b.cover_candidates or []))


@app.get("/api/book/<int:bid>/cover.img")
def api_cover_img(bid):
    with STATE["lock"]:
        b = STATE["books"].get(bid)
    idx = request.args.get("c")
    path = (b.cover_candidates[int(idx)]["path"] if b and idx is not None
            and int(idx) < len(b.cover_candidates or []) else (b.cover if b else ""))
    if not path or not os.path.isfile(path) or not _allowed_image(path):
        abort(404)
    return send_file(path, max_age=3600)


# ---------------------------------------------------------------------------
# Bulk / export
# ---------------------------------------------------------------------------
@app.post("/api/bulk")
def api_bulk():
    data = request.get_json(force=True)
    ids = data.get("ids") or []
    action = data.get("action", "")
    if ids and action in ("skip", "unskip"):
        n = 0
        with STATE["lock"]:
            for bid in ids:
                b = STATE["books"].get(bid)
                if not b or b.status == "copied":
                    continue
                b.status = "skipped" if action == "skip" else ""
                refresh_status(b)
                DB.save_correction(b.fingerprint, {"_status": b.status if action == "skip" else ""})
                n += 1
            touch()
        log(f"{'Skipped' if action == 'skip' else 'Un-skipped'} {n} book(s).")
        return jsonify({"ok": True, "count": n})
    fields = {k: str(v).strip() for k, v in (data.get("fields") or {}).items()
              if k in EDITABLE and str(v).strip() != ""}
    if not ids or not fields:
        return jsonify({"error": "Select some rows and fill in at least one field"}), 400
    n = 0
    with STATE["lock"]:
        for bid in ids:
            b = STATE["books"].get(bid)
            if not b:
                continue
            for k, v in fields.items():
                setattr(b, k, v)
            remember(b, fields)
            b.confidence = enrich.confidence(b)
            refresh_status(b)
            n += 1
        touch()
    log(f"Bulk edit applied to {n} book(s): " + ", ".join(f"{k}='{v}'" for k, v in fields.items()))
    return jsonify({"ok": True, "count": n})


@app.get("/api/export.csv")
def api_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kind", "author", "series", "series_index", "title", "narrator", "year", "asin", "isbn",
                "length", "confidence", "status", "meta_from", "cover", "health", "source_path",
                "files", "destination"])
    with STATE["lock"]:
        for b in sorted(STATE["books"].values(), key=lambda b: (b.author.lower(), b.series.lower(),
                        core.fmt_series_index(b.series_index), b.title.lower())):
            w.writerow([b.kind, b.author, b.series, core.fmt_series_index(b.series_index), b.title,
                        b.narrator, b.year, b.asin, b.isbn, enrich.fmt_duration(b.duration),
                        b.confidence, b.status, b.meta_source, b.cover_info, b.health,
                        b.src_display, len(b.files), b.dest_folder("").lstrip(os.sep)])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=library-plan.csv"})


@app.get("/api/journal.csv")
def api_journal():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "action", "source", "destination", "detail"])
    for ts, action, src, dest, detail in DB.journal_rows():
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), action, src, dest, detail])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=organizer-journal.csv"})


# ---------------------------------------------------------------------------
# Folder browsing
# ---------------------------------------------------------------------------
def _windows_drives():
    """Drive letters from the Windows drive bitmask - instant, and it never
    touches the drives, so a disconnected mapped NAS drive can't stall the
    folder picker (checking each letter with os.path.exists could hang
    20-30 s per dead network drive)."""
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    except Exception:
        import string
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def _listdirs(path, timeout=10):
    """Subfolders of `path`, giving up after `timeout` s (dead network drive)."""
    def work():
        return sorted(d for d in os.listdir(path)
                      if not d.startswith(".") and d not in ("$RECYCLE.BIN", "System Volume Information")
                      and os.path.isdir(os.path.join(path, d)))
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(work).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


@app.get("/api/browse")
def api_browse():
    raw = request.args.get("path", "") or ("" if IS_WINDOWS else "/")
    if IS_WINDOWS and raw in ("", "/", "\\", "My Computer"):
        return jsonify({"path": "My Computer", "parent": None, "dirs": _windows_drives()})
    path = os.path.normpath(raw.strip().strip('"'))
    if IS_WINDOWS and re.fullmatch(r"[A-Za-z]:", path):
        path += "\\"
    try:
        dirs = _listdirs(path)
    except PermissionError:
        return jsonify({"error": f"Permission denied: {path}"}), 403
    except (FileNotFoundError, NotADirectoryError):
        return jsonify({"error": f"Not a folder (or not reachable): {path}"}), 400
    except Exception as e:
        if type(e).__name__ == "TimeoutError" or "Timeout" in type(e).__name__:
            return jsonify({"error": f"{path} is not responding - a disconnected network drive? "
                            "Try its \\\\server\\share path instead."}), 504
        return jsonify({"error": f"Cannot open {path}: {e}"}), 400
    if IS_WINDOWS:
        parent = os.path.dirname(path.rstrip("\\"))
        if not parent or parent == path or re.fullmatch(r"[A-Za-z]:\\?", path):
            parent = "My Computer"
    else:
        parent = os.path.dirname(path) if path != "/" else None
    return jsonify({"path": path, "parent": parent, "dirs": dirs})


# ---------------------------------------------------------------------------
# 1. Scan
# ---------------------------------------------------------------------------
def do_scan(source, opts):
    core.STRIP_ALL_PARENS = bool(opts.get("strip_parens", False))
    core.TRUST_FOLDERS = bool(opts.get("trust_folders", True))
    core.DISTRUST_JSON = bool(opts.get("distrust_json", False))
    core.DISTRUST_OPF = bool(opts.get("distrust_opf", False))
    core.DISTRUST_TAGS = bool(opts.get("distrust_tags", False))
    with STATE["lock"]:
        STATE["books"].clear()
        STATE["next_id"] = 1
        STATE["source"] = source
        touch()
    log(f"Scanning {source} ...")
    try:
        top = sorted(os.listdir(source))
        log(f"  top level: {len(top)} entries" + (" - e.g. " + ", ".join(top[:5]) if top else
            f" - {source} is EMPTY. Check the source path / volume mount."))
    except OSError as e:
        log(f"  cannot list {source}: {e}")
    n = remembered = 0
    for b in core.scan_library(source, bool(opts.get("audio", True)), bool(opts.get("ebooks", True)), log):
        if cancelled():
            log(f"Scan stopped by user after {n} book(s) - they are in the table.")
            break
        b.fingerprint = enrich.book_fingerprint(b, DB)
        before = (b.title, b.author)
        apply_memory(b)
        remembered += (b.title, b.author) != before or bool(b.locked)
        refresh_status(b)
        with STATE["lock"]:
            STATE["books"][STATE["next_id"]] = b
            STATE["next_id"] += 1
        n += 1
        if n % 100 == 0:
            log(f"  ... {n} books found so far")
            progress(n, 0)
    library_pass()
    narrator_pass()
    log(f"Scan complete: {n} book(s) found" + (f", {remembered} restored from your earlier decisions" if remembered else "")
        + ". Next: Enrich (audio scan, online match, covers).")


@app.post("/api/scan")
def api_scan():
    data = request.get_json(force=True)
    source = data.get("source", DEFAULT_SOURCE)
    if not os.path.isdir(source):
        return jsonify({"error": f"Source folder not found: {source}. Check your volume mounts."}), 400
    return run_task("scan", lambda: do_scan(source, data))


# ---------------------------------------------------------------------------
# 2. Enrich
# ---------------------------------------------------------------------------
def _target_ids(data, default="all"):
    ids = data.get("ids") or []
    if ids:
        return ids
    which = data.get("which", default)
    thr = settings()["threshold"]
    with STATE["lock"]:
        items = [(i, b) for i, b in STATE["books"].items() if b.status not in ("copied", "skipped")]
        if which == "low":
            items = [(i, b) for i, b in items if b.confidence < thr]
        elif which == "new":
            items = [(i, b) for i, b in items if not b.enriched]
    return [i for i, _ in items]


def do_enrich(ids, opts):
    s = settings()
    online, covers = bool(opts.get("online", True)), bool(opts.get("covers", True))
    fix = bool(opts.get("fix_names", True))
    intro = None
    if s.get("transcribe") and ai.have_whisper():
        intro = lambda b: ai.transcribe_intro(b.files[0], s)
        log("  speech-to-text on: audiobooks with nothing usable get their intro transcribed first")
    log(f"Enriching {len(ids)} book(s): audio probe + deep metadata"
        + (" + online match" if online else "") + (" + covers" if covers else "") + " ...")
    done, matched, gotcover, damaged = 0, 0, 0, 0
    lock = threading.Lock()

    def one(bid):
        nonlocal done, matched, gotcover, damaged
        if cancelled():
            return
        b = STATE["books"].get(bid)
        if not b:
            return
        try:
            rep = enrich.enrich_book(b, DB, DATA_DIR, online=online, covers=covers, fix_names=fix,
                                     region=s.get("audible_region", "com"), log=log, intro=intro)
            apply_memory(b)            # your locked fields and author aliases always win
            refresh_status(b)
            with lock:
                done += 1
                matched += bool(rep.get("match"))
                gotcover += bool(b.cover)
                damaged += bool(b.health)
                progress(done, len(ids))
            if b.health:
                log(f"  DAMAGED? {b.src_display}: {b.health}")
        except Exception as e:
            log(f"  enrich failed for {b.src_display}: {e}")

    with ThreadPoolExecutor(max_workers=2 if intro else (6 if online else 3)) as pool:
        list(pool.map(one, ids))
    narrator_pass()
    touch()
    log(f"Enrich finished: {done} book(s); {matched} confident online match(es); "
        f"{gotcover} with a cover; {damaged} look damaged."
        + (" Stopped early." if cancelled() else ""))


@app.post("/api/enrich")
def api_enrich():
    data = request.get_json(force=True) or {}
    ids = _target_ids(data)
    if not ids:
        return jsonify({"error": "Nothing to enrich - scan first"}), 400
    return run_task("enrich", lambda: do_enrich(ids, data))


# ---------------------------------------------------------------------------
# 3. AI
# ---------------------------------------------------------------------------
def do_ai(ids):
    s = settings()
    if not ai.configured(s):
        log("AI is not configured - open the AI & Settings tab.")
        return
    ids = ids[: int(s.get("max_books") or 300)]
    log(f"AI identify: {len(ids)} book(s) with {s['provider']} / {s['model']} "
        f"(auto-apply at >= {s['auto_apply']}% confidence) ...")
    applied = suggested = failed = 0
    for n, bid in enumerate(ids, 1):
        if cancelled():
            log("AI run stopped by user.")
            break
        progress(n, len(ids))
        b = STATE["books"].get(bid)
        if not b:
            continue
        try:
            if not b.enriched:
                enrich.enrich_book(b, DB, DATA_DIR, online=True, covers=False, log=log)
            elif not getattr(b, "_candidates", None) and b.title:
                b._candidates = enrich.online_candidates(b, DB, s.get("audible_region", "com"))[:5]
            if b.kind == "ebook" and not b.front_text:
                b.front_text = enrich.ebook_front_text(b)[:3000]
            if b.kind == "audio" and s.get("transcribe") and not b.transcript and b.files:
                try:
                    b.transcript = ai.transcribe_intro(b.files[0], s)
                except Exception as e:
                    log(f"  transcription failed for {b.src_display}: {e}")
            res = ai.identify(b, s, DB)
        except Exception as e:
            failed += 1
            log(f"  AI failed for {b.src_display}: {e}")
            if failed >= 5 and failed == n:
                log("AI: first calls all failed - stopping. Check the key / model / URL in Settings.")
                break
            continue
        conf = res.get("confidence", 0)
        with STATE["lock"]:
            b.ai = dict(res, pending=True)
            locked = set(b.locked)
            need = int(s.get("auto_apply", 75))
            if enrich.is_generic_title(res.get("title") or b.title):
                need = max(need, 90)      # generic titles: only near-certain answers auto-apply
            if conf >= need and res.get("title") and res.get("author"):
                for k in ("title", "author", "series", "series_index", "narrator", "year"):
                    if res.get(k) and k not in locked:
                        setattr(b, k, core.clean_author(res[k]) if k == "author" else res[k])
                b.meta_source = "AI"
                b.match_bonus = max(b.match_bonus, conf - 60)
                b.ai["pending"] = False
                b.flags = [f for f in b.flags if "narrator" not in f] if res.get("author") != b.narrator else b.flags
                apply_memory(b)
                applied += 1
                log(f"  AI {conf}%: {b.author} - {b.title}" + (" (cached)" if res.get("cached") else ""))
            else:
                suggested += 1
                log(f"  AI suggests ({conf}%): {res.get('author')} - {res.get('title')}  <- review: {b.src_display}")
            b.confidence = enrich.confidence(b)
            refresh_status(b)
            touch()
    log(f"AI finished: {applied} applied, {suggested} left as suggestions (filter: 'AI suggestions'), {failed} failed.")


@app.post("/api/ai")
def api_ai():
    data = request.get_json(force=True) or {}
    if not ai.configured(settings()):
        return jsonify({"error": "AI isn't set up yet - add a key (or a local model URL) in AI & Settings."}), 400
    ids = _target_ids(data, default="low")
    if not ids:
        return jsonify({"error": "No books below the confidence threshold - nothing to ask."}), 400
    return run_task("ai", lambda: do_ai(ids))


@app.get("/api/settings")
def api_settings_get():
    s = settings()
    s["api_key"] = ("*" * 8 + s["api_key"][-4:]) if s.get("api_key") else ""
    s["have_whisper"] = ai.have_whisper()
    s["data_dir"] = DATA_DIR
    s["db"] = DB.stats()
    return jsonify(s)


@app.post("/api/settings")
def api_settings_post():
    data = request.get_json(force=True) or {}
    s = settings()
    for k in ai.DEFAULTS:
        if k in data:
            if k == "api_key" and (not data[k] or str(data[k]).startswith("*")):
                continue
            s[k] = type(ai.DEFAULTS[k])(data[k]) if not isinstance(ai.DEFAULTS[k], bool) else bool(data[k])
    if data.get("clear_key"):
        s["api_key"] = ""
    ai.save_settings(SETTINGS_FILE, s)
    touch()
    return jsonify({"ok": True})


@app.post("/api/settings/test")
def api_settings_test():
    try:
        return jsonify({"ok": True, "reply": ai.test_connection(settings())})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# 4a. Duplicates
# ---------------------------------------------------------------------------
@app.get("/api/dupes")
def api_dupes():
    groups = dup_groups()
    out = []
    with STATE["lock"]:
        for g in groups:
            members = []
            for bid in g["ids"]:
                b = STATE["books"].get(bid)
                if not b:
                    continue
                st = dedupe.stats(b)
                members.append({"id": bid, "title": b.title, "author": b.author, "series": b.series,
                                "series_index": core.fmt_series_index(b.series_index),
                                "src": b.src_display, "files": st["nfiles"],
                                "size_mb": round(st["size"] / 1e6, 1), "formats": " ".join(st["exts"]),
                                "length": enrich.fmt_duration(b.duration),
                                "bitrate": round(b.bitrate / 1000) if b.bitrate else "",
                                "chapters": b.chapters, "cover": bool(b.cover), "health": b.health,
                                "status": b.status, "quality": g["quality"].get(bid)})
            out.append({"key": g["key"], "reasons": g["reasons"], "keep": g["keep"],
                        "merge_formats": g["merge_formats"], "members": members})
    return jsonify({"groups": out})


def _resolve(g, keep_id, action):
    with STATE["lock"]:
        if action == "dismiss":
            DB.dismiss_dupe(g["key"])
            touch()
            return 0
        keeper = STATE["books"].get(keep_id)
        n = 0
        for bid in g["ids"]:
            if bid == keep_id:
                continue
            b = STATE["books"].get(bid)
            if not b or b.status == "copied":
                continue
            if action == "merge" and keeper and b.kind == "ebook":
                have = {os.path.splitext(f)[1].lower() for f in keeper.files}
                for f in b.files:
                    if os.path.splitext(f)[1].lower() not in have:
                        keeper.files.append(f)
                        have.add(os.path.splitext(f)[1].lower())
                STATE["books"].pop(bid, None)
                DB.save_correction(b.fingerprint, {"_merge_into": keeper.fingerprint})
                DB.journal("merge-formats", b.src_display, keeper.src_display)
            else:
                b.status, b.dup_skipped = "skipped", True
                DB.save_correction(b.fingerprint, {"_status": "skipped"})
                DB.journal("skip-duplicate", b.src_display, keeper.src_display if keeper else "")
            n += 1
        if keeper and keeper.status == "skipped":
            keeper.status = ""
            refresh_status(keeper)
        touch()
        return n


@app.post("/api/dupes/resolve")
def api_dupes_resolve():
    data = request.get_json(force=True)
    g = next((g for g in dup_groups() if g["key"] == data.get("key")), None)
    if not g:
        return jsonify({"error": "group not found (plan changed?) - reload"}), 404
    n = _resolve(g, int(data.get("keep") or g["keep"]), data.get("action", "keep"))
    return jsonify({"ok": True, "changed": n})


@app.post("/api/dedupe")
def api_dedupe():
    """Resolve EVERY group. policy 'smart' = recommended keeper (merge ebook formats);
    the v1 policies are still accepted."""
    policy = (request.get_json(force=True) or {}).get("policy", "smart")
    keyfns = {"keep_newest": lambda st, b: (st["mtime"], st["size"]),
              "keep_oldest": lambda st, b: (-st["mtime"], st["size"]),
              "keep_largest": lambda st, b: (st["size"], st["mtime"]),
              "keep_best_format": lambda st, b: (-st["rank"], st["size"], st["mtime"]),
              "keep_fewest_files": lambda st, b: (-st["nfiles"], -st["rank"], st["size"])}
    if policy in ("manual", "keep_all"):
        return jsonify({"ok": True, "groups": 0, "skipped": 0})
    groups = dup_groups()
    n_groups = n_changed = 0
    for g in list(groups):
        with STATE["lock"]:
            live = [(bid, STATE["books"][bid]) for bid in g["ids"] if bid in STATE["books"]]
        if len(live) < 2:
            continue
        if policy == "smart":
            keep, action = g["keep"], ("merge" if g["merge_formats"] else "keep")
        elif policy in keyfns:
            keep = max(live, key=lambda m: keyfns[policy](dedupe.stats(m[1]), m[1]))[0]
            action = "keep"
        else:
            return jsonify({"error": f"Unknown policy {policy}"}), 400
        n_changed += _resolve(g, keep, action)
        n_groups += 1
    log(f"Duplicates ({policy}): {n_groups} group(s) resolved, {n_changed} book(s) skipped or merged.")
    return jsonify({"ok": True, "groups": n_groups, "skipped": n_changed})


# ---------------------------------------------------------------------------
# 4b. Authors
# ---------------------------------------------------------------------------
def author_counts():
    counts = {}
    with STATE["lock"]:
        for b in STATE["books"].values():
            if b.author and b.status != "copied":
                counts[b.author] = counts.get(b.author, 0) + 1
    return counts


@app.get("/api/authors")
def api_authors():
    return jsonify({"groups": authors_mod.cluster(author_counts(), DB.rejected_pairs())})


def merge_authors(variants, canonical):
    canonical = canonical.strip()
    vs = set(variants)
    n = 0
    with STATE["lock"]:
        for b in STATE["books"].values():
            if b.author in vs and b.author != canonical and b.status != "copied":
                b.author = canonical
                b.confidence = enrich.confidence(b)
                n += 1
        touch()
    for v in vs:
        if v != canonical:
            DB.add_alias(v, canonical)
    DB.add_alias(canonical, canonical)
    return n


@app.post("/api/authors/merge")
def api_authors_merge():
    data = request.get_json(force=True)
    if not data.get("canonical") or not data.get("variants"):
        return jsonify({"error": "need variants and a canonical name"}), 400
    n = merge_authors(data["variants"], data["canonical"])
    log(f"Authors: {len(data['variants'])} variant(s) -> '{data['canonical']}' ({n} book(s)); remembered.")
    return jsonify({"ok": True, "books": n})


@app.post("/api/authors/reject")
def api_authors_reject():
    vs = (request.get_json(force=True) or {}).get("variants") or []
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            DB.reject_pair(vs[i], vs[j])
    return jsonify({"ok": True})


@app.post("/api/authors/auto")
def api_authors_auto():
    groups = [g for g in authors_mod.cluster(author_counts(), DB.rejected_pairs()) if g["confidence"] == "high"]
    total = sum(merge_authors([v for v, _ in g["variants"]], g["canonical"]) for g in groups)
    log(f"Authors: {len(groups)} safe (high-confidence) group(s) merged, {total} book(s) updated.")
    return jsonify({"ok": True, "groups": len(groups), "books": total})


# ---------------------------------------------------------------------------
# Autopilot: scan -> enrich -> safe author merges -> AI on the uncertain -> dupes
# (never copies - you still review and press Copy)
# ---------------------------------------------------------------------------
@app.post("/api/autopilot")
def api_autopilot():
    data = request.get_json(force=True) or {}
    source = data.get("source", DEFAULT_SOURCE)
    if not os.path.isdir(source):
        return jsonify({"error": f"Source folder not found: {source}"}), 400

    def run():
        do_scan(source, data)
        if cancelled():
            return
        do_enrich(_target_ids({}), data)
        if cancelled():
            return
        groups = [g for g in authors_mod.cluster(author_counts(), DB.rejected_pairs()) if g["confidence"] == "high"]
        merged = sum(merge_authors([v for v, _ in g["variants"]], g["canonical"]) for g in groups)
        log(f"Autopilot: {len(groups)} safe author merge(s), {merged} book(s) updated.")
        if data.get("use_ai", True) and ai.configured(settings()):
            ids = _target_ids({"which": "low"})
            if ids:
                do_ai(ids)
        if data.get("resolve_dupes", True):
            n = 0
            for g in dup_groups():
                n += _resolve(g, g["keep"], "merge" if g["merge_formats"] else "keep")
            log(f"Autopilot: duplicates resolved with the recommended keeper ({n} book(s) skipped/merged) "
                "- check the Duplicates tab and the 'skipped' filter.")
        with STATE["lock"]:
            low = sum(1 for b in STATE["books"].values()
                      if b.status not in ("copied", "skipped") and b.confidence < settings()["threshold"])
        log(f"Autopilot done. {low} book(s) still below the confidence threshold - "
            "filter 'low confidence' to review them, then Copy.")
    return run_task("autopilot", run)


# ---------------------------------------------------------------------------
# 5. Copy
# ---------------------------------------------------------------------------
@app.post("/api/cancel")
def api_cancel():
    with STATE["lock"]:
        if not STATE["busy"]:
            return jsonify({"error": "Nothing is running"}), 400
        STATE["cancel"] = True
    log("Stop requested - finishing the current book, then stopping...")
    return jsonify({"ok": True})


@app.post("/api/copy")
def api_copy():
    data = request.get_json(force=True)
    dest = data.get("dest", DEFAULT_DEST)
    source = os.path.normpath(data.get("source", STATE["source"] or DEFAULT_SOURCE))
    if os.path.normpath(dest).startswith(source + os.sep) or os.path.normpath(dest) == source:
        return jsonify({"error": "Destination must not be inside the source folder."}), 400
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create destination {dest}: {e}. Is it mounted read-write?"}), 400
    with STATE["lock"]:
        todo = [(i, b) for i, b in STATE["books"].items() if b.status not in ("copied", "skipped")]
    held = []
    if data.get("hold_damaged", True):
        held += [(i, b) for i, b in todo if b.health]
    if data.get("hold_review", True):
        held += [(i, b) for i, b in todo if not (b.author and b.title) and not b.health]
    if data.get("min_conf"):
        held += [(i, b) for i, b in todo if b.confidence < int(data["min_conf"]) and (i, b) not in held]
    if held:
        ids = {i for i, _ in held}
        todo = [(i, b) for i, b in todo if i not in ids]
        log(f"Holding back {len(held)} book(s) (damaged, missing author/title, or below the minimum "
            "confidence) - they stay in the plan for review.")
    if not todo:
        return jsonify({"error": "Nothing to copy"}), 400
    layout = data.get("layout", "nested_series")
    core.NUMBER_FOLDERS = bool(data.get("number_folders", False))
    keep_all = data.get("dup_policy") == "keep_all"
    if not data.get("force") and not keep_all:
        seen, collisions = set(), 0
        for _, b in todo:
            d = b.dest_folder(dest, layout).lower()
            collisions += d in seen
            seen.add(d)
        if collisions:
            return jsonify({"error": f"{collisions} book(s) would land in a folder another book already "
                            "uses with this structure - their files would be mixed together. Resolve the "
                            "Duplicates tab first, or pick a more specific structure."}), 507
        need = sum(os.path.getsize(f) for _, b in todo for f in b.files + b.extras if os.path.exists(f))
        free = shutil.disk_usage(dest).free
        if need > free * 0.98:
            gb = 1024 ** 3
            return jsonify({"error": f"Not enough space: this copy needs about {need/gb:.1f} GB but the "
                            f"destination has only {free/gb:.1f} GB free.", "code": "space"}), 507
    opts = dict(write_opf=bool(data.get("write_opf", True)), layout=layout,
                rename_parts=bool(data.get("rename_parts", False)), write_json=bool(data.get("write_json", True)),
                embed=bool(data.get("embed", True)), write_cover=bool(data.get("write_cover", True)),
                embed_cover=bool(data.get("embed_cover", True)))

    def run():
        done, used = 0, set()
        for n, (bid, b) in enumerate(todo, 1):
            if cancelled():
                log(f"Copy stopped by user - {done} book(s) copied; copied books are skipped next run.")
                break
            progress(n, len(todo))
            override = None
            if keep_all:
                base = b.dest_folder(dest, layout)
                cand, k = base, 2
                while cand.lower() in used or (cand != base and os.path.isdir(cand)):
                    cand, k = f"{base} ({k})", k + 1
                used.add(cand.lower())
                override = cand if cand != base else None
            try:
                folder = core.copy_book(b, dest, log, folder_override=override,
                                        journal=lambda a, s, d: DB.journal(a, s, d, b.title), **opts)
                b.status = "copied"
                done += 1
                log(f"Copied: {os.path.relpath(folder, dest)}")
            except Exception as e:
                b.status = f"ERROR: {e}"
                DB.journal("error", b.src_display, "", str(e))
                log(f"ERROR copying '{b.title}': {e}")
            touch()
        log(f"Finished: {done}/{len(todo)} book(s) copied to {dest}. "
            "Point Audiobookshelf or Calibre (Add books > from folders, one book per folder) at it.")
    return run_task("copy", run)


load_plan()
_mount_report()
threading.Thread(target=_autosaver, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    host = os.environ.get("HOST", "127.0.0.1" if IS_WINDOWS else "0.0.0.0")
    print(f"Library Organizer web UI on http://{host}:{port}", flush=True)
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
