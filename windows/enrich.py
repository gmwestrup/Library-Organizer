#!/usr/bin/env python3
"""
Library Organizer - enrichment engine.

Runs after the scan (which finds and names books) and fills in everything a
media server wants but folder names can't give you:

  * content fingerprints  - identical copies found even when renamed
  * audio probe           - real duration, bitrate, codec, chapters, damage
  * deep local metadata   - narrator, year, publisher, description, genres,
                            ASIN / ISBN from tags, sidecars and the epub OPF
  * ebook text            - the title page / copyright page text, with any
                            ISBN on it (the single best key for an exact match)
  * online match          - Audible (duration-aware: the candidate whose
                            runtime matches YOUR file wins), Google Books,
                            Open Library, ISBN lookups
  * covers                - every candidate (embedded, folder, Audible,
                            Google, Open Library) measured and ranked; the
                            best is written as cover.jpg and embedded
  * confidence            - a 0-100 score from how well the sources agree

Nothing here touches the originals.
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from html import unescape

import core

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    from rapidfuzz import fuzz as _fuzz

    def ratio(a: str, b: str) -> float:
        return _fuzz.token_sort_ratio(a or "", b or "") / 100.0
except ImportError:
    def ratio(a: str, b: str) -> float:
        a = " ".join(sorted((a or "").lower().split()))
        b = " ".join(sorted((b or "").lower().split()))
        return core.similar(a, b)

FFPROBE = shutil.which("ffprobe")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
UA = "LibraryOrganizer/2.0 (+https://github.com/gmwestrup/Library-Organizer)"


# ============================================================================
# Fingerprints
# ============================================================================

def file_fingerprint(path: str, db=None, chunk: int = 65536) -> str:
    """Fast content fingerprint: size + first 64 KB + last 64 KB (SHA-1).
    Identical files always match; renamed copies are caught; a 100k-file
    library is read in minutes, not hours. Cached by path+size+mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if db:
        fp = db.cached_fp(path, st.st_size, st.st_mtime)
        if fp:
            return fp
    h = hashlib.sha1(str(st.st_size).encode())
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(chunk))
            if st.st_size > chunk * 2:
                fh.seek(-chunk, os.SEEK_END)
                h.update(fh.read(chunk))
    except OSError:
        return ""
    fp = h.hexdigest()
    if db:
        db.remember_fp(path, st.st_size, st.st_mtime, fp)
    return fp


def book_fingerprint(book, db=None) -> str:
    fps = sorted(filter(None, (file_fingerprint(f, db) for f in book.files)))
    if not fps:
        return ""
    return hashlib.sha1("|".join(fps).encode()).hexdigest()[:20]


# ============================================================================
# Audio probe
# ============================================================================

def _ffprobe(path: str) -> dict:
    if not FFPROBE:
        return {}
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                              "-show_format", "-show_streams", "-show_chapters", path],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def probe_audio(book, log=None) -> dict:
    """Duration (sum of parts), bitrate/codec of the first part, chapter
    count, and a health check. mutagen first (fast, header-only), ffprobe
    as a fallback for formats mutagen can't read."""
    total, bitrate, codec, chapters, bad = 0.0, 0, "", 0, []
    for i, path in enumerate(book.files):
        try:
            if os.path.getsize(path) == 0:
                bad.append(f"{os.path.basename(path)} is empty (0 bytes)")
                continue
        except OSError as e:
            bad.append(f"{os.path.basename(path)}: {e}")
            continue
        length = 0.0
        if core.HAVE_MUTAGEN:
            try:
                f = core.mutagen.File(path)
                if f is not None and f.info:
                    length = float(getattr(f.info, "length", 0) or 0)
                    if i == 0:
                        bitrate = int(getattr(f.info, "bitrate", 0) or 0)
                        codec = (getattr(f.info, "codec", "") or type(f).__name__).lower()
                        try:
                            chapters = len(f.chapters) if getattr(f, "chapters", None) else 0
                        except Exception:
                            chapters = 0
            except Exception:
                length = 0.0
        if not length or (i == 0 and not chapters and path.lower().endswith((".m4b", ".m4a"))):
            pr = _ffprobe(path)
            if pr:
                try:
                    length = length or float(pr.get("format", {}).get("duration") or 0)
                except ValueError:
                    pass
                if i == 0:
                    bitrate = bitrate or int(pr.get("format", {}).get("bit_rate") or 0)
                    a = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), {})
                    codec = codec or a.get("codec_name", "")
                    chapters = chapters or len(pr.get("chapters", []))
        if not length:
            bad.append(f"{os.path.basename(path)} could not be read (damaged or unsupported?)")
        total += length
    return {"duration": round(total, 1), "bitrate": bitrate, "codec": codec,
            "chapters": chapters, "health": "; ".join(bad[:3]) + (" ..." if len(bad) > 3 else "")}


def fmt_duration(sec: float) -> str:
    if not sec:
        return ""
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ============================================================================
# Deep local metadata (fields the scanner doesn't use for naming)
# ============================================================================

ISBN_RE = re.compile(r"(?<![\dX])(97[89][\- ]?(?:\d[\- ]?){9}\d|\d{9}[\dXx])(?![\dX])")


def valid_isbn(s: str) -> str:
    d = re.sub(r"[^0-9Xx]", "", s or "").upper()
    if len(d) == 13 and d.isdigit():
        tot = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))
        return d if (10 - tot % 10) % 10 == int(d[12]) else ""
    if len(d) == 10 and d[:9].isdigit():
        tot = sum((10 - i) * int(c) for i, c in enumerate(d[:9]))
        chk = (11 - tot % 11) % 11
        return d if ("X" if chk == 10 else str(chk)) == d[9] else ""
    return ""


