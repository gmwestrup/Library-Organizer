#!/usr/bin/env python3
"""
Audiobook & Ebook Library Organizer v2 - core engine (no GUI)
-----------------------------------
The scanner/parser and the write-out side. Enrichment (audio probe, online
match, covers) lives in enrich.py, AI in ai.py, duplicates in dedupe.py,
authors in authors.py, remembered decisions in library_db.py.

Scans a folder tree of audiobooks/ebooks, reads embedded metadata, optionally
improves it with online lookups (Google Books / Open Library), lets you review
and edit everything in a table, then COPIES files into a clean structure:

    Destination/
        Author Name/
            Series Name/
                01 - Book Title/
                    (book files)
            Standalone Book Title/
                (book files)

This layout is understood by both Audiobookshelf and Calibre.
Originals are never modified or moved.

Requirements:  Python 3.9+ and requirements.txt.
"""

import os
import re
import sys
import json
import queue
import shutil
import time
import struct
import zipfile
import threading
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from dataclasses import dataclass, field

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wma", ".aac"}
EBOOK_EXTS = {".epub", ".mobi", ".azw", ".azw3", ".pdf"}
EXTRA_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".cue", ".opf", ".txt"}  # copied along with audiobook folders

WINDOWS_BAD = r'<>:"/\|?*'


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def sanitize(name: str) -> str:
    """Make a string safe as a Windows folder/file name."""
    if not name:
        return "Unknown"
    name = re.sub(r"\s*:\s*", " - ", name)                 # colon -> " - " (readable)
    out = "".join("-" if c in WINDOWS_BAD else c for c in name)
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out[:120] or "Unknown"


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def clean_author(author: str) -> str:
    """Normalize 'Last, First' -> 'First Last', strip junk."""
    if not author:
        return ""
    author = strip_author_suffix(re.sub(r"\s*\(\d+\)\s*$", "", author.strip()))   # "Stephen King (4567)"
    # Co-authors ("Neil Gaiman & Terry Pratchett", "A; B", "A and B"): the
    # primary author names the folder. Only a real COMMA means "Last, First" -
    # v1 flipped glued co-authors into "Terry Pratchett Neil Gaiman".
    glued = re.split(r"\s*(?:[;/&]|\band\b|\bwith\b)\s*", author, maxsplit=1, flags=re.I)
    if len(glued) == 2 and glued[0].strip() and glued[1].strip():
        return clean_author(glued[0])
    parts = [p.strip() for p in author.split(",")]
    suffix = ""
    if len(parts) == 3 and re.fullmatch(r"(?i)(jr|sr|ii|iii|iv|phd|md)\.?", parts[2]):
        suffix, parts = " " + parts[2], parts[:2]          # "Smith, John, Jr." -> "John Smith Jr."
    if (len(parts) == 2 and parts[0] and parts[1]
            and len(parts[0].split()) <= 3 and len(parts[1].split()) <= 3
            and not any(ch.isdigit() for ch in author)):
        # "Sanderson, Brandon" or "Le Guin, Ursula K." -> flip
        author = f"{parts[1]} {parts[0]}{suffix}"
    else:
        author = parts[0]
    return re.sub(r"\s+", " ", author).strip()


def fmt_series_index(idx) -> str:
    """'1' -> '01', '1.5' -> '01.5', junk -> as-is."""
    if idx in (None, ""):
        return ""
    try:
        f = float(idx)
        whole = int(f)
        if f == whole:
            return f"{whole:02d}"
        return f"{whole:02d}" + f"{f - whole:.2g}".lstrip("0")
    except (TypeError, ValueError):
        return str(idx).strip()


def title_case_if_shouty(s: str) -> str:
    """Fix ALL-CAPS or all-lower titles/authors without touching normal ones."""
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if letters and (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        small = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "by"}
        words = s.lower().split()
        out = [w.capitalize() if (i == 0 or w not in small) else w for i, w in enumerate(words)]
        return " ".join(out)
    return s


STRIP_ALL_PARENS = False   # set True to remove every (...) / [...] from titles & series
TRUST_FOLDERS = True       # a structured Author/Series/Book path outranks embedded tags
NUMBER_FOLDERS = False     # True: prefix book folders with the series position ("01 - Title")
DISTRUST_JSON = False      # True: metadata.json is used only as a last resort
DISTRUST_OPF = False       # True: .opf sidecars are used only as a last resort
DISTRUST_TAGS = False      # True: embedded tags are used only as a last resort
CONTENT_PAREN = re.compile(r"\d\s*[-\u2013]\s*\d|\b(?:vol(?:ume)?s?|books?|parts?|set|omnibus|collection|"
                           r"trilogy|complete)\b", re.I)
NOISE_PAREN = re.compile(
    r"\s*[\(\[](?:un)?abridged[\)\]]|\s*[\(\[](?:audio ?book|mp3|m4b|m4a|aac|flac|ogg|"
    r"\d{2,3}\s*kbps|retail|epub|kindle|e-?book|hq|complete|full|"
    r"(?:19|20)\d{2})[\)\]]", re.I)
NARRATOR_PAREN = re.compile(
    r"\s*[\(\[][^\)\]]*?(?:read|narrated|performed|told)\s+by\s+(?P<who>[^\)\]]+)[\)\]]", re.I)


# ---- Release / encoder junk (cases from real libraries, many ported from
# deucebucket/library-manager's regression suite, MIT) -----------------------
RELEASE_TAG_RE = re.compile(      # case-sensitive on purpose: "[MAM]" is a tag, "[Dune]" is a title
    r"^\s*[\[\{](?:(?i:[\w-]+\.(?:to|com|org|net|me|cc|io|info|ws|se|nz|li|is))|[A-Z]{2,5}|TGx|"
    r"(?i:audiobookbay|rarbg|yts))[\]\}]\s*[-_.]?\s*")
_JUNK_TOKEN = (r"(?:mp3|m4b|m4a|aac|flac|ogg|opus|wma|vbr|cbr|abr|mono|stereo|lame|multi|"
               r"full|audio\s*book|audiobooks?|unabridged|abridged|unabr|unb|retail|hq|ipod|itunes|"
               r"\d{2,3}\s*k(?:bps|b/s|bit)?|\d{2,3}kbps|\d{2}(?:\.\d)?\s*khz|"
               r"\d+(?:\.\d+)?\s*(?:mb|gb|mib|gib))")
_TRAIL_JUNK_RE = re.compile(r"(?:[\s._\-]+|^)(?:[\(\[\{]\s*" + _JUNK_TOKEN + r"(?:[\s,]+" + _JUNK_TOKEN +
                            r")*\s*[\)\]\}]|" + _JUNK_TOKEN + r")\s*$", re.I)
AUTHOR_SUFFIX_RE = re.compile(r"\s+(?:collection|anthology|omnibus|complete works|selected works|"
                              r"collected works|bibliography|best of|works of|works|audiobooks|ebooks|"
                              r"books|library)\s*$", re.I)


def strip_release_junk(name: str) -> str:
    """'[MAM] Dean Koontz - Watchers' -> 'Dean Koontz - Watchers';
    'Death's End {465mb} 128k stereo lame' -> "Death's End";
    'audiobook_The_Great_Gatsby_full' -> 'The Great Gatsby'.
    Only LEADING release tags and TRAILING runs of junk tokens are removed, so
    '11.22.63', '1984' and 'Stereo Hearts' survive."""
    if not name:
        return name
    s = RELEASE_TAG_RE.sub("", name)
    if " " not in s.strip() and s.count("_") >= 2:
        s = s.replace("_", " ")
    s = re.sub(r"^\s*(?:full\s+)?audio\s*book[\s_\-:]+", "", s, flags=re.I)
    for _ in range(8):
        new = _TRAIL_JUNK_RE.sub("", s)
        if new == s or not new.strip():
            break
        s = new
    return s.strip(" -_.") or name


def strip_author_suffix(name: str) -> str:
    """'Stephen King Collection' -> 'Stephen King' (only with 2+ words left)."""
    new = AUTHOR_SUFFIX_RE.sub("", name or "").strip()
    return new if len(new.split()) >= 2 else name


