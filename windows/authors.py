#!/usr/bin/env python3
"""
Library Organizer - author canonicalization.

Ported from calibre-author-cleanup, but run on the plan before copying, so
"S. King", "King, Stephen", "Stephen  King" and "Stephen Kng" all land in
ONE author folder instead of four.

  high    - identical after normalisation (accents, initials, Last-First,
            spacing, casing). Safe; can be approved in bulk.
  medium  - fuzzy score >= threshold within the same initial+surname block
  low     - gray zone. "Robert Jordan" vs "Robert Jordan Jr." lands here on
            purpose: it can be two different people.
  glued   - "John Smith and Jane Doe" stored as one author

Approved merges become permanent aliases in the decisions DB and are applied
automatically on every future scan. Rejections are remembered too, so a
re-scan only ever shows you new questions.
"""

import re
from collections import defaultdict

from library_db import author_key, SUFFIXES

try:
    from rapidfuzz import fuzz

    def token_sort(a, b):
        return fuzz.token_sort_ratio(a, b)
except ImportError:
    from difflib import SequenceMatcher

    def token_sort(a, b):
        a, b = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
        return SequenceMatcher(None, a, b).ratio() * 100

GLUE = [r"\s+and\s+", r"\s*&\s*", r"\s*;\s*", r"\s+with\s+"]


def looks_glued(name: str) -> bool:
    return any(re.search(p, name, re.I) for p in GLUE)


def signature(name: str) -> str:
    toks = [t for t in author_key(name).split() if t not in SUFFIXES]
    return f"{toks[0][:1]}|{toks[-1][:4]}" if toks else ""


def pick_canonical(variants, counts, trust=None):
    """Most books wins; on a tie, the spelling used by the better-confirmed
    books (tags / online matches agree) - so one 'Pierce Browne' folder can't
    out-vote 'Pierce Brown'. Then no commas, clean spacing, full words over
    bare initials, proper capitals, accents kept."""
    trust = trust or {}

    def score(v):
        return (counts.get(v, 0), trust.get(v, 0), "," not in v, "  " not in v,
                len(author_key(v).split()), sum(1 for t in v.split() if t[:1].isupper()),
                sum(1 for c in v if ord(c) > 127), min(v.count("."), 4), len(v), v)
    return sorted(variants, key=score, reverse=True)[0]


def cluster(counts: dict, rejected: set = frozenset(), threshold: int = 90, gray: int = 82,
            trust: dict = None) -> list:
    """counts: {author display name: number of books}.
    Returns [{'confidence','variants':[(name,count)],'canonical'}]."""
    by_key = defaultdict(list)
    for n in counts:
        if n.strip():
            by_key[author_key(n)].append(n)
    groups, reps = [], {}
    for k, vs in by_key.items():
        if len(vs) > 1:
            groups.append({"confidence": "high", "names": vs})
        reps[k] = pick_canonical(vs, counts, trust)

    buckets = defaultdict(list)
    for k, rep in reps.items():
        buckets[signature(rep)].append(k)
    owner, fuzzy = {}, []
    for keys in buckets.values():
        if len(keys) < 2 or len(keys) > 3000:
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                s = token_sort(a, b)
                if s < gray or tuple(sorted((a, b))) in rejected:
                    continue
                # "Robert Jordan" vs "Robert Jordan Jr" - never above low
                sa, sb = set(a.split()) & SUFFIXES, set(b.split()) & SUFFIXES
                conf = "low" if (sa != sb or s < threshold) else "medium"
                ga, gb = owner.get(a), owner.get(b)
                if ga is None and gb is None:
                    fuzzy.append({"keys": {a, b}, "confidence": conf})
                    owner[a] = owner[b] = len(fuzzy) - 1
                elif ga is not None and gb is None:
                    fuzzy[ga]["keys"].add(b); owner[b] = ga
                    if conf == "low":
                        fuzzy[ga]["confidence"] = "low"
                elif gb is not None and ga is None:
                    fuzzy[gb]["keys"].add(a); owner[a] = gb
                    if conf == "low":
                        fuzzy[gb]["confidence"] = "low"
    # a fuzzy group swallows the exact groups of its keys
    swallowed = set()
    for g in fuzzy:
        names = [n for k in g["keys"] for n in by_key[k]]
        swallowed.update(g["keys"])
        groups.append({"confidence": g["confidence"], "names": names})
    groups = [g for g in groups
              if not (g["confidence"] == "high" and author_key(g["names"][0]) in swallowed)]
    for n in counts:
        if looks_glued(n) and counts[n]:
            groups.append({"confidence": "glued", "names": [n]})
    out = []
    order = {"high": 0, "medium": 1, "low": 2, "glued": 3}
    for g in groups:
        names = sorted(set(g["names"]), key=lambda v: -counts.get(v, 0))
        out.append({"confidence": g["confidence"],
                    "variants": [(v, counts.get(v, 0)) for v in names],
                    "canonical": pick_canonical(names, counts, trust) if g["confidence"] != "glued"
                    else re.split("|".join(GLUE), names[0], flags=re.I)[0].strip(" .,&")})
    out.sort(key=lambda g: (order[g["confidence"]], -sum(c for _, c in g["variants"])))
    return out