def find_isbn(text: str) -> str:
    for m in ISBN_RE.finditer(text or ""):
        v = valid_isbn(m.group(1))
        if v:
            return v
    return ""


def _first(v):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if isinstance(v, bytes):                      # MP4FreeForm is a bytes subclass
        v = bytes(v).decode("utf-8", "ignore")
    return str(v).strip() if v else ""


def read_audio_extras(path: str) -> dict:
    """narrator, year, description, publisher, genres, asin, isbn, language."""
    out = {}
    if not core.HAVE_MUTAGEN:
        return out
    try:
        f = core.mutagen.File(path)
        tags = f.tags if f is not None else None
        if tags is None:
            return out
        if isinstance(f, core.MP4):
            g = lambda k: _first(tags.get(k))
            ff = lambda n: _first(tags.get("----:com.apple.iTunes:" + n))
            out["narrator"] = g("\xa9nrt") or ff("NARRATOR") or g("\xa9wrt")
            out["year"] = g("\xa9day")
            out["description"] = g("ldes") or g("desc") or g("\xa9cmt")
            out["publisher"] = ff("PUBLISHER") or g("\xa9pub") or ff("LABEL")
            out["genres"] = g("\xa9gen")
            out["asin"] = ff("ASIN") or ff("AUDIBLE_ASIN") or g("CDEK")
            out["isbn"] = ff("ISBN")
            out["language"] = ff("LANGUAGE")
        elif hasattr(tags, "getall"):
            def t(frame):
                fr = tags.getall(frame)
                return str(fr[0].text[0]).strip() if fr and getattr(fr[0], "text", None) else ""
            txxx = {fr.desc.upper(): str(fr.text[0]).strip()
                    for fr in tags.getall("TXXX") if fr.text}
            comm = tags.getall("COMM")
            out["narrator"] = txxx.get("NARRATOR") or t("TCOM")
            out["year"] = t("TDRC") or t("TYER") or t("TDRL")
            out["description"] = txxx.get("DESCRIPTION") or (str(comm[0].text[0]) if comm and comm[0].text else "")
            out["publisher"] = t("TPUB")
            out["genres"] = t("TCON")
            out["asin"] = txxx.get("ASIN") or txxx.get("AUDIBLE_ASIN")
            out["isbn"] = txxx.get("ISBN")
            out["language"] = t("TLAN")
        else:
            v = lambda *ks: next((_first(tags.get(k)) for k in ks if tags.get(k)), "")
            out["narrator"] = v("narrator", "performer", "composer")
            out["year"] = v("date", "year")
            out["description"] = v("description", "comment")
            out["publisher"] = v("publisher", "label", "organization")
            out["genres"] = v("genre")
            out["asin"] = v("asin", "audible_asin")
            out["isbn"] = v("isbn")
            out["language"] = v("language")
    except Exception:
        pass
    out = {k: v for k, v in out.items() if v}
    if out.get("year"):
        m = re.search(r"(1[5-9]\d\d|20\d\d)", out["year"])
        out["year"] = m.group(1) if m else ""
    if out.get("isbn"):
        out["isbn"] = valid_isbn(out["isbn"])
    if out.get("asin") and not re.fullmatch(r"[A-Z0-9]{10}", out["asin"].upper()):
        out.pop("asin")
    return {k: v for k, v in out.items() if v}


def _epub_opf(z: zipfile.ZipFile):
    cns = "{urn:oasis:names:tc:opendocument:xmlns:container}"
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(f".//{cns}rootfile").get("full-path")
    return opf_path, ET.fromstring(z.read(opf_path))


DC = "{http://purl.org/dc/elements/1.1/}"
OPFNS = "{http://www.idpf.org/2007/opf}"


def read_epub_extras(path: str) -> dict:
    out = {}
    try:
        with zipfile.ZipFile(path) as z:
            _, opf = _epub_opf(z)
        g = lambda tag: (opf.find(f".//{DC}{tag}").text or "").strip() \
            if opf.find(f".//{DC}{tag}") is not None and opf.find(f".//{DC}{tag}").text else ""
        out["description"] = re.sub(r"<[^>]+>", " ", unescape(g("description"))).strip()
        out["publisher"] = g("publisher")
        out["language"] = g("language")
        m = re.search(r"(1[5-9]\d\d|20\d\d)", g("date"))
        out["year"] = m.group(1) if m else ""
        out["genres"] = ", ".join(s.text.strip() for s in opf.iter(f"{DC}subject") if s.text)[:200]
        for ident in opf.iter(f"{DC}identifier"):
            txt = ident.text or ""
            scheme = (ident.get(f"{OPFNS}scheme") or "").lower()
            if "isbn" in scheme or "isbn" in txt.lower() or ISBN_RE.search(txt):
                v = find_isbn(txt)
                if v:
                    out["isbn"] = v
                    break
    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