def clean_text(text: str, author: str = "", series: str = "") -> tuple:
    """Remove junk parentheticals from a title/series. Returns (clean, narrator).
    'Belinda (read by Ray Bouche)'      -> ('Belinda', 'Ray Bouche')
    'Star Force Universe (Jyr)'         -> ('Star Force Universe', '')  [author fragment]
    'Dune (Unabridged) [2019]'          -> ('Dune', '')
    'Box Set (5-8)'                     -> unchanged unless STRIP_ALL_PARENS"""
    if not text:
        return text, ""
    narrator = ""
    text = strip_release_junk(text)
    m = NARRATOR_PAREN.search(text)
    if m:
        narrator = m.group("who").strip()
        text = NARRATOR_PAREN.sub("", text)
    text = NOISE_PAREN.sub("", text)
    text = re.sub(r"\s*[\(\[]\s*\d{1,3}\s*of\s*\d{1,3}\s*[\)\]]", "", text)   # "(01 of 12)"
    text = re.sub(r"\s*\bB0[0-9A-Z]{8}\b", "", text, flags=re.I)           # Amazon ASIN
    text = re.sub(r"\s*[\(\[]\s*\d+\s*(?:mp3s?|files|tracks|discs?|parts?|cds?|books?)\b[^\)\]]*[\)\]]", "", text, flags=re.I)
    text = re.sub(r"[\s\-_.,]*\b(?:unb|unabr|unabridged|abridged)\b[\s\-_.,]*", " ", text, flags=re.I)
    text = re.sub(r"\s+\d{10}(?:\d{3})?\s*$", "", text)                      # bare ISBN
    text = re.sub(r"^[A-Z]{1,4}\s?\d{1,3}\s+(?=[A-Za-z])", "", text)         # "MR07 Memorial Day", "MR 23 ..."
    if author:
        text = re.sub(r"^" + re.escape(author) + r"\s*[-:]\s*", "", text, flags=re.I)    # "Larry Niven-Footfall"
    text = re.sub(r"^(?:collection|novel|audiobook)\s*[-:]\s*", "", text, flags=re.I)
    text = re.sub(r"[\s:,\-]*\bA Novel\s*$", "", text, flags=re.I)
    text = re.sub(r"(\w)_ (?=\w)", r"\1: ", text)          # "Dawn Girl_ A Thriller" -> "Dawn Girl: A Thriller"
    text = re.sub(r"(\w)- (?=[A-Za-z])", r"\1: ", text)     # "Star Force- Origin" -> "Star Force: Origin"
    if author:
        # trailing "by Leslie Wolfe" duplicating the author
        text = re.sub(r"\s+by\s+" + re.escape(author) + r"\s*$", "", text, flags=re.I)
    if series:
        text = re.sub(r"\s*[\(\[]\s*" + re.escape(series) + r"(?:\s*(?:,|#|book)?\s*\d+)?\s*[\)\]]",
                      "", text, flags=re.I)
    if author:
        # "(Jyr)" when the author is "Aer-ki Jyr": a disambiguation fragment
        a_words = {w.lower().strip(".,") for w in author.split() if len(w) > 2}
        def _author_frag(mm):
            inner = mm.group(1).strip().lower()
            return "" if inner in a_words or inner == author.lower() else mm.group(0)
        text = re.sub(r"\s*[\(\[]([^\)\]]+)[\)\]]", _author_frag, text)
    if STRIP_ALL_PARENS:
        # strip everything EXCEPT parentheticals carrying box-set/volume info
        text = re.sub(r"\s*[\(\[]([^\)\]]*)[\)\]]",
                      lambda mm: mm.group(0) if CONTENT_PAREN.search(mm.group(1)) else "", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.,:;")
    return text, narrator


# ----------------------------------------------------------------------------
# Metadata extraction: audio files (mutagen)
# ----------------------------------------------------------------------------

def read_audio_metadata(path: str, multi: bool = False) -> dict:
    """Return {'title','author','series','series_index'} best-effort from tags.
    For multi-file books (multi=True) only the ALBUM tag names the book -
    the per-track TITLE tag is a chapter name and must not be used."""
    meta = {}
    if not HAVE_MUTAGEN:
        return meta
    try:
        f = mutagen.File(path)
        if f is None:
            return meta
        tags = f.tags
        if tags is None:
            return meta

        if isinstance(f, MP4):
            def mp4get(key):
                v = tags.get(key)
                if v:
                    return str(v[0])
                return None
            meta["title"] = mp4get("\xa9alb") or (None if multi else mp4get("\xa9nam"))
            meta["author"] = mp4get("aART") or mp4get("\xa9ART") or mp4get("\xa9wrt")
            meta["series"] = mp4get("\xa9mvn") or mp4get("----:com.apple.iTunes:SERIES")
            mvi = tags.get("\xa9mvi")
            if mvi:
                try:
                    meta["series_index"] = str(mvi[0][0] if isinstance(mvi[0], tuple) else mvi[0])
                except Exception:
                    pass
            if not meta.get("series_index"):
                sp = mp4get("----:com.apple.iTunes:SERIES-PART")
                if sp:
                    meta["series_index"] = sp
        elif hasattr(tags, "getall"):  # ID3 (mp3)
            def id3text(frame):
                fr = tags.getall(frame)
                return str(fr[0].text[0]) if fr and fr[0].text else None
            meta["title"] = id3text("TALB") or (None if multi else id3text("TIT2"))
            meta["author"] = id3text("TPE2") or id3text("TPE1") or id3text("TCOM")
            meta["series"] = id3text("MVNM")   # TIT1 (grouping) is often chapter/part junk
            meta["series_index"] = id3text("MVIN")
            if not meta.get("series"):
                for fr in tags.getall("TXXX"):
                    d = fr.desc.lower()
                    if d == "series":
                        meta["series"] = str(fr.text[0])
                    elif d in ("series-part", "series_index", "seriespart"):
                        meta["series_index"] = str(fr.text[0])
        else:  # Vorbis comments (flac/ogg/opus)
            def vget(*keys):
                for k in keys:
                    v = tags.get(k)
                    if v:
                        return str(v[0])
                return None
            meta["title"] = vget("album") or (None if multi else vget("title"))
            meta["author"] = vget("albumartist", "artist", "author")
            meta["series"] = vget("series", "mvnm")
            meta["series_index"] = vget("series-part", "seriesindex", "mvin")
    except Exception:
        pass
    # Decode possible byte values, strip empties
    return {k: str(v).strip() for k, v in meta.items() if v and str(v).strip()}


# ----------------------------------------------------------------------------
# Metadata extraction: EPUB
# ----------------------------------------------------------------------------

def read_epub_metadata(path: str) -> dict:
    meta = {}
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            cns = "{urn:oasis:names:tc:opendocument:xmlns:container}"
            rootfile = container.find(f".//{cns}rootfile")
            opf_path = rootfile.get("full-path")
            opf = ET.fromstring(z.read(opf_path))

        dc = "{http://purl.org/dc/elements/1.1/}"
        opfns = "{http://www.idpf.org/2007/opf}"
        t = opf.find(f".//{dc}title")
        a = opf.find(f".//{dc}creator")
        if t is not None and t.text:
            meta["title"] = t.text.strip()
        if a is not None and a.text:
            meta["author"] = a.text.strip()

        # Calibre-style series metadata + EPUB3 collections
        collections = {}
        for m in opf.iter(f"{opfns}meta"):
            name, content, prop = m.get("name"), m.get("content"), m.get("property")
            if name == "calibre:series" and content:
                meta["series"] = content.strip()
            elif name == "calibre:series_index" and content:
                meta["series_index"] = content.strip()
            elif prop == "belongs-to-collection" and m.text:
                collections[m.get("id")] = m.text.strip()
            elif prop == "group-position" and m.text and not meta.get("series_index"):
                meta["series_index"] = m.text.strip()
        if not meta.get("series") and collections:
            meta["series"] = next(iter(collections.values()))
    except Exception:
        pass
    return meta


def read_opf_file(path: str) -> dict:
    """Read a sidecar .opf file (Calibre-style) sitting next to the book."""
    meta = {}
    try:
        opf = ET.parse(path).getroot()
        dc = "{http://purl.org/dc/elements/1.1/}"
        opfns = "{http://www.idpf.org/2007/opf}"
        t = opf.find(f".//{dc}title")
        a = opf.find(f".//{dc}creator")
        if t is not None and t.text:
            meta["title"] = t.text.strip()
        if a is not None and a.text:
            meta["author"] = a.text.strip()
        for m in opf.iter(f"{opfns}meta"):
            name, content = m.get("name"), m.get("content")
            if name == "calibre:series" and content:
                meta["series"] = content.strip()
            elif name == "calibre:series_index" and content:
                meta["series_index"] = content.strip()
    except Exception:
        pass
    return meta


def read_abs_metadata_json(path: str) -> dict:
    """Read an Audiobookshelf metadata.json sitting in the book folder."""
    meta = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("title"), str):
            meta["title"] = data["title"].strip()
        authors = data.get("authors") or []
        if authors:
            a = authors[0]
            name = a.get("name") if isinstance(a, dict) else a
            if isinstance(name, str) and name.strip():
                meta["author"] = name.strip()
        series = data.get("series") or []
        if series:
            s = series[0]
            if isinstance(s, dict):
                if s.get("name"):
                    meta["series"] = str(s["name"]).strip()
                if s.get("sequence") not in (None, ""):
                    meta["series_index"] = str(s["sequence"])
            elif isinstance(s, str) and s.strip():
                # "Series Name #2" form
                m = re.match(r"^(.*?)(?:\s*#\s*([\d.]+))?$", s.strip())
                meta["series"] = m.group(1).strip()
                if m.group(2):
                    meta["series_index"] = m.group(2)
    except Exception:
        pass
    return meta


def read_sidecar_metadata(dirpath: str, filenames) -> tuple:
    """
    Curated metadata files sitting in the book folder beat everything else.
    A Calibre .opf outranks metadata.json: OPFs are deliberately curated,
    while Audiobookshelf often auto-generates metadata.json from whatever
    folder names it saw on first import (garbage in, garbage out).
    Returns (meta_dict, source_label).
    """
    opfs = [f for f in filenames if f.lower().endswith(".opf")]
    opfs.sort(key=lambda f: (f.lower() != "metadata.opf", f.lower()))
    if not DISTRUST_OPF:
        for f in opfs:
            meta = read_opf_file(os.path.join(dirpath, f))
            if meta:
                return meta, "opf"
    names = {f.lower(): f for f in filenames}
    if "metadata.json" in names and not DISTRUST_JSON:
        meta = read_abs_metadata_json(os.path.join(dirpath, names["metadata.json"]))
        if meta:
            return meta, "metadata.json"
    return {}, ""


# ----------------------------------------------------------------------------
# Metadata extraction: MOBI / AZW3 (raw EXTH header parsing, best-effort)
# ----------------------------------------------------------------------------

def read_mobi_metadata(path: str) -> dict:
    meta = {}
    try:
        with open(path, "rb") as fh:
            head = fh.read(78 + 8 * 2)  # PalmDB header + first two record entries
            if len(head) < 94:
                return meta
            rec0_off = struct.unpack(">I", head[78:82])[0]
            rec1_off = struct.unpack(">I", head[86:90])[0]
            fh.seek(rec0_off)
            rec0 = fh.read(max(rec1_off - rec0_off, 4096))
        if rec0[16:20] != b"MOBI":
            return meta
        header_len = struct.unpack(">I", rec0[20:24])[0]
        exth_flag = struct.unpack(">I", rec0[128:132])[0]

        # Full book name
        try:
            fn_off = struct.unpack(">I", rec0[84:88])[0]
            fn_len = struct.unpack(">I", rec0[88:92])[0]
            title = rec0[fn_off:fn_off + fn_len].decode("utf-8", "ignore").strip()
            if title:
                meta["title"] = title
        except Exception:
            pass

        if exth_flag & 0x40:
            exth = rec0[16 + header_len:]
            if exth[:4] == b"EXTH":
                count = struct.unpack(">I", exth[8:12])[0]
                pos = 12
                for _ in range(count):
                    if pos + 8 > len(exth):
                        break
                    rtype, rlen = struct.unpack(">II", exth[pos:pos + 8])
                    if rlen < 8 or pos + rlen > len(exth):
                        break
                    val = exth[pos + 8:pos + rlen].decode("utf-8", "ignore").strip()
                    if rtype == 100 and val:
                        meta["author"] = val
                    elif rtype == 503 and val:
                        meta["title"] = val
                    pos += rlen
    except Exception:
        pass
    return meta


# ----------------------------------------------------------------------------
# Filename / folder-name parsing fallback
# ----------------------------------------------------------------------------

SERIES_PATTERNS = [
    re.compile(r"^(?P<series>.+?)\s*(?:,|-)?\s*(?:book|bk|vol(?:ume)?|#)\s*(?P<idx>\d+(?:\.\d+)?)\s*[-:]\s*(?P<title>.+)$", re.I),
    re.compile(r"^(?P<series>.+?)\s+(?P<idx>\d{1,2}(?:\.\d+)?)\s*[-:]\s*(?P<title>.+)$"),
]


