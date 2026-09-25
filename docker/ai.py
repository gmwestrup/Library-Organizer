#!/usr/bin/env python3
"""
Library Organizer - AI assist.

The AI is a *tie-breaker*, not the first resort. The scanner, tags, sidecars
and online catalogs settle most books for free; only books still below the
confidence threshold are sent, and every answer is cached against the book's
content fingerprint, so a rescan never pays twice.

What the model sees ("evidence"): the folder path, file names, every
metadata source that disagreed, the probed duration, online candidates with
their runtimes, the ebook's title/copyright-page text, and (optionally) a
transcript of the first 90 seconds of the audiobook - where the narrator
usually reads out the title, author and their own name.

Providers:
  anthropic  - Claude via api.anthropic.com (needs an API key)
  openai     - any OpenAI-compatible /v1/chat/completions endpoint:
               OpenAI, a local Ollama (http://HOST:11434/v1), LM Studio ...
               Local models keep everything on your network and cost nothing.
"""

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request

import core

DEFAULTS = {
    "provider": "anthropic",
    "api_key": "",
    "model": "claude-haiku-4-5-20251001",
    "base_url": "",                # openai-compatible only, e.g. http://192.168.1.10:11434/v1
    "threshold": 70,               # only books below this confidence are sent
    "auto_apply": 75,              # AI answers at/above this confidence are applied directly
    "max_books": 300,              # safety cap per run
    "transcribe": False,           # speech-to-text of audiobook intros (needs faster-whisper)
    "whisper_model": "base.en",
    "intro_seconds": 90,
    "audible_region": "com",
}

SYSTEM = """You identify books and audiobooks from messy file collections.
You get evidence gathered from one book's files. Work out the correct
metadata. Rules:
- Prefer hard evidence: ISBN/ASIN matches, an online candidate whose runtime
  matches the probed duration, the narrator's spoken intro, the title page.
- Folder and file names are often wrong, swapped (title<->author), or carry
  junk (bitrates, "Unabridged", release-group tags, part numbers).
- Author: the book's author(s) in "First Last" form; never the narrator,
  never a publisher, never a folder like "Audiobooks". Well-known narrators
  (Ray Porter, Scott Brick, R.C. Bray, Kate Reading, Michael Kramer...) are
  often wrongly filed as the author - check the evidence.
- Generic titles ("Home", "The Game", "Fallen", "Book One") match thousands
  of books. For those, name an author ONLY if the evidence independently
  supports it (tags, runtime, ISBN/ASIN, spoken intro); otherwise author=null.
  Returning null is better than inventing a plausible author.
- series_index only when the evidence states or clearly implies it.
- If you are not sure of a field, return null for it. Do not invent.
- confidence (0-100) is how sure you are that title AND author are right.
Reply with ONLY a JSON object, no prose, no code fences:
{"title": str|null, "author": str|null, "series": str|null,
 "series_index": str|null, "narrator": str|null, "year": str|null,
 "confidence": int, "reason": "one short sentence citing the deciding evidence"}"""


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