def read_sidecar_extras(dirpath: str) -> dict:
    """Extra fields from metadata.json / metadata.opf / desc.txt / reader.txt."""
    out = {}
    try:
        names = {f.lower(): f for f in os.listdir(dirpath)}
    except OSError:
        return out
    if "metadata.json" in names:
        try:
            with open(os.path.join(dirpath, names["metadata.json"]), encoding="utf-8") as fh:
                d = json.load(fh)
            nar = d.get("narrators") or []
            out["narrator"] = ", ".join(n if isinstance(n, str) else n.get("name", "") for n in nar)
            out["year"] = str(d.get("publishedYear") or "")
            out["description"] = d.get("description") or ""
            out["publisher"] = d.get("publisher") or ""
            out["genres"] = ", ".join(d.get("genres") or [])
            out["asin"] = (d.get("asin") or "").upper()
            out["isbn"] = valid_isbn(d.get("isbn") or "")
            out["language"] = d.get("language") or ""
        except Exception:
            pass
    opf = names.get("metadata.opf") or next((names[n] for n in names if n.endswith(".opf")), None)
    if opf:
        try:
            root = ET.parse(os.path.join(dirpath, opf)).getroot()
            for c in root.iter(f"{DC}contributor"):
                if (c.get(f"{OPFNS}role") or "") == "nrt" and c.text and not out.get("narrator"):
                    out["narrator"] = c.text.strip()
            for ident in root.iter(f"{DC}identifier"):
                scheme = (ident.get(f"{OPFNS}scheme") or "").upper()
                if scheme == "ISBN" and not out.get("isbn"):
                    out["isbn"] = valid_isbn(ident.text or "")
                if scheme in ("ASIN", "AMAZON", "AUDIBLE") and not out.get("asin"):
                    out["asin"] = (ident.text or "").strip().upper()
            d = root.find(f".//{DC}description")
            if d is not None and d.text and not out.get("description"):
                out["description"] = re.sub(r"<[^>]+>", " ", unescape(d.text)).strip()
        except Exception:
            pass
    for fn in ("desc.txt", "description.txt"):
        if fn in names and not out.get("description"):
            try:
                with open(os.path.join(dirpath, names[fn]), encoding="utf-8", errors="ignore") as fh:
                    out["description"] = fh.read(5000).strip()
            except OSError:
                pass
    if "reader.txt" in names and not out.get("narrator"):
        try:
            with open(os.path.join(dirpath, names["reader.txt"]), encoding="utf-8", errors="ignore") as fh:
                out["narrator"] = fh.readline().strip()
        except OSError:
            pass
    return {k: v for k, v in out.items() if v}


# ============================================================================
# Ebook text: the front matter says what the book is
# ============================================================================

def epub_front_text(path: str, max_chars: int = 6000) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            opf_path, opf = _epub_opf(z)
            base = os.path.dirname(opf_path)
            manifest = {i.get("id"): i.get("href") for i in opf.iter(f"{OPFNS}item")}
            spine = [manifest.get(r.get("idref")) for r in opf.iter(f"{OPFNS}itemref")]
            text = []
            for href in [h for h in spine if h][:8]:
                name = os.path.normpath(os.path.join(base, urllib.parse.unquote(href))).replace("\\", "/")
                try:
                    html = z.read(name).decode("utf-8", "ignore")
                except KeyError:
                    continue
                html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
                t = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()
                if t:
                    text.append(t)
                if sum(map(len, text)) > max_chars:
                    break
            return " \n".join(text)[:max_chars]
    except Exception:
        return ""


def pdf_front_text(path: str, max_chars: int = 6000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        r = PdfReader(path)
        out = ""
        for p in r.pages[:6]:
            out += (p.extract_text() or "") + "\n"
            if len(out) > max_chars:
                break
        return out[:max_chars]
    except Exception:
        return ""


def ebook_front_text(book) -> str:
    for f in book.files:
        ext = os.path.splitext(f)[1].lower()
        if ext == ".epub":
            t = epub_front_text(f)
            if t:
                return t
    for f in book.files:
        if f.lower().endswith(".pdf"):
            t = pdf_front_text(f)
            if t:
                return t
    return ""


# ============================================================================
# Online lookups (duration-aware)
# ============================================================================

def _get(url: str, timeout: int = 12, raw: bool = False, limit: int = 12_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit)
    return data if raw else json.loads(data.decode("utf-8", "ignore"))


AUDIBLE_GROUPS = "contributors,series,media,product_desc,product_attrs,product_extended_attrs,category_ladders"


def _audible_record(p: dict) -> dict:
    series = p.get("series") or []
    imgs = p.get("product_images") or {}
    img = imgs.get("2400") or imgs.get("1024") or imgs.get("500") or ""
    if img:
        img = re.sub(r"\._[A-Z]{2}\d+_", "", img)       # strip the size suffix -> original art
    genres = []
    for ladder in p.get("category_ladders") or []:
        for step in ladder.get("ladder") or []:
            if step.get("name") and step["name"] not in genres:
                genres.append(step["name"])
    desc = re.sub(r"<[^>]+>", " ", unescape(p.get("publisher_summary") or p.get("merchandising_summary") or ""))
    return {
        "source": "Audible", "asin": p.get("asin", ""),
        "title": p.get("title", ""), "subtitle": p.get("subtitle") or "",
        "author": ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:3]),
        "narrator": ", ".join(n.get("name", "") for n in (p.get("narrators") or [])[:4]),
        "series": re.sub(r"\s*\(.*\)$", "", (series[0].get("title") or "")).strip() if series else "",
        "series_index": str(series[0].get("sequence") or "") if series else "",
        "runtime": int(p.get("runtime_length_min") or 0) * 60,
        "year": (p.get("release_date") or p.get("issue_date") or "")[:4],
        "publisher": p.get("publisher_name") or "",
        "language": (p.get("language") or "").title(),
        "description": re.sub(r"\s+", " ", desc).strip(),
        "genres": ", ".join(genres[:4]),
        "cover_url": img,
    }