def parse_name(name: str) -> dict:
    """Guess author/series/title from a folder or file name."""
    meta = {}
    base = re.sub(r"\.(m4b|m4a|mp3|flac|ogg|opus|wma|aac|epub|mobi|azw3?|pdf)$", "", name, flags=re.I)
    base = re.sub(r"[\[\(](?:unabridged|abridged|audiobook|\d{4}|mp3|64k|128k)[\]\)]", "", base, flags=re.I).strip()
    base = re.sub(r"^(?:19|20)\d{2}\s*[-._]\s*", "", base).strip()   # "1986 - Belinda"
    base = strip_release_junk(base)

    by_base = re.sub(r"(?:\s*[\(\[][^\)\]]*[\)\]])+\s*$", "", base)     # "... by X (Audiobook)(Nonfiction)"
    m = re.match(r"^(?P<title>.+?)\s+by\s+(?P<author>[A-Za-z][\w.'\- ]+)$", by_base, re.I)
    if m:
        who = m.group("author").strip()
        # "THE MARTIAN by andy weir": a lowercase author must be 2-4 words, so
        # "Death by chocolate" is not read as the author "chocolate"
        pronoun = who.lower() in ("me", "you", "us", "him", "her", "them", "it", "night", "day",
                                  "starlight", "moonlight", "candlelight", "design", "default")
        if not pronoun and (who[:1].isupper() or 2 <= len(who.split()) <= 4):
            if who.islower():
                who = who.title()
            meta["title"], meta["author"] = m.group("title").strip(), who
            return meta

    bs = re.match(r"^[\[\(](?P<series>[^\]\)]+?)\s+(?P<idx>\d{1,3}(?:\.\d+)?)[\]\)]\s*[-:]?\s*(?P<title>.+)$", base)
    if bs:
        return {"series": bs.group("series").strip(), "series_index": bs.group("idx"),
                "title": bs.group("title").strip()}
    tri = re.match(r"^(?P<author>[A-Z][\w.']+ [A-Z][\w.']+)\s+(?:Trilogy|Series|Saga)\s+(?P<series>.+?)\s*[-\s]+(?P<idx>\d{1,2})[-\s]*(?P<title>[A-Za-z].+)$", base)
    if tri:
        return {"author": tri.group("author"), "series": tri.group("series").strip(" -"),
                "series_index": tri.group("idx"), "title": tri.group("title").strip()}
    parens = re.findall(r"\s*[\(\[][^\)\]]*[\)\]]", base)
    base_np = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", base).strip()
    parts = [p.strip() for p in base_np.split(" - ") if p.strip()]
    if parens and parts:
        parts[-1] = parts[-1] + "".join(parens)     # parens belong to the title
    if len(parts) == 1 and base_np.count("-") == 1 and " - " not in base_np:
        left, right = [p.strip() for p in base_np.split("-")]
        # "Larry Niven-Saturns Race": left looks like a person, right like a title
        if 2 <= len(left.split()) <= 4 and len(right.split()) >= 2 \
                and not re.match(r"(?i)(?:the|a|an)\s", left) \
                and all(w[:1].isupper() for w in left.split()) and not any(ch.isdigit() for ch in left):
            parts = [left, right + "".join(parens)]
    if len(parts) >= 3:
        # Author - Series NN - Title   or   Author - Series - NN Title
        meta["author"] = parts[0]
        sw = re.match(r"^(?P<series>.+?)(?:\s+series)?,?\s*(?:book|bk|vol(?:ume)?|#)\s*(?P<idx>\d{1,3})\s*$", parts[-1], re.I)
        if sw and not re.search(r"\b(?:book|vol|series)\b", parts[1], re.I):
            # Author - Title - Series, Book N
            meta["series"], meta["series_index"], meta["title"] = sw.group("series").strip(), sw.group("idx"), parts[1]
            return meta
        rest = " - ".join(parts[1:])
        for pat in SERIES_PATTERNS:
            m = pat.match(rest)
            if m:
                meta["series"] = m.group("series").strip()
                meta["series_index"] = m.group("idx")
                meta["title"] = m.group("title").strip()
                return meta
        meta["series"] = parts[1]
        meta["title"] = parts[-1]
        m = re.match(r"^(\d{1,2}(?:\.\d)?)\s*[-. ]\s*(.+)$", meta["title"])
        if m:
            meta["series_index"], meta["title"] = m.group(1), m.group(2)
        return meta
    if len(parts) == 2:
        sp = re.match(r"^(?P<series>[A-Za-z][^-]*?)\s+(?P<idx>\d{1,3}(?:\.\d+)?)$", parts[0])
        if sp and not any(ch.isdigit() for ch in parts[1][:1]):
            # "Jack Ryan 12 - Teeth of the Tiger": series+index, then title
            return {"series": sp.group("series").strip(), "series_index": sp.group("idx"), "title": parts[1]}
        meta["author"], meta["title"] = parts[0], parts[1]
        for pat in SERIES_PATTERNS:
            m = pat.match(parts[1])
            if m:
                meta["series"] = m.group("series").strip()
                meta["series_index"] = m.group("idx")
                meta["title"] = m.group("title").strip()
                break
        return meta
    meta["title"] = parts[0] if parts else base
    for pat in SERIES_PATTERNS:
        m = pat.match(base)
        if m:
            meta["series"] = m.group("series").strip()
            meta["series_index"] = m.group("idx")
            meta["title"] = m.group("title").strip()
            break
    return meta


def _strip_calibre_id(name: str, any_digits: bool = False) -> str:
    """Calibre folders look like 'The Martian (123)' - drop the id. Short numbers
    like '(5)' are kept for audiobooks (box-set volumes) unless any_digits."""
    pat = r"\s*\(\d+\)\s*$" if any_digits else r"\s*\(\d{3,}\)\s*$"
    return re.sub(pat, "", name).strip()


JUNK_WORDS = {"audiobooks", "audiobook", "ebooks", "ebook", "books", "book",
              "unsorted", "new", "folder", "downloads", "download", "fiction",
              "nonfiction", "series", "misc", "temp", "incoming", "media",
              "source", "dest", "output", "complete", "collection", "library",
              "torrents", "import", "todo", "stuff", "unknown", "various", "author",
              "authors", "tmp", "metadata", "cache", "watch", "untitled"}


def _is_junk_folder(name: str) -> bool:
    """'New folder (3)', 'Unsorted', 'downloads' - carries no book info."""
    words = _strip_calibre_id(name, any_digits=True).replace("_", " ").split()
    return bool(words) and all(w.lower().strip(".") in JUNK_WORDS or w.isdigit()
                               for w in words)


def _looks_like_author(name: str) -> bool:
    """Heuristic: 'Brandon Sanderson', 'J.R.R. Tolkien', 'Le Guin, Ursula K.'"""
    name = strip_author_suffix(_strip_calibre_id(name.strip(), any_digits=True))
    if not name or " - " in name or any(ch.isdigit() for ch in name):
        return False
    if name.count("-") == 1 and all(len(p.split()) >= 2 for p in name.split("-")):
        return False   # "Larry Niven-Saturns Race" is Author-Title, not an author
    words = name.replace(",", " ").split()
    if not 1 <= len(words) <= 4:
        return False
    # A single all-lowercase word ('dupes', 'ripped', 'misc2') is a folder,
    # not an author - real single-name authors are capitalized ('Homer').
    if len(words) == 1 and (name[:1].islower() or len(name.strip(".")) < 3):
        return False   # 'dupes', or alphabetical bins like 'A', 'B', 'M-Z'
    if len(words) == 1 and re.fullmatch(r"[A-Za-z]-[A-Za-z]", name.strip()):
        return False
    return not any(w.lower().strip(".") in JUNK_WORDS for w in words)


SERIESY_WORDS = re.compile(r"\b(series|saga|trilogy|chronicles?|cycle|quartet|"
                           r"quintet|sequence|collection|omnibus|archives?|files|"
                           r"universe|adventures|mysteries|tales|legends)\b", re.I)


def _clean_series_name(name: str) -> str:
    out = re.sub(r"\s*(series|saga|trilogy|cycle|sequence)$", "", name, flags=re.I).strip()
    return out or name


def infer_from_path(rel_dir: str, source_root_name: str = "") -> dict:
    """
    Use the folder hierarchy between the source root and the book as a signal:
        Author/Book                    -> author
        Author/Series/Book             -> author + series
        Series Saga/01 - Book          -> series (top folder is series-like)
        Series/Book 2 - Title          -> series (numbered leaf implies a series above it)
    Only fills fields it is reasonably confident about.
    """
    meta = {}
    if not rel_dir or rel_dir == ".":
        return meta
    parts = [_strip_calibre_id(p) for p in rel_dir.replace("\\", "/").split("/") if p]
    if not parts:
        return meta

    leaf = parts[-1]
    leaf_m = re.match(r"^(?:book|vol(?:ume)?|part|#)?\s*(\d{1,3}(?:\.\d)?)\s*[-. ]\s*(.+)$",
                      leaf, re.I)

    top = parts[0]
    by = re.match(r"^(?P<series>.+?)\s+by\s+(?P<author>[A-Z][\w.'\- ]+)$", top)
    if by and not _is_junk_folder(by.group("series")):
        meta["author"] = by.group("author").strip()
        if len(parts) >= 2:
            meta["series"] = _clean_series_name(by.group("series").strip())
        if leaf_m and meta.get("series"):
            meta["series_index"] = leaf_m.group(1)
            meta["title"] = leaf_m.group(2).strip()
        return meta
    top_is_seriesy = bool(SERIESY_WORDS.search(top))
    if _looks_like_author(top) and not top_is_seriesy:
        meta["author"] = top
        if len(parts) >= 3 and " - " not in parts[1] and not _is_junk_folder(parts[1]):
            meta["series"] = _clean_series_name(parts[1])
    elif not _is_junk_folder(top) and " - " not in top and (
            top_is_seriesy or (len(parts) >= 2 and leaf_m)):
        # Top folder is a series: either it says so ("... Saga"), or it sits
        # directly above numbered book folders ("Series/01 - Title").
        meta["series"] = _clean_series_name(top)
        if len(parts) >= 3 and _looks_like_author(parts[1]):
            pass  # unusual Series/Author nesting - don't guess

    # Numbered leaf: pull the index (and clean title) when a series is known,
    # or record just the index when the numbering is explicit ("Book 2 - ...").
    if leaf_m:
        explicit = bool(re.match(r"^(?:book|vol(?:ume)?|part|#)", leaf, re.I))
        if meta.get("series") or explicit:
            meta["series_index"] = leaf_m.group(1)
            meta["title"] = leaf_m.group(2).strip()
    return meta


# ----------------------------------------------------------------------------
# Online lookup: Google Books, then Open Library (no API keys needed)
# ----------------------------------------------------------------------------

def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "BookOrganizer/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def audible_lookup(title: str, author: str) -> dict:
    """Audible catalog search - the best source for audiobook series info."""
    try:
        params = {"title": title, "num_results": 5,
                  "response_groups": "contributors,series",
                  "products_sort_by": "Relevance"}
        if author:
            params["author"] = author
        url = "https://api.audible.com/1.0/catalog/products?" + urllib.parse.urlencode(params)
        data = _http_json(url)
        for p in data.get("products", []):
            at = p.get("title", "")
            aa = (p.get("authors") or [{}])[0].get("name", "")
            if similar(at, title) > 0.55 and (not author or similar(aa, author) > 0.5):
                out = {"title": at, "author": aa, "source": "Audible"}
                series = p.get("series") or []
                if series:
                    st = series[0].get("title", "")
                    if st:
                        out["series"] = re.sub(r"\s*\(.*\)$", "", st).strip()
                    seq = series[0].get("sequence")
                    if seq:
                        out["series_index"] = str(seq)
                return out
    except Exception:
        pass
    return {}


def lookup_online(title: str, author: str, kind: str = "") -> dict:
    """Return improved {'title','author'[,'series','series_index']} on a confident match."""
    if not title:
        return {}
    # --- Audible first for audiobooks: it actually knows series ---
    if kind == "audio":
        found = audible_lookup(title, author)
        if found:
            return found
    # --- Google Books ---
    try:
        q = f"intitle:{title}"
        if author:
            q += f" inauthor:{author}"
        url = "https://www.googleapis.com/books/v1/volumes?maxResults=5&q=" + urllib.parse.quote(q)
        data = _http_json(url)
        for item in data.get("items", []):
            info = item.get("volumeInfo", {})
            gt = info.get("title", "")
            ga = (info.get("authors") or [""])[0]
            if similar(gt, title) > 0.55 and (not author or similar(ga, author) > 0.5):
                out = {"title": gt, "author": ga, "source": "Google Books"}
                sub = info.get("subtitle")
                if sub and re.search(r"(book|novel)\s+\w+\s+of", sub, re.I):
                    pass  # subtitle often contains series info but is unreliable; skip
                return out
    except Exception:
        pass
    # --- Open Library ---
    try:
        params = {"title": title, "limit": 5, "fields": "title,author_name"}
        if author:
            params["author"] = author
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
        data = _http_json(url)
        for doc in data.get("docs", []):
            ot = doc.get("title", "")
            oa = (doc.get("author_name") or [""])[0]
            if similar(ot, title) > 0.55 and (not author or similar(oa, author) > 0.5):
                return {"title": ot, "author": oa, "source": "Open Library"}
    except Exception:
        pass
    return {}


