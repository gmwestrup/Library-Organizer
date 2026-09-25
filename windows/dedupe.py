#!/usr/bin/env python3
"""
Library Organizer - duplicate detection.

Combines the matching rules of ABS Duplicate Finder (title/author
normalisation, number guards, same-author-same-length) with the content
fingerprints and format grouping of calibre-library-cleaner, and runs them
on the plan BEFORE anything is copied - so duplicates never reach
Audiobookshelf or Calibre in the first place.

Match reasons, strongest first:
  Identical files          - same content fingerprint (renamed copies)
  Same ASIN / Same ISBN
  Same title and author    - after normalising accents, "The", "(Unabridged)",
                             initials, "Last, First"
  Same title, other subtitle
  Similar title            - fuzzy, same author, same numbers in the title
  Same author and length   - audiobooks within ~0.05% runtime (18 s / 10 h)

Guards: "Book 1" never pairs with "Book 2"; different series positions never
pair; audiobooks never pair with ebooks (they are different products and go
to different servers).

Each group gets a recommended keeper (best copy) - and for ebooks where the
copies are different FORMATS of one book (epub + mobi + pdf), the
recommendation is to MERGE them into one folder, which is what Calibre wants.
"""

import os
import re
import unicodedata
from collections import defaultdict

import core
from library_db import author_key

try:
    from rapidfuzz import fuzz as _fuzz

    def sim(a, b):
        return _fuzz.ratio(a, b)
except ImportError:
    def sim(a, b):
        return core.similar(a, b) * 100

REASONS = {"Identical files": 0, "Same ASIN": 1, "Same ISBN": 2, "Same title and author": 3,
           "Same title, other subtitle": 4, "Similar title": 5, "Same author and length": 6}

_JUNK_BRACKETS = re.compile(r"[\(\[][^\)\]]*(?:unabridged|abridged|audiobook|retail|mp3|m4b|"
                            r"kbps|epub|mobi|pdf|dramati[sz]ed|narrated|read by)[^\)\]]*[\)\]]", re.I)
_JUNK_WORDS = re.compile(r"\b(unabridged|abridged|audiobook|a novel|the novel|retail)\b", re.I)
NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
            "seven": "7", "eight": "8", "nine": "9", "ten": "10", "i": "1", "ii": "2",
            "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
            "first": "1", "second": "2", "third": "3"}
FORMAT_RANK = {".m4b": 0, ".m4a": 1, ".flac": 2, ".opus": 3, ".ogg": 4, ".mp3": 5,
               ".aac": 6, ".wma": 7,
               ".epub": 0, ".azw3": 1, ".azw": 2, ".mobi": 3, ".pdf": 4}