# Narrators that folder names and careless taggers often put in the AUTHOR
# slot (list started from deucebucket/library-manager, MIT). The library's own
# narrator tags are added to this at runtime - see app.narrator_pass().
KNOWN_NARRATORS = {
    "scott brick", "ray porter", "luke daniels", "steven pacey", "tim gerard reynolds",
    "r.c. bray", "rc bray", "r c bray", "nick podehl", "simon vance", "michael kramer",
    "kate reading", "january lavoy", "rebecca soler", "kirby heyborne", "rob inglis",
    "toby longworth", "joe morton", "bahni turpin", "robin miles", "dion graham",
    "jim dale", "frank muller", "george guidall", "davina porter", "grover gardner",
    "jefferson mays", "edoardo ballerini", "julia whelan", "will patton", "dick hill",
    "michael page", "stefan rudnicki", "jayne entwistle", "cassandra campbell",
    "jennifer ikeda", "tavia gilbert", "mary robinette kowal", "jeff hays", "andrea parsneau",
    "emily woo zeller", "travis baldree", "kevin r. free", "adjoa andoh", "roy dotrice",
}

# Titles so generic that a name-only match is meaningless (from
# library-manager's hallucination guard, MIT). A book with one of these titles
# needs corroboration (ISBN/ASIN, runtime, or an agreeing author) to be trusted.
GENERIC_TITLES = {
    "match game", "the game", "game on", "end game", "final game", "the end", "the beginning",
    "new beginnings", "fresh start", "home", "coming home", "going home", "home again",
    "the choice", "choices", "decisions", "the list", "the plan", "the promise", "the secret",
    "forever", "always", "never", "maybe", "lost", "found", "broken", "fallen", "risen",
    "dark", "light", "shadow", "shadows", "fire", "ice", "storm", "rain", "book one",
    "book two", "book 1", "book 2", "part one", "part two", "part 1", "part 2",
    "chapter one", "chapter 1", "untitled", "the gift", "the return", "the hunt", "the escape",
}


def is_generic_title(t: str) -> bool:
    return re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip() in GENERIC_TITLES


def is_known_narrator(name: str, extra: set = frozenset()) -> bool:
    k = re.sub(r"\s+", " ", (name or "").lower().replace(".", ". ")).replace(" .", ".").strip()
    k2 = re.sub(r"[^a-z ]+", "", (name or "").lower()).strip()
    return bool(name) and (k in KNOWN_NARRATORS or k2 in KNOWN_NARRATORS or k2 in extra)


AUDNEXUS_REGION = {"com": "us", "co.uk": "uk", "ca": "ca", "com.au": "au", "de": "de",
                   "fr": "fr", "it": "it", "es": "es", "co.jp": "jp", "in": "in"}


def audnexus_book(asin: str, region: str = "com") -> dict:
    """Audnexus (what Audiobookshelf itself uses): cleaner series, genres,
    narrators and full-size art for an ASIN. Free, no key."""
    try:
        d = _get(f"https://api.audnex.us/books/{asin}?region={AUDNEXUS_REGION.get(region, 'us')}")
    except Exception:
        return {}
    if not d or not d.get("title"):
        return {}
    sp = d.get("seriesPrimary") or {}
    genres = [g.get("name") for g in (d.get("genres") or []) if g.get("type") == "genre" and g.get("name")]
    return {
        "source": "Audnexus", "asin": asin, "title": d.get("title", ""), "subtitle": d.get("subtitle") or "",
        "author": ", ".join(a.get("name", "") for a in (d.get("authors") or [])[:3]),
        "narrator": ", ".join(n.get("name", "") for n in (d.get("narrators") or [])[:4]),
        "series": sp.get("name") or "", "series_index": str(sp.get("position") or ""),
        "runtime": int(d.get("runtimeLengthMin") or 0) * 60,
        "year": (d.get("releaseDate") or "")[:4], "publisher": d.get("publisherName") or "",
        "language": (d.get("language") or "").title(), "isbn": valid_isbn(d.get("isbn") or ""),
        "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(d.get("summary") or d.get("description") or ""))).strip(),
        "genres": ", ".join(genres[:4]), "cover_url": d.get("image") or "",
    }


def _merge_audnexus(c: dict, region: str) -> dict:
    if not c.get("asin"):
        return c
    ax = audnexus_book(c["asin"], region)
    if not ax:
        return c
    out = dict(c)
    for k in ("series", "series_index", "genres", "narrator", "isbn", "description", "cover_url"):
        if ax.get(k):
            out[k] = ax[k]
    out["source"] = "Audible+Audnexus"
    return out


def audible_search(title: str, author: str = "", asin: str = "", region: str = "com") -> list:
    host = f"https://api.audible.{region}/1.0/catalog/products"
    try:
        if asin:
            d = _get(f"{host}/{asin}?response_groups={AUDIBLE_GROUPS}&image_sizes=2400,1024,500")
            return [_audible_record(d["product"])] if d.get("product") else []
        params = {"title": title, "num_results": 10, "products_sort_by": "Relevance",
                  "response_groups": AUDIBLE_GROUPS, "image_sizes": "2400,1024,500"}
        if author:
            params["author"] = author
        d = _get(host + "?" + urllib.parse.urlencode(params))
        return [_audible_record(p) for p in d.get("products", [])]
    except Exception:
        return []