# ----------------------------------------------------------------------------
# Scanner: walk the tree, group files into "books"
# ----------------------------------------------------------------------------

@dataclass
class Book:
    kind: str                     # "audio" or "ebook"
    files: list = field(default_factory=list)   # absolute paths of book files
    extras: list = field(default_factory=list)  # covers, nfo, etc. (audio folders)
    src_display: str = ""
    author: str = ""
    title: str = ""
    series: str = ""
    series_index: str = ""
    status: str = "pending"
    meta_source: str = "filename"
    narrator: str = ""
    # ---- v2: enrichment (filled by enrich.py / ai.py, never by the parser) ----
    fingerprint: str = ""         # content fingerprint of the book's files
    year: str = ""
    publisher: str = ""
    description: str = ""
    genres: str = ""
    language: str = ""
    asin: str = ""
    isbn: str = ""
    duration: float = 0.0         # seconds, all parts
    bitrate: int = 0
    codec: str = ""
    chapters: int = 0
    health: str = ""              # "" = fine; otherwise what looks damaged
    cover: str = ""               # path of the chosen cover image
    cover_info: str = ""
    cover_candidates: list = field(default_factory=list)
    confidence: int = 0
    match_bonus: int = 0
    online_best: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)        # last AI answer for this book
    transcript: str = ""
    front_text: str = ""
    locked: list = field(default_factory=list)    # fields YOU set - nothing overwrites them
    enriched: bool = False
    dup_skipped: bool = False
    flags: list = field(default_factory=list)     # warnings, e.g. "author looks like a narrator"

    def dest_folder(self, dest_root: str, layout: str = "nested_series") -> str:
        a = sanitize(self.author or "Unknown Author")
        t = sanitize(self.title)
        s = sanitize(self.series) if self.series else ""
        idx = fmt_series_index(self.series_index)
        if layout == "nested_noseries":
            return os.path.join(dest_root, a, t)
        if layout == "flat_ast":
            if s:
                name = f"{a} - {s} {idx} - {t}" if (idx and NUMBER_FOLDERS) else f"{a} - {s} - {t}"
            else:
                name = f"{a} - {t}"
            return os.path.join(dest_root, sanitize(name))
        if layout == "flat_at":
            return os.path.join(dest_root, sanitize(f"{a} - {t}"))
        if layout == "flat_t":
            return os.path.join(dest_root, t)
        # default: nested_series -> Author/Series/NN - Title
        parts = [dest_root, a]
        if s:
            parts.append(s)
            leaf = f"{idx} - {t}" if (idx and NUMBER_FOLDERS) else t
        else:
            leaf = t
        parts.append(leaf)
        return os.path.join(*parts)


def _fix_ambiguous_order(meta: dict, known_author: str) -> dict:
    """
    Names like 'The Martian - Andy Weir' parse as author='The Martian',
    title='Andy Weir'. If we already know the author from tags or the folder
    hierarchy, detect and swap when the parsed *title* is actually the author.
    """
    if not known_author or not meta:
        return meta
    t, a = meta.get("title", ""), meta.get("author", "")
    if t and similar(t, known_author) > 0.8 and a and similar(a, known_author) < 0.6:
        meta = dict(meta)
        meta["title"], meta["author"] = a, t
    elif a and similar(a, known_author) < 0.4 and t and similar(t, known_author) < 0.4:
        pass  # neither part is the known author; leave as parsed
    return meta


def _note_conflict(book: Book, tags: dict, path_meta: dict, side: dict = None):
    """Mark the row when the chosen source disagrees with the folder path -
    those rows are the ones worth a glance in review."""
    def _differs(a, b):
        return bool(a and b and similar(clean_author(a) if k == "author" else a,
                                        clean_author(b) if k == "author" else b) < 0.6)
    for k in ("title", "author"):
        p = path_meta.get(k) if path_meta else None
        if side and _differs(side.get(k), p) and "differs" not in book.meta_source:
            book.meta_source += " (path differs)"
            return
        t = tags.get(k) if tags else None
        if _differs(t, p) and "differ" not in book.meta_source:
            book.meta_source += " (tags differ)" if TRUST_FOLDERS else " (path differs)"
            return


def _late_sources(book: Book, dirpath: str, tags: dict = None):
    """Distrusted sources (opf / metadata.json / embedded tags) only fill
    what nothing else could - applied after folders and filenames."""
    if book.title and not any(ch.isalpha() for ch in book.title):
        book.title = ""          # "01" from 01.mp3 is not a title - let a real source fill it
    if DISTRUST_TAGS and tags:
        _merge_meta(book, tags, "tags (last resort)")
    if DISTRUST_OPF:
        try:
            opfs = sorted((f for f in os.listdir(dirpath) if f.lower().endswith(".opf")),
                          key=lambda f: (f.lower() != "metadata.opf", f.lower()))
        except OSError:
            opfs = []
        for f in opfs:
            meta = read_opf_file(os.path.join(dirpath, f))
            if meta:
                _merge_meta(book, meta, "opf (last resort)")
                break
    if DISTRUST_JSON:
        p = os.path.join(dirpath, "metadata.json")
        if os.path.isfile(p):
            _merge_meta(book, read_abs_metadata_json(p), "metadata.json (last resort)")


def _merge_meta(book: Book, meta: dict, source: str):
    changed = False
    for k in ("title", "author", "series", "series_index"):
        if meta.get(k) and not getattr(book, k):
            if k == "author" and (not any(ch.isalpha() for ch in str(meta[k]))
                                  or any(ch.isdigit() for ch in str(meta[k]))
                                  or _is_junk_folder(str(meta[k]))):
                continue
            setattr(book, k, str(meta[k]).strip())
            changed = True
    if changed and book.meta_source == "filename":
        book.meta_source = source


# ---- Disc / part awareness -------------------------------------------------
DISC_WORD = r"(?:disc|disk|cd|part|pt|side|tape)"
ROMAN = r"(?=[IVXLC])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
NUM = r"(?P<n>\d{1,3}|" + ROMAN + r")"
DISC_TAIL_RE = re.compile(
    r"^(?P<base>.*?)[\s._\-\(\[]*" + DISC_WORD + r"[\s._\-]*" + NUM +
    r"(?:\s*(?:of|/)\s*\d{1,3})?[\)\]]*\s*$", re.I)
DISC_ONLY_RE = re.compile(
    r"^" + DISC_WORD + r"[\s._\-]*" + NUM + r"(?:\s*(?:of|/)\s*\d{1,3})?$", re.I)
# Child folders of a book: "1_ Part I - Subtitle", "02 - Part II", "Part 3", "CD1", "01"
DISC_CHILD_RE = re.compile(
    r"^(?:(?P<idx>\d{1,3})[\s._\-)]+)?" + DISC_WORD + r"[\s._\-]*" + NUM +
    r"(?:\s*(?:of|/)\s*\d{1,3})?(?:\s*[-:_.]\s*(?P<sub>.+))?$", re.I)


def _to_int(numstr: str) -> int:
    if numstr.isdigit():
        return int(numstr)
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, prev = 0, 0
    for ch in reversed(numstr.upper()):
        v = vals.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def split_disc_marker(name: str):
    """'The Martian - Disc 2' -> ('The Martian', 2); 'CD3' -> ('', 3);
    'Part IV' -> ('', 4); 'Fahrenheit 451' -> ('Fahrenheit 451', None)."""
    n = name.strip()
    m = DISC_ONLY_RE.match(n)
    if m:
        return "", _to_int(m.group("n"))
    m = DISC_TAIL_RE.match(n)
    if m and m.group("base").strip(" -_.([") and not m.group("base").lower().rstrip(" -_.").endswith(("vol", "volume", "book")):
        return m.group("base").strip(" -_.([").strip(), _to_int(m.group("n"))
    return n, None


def disc_child_number(name: str):
    """Is this folder a part of its parent book? Returns the part number or None.
    Accepts '1_ Part I - The World of Jeremy Walker', 'Part VII', 'CD 2',
    '03', or 'Disc 1 of 5'."""
    n = name.strip()
    if re.fullmatch(r"\d{1,3}", n):
        return int(n)
    m = re.fullmatch(r"[A-Za-z]{1,2}(\d{1,3})", n)          # "D01", "PT2"
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"[\(\[]?\s*(\d{1,3})\s*of\s*\d{1,3}\s*[\)\]]?", n)   # "(6of17)"
    if m:
        return int(m.group(1))
    m = DISC_CHILD_RE.match(n)
    if m:
        return _to_int(m.group("n"))
    base, dn = split_disc_marker(n)
    return dn if dn is not None and base == "" else None


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def strip_part_numbers(stem: str) -> str:
    """Remove track/part numbering from a file stem, case preserved.
    'LNSR01-95 Larry Niven - Saturn's Race' -> 'Larry Niven - Saturn's Race'
    '01 - Dune' -> 'Dune'; 'Dune Part 3' -> 'Dune'; 'track07' -> 'track'
    'Fahrenheit 451' / '1984' stay intact (numbers of 3+ digits are content)."""
    stem, _ = split_disc_marker(stem)
    stem = re.sub(r"[\s._\-]+(?:of|/)[\s._\-]*\d{1,3}$", "", stem)      # "3 of 12"
    stem = re.sub(r"[\s._\-]*[\(\[]\s*\d{1,3}(?:\s*(?:of|/)\s*\d{1,3})?\s*[\)\]]", "", stem)  # "(3 of 12)"
    # Leading numbered tokens: "01 -", "Track03", "LNSR01-95", "1-95", "cd2_"
    # (a token counts as numbering if it has digits but no 4-digit run)
    while True:
        m = re.match(r"^(\S*\d\S*)([\s._\-]+)", stem)
        if not m or re.search(r"\d{4}", m.group(1)) or len(re.sub(r"\D", "", m.group(1))) > 5:
            break
        stem = stem[m.end():]
    # Trailing 1-2 digit part number, with or without a separator
    stem = re.sub(r"[\s._\-]*(?<!\d)\d{1,2}$", "", stem)
    stem = re.sub(r"^\d{1,3}$", "", stem)
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip(" -_.")


def file_group_key(filename: str) -> str:
    """Stem with disc/part/track numbers removed, lower-cased: files that
    share it (in one folder) are parts of ONE book."""
    return strip_part_numbers(os.path.splitext(filename)[0]).lower()


def _disc_sort_key(path: str):
    """Order parts: by disc number found in the parent folder or the file name,
    then natural filename order."""
    parent = os.path.basename(os.path.dirname(path))
    dn = disc_child_number(parent)
    if dn is None:
        _, dn = split_disc_marker(parent)
    if dn is None:
        _, dn = split_disc_marker(os.path.splitext(os.path.basename(path))[0])
    return (dn if dn is not None else 0, natural_key(os.path.basename(path)))