def load_settings(path: str) -> dict:
    s = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            s.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    # environment variables fill anything not set in the UI
    env = {"api_key": os.environ.get("AI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
           "provider": os.environ.get("AI_PROVIDER"), "model": os.environ.get("AI_MODEL"),
           "base_url": os.environ.get("AI_BASE_URL")}
    for k, v in env.items():
        if v and (not s.get(k) or s[k] == DEFAULTS[k]):
            s[k] = v
    return s


def save_settings(path: str, s: dict):
    clean = {k: s.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    os.replace(tmp, path)


def configured(s: dict) -> bool:
    if s.get("provider") == "openai":
        return bool(s.get("base_url") or s.get("api_key"))
    return bool(s.get("api_key"))


# ----------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------

def _post(url, headers, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError(f"HTTP {e.code} from AI provider: {detail}") from None


def complete(s: dict, system: str, user: str, max_tokens: int = 600) -> str:
    if s.get("provider") == "openai":
        base = (s.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if s.get("api_key"):
            headers["Authorization"] = f"Bearer {s['api_key']}"
        d = _post(base + "/chat/completions", headers, {
            "model": s.get("model") or "llama3.1", "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        return d["choices"][0]["message"]["content"]
    d = _post("https://api.anthropic.com/v1/messages",
              {"Content-Type": "application/json", "x-api-key": s.get("api_key", ""),
               "anthropic-version": "2023-06-01"},
              {"model": s.get("model") or DEFAULTS["model"], "max_tokens": max_tokens,
               "temperature": 0, "system": system,
               "messages": [{"role": "user", "content": user}]})
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def parse_json(text: str) -> dict:
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def test_connection(s: dict) -> str:
    reply = complete(s, "Reply with exactly: OK", "ping", max_tokens=5)
    return reply.strip()[:40]


# ----------------------------------------------------------------------------
# Speech-to-text of the audiobook intro (optional)
# ----------------------------------------------------------------------------

_WHISPER = {}


def have_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        import av  # noqa: F401
        return True
    except ImportError:
        return False


def _decode_head(path: str, seconds: int):
    """Decode only the first N seconds to 16 kHz mono float32 (PyAV) - never
    the whole 20-hour file."""
    import av
    import numpy as np
    out, got = [], 0
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                arr = rf.to_ndarray().flatten()
                out.append(arr)
                got += arr.shape[0]
            if got >= seconds * 16000:
                break
    if not out:
        return None
    return (np.concatenate(out)[: seconds * 16000].astype("float32") / 32768.0)


def transcribe_intro(path: str, s: dict) -> str:
    if not have_whisper():
        return ""
    from faster_whisper import WhisperModel
    name = s.get("whisper_model") or "base.en"
    if name not in _WHISPER:
        _WHISPER[name] = WhisperModel(name, device="cpu", compute_type="int8")
    audio = _decode_head(path, int(s.get("intro_seconds") or 90))
    if audio is None:
        return ""
    segs, _ = _WHISPER[name].transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segs)[:1500]


INTRO_RE = re.compile(
    r"(?:presents?|this is|welcome to)?\s*(?P<title>[A-Z][^.,]{2,80}?),?\s+(?:written\s+)?by\s+"
    r"(?P<author>[A-Z][\w.' -]{2,50}?)[,.]?\s+(?:read|narrated|performed)\s+by\s+"
    r"(?P<narr>[A-Z][\w.' -]{2,50}?)[.,]", re.S)


def parse_intro(text: str) -> dict:
    """'Audible Studios presents The Martian by Andy Weir, narrated by R.C. Bray.'"""
    m = INTRO_RE.search(text or "")
    if not m:
        return {}
    return {k: v.strip(" .,") for k, v in
            (("title", m.group("title")), ("author", m.group("author")), ("narrator", m.group("narr")))}


# ----------------------------------------------------------------------------
# Evidence and identification
# ----------------------------------------------------------------------------

def evidence(book, source_root: str = "") -> dict:
    files = [os.path.basename(f) for f in book.files]
    ev = {
        "kind": "audiobook" if book.kind == "audio" else "ebook",
        "folder_path": book.src_display,
        "file_count": len(files),
        "file_names": files[:12] + ([f"... +{len(files) - 12} more"] if len(files) > 12 else []),
        "current_guess": {"title": book.title, "author": book.author, "series": book.series,
                          "series_index": book.series_index, "narrator": book.narrator,
                          "chosen_from": book.meta_source},
    }
    for label, reader in (("embedded_tags", lambda: core.read_audio_metadata(book.files[0], len(book.files) > 1)
                           if book.kind == "audio" else core.read_epub_metadata(book.files[0])
                           if book.files[0].lower().endswith(".epub") else {}),
                          ("parsed_from_path", lambda: core.infer_from_path(book.src_display))):
        try:
            v = reader()
            if v:
                ev[label] = v
        except Exception:
            pass
    if getattr(book, "duration", 0):
        ev["probed_duration"] = f"{book.duration / 3600:.2f} hours"
    for k in ("isbn", "asin", "year", "publisher"):
        if getattr(book, k, ""):
            ev[k] = getattr(book, k)
    cands = getattr(book, "_candidates", None) or []
    if cands:
        ev["online_candidates"] = [
            {k: c.get(k) for k in ("source", "title", "author", "series", "series_index",
                                   "narrator", "year") if c.get(k)}
            | ({"runtime_hours": round(c["runtime"] / 3600, 2)} if c.get("runtime") else {})
            | {"match_score": c.get("score")}
            for c in cands[:5]]
    if getattr(book, "front_text", ""):
        ev["ebook_front_matter"] = book.front_text[:1800]
    if getattr(book, "flags", None):
        ev["warnings"] = book.flags
    if getattr(book, "transcript", ""):
        ev["spoken_intro_transcript"] = book.transcript[:1200]
    return ev


def identify(book, s: dict, db=None) -> dict:
    ev = evidence(book)
    key = "ai:" + hashlib.sha1((s.get("model", "") + json.dumps(ev, sort_keys=True)).encode()).hexdigest()
    if db:
        hit = db.cache_get(key, 365)
        if hit:
            hit["cached"] = True
            return hit
    reply = complete(s, SYSTEM, "Evidence:\n" + json.dumps(ev, indent=1, ensure_ascii=False))
    out = parse_json(reply)
    clean = {}
    for k in ("title", "author", "series", "series_index", "narrator", "year", "reason"):
        v = out.get(k)
        if v not in (None, "", "null"):
            clean[k] = str(v).strip()
    try:
        clean["confidence"] = max(0, min(100, int(out.get("confidence", 0))))
    except (TypeError, ValueError):
        clean["confidence"] = 0
    if db:
        db.cache_put(key, clean)
    return clean