def google_search(title: str = "", author: str = "", isbn: str = "") -> list:
    try:
        if isbn:
            q = f"isbn:{isbn}"
        else:
            q = f"intitle:{title}" + (f" inauthor:{author}" if author else "")
        d = _get("https://www.googleapis.com/books/v1/volumes?maxResults=6&q=" + urllib.parse.quote(q))
    except Exception:
        return []
    out = []
    for item in d.get("items", []):
        v = item.get("volumeInfo", {})
        isbns = {i.get("type"): i.get("identifier") for i in v.get("industryIdentifiers", [])}
        img = (v.get("imageLinks") or {})
        cover = img.get("extraLarge") or img.get("large") or img.get("medium") or img.get("thumbnail") or ""
        if cover:
            cover = cover.replace("http://", "https://").replace("&edge=curl", "")
            cover = re.sub(r"&zoom=\d", "", cover) + "&fife=w1200"
        out.append({
            "source": "Google Books", "title": v.get("title", ""), "subtitle": v.get("subtitle") or "",
            "author": ", ".join((v.get("authors") or [])[:3]),
            "year": (v.get("publishedDate") or "")[:4], "publisher": v.get("publisher") or "",
            "description": re.sub(r"<[^>]+>", " ", unescape(v.get("description") or "")),
            "isbn": isbns.get("ISBN_13") or isbns.get("ISBN_10") or "",
            "genres": ", ".join((v.get("categories") or [])[:3]),
            "language": v.get("language") or "", "cover_url": cover,
            "series": "", "series_index": "",
        })
    return out


def openlibrary_search(title: str = "", author: str = "", isbn: str = "") -> list:
    try:
        params = {"limit": 5, "fields": "title,author_name,first_publish_year,isbn,cover_i,publisher,language"}
        if isbn:
            params["isbn"] = isbn
        else:
            params["title"] = title
            if author:
                params["author"] = author
        d = _get("https://openlibrary.org/search.json?" + urllib.parse.urlencode(params))
    except Exception:
        return []
    out = []
    for doc in d.get("docs", []):
        cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg" if doc.get("cover_i") else ""
        out.append({
            "source": "Open Library", "title": doc.get("title", ""),
            "author": ", ".join((doc.get("author_name") or [])[:3]),
            "year": str(doc.get("first_publish_year") or ""),
            "publisher": (doc.get("publisher") or [""])[0],
            "isbn": next((valid_isbn(i) for i in (doc.get("isbn") or []) if valid_isbn(i)), ""),
            "cover_url": cover, "series": "", "series_index": "", "description": "",
            "genres": "", "language": "",
        })
    return out


def score_candidate(c: dict, book) -> float:
    """0..1 - how well an online record matches what we know about the book."""
    t = max(ratio(c.get("title", ""), book.title),
            ratio(f"{c.get('title','')} {c.get('subtitle','')}", book.title))
    a = ratio(core.clean_author(c.get("author", "").split(",")[0]), book.author) if book.author else 0.6
    c.pop("narrator_swap", None)
    if book.author and a < 0.7 and c.get("narrator"):
        # "Ray Porter - Project Hail Mary": our "author" is this edition's NARRATOR
        if any(ratio(n.strip(), book.author) >= 0.85 for n in c["narrator"].split(",")):
            a, c["narrator_swap"] = 0.95, True
    s = t * 0.6 + a * 0.4
    # runtime is the audiobook tie-breaker: an abridged edition or a different
    # book with the same title will not be within 3% of your file's length
    if c.get("runtime") and getattr(book, "duration", 0):
        diff = abs(c["runtime"] - book.duration) / max(book.duration, 1)
        s += 0.15 if diff < 0.03 else 0.05 if diff < 0.08 else -0.15
    if (c.get("isbn") and getattr(book, "isbn", "") and c["isbn"] == book.isbn) or \
            (c.get("asin") and getattr(book, "asin", "") and c["asin"] == book.asin):
        s = max(s + 0.3, 0.92)          # an identifier match is exact, whatever the title says
    id_match = (c.get("isbn") and c.get("isbn") == getattr(book, "isbn", "")) or \
        (c.get("asin") and c.get("asin") == getattr(book, "asin", ""))
    if is_generic_title(book.title) and not id_match and a < 0.85 and not (
            c.get("runtime") and getattr(book, "duration", 0)
            and abs(c["runtime"] - book.duration) / max(book.duration, 1) < 0.03):
        s = min(s, 0.6)     # "Home" by nobody-in-particular is not a match
    if book.series_index and c.get("series_index") and \
            core.fmt_series_index(c["series_index"]) != core.fmt_series_index(book.series_index):
        s -= 0.2
    return round(max(0.0, min(s, 1.2)), 3)