def _collect_audio_under(dirpath: str):
    """All audio files under a disc folder (recursively), plus extras."""
    audio, extras = [], []
    for dp, dn, fn in os.walk(dirpath):
        dn.sort(key=natural_key)
        for f in sorted(fn, key=natural_key):
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTS:
                audio.append(os.path.join(dp, f))
            elif ext in EXTRA_EXTS:
                extras.append(os.path.join(dp, f))
    return audio, extras


# ---- Grouping audio files within one folder into books ----------------------
_UNIT = r"(?:\s*(?:of|/|-|to)\s*\d{1,3}(?!\d))?"       # "09-49", "3 of 12", "01 to 14"
_CUT_RE = re.compile(
    r"[\s._\-\(\[]*(?:(?<![A-Za-z])(?:disc|disk|cd|part|pt|side|tape|chapter|chap|ch|track|trk|episode|ep)"
    r"[\s._\-]*(?:\d{1,3}|" + ROMAN + r")(?![a-z])" + _UNIT +
    r"|(?<![A-Za-z\d])[A-Za-z]{1,5}\d{1,3}(?:\.\d{1,3})?(?!\d)" + _UNIT +
    r"|(?<!\d)\d{1,3}(?:\.\d{1,3})?(?!\d)" + _UNIT + r")", re.I)
_TRAIL_RE = re.compile(r"^(?P<mid>.*?\S)[\s._\-]+(?P<n>\d{1,3}(?!\d|\.\d)" + _UNIT + r")\s*$")
_CHAPTERISH = re.compile(r"\b(?:chapter|chap|ch|track|trk|part|pt|disc|disk|cd|episode|ep)\.?\s*$", re.I)
_INDEXISH = re.compile(r"(?:\b(?:book|bk|vol|volume|no|number|season|series)\.?|#)\s*$", re.I)
_CODEISH = re.compile(r"\b[A-Z]{2,4}\s*$")          # "MR 23", "DT 4" - a series code before the number


def _cut_span(stem: str):
    """Where does the part/track numbering start in this stem?
    'Dawn Girl - 01 - Opening Credits'  -> at '01'
    'Lee Child - Reacher 27 - Personal 12' -> at '12' (27 is a series index)
    'Dawn Girl - 02 - Chapter 1'         -> at '02' (the 1 is a chapter number)"""
    m = None
    for cand in _CUT_RE.finditer(stem):
        before = stem[:cand.start()]
        if cand.start() > 0 and (_INDEXISH.search(before) or _CODEISH.search(before)) \
                and not re.search(r"(?:disc|cd|part|track|chapter)", cand.group(0), re.I):
            continue        # "Book 18", "MR 23", "No. 4": a series index, keep looking
        m = cand
        break
    if not m:
        return None
    if m.start() == 0:
        return (0, m.end())     # a leading number is always the track number
    rest = stem[m.end():]
    t = _TRAIL_RE.match(rest)
    if t and not _CHAPTERISH.search(t.group("mid")) and re.search(r"[A-Za-z]", t.group("mid")):
        # the first number is followed by more words that END in a plain number:
        # that trailing number is the real part number
        return (m.end() + t.start("n") - len(re.match(r"[\s._\-]*", rest[t.start("n")-1:t.start("n")]).group(0)), len(stem))
    return (m.start(), m.end())


