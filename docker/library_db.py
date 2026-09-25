#!/usr/bin/env python3
"""
Library Organizer - decisions database (SQLite).

Everything you decide survives a rescan, a restart, and a fresh plan:

  fingerprints  path+size+mtime -> content fingerprint (so 100k files are
                read once, not every scan)
  corrections   your edits, keyed by the BOOK's content fingerprint - move or
                rename the files and your fixes still find them
  aliases       approved author merges ("S. King" -> "Stephen King"),
                applied automatically on every future scan
  rejects       author pairs you said are different people
  not_dupes     duplicate groups you said are not duplicates
  cache         online-lookup and AI answers (never pay for the same call twice)
  journal       every copy/skip/quarantine, src -> dest (your undo map)

Adapted from calibre-library-cleaner's DecisionsDB.
"""

import json
import os
import re
import sqlite3
import threading
import time
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints(
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, fp TEXT);
CREATE TABLE IF NOT EXISTS corrections(
    fp TEXT PRIMARY KEY, fields TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS aliases(
    variant_key TEXT PRIMARY KEY, canonical TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS rejects(
    a TEXT, b TEXT, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS not_dupes(
    key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS cache(
    key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, action TEXT,
    src TEXT, dest TEXT, detail TEXT);
"""


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}


def author_key(name: str) -> str:
    """Aggressive canonical key: casefold, de-accent, 'Last, First' reorder,
    initials collapsed ('J. R. R.' == 'JRR'), punctuation dropped.
    (from calibre-author-cleanup)"""
    s = strip_diacritics(name).casefold().strip()
    if s.count(",") == 1:
        last, first = [p.strip() for p in s.split(",")]
        if last and first and first not in SUFFIXES:
            s = f"{first} {last}"
    s = re.sub(r"[.\-_'\u2019]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t]
    out, run = [], []
    for t in tokens:
        if len(t) == 1:
            run.append(t)
        else:
            if run:
                out.append("".join(run))
                run = []
            out.append(t)
    if run:
        out.append("".join(run))
    return " ".join(out)


class DecisionsDB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql, args=(), fetch=None, commit=False):
        with self._lock:
            cur = self.conn.execute(sql, args)
            out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
            if commit:
                self.conn.commit()
            return out

    # -- fingerprints --------------------------------------------------------
    def cached_fp(self, path: str, size: int, mtime: float):
        row = self._exec("SELECT size, mtime, fp FROM fingerprints WHERE path=?",
                         (path,), fetch="one")
        if row and row[0] == size and abs(row[1] - mtime) < 2:
            return row[2]
        return None

    def remember_fp(self, path: str, size: int, mtime: float, fp: str):
        self._exec("INSERT OR REPLACE INTO fingerprints VALUES(?,?,?,?)",
                   (path, size, mtime, fp), commit=True)

    # -- corrections ---------------------------------------------------------
    def correction(self, fp: str):
        if not fp:
            return None
        row = self._exec("SELECT fields FROM corrections WHERE fp=?", (fp,), fetch="one")
        return json.loads(row[0]) if row else None

    def save_correction(self, fp: str, fields: dict):
        """Merge `fields` into whatever was already corrected for this book."""
        if not fp or not fields:
            return
        cur = self.correction(fp) or {}
        cur.update(fields)
        self._exec("INSERT OR REPLACE INTO corrections VALUES(?,?,?)",
                   (fp, json.dumps(cur), time.time()), commit=True)

    def correction_count(self) -> int:
        return self._exec("SELECT COUNT(*) FROM corrections", fetch="one")[0]

    # -- author aliases ------------------------------------------------------
    def canonical_author(self, name: str):
        if not name:
            return None
        row = self._exec("SELECT canonical FROM aliases WHERE variant_key=?",
                         (author_key(name),), fetch="one")
        return row[0] if row else None

    def all_aliases(self) -> dict:
        return dict(self._exec("SELECT variant_key, canonical FROM aliases", fetch="all"))

    def add_alias(self, variant: str, canonical: str):
        self._exec("INSERT OR REPLACE INTO aliases VALUES(?,?,?)",
                   (author_key(variant), canonical.strip(), time.time()), commit=True)

    def reject_pair(self, a: str, b: str):
        ka, kb = sorted([author_key(a), author_key(b)])
        self._exec("INSERT OR IGNORE INTO rejects VALUES(?,?)", (ka, kb), commit=True)

    def rejected_pairs(self) -> set:
        return {tuple(r) for r in self._exec("SELECT a, b FROM rejects", fetch="all")}

    # -- duplicate dismissals -----------------------------------------------
    def dismiss_dupe(self, key: str):
        self._exec("INSERT OR REPLACE INTO not_dupes VALUES(?,?)", (key, time.time()), commit=True)

    def dismissed_dupes(self) -> set:
        return {r[0] for r in self._exec("SELECT key FROM not_dupes", fetch="all")}

    # -- cache ---------------------------------------------------------------
    def cache_get(self, key: str, max_age_days: float = 90):
        row = self._exec("SELECT value, ts FROM cache WHERE key=?", (key,), fetch="one")
        if row and time.time() - row[1] < max_age_days * 86400:
            try:
                return json.loads(row[0])
            except ValueError:
                return None
        return None

    def cache_put(self, key: str, value):
        self._exec("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                   (key, json.dumps(value), time.time()), commit=True)

    # -- journal -------------------------------------------------------------
    def journal(self, action: str, src: str = "", dest: str = "", detail: str = ""):
        self._exec("INSERT INTO journal(ts,action,src,dest,detail) VALUES(?,?,?,?,?)",
                   (time.time(), action, src, dest, detail), commit=True)

    def journal_rows(self, limit: int = 100000):
        return self._exec("SELECT ts, action, src, dest, detail FROM journal "
                          "ORDER BY id DESC LIMIT ?", (limit,), fetch="all")

    def stats(self) -> dict:
        q = lambda t: self._exec(f"SELECT COUNT(*) FROM {t}", fetch="one")[0]
        return {t: q(t) for t in ("fingerprints", "corrections", "aliases",
                                  "rejects", "not_dupes", "cache", "journal")}