def online_candidates(book, db=None, region: str = "com") -> list:
    """All plausible online records for a book, best first, each with a 'score'."""
    key = "online:" + hashlib.sha1(json.dumps([book.kind, book.title, book.author,
                                               getattr(book, "isbn", ""), getattr(book, "asin", ""),
                                               region]).encode()).hexdigest()
    cands = db.cache_get(key, 30) if db else None
    if cands is None:
        cands = []
        if book.kind == "audio":
            if getattr(book, "asin", ""):
                cands += audible_search("", asin=book.asin, region=region)
                cands = [c for c in cands if c.get("title")]
            if book.title and len(cands) < 2:
                cands += audible_search(book.title, book.author, region=region)
                if book.author and (not cands or is_known_narrator(book.author)
                                    or "author looks like a narrator" in (book.flags or [])):
                    cands += audible_search(book.title, "", region=region)   # the "author" may be the narrator
            # Audnexus enriches the best Audible hits (series, genres, full-size art)
            for i, c in enumerate(cands[:3]):
                if c.get("source") == "Audible":
                    cands[i] = _merge_audnexus(c, region)
        isbn = getattr(book, "isbn", "")
        if isbn:
            cands += google_search(isbn=isbn) + openlibrary_search(isbn=isbn)
        if book.title and (book.kind == "ebook" or len(cands) < 2):
            cands += google_search(book.title, book.author)
            if len(cands) < 3:
                cands += openlibrary_search(book.title, book.author)
        if db:
            db.cache_put(key, cands)
    for c in cands:
        c["score"] = score_candidate(c, book)
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands


# ============================================================================
# Covers
# ============================================================================

def image_size(data: bytes):
    """(width, height) of JPEG/PNG/WEBP bytes without needing Pillow."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and HAVE_PIL:
            return Image.open(io.BytesIO(data)).size
    except Exception:
        pass
    if HAVE_PIL:
        try:
            return Image.open(io.BytesIO(data)).size
        except Exception:
            pass
    return (0, 0)


def cover_score(w: int, h: int, kind: str) -> float:
    if not w or not h:
        return 0
    ideal = 1.0 if kind == "audio" else 0.66
    aspect = w / h
    aspect_pen = min(abs(aspect - ideal) * 1.5, 0.9)
    pixels = min(w, h)
    res = min(pixels, 1600) / 1600
    small = 0.5 if pixels < 300 else 0
    return round(max(0.0, res - aspect_pen * 0.6 - small), 3)


def embedded_cover(path: str) -> bytes:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(path) as z:
                opf_path, opf = _epub_opf(z)
                base = os.path.dirname(opf_path)
                items = list(opf.iter(f"{OPFNS}item"))
                cover_id = next((m.get("content") for m in opf.iter(f"{OPFNS}meta")
                                 if m.get("name") == "cover"), None)
                href = next((i.get("href") for i in items if "cover-image" in (i.get("properties") or "")), None)
                if not href and cover_id:
                    href = next((i.get("href") for i in items if i.get("id") == cover_id), None)
                if not href:
                    href = next((i.get("href") for i in items
                                 if "cover" in (i.get("href") or "").lower()
                                 and (i.get("media-type") or "").startswith("image/")), None)
                if href:
                    name = os.path.normpath(os.path.join(base, urllib.parse.unquote(href))).replace("\\", "/")
                    return z.read(name)
            return b""
        if not core.HAVE_MUTAGEN or ext not in core.AUDIO_EXTS:
            return b""
        f = core.mutagen.File(path)
        if f is None:
            return b""
        if isinstance(f, core.MP4):
            covr = (f.tags or {}).get("covr")
            return bytes(covr[0]) if covr else b""
        if hasattr(f, "pictures") and f.pictures:
            return f.pictures[0].data
        tags = f.tags
        if tags is not None and hasattr(tags, "getall"):
            apics = tags.getall("APIC")
            if apics:
                front = [a for a in apics if a.type == 3] or apics
                return front[0].data
        if tags is not None:
            b64 = tags.get("metadata_block_picture")
            if b64:
                from mutagen.flac import Picture
                return Picture(base64.b64decode(b64[0])).data
    except Exception:
        pass
    return b""


def _cover_cache_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "covers")
    os.makedirs(d, exist_ok=True)
    return d


def _save_candidate(data: bytes, data_dir: str) -> str:
    h = hashlib.sha1(data).hexdigest()[:16]
    ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".webp" if data[8:12] == b"WEBP" else ".jpg"
    p = os.path.join(_cover_cache_dir(data_dir), h + ext)
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(data)
    return p


def gather_covers(book, data_dir: str, online: list = None, fetch_online: bool = True) -> list:
    """Every cover candidate for a book: [{'path','origin','w','h','score'}], best first."""
    found, seen = [], set()

    def add(data, origin):
        if not data or len(data) < 1500:
            return
        w, h = image_size(data)
        if not w:
            return
        p = _save_candidate(data, data_dir)
        if p in seen:
            return
        seen.add(p)
        found.append({"path": p, "origin": origin, "w": w, "h": h,
                      "score": cover_score(w, h, book.kind)})

    # 1. images in the book folder(s)
    dirs = []
    for f in book.files:
        d = os.path.dirname(f)
        if d not in dirs:
            dirs.append(d)
    folder_imgs = [e for e in book.extras if os.path.splitext(e)[1].lower() in IMAGE_EXTS]
    if book.kind == "ebook":            # ebook folders don't carry extras; look beside the file
        for d in dirs[:1]:
            try:
                for fn in os.listdir(d):
                    if os.path.splitext(fn)[1].lower() in IMAGE_EXTS and (
                            "cover" in fn.lower() or os.path.splitext(fn)[0].lower() ==
                            os.path.splitext(os.path.basename(book.files[0]))[0].lower()):
                        folder_imgs.append(os.path.join(d, fn))
            except OSError:
                pass
    folder_imgs.sort(key=lambda p: (not re.search(r"cover|folder|front", os.path.basename(p), re.I), p))
    for p in folder_imgs[:6]:
        try:
            with open(p, "rb") as fh:
                add(fh.read(15_000_000), "folder: " + os.path.basename(p))
        except OSError:
            pass
    # 2. embedded in the first audio / ebook file
    for f in book.files[:1] + [f for f in book.files if f.lower().endswith(".epub")][:1]:
        add(embedded_cover(f), "embedded")
    # 3. online (only from confident matches)
    if fetch_online:
        for c in (online or [])[:4]:
            if c.get("cover_url") and c.get("score", 0) >= 0.75:
                try:
                    add(_get(c["cover_url"], raw=True, timeout=15), c["source"])
                except Exception:
                    pass
        isbn = getattr(book, "isbn", "")
        if isbn and book.kind == "ebook":
            try:
                add(_get(f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",
                         raw=True, timeout=15), "Open Library (ISBN)")
            except Exception:
                pass
    found.sort(key=lambda c: c["score"], reverse=True)
    return found


def cover_jpeg(path: str, max_side: int = 0) -> bytes:
    """The chosen cover as JPEG bytes (converted / downscaled when Pillow is
    available; raw bytes otherwise)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if not HAVE_PIL:
        return data
    try:
        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max_side and max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        elif im.format == "JPEG" and not max_side:
            return data
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return data