def _normalize_stem(stem: str) -> str:
    """'Long.Shadows.[Unabridged].-.006' -> 'Long Shadows [Unabridged] - 006'."""
    if " " not in stem and stem.count(".") >= 2:
        stem = stem.replace(".", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    # "... (Unabridged) 32k": a bitrate / size / codec tag is not a part number
    return strip_release_junk(stem) if re.search(r"(?i)\d\s*(?:k|kbps|mb|gb|khz)\b|\b(?:mp3|m4b|aac|vbr|cbr)\b", stem) else stem


def book_prefix_key(stem: str):
    """Text BEFORE the first numbering token, or None if the stem has none.
    'Dawn Girl - 01 - Opening Credits' -> 'dawn girl'
    '03 - Beauty'                      -> ''
    'LNSR01-95 Larry Niven - ...'      -> 'lnsr'
    'The Whole Truth Part 2-001'       -> 'the whole truth'
    'Opening Credits'                  -> None (no number at all)"""
    stem = _normalize_stem(stem)
    sp = _cut_span(stem)
    if not sp:
        return None
    return re.sub(r"\s+", " ", stem[:sp[0]].replace("_", " ")).strip(" -_.,([").lower()


def group_audio_files(paths, has_sidecar: bool):
    """Split a folder's audio files into books. Returns list of (hint, [paths]).
    A folder with a metadata file is one book. Otherwise files are grouped by
    the text before their track/part number; chapter names after the number
    are ignored. Files with no number at all join the main group."""
    if has_sidecar or len(paths) == 1:
        return [(None, list(paths))]
    keyed = {}
    unnumbered = []
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        k = book_prefix_key(stem)
        if k is None:
            unnumbered.append(p)
        else:
            keyed.setdefault(k, []).append(p)
    def own_book(p, against):
        """A loose file that is clearly a DIFFERENT complete book: a whole-book
        format (m4b/m4a), or a name that states its own author AND title, where
        that title isn't the group's. 'Bonus.mp3' / 'Epilogue.mp3' are not."""
        stem = _normalize_stem(os.path.splitext(os.path.basename(p))[0])
        if os.path.splitext(p)[1].lower() in (".m4b", ".m4a"):
            return True
        pn = parse_name(stem)
        if not (pn.get("author") and pn.get("title")):
            return False
        want = re.sub(r"[^a-z0-9]+", " ", (against or "").lower()).strip()
        have = re.sub(r"[^a-z0-9]+", " ", f"{pn['author']} {pn['title']}".lower()).strip()
        return not (want and (want in have or have in want or similar(want, have) > 0.8))

    if not keyed:
        # nothing numbered: one book - unless the files are clearly separate books
        # ("Author A - Book.m4b", "Author B - Other Book.mp3" loose in one folder)
        if len(paths) > 1 and all(own_book(p, None) for p in paths):
            return [(group_hint([p]), [p]) for p in sorted(paths)]
        return [(None, list(paths))]
    big = max(keyed, key=lambda k: len(keyed[k]))
    # singleton mp3-type groups (a stray "Bonus 1.mp3") join the biggest group;
    # whole-book formats and clearly separate books stay on their own
    for k in list(keyed):
        if k != big and len(keyed[k]) == 1 and not own_book(keyed[k][0], big):
            keyed[big] += keyed.pop(k)
    for p in unnumbered:
        if own_book(p, big):
            keyed[os.path.basename(p).lower()] = [p]
        else:
            keyed[big].append(p)
    out = []
    for k, files in sorted(keyed.items()):
        out.append((group_hint(files), files))
    return out


def group_hint(files):
    """Best title hint for a group of part files: the text before the track
    number ('Daniel Silva - The English Girl 09-49' -> 'Daniel Silva - The
    English Girl'); if that is empty, the text AFTER the number when it is the
    same on every file ('028 - Prodigal_Son' -> 'Prodigal Son')."""
    stems = [_normalize_stem(os.path.splitext(os.path.basename(f))[0]) for f in files]
    if len(files) == 1:
        # a lone file's whole name is its title ("Fahrenheit 451", "1984")
        return split_disc_marker(stems[0])[0].replace("_", " ").strip(" -_.,([") or None
    sp = _cut_span(stems[0])
    if not sp:
        return None
    pre = stems[0][:sp[0]].replace("_", " ").strip(" -_.,([")
    pre = split_disc_marker(pre)[0] if pre else pre      # "The Whole Truth Part 2" -> "The Whole Truth"
    if pre:
        return pre
    suffixes = set()
    for st in stems:
        ss = _cut_span(st)
        suffixes.add((st[ss[1]:] if ss else st).replace("_", " ").strip(" -_.,)]").lower())
    if len(suffixes) == 1 and len(files) > 1:
        suf = next(iter(suffixes))
        if suf and not re.fullmatch(r"(?:chapter|ch|track|part)?\s*\d*", suf):
            ss = _cut_span(stems[0])
            out = stems[0][ss[1]:].replace("_", " ").strip(" -_.,)]")
            out = re.sub(r"^\d{1,3}\s*[-–]\s*", "", out)                 # "617 - " total count
            out = re.sub(r"[\s._\-]*(?<!\d)\d{1,2}$", "", out).strip(" -_.,)]")
            return out or None
    return None


def scan_library(source: str, include_audio: bool, include_ebooks: bool, log):
    """Yield Book objects found under `source`."""
    def _walk_err(err):
        log(f"  cannot read: {getattr(err, 'filename', err)} ({err.strerror or err})")

    # Sibling disc folders ("Title - Disc 1", "Title - Disc 2") are merged here
    # and yielded at the end: key -> {"files","extras","dirpath","rel","base"}
    sibling_groups = {}

    for dirpath, dirnames, filenames in os.walk(source, onerror=_walk_err):
        dirnames.sort(key=natural_key)
        filenames.sort(key=natural_key)
        rel = os.path.relpath(dirpath, source)
        folder_name = os.path.basename(dirpath)

        # ---- 1. Nested disc subfolders: absorb "Disc N" children into this book ----
        absorbed_audio, absorbed_extras = [], []
        if include_audio:
            keep = []
            folder_cmp = re.sub(r"^(?:19|20)\d{2}\s*[-._]\s*", "", folder_name)
            folder_cmp = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", folder_cmp).strip().lower()
            for d in dirnames:
                dn = disc_child_number(d)
                if dn is None:
                    base, dn2 = split_disc_marker(d)
                    if dn2 is None:
                        mm = re.match(r"^(.*?)[\s._\-]+(\d{1,3})$", d)   # "The Last Juror 07"
                        if not mm:                                          # "Title (6of17)"
                            mm = re.match(r"^(.*?)[\s._\-]*[\(\[]?\s*(\d{1,3})\s*of\s*\d{1,3}\s*[\)\]]?$", d, re.I)
                        if mm:
                            base, dn2 = mm.group(1), int(mm.group(2))
                    if dn2 is not None and (similar(base.lower(), folder_cmp) > 0.8
                                            or similar(base, folder_name) > 0.8):
                        dn = dn2
                if dn is not None:
                    sub = os.path.join(dirpath, d)
                    try:
                        direct = any(os.path.splitext(f)[1].lower() in AUDIO_EXTS
                                     for f in os.listdir(sub))
                    except OSError:
                        direct = False
                    is_root_child = os.path.normpath(os.path.dirname(dirpath)) == os.path.normpath(source)
                    parent_has_audio = any(os.path.splitext(f)[1].lower() in AUDIO_EXTS for f in filenames)
                    if not direct or (is_root_child and not parent_has_audio and dn is not None
                                      and disc_child_number(d) is not None and " " not in d.strip()):
                        keep.append(d)      # "01/" holding a book folder, or "Side 1" under an author
                        continue
                    a, e = _collect_audio_under(sub)
                    if a:
                        absorbed_audio += a
                        absorbed_extras += e
                        continue
                keep.append(d)
            dirnames[:] = keep     # prune absorbed folders from the walk

        own_audio = [os.path.join(dirpath, f) for f in filenames
                     if os.path.splitext(f)[1].lower() in AUDIO_EXTS]
        extras = [os.path.join(dirpath, f) for f in filenames
                  if os.path.splitext(f)[1].lower() in EXTRA_EXTS]
        ebooks = [f for f in filenames if os.path.splitext(f)[1].lower() in EBOOK_EXTS]

        # ---- 2. Audio in this folder ----
        if include_audio and (own_audio or absorbed_audio):
            base, dn = split_disc_marker(folder_name)
            if dn is not None and base and not absorbed_audio:
                # This folder itself is "Title - Disc N": buffer with its siblings
                key = (os.path.dirname(dirpath).lower(), base.lower())
                g = sibling_groups.setdefault(key, {"files": [], "extras": [], "dirpath": dirpath,
                                                    "rel": os.path.dirname(rel) if rel != "." else ".",
                                                    "base": base})
                g["files"] += own_audio
                g["extras"] += extras
            else:
                all_audio = own_audio + absorbed_audio
                if absorbed_audio:
                    # one book spanning disc subfolders
                    all_audio.sort(key=_disc_sort_key)
                    yield _make_audio_book(dirpath, all_audio, extras + absorbed_extras,
                                           rel, log, source)
                else:
                    # group files into books: numbered chapters = one book
                    has_side = any(f.lower() in ("metadata.json", "metadata.opf") or
                                   f.lower().endswith(".opf") for f in filenames)
                    groups = group_audio_files(own_audio, has_side)
                    for hint, files in groups:
                        files.sort(key=_disc_sort_key)
                        if hint is None and len(files) > 1:
                            hint = group_hint(files)
                        useful = bool(hint) and (len(groups) > 1 or bool(parse_name(hint).get("author")))
                        yield _make_audio_book(dirpath, files, extras if len(groups) == 1 else [],
                                               rel, log, source, name_hint=hint if useful else None)

        # ---- 3. Ebooks: each file is a book (same-named formats grouped) ----
        if include_ebooks and ebooks:
            egroups = {}
            for f in ebooks:
                egroups.setdefault(os.path.splitext(f)[0].lower(), []).append(f)
            for _, fl in sorted(egroups.items()):
                yield _make_ebook(dirpath, fl, rel, log)

    # ---- 4. Flush sibling-disc groups as single books ----
    for g in sibling_groups.values():
        files = sorted(g["files"], key=_disc_sort_key)
        parent = os.path.dirname(g["dirpath"])
        yield _make_audio_book(parent, files, g["extras"], g["rel"], log, source,
                               name_hint=g["base"], src_label=f"{g['base']} ({len(files)} parts across disc folders)")


def _make_audio_book(dirpath, audio_paths, extras, rel, log, source_root="",
                     name_hint=None, src_label=None) -> Book:
    """audio_paths are full paths, already in playback order."""
    b = Book(kind="audio", files=list(audio_paths), extras=list(extras),
             src_display=src_label or (rel if rel != "." else os.path.basename(dirpath)))
    # Priority: sidecar metadata file > embedded tags > folders > name parsing
    try:
        listing = os.listdir(dirpath)
    except OSError:
        listing = []
    side, side_src = read_sidecar_metadata(dirpath, listing)
    if side:
        _merge_meta(b, side, side_src)
    meta = read_audio_metadata(b.files[0], multi=len(b.files) > 1)
    path_meta = infer_from_path(rel)
    if TRUST_FOLDERS:
        _merge_meta(b, path_meta, "folders")
        if meta and not DISTRUST_TAGS:
            _merge_meta(b, meta, "tags")
    else:
        if meta and not DISTRUST_TAGS:
            _merge_meta(b, meta, "tags")
        _merge_meta(b, path_meta, "folders")
    _note_conflict(b, meta, path_meta, side)
    folder_name = _strip_calibre_id(os.path.basename(dirpath))
    folder_name, _ = split_disc_marker(folder_name)
    folder_is_junk = _is_junk_folder(folder_name) or \
        os.path.normpath(dirpath) == os.path.normpath(source_root or "")
    if folder_is_junk and re.fullmatch(r"\d{1,4}", folder_name.strip()) and source_root \
            and os.path.normpath(os.path.dirname(dirpath)) != os.path.normpath(source_root) \
            and _looks_like_author(os.path.basename(os.path.dirname(dirpath))):
        folder_is_junk = False          # "George Orwell/1984/" - a title, not a year folder
    if name_hint:
        _merge_meta(b, _fix_ambiguous_order(parse_name(name_hint), b.author), "filename")
    if not folder_is_junk:
        _merge_meta(b, _fix_ambiguous_order(parse_name(folder_name), b.author), "filename")
    if len(b.files) == 1:
        stem, _ = split_disc_marker(os.path.splitext(os.path.basename(b.files[0]))[0])
        _merge_meta(b, _fix_ambiguous_order(parse_name(stem), b.author), "filename")
    parent_dir = os.path.dirname(dirpath)
    parent = os.path.basename(parent_dir)
    inside_root = source_root and os.path.normpath(parent_dir) != os.path.normpath(source_root) \
        and os.path.normpath(parent_dir).startswith(os.path.normpath(source_root))
    if not b.author and inside_root and _looks_like_author(parent) \
            and not SERIESY_WORDS.search(parent):
        b.author = parent
    _late_sources(b, dirpath, meta)
    if not b.title:
        b.title = name_hint or (os.path.splitext(os.path.basename(b.files[0]))[0]
                                if folder_is_junk else folder_name)
    if b.series and b.title and not folder_is_junk:
        # "Vince Flynn - Transfer of Power - Rapp 01": the folder says which one is the title
        fc = clean_text(re.sub(r"^(?:19|20)\d{2}\s*[-._]\s*", "", folder_name), b.author)[0].lower()
        if fc and similar(fc, b.series.lower()) > 0.85 and similar(fc, b.title.lower()) < 0.6:
            b.title, b.series = b.series, b.title
    b.title = title_case_if_shouty(b.title)
    b.author = title_case_if_shouty(clean_author(b.author))
    _finish_book(b, os.path.basename(dirpath) + " " + (name_hint or ""))
    return b


def _finish_book(b: Book, folder_text: str = ""):
    """Strip narrator / noise parentheticals from title and series. The
    narrator is also mined from the source folder name when the title came
    from a sidecar that didn't carry it."""
    b.title = re.sub(r"\.(?:doc|docx|txt|rtf|lit|htm|html|epub|mobi|azw3?|pdf)$", "", b.title, flags=re.I)
    if b.series:
        words = [w for w in b.series.split() if w.lower().strip(".,") not in JUNK_WORDS]
        cleaned = " ".join(words)
        if not cleaned or similar(cleaned.lower(), (b.author or "").lower()) > 0.9:
            b.series = ""          # "Anne Rice ebooks", "Novels": not a series
    if b.series and b.title:
        # "If I Had a Nickel: Roy Ballard Mysteries, Book 3" -> "If I Had a Nickel"
        b.title = re.sub(r"\s*[:\-,(]\s*(?:the\s+)?" + re.escape(b.series) +
                         r"[^,:()]*?(?:,?\s*(?:book|bk|vol(?:ume)?|#)\s*\d+)?\s*\)?\s*$",
                         "", b.title, flags=re.I).strip() or b.title
    if b.series:
        # generic "...: Some Series, Book 5" subtitle echo (even when the spelling differs)
        b.title = re.sub(r"\s*[:\-]\s*[^:]*?\b(?:book|bk|vol(?:ume)?)\s*\d+\s*$", "", b.title, flags=re.I).strip() or b.title
    b.title, narr = clean_text(b.title, b.author, b.series)
    if narr and not b.narrator:
        b.narrator = narr
    if b.series:
        b.series, narr2 = clean_text(b.series, b.author)
        if narr2 and not b.narrator:
            b.narrator = narr2
    if b.series and similar(b.series, b.title) > 0.9:
        b.series = ""   # series identical to the title is not a series
    if not b.narrator and folder_text:
        m = NARRATOR_PAREN.search(folder_text)
        if m:
            b.narrator = m.group("who").strip()
    # Box sets: "Star Force Box Set (5-8)" on the folder but a generic
    # "Star Force Box Set" title from the sidecar -> keep the range.
    if folder_text and b.title and not re.search(r"\d+\s*[-\u2013]\s*\d+", b.title):
        m = re.search(r"[\(\[]\s*(?:books?|vols?\.?|volumes?)?\s*(\d{1,3}\s*[-\u2013]\s*\d{1,3})\s*[\)\]]"
                      r"|\b(?:books?|vols?\.?|volumes?)\s+(\d{1,3}\s*[-\u2013]\s*\d{1,3})\b",
                      folder_text, re.I)
        if m:
            rng = re.sub(r"\s+", "", (m.group(1) or m.group(2)).replace("\u2013", "-"))
            lo, hi = rng.split("-")
            if int(lo) < int(hi) <= 200:       # a real range, not a year or a date
                b.title = f"{b.title} ({rng})"
                if b.series and not b.series_index:
                    b.series_index = rng     # Audiobookshelf sorts "5-8" among the singles
    if not b.title:
        b.title = "Untitled"


def _make_ebook(dirpath, files, rel, log) -> Book:
    b = Book(kind="ebook",
             files=[os.path.join(dirpath, f) for f in sorted(files)],
             src_display=os.path.join(rel, files[0]) if rel != "." else files[0])
    side, side_src = read_sidecar_metadata(dirpath, os.listdir(dirpath))
    if side:
        _merge_meta(b, side, side_src)
    emb = {}
    for f in b.files:
        ext = os.path.splitext(f)[1].lower()
        if ext == ".epub":
            emb = read_epub_metadata(f) or emb
        elif ext in (".mobi", ".azw", ".azw3"):
            emb = read_mobi_metadata(f) or emb
    path_meta = infer_from_path(rel)
    if TRUST_FOLDERS:
        _merge_meta(b, path_meta, "folders")
        if not DISTRUST_TAGS:
            _merge_meta(b, emb, "tags")
    else:
        if not DISTRUST_TAGS:
            _merge_meta(b, emb, "tags")
        _merge_meta(b, path_meta, "folders")
    _note_conflict(b, emb, path_meta, side)
    _merge_meta(b, _fix_ambiguous_order(parse_name(os.path.basename(b.files[0])), b.author), "filename")
    dname = _strip_calibre_id(os.path.basename(dirpath), any_digits=True)
    if not _is_junk_folder(dname):
        _merge_meta(b, _fix_ambiguous_order(parse_name(dname), b.author), "filename")
    _late_sources(b, dirpath, emb)
    if not b.title:
        b.title = os.path.splitext(os.path.basename(b.files[0]))[0]
    b.title = title_case_if_shouty(b.title)
    b.author = title_case_if_shouty(clean_author(b.author))
    _finish_book(b, os.path.basename(dirpath))
    return b


# ----------------------------------------------------------------------------
# Copying
# ----------------------------------------------------------------------------

def write_metadata_opf(book: Book, folder: str, cover_name: str = ""):
    """Write a Calibre/Audiobookshelf-readable metadata.opf into the book folder.
    The reviewed data is the truth, so an existing OPF in the COPY is replaced."""
    from xml.sax.saxutils import escape
    q = {'"': "&quot;"}
    g = lambda k: (getattr(book, k, "") or "").strip()
    lines = ["    <dc:title>" + escape(book.title) + "</dc:title>"]
    for a in [x.strip() for x in re.split(r"\s*[;&]\s*", book.author) if x.strip()] or [book.author]:
        lines.append('    <dc:creator opf:role="aut">' + escape(a) + "</dc:creator>")
    for n in [x.strip() for x in g("narrator").split(",") if x.strip()]:
        lines.append('    <dc:contributor opf:role="nrt">' + escape(n) + "</dc:contributor>")
    if g("isbn"):
        lines.append('    <dc:identifier opf:scheme="ISBN">' + escape(g("isbn")) + "</dc:identifier>")
    if g("asin"):
        lines.append('    <dc:identifier opf:scheme="ASIN">' + escape(g("asin")) + "</dc:identifier>")
    if g("year"):
        lines.append("    <dc:date>" + escape(g("year")) + "</dc:date>")
    if g("publisher"):
        lines.append("    <dc:publisher>" + escape(g("publisher")) + "</dc:publisher>")
    if g("language"):
        lines.append("    <dc:language>" + escape(g("language")) + "</dc:language>")
    for gen in [x.strip() for x in g("genres").split(",") if x.strip()][:6]:
        lines.append("    <dc:subject>" + escape(gen) + "</dc:subject>")
    if g("description"):
        lines.append("    <dc:description>" + escape(g("description")) + "</dc:description>")
    if book.series:
        lines.append('    <meta name="calibre:series" content="' + escape(book.series, q) + '"/>')
        idx = fmt_series_index(book.series_index)
        if idx:
            lines.append('    <meta name="calibre:series_index" content="'
                         + escape(idx.lstrip("0") or "0", q) + '"/>')
    guide = ('  <guide>\n    <reference type="cover" title="Cover" href="' + escape(cover_name, q)
             + '"/>\n  </guide>\n') if cover_name else ""
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uuid_id">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n'
           + "\n".join(lines) + "\n"
           '  </metadata>\n' + guide +
           '</package>\n')
    with open(os.path.join(folder, "metadata.opf"), "w", encoding="utf-8") as fh:
        fh.write(xml)