def ascii_lower(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def norm_title(t):
    t = ascii_lower(t).replace("&", " and ")
    t = _JUNK_BRACKETS.sub(" ", t)
    t = _JUNK_WORDS.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


def main_title(t):
    return norm_title(re.split(r"\s*:\s+|\s+[-\u2013\u2014]\s+", t or "", maxsplit=1)[0])


def numbers(nt):
    return frozenset(NUMWORDS.get(w, str(int(w)) if w.isdigit() else None)
                     for w in nt.split() if w.isdigit() or w in NUMWORDS) - {None}


def stats(b) -> dict:
    size = mtime = 0
    rank, exts = 9, set()
    for f in b.files:
        try:
            st = os.stat(f)
            size += st.st_size
            mtime = max(mtime, st.st_mtime)
        except OSError:
            pass
        e = os.path.splitext(f)[1].lower()
        exts.add(e)
        rank = min(rank, FORMAT_RANK.get(e, 9))
    return {"size": size, "mtime": mtime, "rank": rank, "nfiles": len(b.files), "exts": sorted(exts)}


def quality(b, st=None) -> float:
    """Best-copy score. Higher = keep this one."""
    st = st or stats(b)
    s = (9 - st["rank"]) * 10
    if b.kind == "audio":
        s += min((getattr(b, "bitrate", 0) or 0) / 1000, 256) / 8          # up to +32 for bitrate
        s += 8 if getattr(b, "chapters", 0) else 0                           # chapter markers
        s += 6 if st["nfiles"] == 1 else max(0, 4 - st["nfiles"] / 25)      # one file > 300 parts
        s += min(st["size"] / 1e9, 2) * 3
    else:
        s += min(st["size"] / 5e6, 3)
        names = " ".join(os.path.basename(f).lower() for f in b.files)
        s += 5 if "retail" in names else 0
        s -= 4 if re.search(r"z-?lib|libgen|scan|ocr", names) else 0
    s += 4 if getattr(b, "cover", "") else 0
    s += (getattr(b, "confidence", 0) or 0) / 20
    s -= 25 if getattr(b, "health", "") else 0
    return round(s, 2)


def find_groups(books: dict, dismissed: set = frozenset(), threshold: int = 90) -> list:
    """books: {id: Book}. Returns [{'key','ids','reasons','keep','merge_formats'}]."""
    recs = []
    for bid, b in books.items():
        if b.status == "skipped":
            continue          # skipped (including resolved duplicates) never re-forms a group
        if not b.title:
            continue
        nt = norm_title(b.title)
        ak = author_key(b.author.split(",")[0]) if b.author else ""
        recs.append({"id": bid, "b": b, "kind": b.kind, "nt": nt, "mt": main_title(b.title),
                     "nums": numbers(nt), "mnums": numbers(main_title(b.title)), "ak": ak,
                     "surname": ak.split()[-1] if ak else "",
                     "seq": core.fmt_series_index(b.series_index) if b.series else "",
                     "fp": getattr(b, "fingerprint", ""), "asin": (getattr(b, "asin", "") or "").upper(),
                     "isbn": getattr(b, "isbn", "") or "", "dur": getattr(b, "duration", 0) or 0})
    n = len(recs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []

    def link(i, j, reason, score=100):
        a, b = recs[i], recs[j]
        if a["kind"] != b["kind"]:
            return
        if a["seq"] and b["seq"] and a["seq"] != b["seq"] and reason != "Identical files":
            return
        edges.append((i, j, reason, int(score)))
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def bucket(keyfn, reason, max_bucket=50):
        buckets = defaultdict(list)
        for i, r in enumerate(recs):
            k = keyfn(r)
            if k:
                buckets[k].append(i)
        for ids in buckets.values():
            if 1 < len(ids) <= max_bucket:
                for x in range(len(ids)):
                    for y in range(x + 1, len(ids)):
                        link(ids[x], ids[y], reason)

    bucket(lambda r: r["fp"], "Identical files")
    bucket(lambda r: r["asin"] if len(r["asin"]) == 10 else "", "Same ASIN")
    bucket(lambda r: r["isbn"] if len(r["isbn"]) in (10, 13) else "", "Same ISBN")
    bucket(lambda r: (r["kind"], r["nt"], r["ak"]) if r["nt"] and r["ak"] else "", "Same title and author")

    blocks = defaultdict(list)
    for i, r in enumerate(recs):
        if r["surname"]:
            blocks[(r["kind"], r["surname"])].append(i)
    for ids in blocks.values():
        if len(ids) < 2 or len(ids) > 4000:
            continue
        for x in range(len(ids)):
            a = recs[ids[x]]
            for y in range(x + 1, len(ids)):
                b = recs[ids[y]]
                if a["nt"] == b["nt"]:
                    continue
                if a["ak"] != b["ak"] and sim(a["ak"], b["ak"]) < 85:
                    continue
                if len(a["mt"]) >= 4 and a["mt"] == b["mt"] and a["mnums"] == b["mnums"]:
                    link(ids[x], ids[y], "Same title, other subtitle")
                    continue
                if a["nums"] == b["nums"]:
                    s = sim(a["nt"], b["nt"])
                    if s >= threshold:
                        link(ids[x], ids[y], "Similar title", s)
        # same author + same running time (audio)
        by_author = defaultdict(list)
        for i in ids:
            if recs[i]["kind"] == "audio" and recs[i]["dur"] >= 1200:
                by_author[recs[i]["ak"]].append(i)
        for aids in by_author.values():
            aids.sort(key=lambda i: recs[i]["dur"])
            for x in range(len(aids)):
                da = recs[aids[x]]["dur"]
                tol = max(3.0, da * 0.0005)
                for y in range(x + 1, len(aids)):
                    if recs[aids[y]]["dur"] - da > tol:
                        break
                    link(aids[x], aids[y], "Same author and length")

    members, reasons = defaultdict(set), defaultdict(dict)
    for i, j, reason, score in edges:
        root = find(i)
        members[root].update((i, j))
        reasons[root][reason] = max(score, reasons[root].get(reason, 0))

    groups = []
    for root, idx in members.items():
        ids = sorted(recs[i]["id"] for i in idx)
        key = "|".join(map(str, sorted((getattr(recs[i]["b"], "fingerprint", "") or
                                        f"{recs[i]['nt']}@{recs[i]['ak']}") for i in idx)))
        if key in dismissed:
            continue
        rs = sorted(({"reason": k, "score": v} for k, v in reasons[root].items()),
                    key=lambda r: REASONS[r["reason"]])
        scored = []
        for i in idx:
            b = recs[i]["b"]
            st = stats(b)
            scored.append((quality(b, st), recs[i]["id"], st))
        scored.sort(reverse=True)
        keep = scored[0][1]
        # ebooks: different formats of one book -> merge into one folder
        merge = False
        if recs[next(iter(idx))]["kind"] == "ebook" and rs[0]["reason"] != "Identical files":
            ext_sets = [set(st["exts"]) for _, _, st in scored]
            union = set().union(*ext_sets)
            merge = sum(len(e) for e in ext_sets) == len(union) and len(union) > 1
        groups.append({"key": key, "ids": ids, "reasons": rs, "keep": keep,
                       "merge_formats": merge,
                       "quality": {bid: q for q, bid, _ in scored}})
    groups.sort(key=lambda g: (REASONS[g["reasons"][0]["reason"]], g["ids"][0]))
    return groups