def embed_cover(paths, jpeg: bytes, log=None) -> int:
    """Embed a cover into COPIED audio files."""
    if not core.HAVE_MUTAGEN or not jpeg:
        return 0
    from mutagen.mp4 import MP4Cover
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC
    from mutagen.flac import FLAC, Picture
    fmt_png = jpeg[:8] == b"\x89PNG\r\n\x1a\n"
    mime = "image/png" if fmt_png else "image/jpeg"
    n = 0
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        try:
            if ext in (".m4b", ".m4a", ".aac"):
                f = core.MP4(p)
                if f.tags is None:
                    f.add_tags()
                f.tags["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_PNG if fmt_png else MP4Cover.FORMAT_JPEG)]
                f.save()
            elif ext == ".mp3":
                try:
                    t = ID3(p)
                except ID3NoHeaderError:
                    t = ID3()
                t.delall("APIC")
                t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=jpeg))
                t.save(p)
            elif ext == ".flac":
                f = FLAC(p)
                f.clear_pictures()
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, mime, jpeg
                w, h = image_size(jpeg)
                pic.width, pic.height, pic.depth = w, h, 24
                f.add_picture(pic)
                f.save()
            elif ext in (".ogg", ".opus"):
                f = core.mutagen.File(p)
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, mime, jpeg
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
                f.save()
            else:
                continue
            n += 1
        except Exception as e:
            if log:
                log(f"  could not embed cover in {os.path.basename(p)}: {e}")
    return n


# ============================================================================
# Confidence
# ============================================================================

BASE_CONF = {"opf": 72, "metadata.json": 66, "tags": 62, "folders": 56,
             "filename": 38, "AI": 60, "you": 100}


def confidence(book) -> int:
    if not book.title or not book.author:
        return 10 if book.title else 0
    ms = book.meta_source or "filename"
    if getattr(book, "locked", None) and "title" in book.locked and "author" in book.locked:
        return 100
    base = next((v for k, v in BASE_CONF.items() if ms.startswith(k)), 45)
    if ms.startswith(("Audible", "Google", "Open Library", "online")):
        base = 70
    if "differ" in ms:
        base -= 15
    if "last resort" in ms:
        base -= 10
    base += int(getattr(book, "match_bonus", 0) or 0)
    if getattr(book, "health", ""):
        base -= 10
    if re.fullmatch(r"(?i)(untitled|unknown.*|track \d+|chapter \d+|\d{1,3})", book.title.strip()):
        base = min(base, 25)
    if getattr(book, "flags", None):
        base -= 20
    if is_generic_title(book.title) and (getattr(book, "match_bonus", 0) or 0) < 12:
        base = min(base, 55)       # generic title with no corroboration: always review
    return max(0, min(100, base))


def agreement_bonus(book) -> int:
    """Independent sources (sidecar, tags, folder path, folder name, file name)
    that confirm the chosen title or author: +5 per extra confirming vote on
    each field, max +16. Folders usually give only the author - that still
    counts as an independent confirmation of the author."""
    if not book.title or not book.files:
        return 0
    first = book.files[0]
    d = os.path.dirname(first)
    srcs = []
    try:
        srcs.append(core.read_sidecar_metadata(d, os.listdir(d))[0])
    except OSError:
        pass
    try:
        if book.kind == "audio":
            srcs.append(core.read_audio_metadata(first, multi=len(book.files) > 1))
        elif first.lower().endswith(".epub"):
            srcs.append(core.read_epub_metadata(first))
        rel = book.src_display if book.kind == "audio" else os.path.dirname(book.src_display)
        srcs.append(core.infer_from_path(rel))
        leaf = os.path.basename(d)
        if not core._is_junk_folder(leaf):
            srcs.append(core.parse_name(core._strip_calibre_id(leaf)))
        if len(book.files) == 1:
            srcs.append(core.parse_name(os.path.splitext(os.path.basename(first))[0]))
    except Exception:
        pass
    tv = av = 0
    for m in srcs:
        if not m:
            continue
        if m.get("title") and ratio(core.clean_text(m["title"], book.author)[0], book.title) >= 0.85:
            tv += 1
        if m.get("author") and book.author and ratio(core.clean_author(m["author"]), book.author) >= 0.85:
            av += 1
    return min(16, 5 * max(0, tv - 1) + 5 * max(0, av - 1))