def write_metadata_json(book: Book, folder: str, log=None):
    """Write an Audiobookshelf-style metadata.json from the reviewed data."""
    path = os.path.join(folder, "metadata.json")
    idx = fmt_series_index(book.series_index)
    data = {
        "title": book.title,
        "subtitle": None,
        "authors": [book.author] if book.author else [],
        "narrators": [n.strip() for n in (getattr(book, "narrator", "") or "").split(",") if n.strip()],
        "series": ([f"{book.series} #{idx.lstrip('0') or '0'}" if idx else book.series]
                   if book.series else []),
        "genres": [x.strip() for x in (getattr(book, "genres", "") or "").split(",") if x.strip()],
        "tags": [], "chapters": [],
        "publishedYear": getattr(book, "year", "") or None,
        "publisher": getattr(book, "publisher", "") or None,
        "description": getattr(book, "description", "") or None,
        "isbn": getattr(book, "isbn", "") or None, "asin": getattr(book, "asin", "") or None,
        "language": getattr(book, "language", "") or None, "explicit": False,
        "abridged": False,
    }
    # keep fields we don't manage from an existing file (description, genres...)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            for k in ("genres", "tags", "chapters", "publishedYear", "publisher",
                      "description", "isbn", "asin", "language", "explicit", "abridged", "subtitle"):
                if old.get(k) not in (None, [], "", False) and data.get(k) in (None, [], "", False):
                    data[k] = old[k]
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _sidecar_agrees(meta: dict, book: Book) -> bool:
    return (similar(meta.get("title", ""), book.title) > 0.95
            and similar(clean_author(meta.get("author", "")), book.author) > 0.95
            and similar(meta.get("series", ""), book.series or "") > 0.95)


def embed_tags(book: Book, audio_paths, log=None):
    """Write the reviewed metadata into the COPIED audio files' tags (mutagen).
    Sets the fields Audiobookshelf reads: album=title, artist/albumartist=author,
    composer=narrator, series + series-part, and track numbers in playback order."""
    if not HAVE_MUTAGEN:
        if log:
            log("  (mutagen missing - embedded tags not written)")
        return 0
    from mutagen.mp4 import MP4, MP4FreeForm
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TPE1, TPE2, TCOM, TIT2, TRCK, TXXX
    try:
        from mutagen.id3 import MVNM, MVIN
        HAVE_MV = True
    except ImportError:
        HAVE_MV = False
    idx = fmt_series_index(book.series_index)
    idx_plain = (idx.lstrip("0") or "0") if idx else ""
    total = len(audio_paths)
    single = total == 1
    done = 0
    for n, path in enumerate(audio_paths, 1):
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".m4b", ".m4a", ".aac"):
                f = MP4(path)
                t = f.tags if f.tags is not None else f.add_tags() or f.tags
                t["\xa9alb"] = [book.title]
                t["\xa9ART"] = [book.author]
                t["aART"] = [book.author]
                if single:
                    t["\xa9nam"] = [book.title]
                if getattr(book, "narrator", ""):
                    t["\xa9wrt"] = [book.narrator]
                if book.series:
                    t["\xa9mvn"] = [book.series]
                    t["----:com.apple.iTunes:SERIES"] = [MP4FreeForm(book.series.encode("utf-8"))]
                    if idx_plain:
                        try:
                            t["\xa9mvi"] = [int(float(idx_plain))]
                        except ValueError:
                            pass
                        t["----:com.apple.iTunes:SERIES-PART"] = [MP4FreeForm(idx_plain.encode("utf-8"))]
                t["trkn"] = [(n, total)]
                if getattr(book, "year", ""):
                    t["\xa9day"] = [book.year]
                if getattr(book, "genres", ""):
                    t["\xa9gen"] = [book.genres.split(",")[0].strip()]
                if getattr(book, "description", ""):
                    t["desc"] = [book.description[:255]]
                    t["ldes"] = [book.description]
                if getattr(book, "narrator", ""):
                    t["\xa9nrt"] = [book.narrator]
                for key, val in (("ASIN", getattr(book, "asin", "")), ("ISBN", getattr(book, "isbn", ""))):
                    if val:
                        t["----:com.apple.iTunes:" + key] = [MP4FreeForm(val.encode("utf-8"))]
                f.save()
            elif ext == ".mp3":
                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.delall("TALB"); tags.add(TALB(encoding=3, text=book.title))
                tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=book.author))
                tags.delall("TPE2"); tags.add(TPE2(encoding=3, text=book.author))
                if single:
                    tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=book.title))
                if getattr(book, "narrator", ""):
                    tags.delall("TCOM"); tags.add(TCOM(encoding=3, text=book.narrator))
                tags.delall("TIT1")   # grouping: usually chapter junk - clear it
                for fr in list(tags.getall("TXXX")):
                    if fr.desc.lower() in ("series", "series-part"):
                        tags.delall("TXXX:" + fr.desc)
                if book.series:
                    tags.add(TXXX(encoding=3, desc="SERIES", text=book.series))
                    if HAVE_MV:
                        tags.delall("MVNM"); tags.add(MVNM(encoding=3, text=book.series))
                    if idx_plain:
                        tags.add(TXXX(encoding=3, desc="SERIES-PART", text=idx_plain))
                        if HAVE_MV:
                            tags.delall("MVIN"); tags.add(MVIN(encoding=3, text=idx_plain))
                tags.delall("TRCK"); tags.add(TRCK(encoding=3, text=f"{n}/{total}"))
                from mutagen.id3 import TDRC, TCON, COMM
                if getattr(book, "year", ""):
                    tags.delall("TDRC"); tags.add(TDRC(encoding=3, text=book.year))
                if getattr(book, "genres", ""):
                    tags.delall("TCON"); tags.add(TCON(encoding=3, text=book.genres.split(",")[0].strip()))
                if getattr(book, "description", ""):
                    tags.delall("COMM"); tags.add(COMM(encoding=3, lang="eng", desc="", text=book.description))
                for key, val in (("ASIN", getattr(book, "asin", "")), ("ISBN", getattr(book, "isbn", ""))):
                    if val:
                        tags.delall("TXXX:" + key); tags.add(TXXX(encoding=3, desc=key, text=val))
                tags.save(path, v2_version=3 if not HAVE_MV else 4)
            elif ext in (".flac", ".ogg", ".opus"):
                f = mutagen.File(path)
                if f is None:
                    continue
                if f.tags is None:
                    f.add_tags()
                f["album"] = book.title
                f["artist"] = book.author
                f["albumartist"] = book.author
                if single:
                    f["title"] = book.title
                if getattr(book, "narrator", ""):
                    f["composer"] = book.narrator
                if book.series:
                    f["series"] = book.series
                    if idx_plain:
                        f["series-part"] = idx_plain
                f["tracknumber"] = f"{n}/{total}"
                for key, attr in (("date", "year"), ("genre", "genres"), ("description", "description"),
                                  ("asin", "asin"), ("isbn", "isbn")):
                    if getattr(book, attr, ""):
                        f[key] = getattr(book, attr)
                f.save()
            else:
                continue
            done += 1
        except Exception as e:
            if log:
                log(f"  could not tag {os.path.basename(path)}: {e}")
    return done


def rewrite_epub_metadata(path: str, book: Book, log=None) -> bool:
    """Rewrite title/author/series inside a COPIED epub's OPF."""
    try:
        with zipfile.ZipFile(path) as z:
            cns = "{urn:oasis:names:tc:opendocument:xmlns:container}"
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(f".//{cns}rootfile").get("full-path")
            items = {i.filename: z.read(i.filename) for i in z.infolist()}
            infos = z.infolist()
        opf = ET.fromstring(items[opf_path])
        DC = "http://purl.org/dc/elements/1.1/"; OPF = "http://www.idpf.org/2007/opf"
        ET.register_namespace("dc", DC); ET.register_namespace("opf", OPF)
        md = opf.find(f"{{{OPF}}}metadata")
        if md is None:
            return False
        def set_dc(tag, value):
            el = md.find(f"{{{DC}}}{tag}")
            if el is None:
                el = ET.SubElement(md, f"{{{DC}}}{tag}")
            el.text = value
        set_dc("title", book.title)
        set_dc("creator", book.author)
        for tag, attr in (("date", "year"), ("publisher", "publisher"), ("description", "description"),
                          ("language", "language")):
            val = getattr(book, attr, "")
            el = md.find(f"{{{DC}}}{tag}")
            if val and (el is None or not (el.text or "").strip()):
                set_dc(tag, val)
        for m in list(md.findall(f"{{{OPF}}}meta")):
            if m.get("name") in ("calibre:series", "calibre:series_index"):
                md.remove(m)
        if book.series:
            ET.SubElement(md, f"{{{OPF}}}meta", name="calibre:series", content=book.series)
            idx = fmt_series_index(book.series_index)
            if idx:
                ET.SubElement(md, f"{{{OPF}}}meta", name="calibre:series_index",
                              content=idx.lstrip("0") or "0")
        items[opf_path] = ET.tostring(opf, encoding="utf-8", xml_declaration=True)
        tmp = path + ".tmp"
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                comp = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
                zout.writestr(info.filename, items[info.filename], compress_type=comp)
        os.replace(tmp, path)
        return True
    except Exception as e:
        if log:
            log(f"  could not rewrite epub metadata in {os.path.basename(path)}: {e}")
        return False


class BookExists(Exception):
    """The destination already has a different copy of this book and the
    conflict policy said to leave it alone."""


OS_JUNK = {"thumbs.db", "desktop.ini", ".ds_store", "@eadir", ".@__thumb", ".@__desc"}