# ============================================================================
# The enrichment pass for one book
# ============================================================================

FILL_FIELDS = ("narrator", "year", "publisher", "description", "genres", "asin", "isbn", "language")


def enrich_book(book, db, data_dir: str, online: bool = True, covers: bool = True,
                fix_names: bool = True, region: str = "com", log=None, intro=None) -> dict:
    """Mutates `book`. Returns a small report dict."""
    report = {}
    locked = set(getattr(book, "locked", []) or [])
    if not book.fingerprint:
        book.fingerprint = book_fingerprint(book, db)
    # -- local, free --------------------------------------------------------
    if book.kind == "audio":
        pr = probe_audio(book, log)
        book.duration, book.bitrate, book.codec = pr["duration"], pr["bitrate"], pr["codec"]
        book.chapters, book.health = pr["chapters"], pr["health"]
        local = read_audio_extras(book.files[0]) if book.files else {}
    else:
        local = {}
        for f in book.files:
            if f.lower().endswith(".epub"):
                local = read_epub_extras(f)
                break
        text = ebook_front_text(book)
        if text:
            book.front_text = text[:3000]
            if not local.get("isbn"):
                isbn = find_isbn(text)
                if isbn:
                    local["isbn"] = isbn
                    report["isbn_from_text"] = isbn
        empty = [f for f in book.files if os.path.exists(f) and os.path.getsize(f) == 0]
        book.health = "; ".join(os.path.basename(f) + " is empty (0 bytes)" for f in empty)
    side = read_sidecar_extras(os.path.dirname(book.files[0])) if book.files else {}
    for src in (side, local):
        for k in FILL_FIELDS:
            if src.get(k) and not getattr(book, k) and k not in locked:
                setattr(book, k, src[k])
    if book.narrator and core.similar(book.narrator.lower(), (book.author or "").lower()) > 0.9:
        book.narrator = ""       # composer tag holding the author, not a narrator

    # -- spoken intro FIRST when the files told us nothing usable -----------
    # (library-manager reports ~half of books identified from the intro alone)
    if intro and book.kind == "audio" and not book.transcript and (
            not book.author or confidence(book) < 40 or "author looks like a narrator" in book.flags):
        try:
            book.transcript = intro(book) or ""
        except Exception as e:
            if log:
                log(f"  transcription failed for {book.src_display}: {e}")
        import ai as _ai
        heard = _ai.parse_intro(book.transcript)
        if heard:
            report["intro"] = heard
            for k in ("title", "author", "narrator"):
                if heard.get(k) and k not in locked and (not getattr(book, k) or confidence(book) < 40):
                    setattr(book, k, core.clean_author(heard[k]) if k == "author" else heard[k])
            book.meta_source = "spoken intro"
    # -- agreement: independent sources saying the same thing ---------------
    book.match_bonus = agreement_bonus(book)
    # -- online -------------------------------------------------------------
    if online and (book.title or book.isbn or book.asin):
        cands = online_candidates(book, db, region)
        best = cands[0] if cands else None
        book.online_best = {k: best.get(k) for k in ("source", "title", "author", "series",
                                                     "series_index", "narrator", "year", "score",
                                                     "runtime", "asin", "isbn")} if best else {}
        if best and best["score"] >= 0.8:
            report["match"] = f"{best['source']} ({best['score']:.2f})"
            if best.get("narrator_swap") and "author" not in locked:
                book.narrator = book.narrator or book.author
                book.author = core.clean_author(best["author"].split(",")[0].strip())
                book.flags = [f for f in book.flags if "narrator" not in f]
                report["narrator_fixed"] = book.author
                if log:
                    log(f"  narrator was filed as author: {book.narrator} -> author {book.author} ({book.title})")
            book.match_bonus += 20 if best["score"] >= 0.95 else 12
            if not book.title and "title" not in locked and best.get("title"):
                book.title, book.meta_source = best["title"], best["source"] + " (by ID)"
            if not book.author and "author" not in locked and best.get("author"):
                book.author = core.clean_author(best["author"].split(",")[0].strip())
            curated = (book.meta_source or "").startswith(("opf", "metadata.json"))
            if fix_names and not curated:
                if "title" not in locked and best.get("title") and ratio(best["title"], book.title) > 0.75:
                    book.title = core.title_case_if_shouty(best["title"])
                if "author" not in locked and best.get("author") and ratio(
                        core.clean_author(best["author"].split(",")[0]), book.author) > 0.7:
                    book.author = core.clean_author(best["author"].split(",")[0].strip())
            if best.get("series") and not book.series and "series" not in locked:
                book.series = best["series"]
                book.series_index = best.get("series_index") or book.series_index
            for k in FILL_FIELDS:
                if best.get(k) and not getattr(book, k) and k not in locked:
                    setattr(book, k, best[k])
            if book.meta_source in ("filename", "folders"):
                book.meta_source = best["source"]
        book._candidates = cands[:5]
    # -- covers -------------------------------------------------------------
    if covers:
        cand = gather_covers(book, data_dir, getattr(book, "_candidates", []), fetch_online=online)
        book.cover_candidates = [{k: c[k] for k in ("path", "origin", "w", "h", "score")} for c in cand[:8]]
        if cand and not ("cover" in locked and book.cover and os.path.exists(book.cover)):
            book.cover = cand[0]["path"]
            book.cover_info = f"{cand[0]['w']}x{cand[0]['h']} {cand[0]['origin']}"
    book.confidence = confidence(book)
    book.enriched = True
    return report