def _same_content(a: str, b: str) -> bool:
    """Same size and same first/last 64 KB (the content fingerprint)."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        import enrich
        return enrich.file_fingerprint(a) == enrich.file_fingerprint(b)
    except OSError:
        return False


def _same_drive(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _transfer(src, target, move, done_ops, delete_after):
    """Copy, or move (rename on the same drive; copy+verify, delete later, across drives)."""
    if move:
        if _same_drive(src, os.path.dirname(target)):
            os.rename(src, target)
            done_ops.append(("rename", src, target))
            return
    shutil.copy2(src, target)
    done_ops.append(("copy", src, target))
    if os.path.getsize(target) != os.path.getsize(src):
        raise IOError(f"verification failed - size mismatch after copying {os.path.basename(target)}")
    if move:
        delete_after.append(src)


def _rollback(done_ops, log):
    """Undo one book's partial transfer: renamed files go back, copies are removed."""
    for method, src, target in reversed(done_ops):
        try:
            if method == "rename":
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(target, src)
            elif method == "copy" and os.path.exists(target):
                os.remove(target)
            elif method == "set-aside":            # a replaced destination file: put it back
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(target, src)
        except OSError as e:
            log(f"  ROLLBACK PROBLEM: could not restore {src}: {e}")
    if done_ops:
        log(f"  rolled back {len(done_ops)} file operation(s) - the source is as it was")


def _plan_names(book, rename_parts, cover_src, move):
    """[(src, target_name, is_primary)] - the file names this book will get."""
    image_extras = {e for e in book.extras if os.path.splitext(e)[1].lower() in (".jpg", ".jpeg", ".png")}
    single_audio = book.kind == "audio" and len(book.files) == 1
    single_ebook = book.kind == "ebook"
    part_no = {src: i for i, src in enumerate(book.files, 1)}   # files are in playback order
    spans_discs = len({os.path.dirname(f) for f in book.files}) > 1
    used, out = set(), []
    for src in book.files + book.extras:
        ext = os.path.splitext(src)[1].lower()
        is_cover_file = cover_src and src in image_extras and \
            re.match(r"(?i)^(cover|folder|front)\.", os.path.basename(src))
        if is_cover_file and not move:
            continue          # replaced by the chosen cover.jpg
        if is_cover_file:
            name = "original-" + os.path.basename(src)      # moving: keep it, renamed
        elif (single_audio or single_ebook) and ext in AUDIO_EXTS | EBOOK_EXTS:
            name = sanitize(book.title) + ext
        elif rename_parts and src in part_no and ext in AUDIO_EXTS:
            name = f"{sanitize(book.title)} - {part_no[src]:02d}{ext}"
        else:
            name = os.path.basename(src)  # keep original part names
            if spans_discs and ext in AUDIO_EXTS:
                # parts from several disc folders: prefix ALL of them with the disc folder
                name = f"{sanitize(os.path.basename(os.path.dirname(src)))} - {name}"
            if name.lower() in used:
                stem, e = os.path.splitext(name)
                name = f"{stem} ({part_no.get(src, 0)}){e}"
        used.add(name.lower())
        out.append((src, name, src in part_no))
    return out


def _quality(paths) -> tuple:
    """Rough 'which copy is better': total size, then bitrate of the first file."""
    size, br = 0, 0
    for p in paths:
        try:
            size += os.path.getsize(p)
        except OSError:
            pass
    if paths and HAVE_MUTAGEN:
        try:
            f = mutagen.File(paths[0])
            br = int(getattr(getattr(f, "info", None), "bitrate", 0) or 0)
        except Exception:
            pass
    return (br, size)


def _free_folder(base: str) -> str:
    cand, k = base, 2
    while os.path.exists(cand):
        cand, k = f"{base} ({k})", k + 1
    return cand


def copy_book(book: Book, dest_root: str, log, write_opf: bool = False,
              layout: str = "nested_series", rename_parts: bool = False,
              folder_override: str = None, write_json: bool = False,
              embed: bool = False, write_cover: bool = False,
              embed_cover: bool = False, journal=None, move: bool = False,
              source_root: str = "", dir_in_use=None, conflict: str = "skip") -> str:
    """Copy (default) or MOVE one book into the clean structure.

    conflict - what to do when the destination already holds a DIFFERENT
    copy of this book's audio/ebook files:
      skip         leave this book where it is (raises BookExists)
      keep_better  keep whichever copy is better; if the destination wins,
                   leave this one where it is (raises BookExists)
      replace      set the old destination files aside in
                   <dest>/.replaced/<date>/... and put this copy in place
      keep_both    put this copy in a numbered folder, "Title (2)"
    Files that are IDENTICAL to what's already there never count as a
    conflict: a copy skips them, a move just removes the source file.

    Move is all-or-nothing per book: same drive = rename (instant, no extra
    space); other drive = copy, verify, delete afterwards. On any failure,
    everything already done for this book is undone. Leftover sidecars in a
    source folder no other book uses follow the book; emptied source folders
    are removed. Nothing but OS junk (Thumbs.db, desktop.ini) is deleted."""
    folder = folder_override or book.dest_folder(dest_root, layout)
    cover_src = getattr(book, "cover", "") if write_cover else ""
    plan = _plan_names(book, rename_parts, cover_src, move)

    # ---- does the destination already hold a DIFFERENT copy of this book? ----
    clashes = [(src, os.path.join(folder, name)) for src, name, primary in plan
               if primary and os.path.exists(os.path.join(folder, name))
               and not _same_content(src, os.path.join(folder, name))]
    if clashes:
        where = os.path.relpath(folder, dest_root)
        if conflict == "keep_both":
            folder = _free_folder(folder)
            log(f"  already in destination - keeping both: {os.path.relpath(folder, dest_root)}")
        elif conflict == "replace":
            pass                                   # set aside below, inside the rollback scope
        elif conflict == "keep_better":
            existing = [t for _, t in clashes]
            mine = [s for s, _ in clashes]
            if _quality(mine) <= _quality(existing):
                raise BookExists(f"destination already has an equal or better copy ({where})")
            log(f"  this copy is better than the one in {where} - replacing it")
            conflict = "replace"
        else:
            raise BookExists(f"a different copy is already at {where}")

    done_ops, delete_after = [], []
    copied_audio, copied_ebooks = [], []
    os.makedirs(folder, exist_ok=True)
    try:
        if clashes and conflict == "replace":
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            for _, t in clashes:
                aside = os.path.join(dest_root, ".replaced", stamp, os.path.relpath(t, dest_root))
                os.makedirs(os.path.dirname(aside), exist_ok=True)
                os.rename(t, aside)
                done_ops.append(("set-aside", t, aside))
                if journal:
                    journal("replaced", t, aside)
        for src, name, primary in plan:
            ext = os.path.splitext(src)[1].lower()
            target = os.path.join(folder, name)
            if os.path.exists(target):
                if _same_content(src, target):
                    log(f"  already there (identical): {name}")
                    if move:
                        delete_after.append(src)      # safely at the destination already
                    continue
                stem, e = os.path.splitext(name)      # a differing extra (nfo, image...)
                target = os.path.join(folder, f"{stem} (copy){e}")
            _transfer(src, target, move, done_ops, delete_after)
            if journal:
                journal("move" if move else "copy", src, target)
            if primary and ext in AUDIO_EXTS:
                copied_audio.append(target)
            elif ext == ".epub" and src in book.files:
                copied_ebooks.append(target)
    except Exception:
        _rollback(done_ops, log)
        raise
    if move:
        for src in delete_after:
            try:
                os.remove(src)
            except OSError as e:
                log(f"  moved, but could not delete the original {os.path.basename(src)}: {e}")
        _tidy_source_dirs(book, folder, source_root, dir_in_use, journal, log)
    return _finish_copy(book, folder, copied_audio, copied_ebooks, cover_src, write_opf,
                        write_json, embed, embed_cover, log)


SIDECAR_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".nfo", ".txt", ".opf", ".json",
                ".cue", ".pdf", ".m3u", ".m3u8", ".sfv", ".md5", ".log", ".url"}


def _tidy_source_dirs(book, folder, source_root, dir_in_use, journal, log):
    """After a move: sidecars left in a folder that no other book uses follow
    the book; then empty folders are removed, up to (never including) the
    source root."""
    if not source_root:
        return
    root = os.path.normpath(source_root)
    dirs = sorted({os.path.dirname(f) for f in book.files}, key=len, reverse=True)
    for d in dirs:
        if dir_in_use and dir_in_use(d):
            continue                      # other books still live here
        try:
            names = os.listdir(d)
        except OSError:
            continue
        rest = [n for n in names if n.lower() not in OS_JUNK]
        if rest and all(os.path.isfile(os.path.join(d, n)) and
                        os.path.splitext(n)[1].lower() in SIDECAR_EXTS for n in rest):
            for n in rest:
                src = os.path.join(d, n)
                target = os.path.join(folder, n)
                if os.path.exists(target):
                    if _same_content(src, target):
                        os.remove(src)
                        continue
                    target = os.path.join(folder, "original-" + n)
                    if os.path.exists(target):
                        continue          # leave it; never overwrite
                try:
                    shutil.move(src, target)
                    if journal:
                        journal("move", src, target)
                except OSError as e:
                    log(f"  could not move leftover {n}: {e}")
        # remove the folder (and emptied parents) if nothing but OS junk is left
        cur = os.path.normpath(d)
        while cur != root and cur.startswith(root + os.sep):
            try:
                left = os.listdir(cur)
            except OSError:
                break
            if any(n.lower() not in OS_JUNK for n in left):
                break
            try:
                for n in left:
                    p = os.path.join(cur, n)
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
                os.rmdir(cur)
            except OSError:
                break
            cur = os.path.dirname(cur)


def _finish_copy(book, folder, copied_audio, copied_ebooks, cover_src, write_opf,
                 write_json, embed, embed_cover, log):
    # ---- The chosen cover -> cover.jpg (+ embedded) ----
    cover_name = ""
    if cover_src and os.path.isfile(cover_src):
        try:
            import enrich
            jpeg = enrich.cover_jpeg(cover_src)
            cover_name = "cover.png" if jpeg[:8] == b"\x89PNG\r\n\x1a\n" else "cover.jpg"
            with open(os.path.join(folder, cover_name), "wb") as fh:
                fh.write(jpeg)
            if embed_cover and copied_audio:
                small = enrich.cover_jpeg(cover_src, max_side=1400 if len(copied_audio) == 1 else 800)
                n = enrich.embed_cover(copied_audio, small, log)
                if n:
                    log(f"  cover embedded in {n} file(s)")
        except Exception as e:
            log(f"  could not write cover: {e}")
    # ---- Sync the reviewed metadata everywhere ----
    if write_opf and (book.author or book.title):
        try:
            write_metadata_opf(book, folder, cover_name)
        except OSError as e:
            log(f"  could not write metadata.opf: {e}")
    if write_json and (book.author or book.title):
        try:
            write_metadata_json(book, folder, log)
        except OSError as e:
            log(f"  could not write metadata.json: {e}")
    if embed and (book.author or book.title):
        n = embed_tags(book, copied_audio, log)
        for e in copied_ebooks:
            if rewrite_epub_metadata(e, book, log):
                n += 1
        if n:
            log(f"  embedded tags written to {n} file(s)")
    return folder


