#!/usr/bin/env python3
"""
Real-world naming cases, run through the FULL scanner (not one helper).

Most cases are ported from deucebucket/library-manager's regression suite
(MIT licensed - each came from a real user's GitHub issue there), plus cases
from this project's own history. Each case builds a tiny folder, scans it,
and checks what the scanner concluded.

Run:  python tests/test_realworld_names.py
"""
import os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "docker"))
import core  # noqa: E402

failures = []


def scan_one(rel, loose=False):
    tmp = tempfile.mkdtemp(prefix="lo-rw-")
    try:
        if loose:
            d = os.path.join(tmp, os.path.dirname(rel))
            os.makedirs(d, exist_ok=True)
            open(os.path.join(tmp, rel + ".m4b"), "wb").write(b"x" * 20)
        else:
            d = os.path.join(tmp, rel)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "audio.m4b"), "wb").write(b"x" * 20)
        books = list(core.scan_library(tmp, True, True, log=lambda m: None))
        return books[0] if books else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check(label, ok, got=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   -> got {got}"))
    if not ok:
        failures.append(label)


def title_is(rel, want, loose=False, author=None):
    b = scan_one(rel, loose)
    got = (b.author, b.title) if b else None
    ok = b is not None and b.title.lower() == want.lower() and (author is None or b.author == author)
    check(f"{rel!r:62} -> {author + ' / ' if author else ''}{want}", ok, got)


print("\n== Torrent / encoder junk (library-manager #31, #79) ==")
title_is("2018 - Blue Collar Space (multi) 128k {465mb}", "Blue Collar Space")
title_is("[bitsearch.to] Dean Koontz - Watchers", "Watchers", author="Dean Koontz")
title_is("The Martian Full Audiobook Unabridged", "The Martian")
title_is("audiobook_The_Great_Gatsby_full", "The Great Gatsby")
title_is("Brandon Sanderson - Mistborn 01 - The Final Empire [MP3]", "The Final Empire", author="Brandon Sanderson")
title_is("Gateway MP3", "Gateway")
title_is("Foundation AAC 256k", "Foundation")
title_is("The Hobbit 320kbps", "The Hobbit")
title_is("The Three-Body Problem 128k mono vbr", "The Three-Body Problem")
title_is("Death's End {465mb} 128k stereo lame", "Death's End")
title_is("[MAM] Dean Koontz - Watchers (2021)", "Watchers", author="Dean Koontz")

print("\n== Titles that LOOK like junk but aren't ==")
title_is("Stephen King - 11.22.63", "11.22.63", author="Stephen King")
title_is("1984", "1984", loose=True)          # a bare "1984/" FOLDER is a year folder
title_is("George Orwell/1984", "1984", author="George Orwell")
title_is("George Orwell - 1984", "1984", author="George Orwell")
title_is("Joseph Heller - Catch-22", "Catch-22", author="Joseph Heller")
title_is("Stephen King - It", "It", author="Stephen King")

print("\n== 'Title by Author' (library-manager by-author suite) ==")
title_is("A Space Odyssey by Arthur C. Clarke", "A Space Odyssey", author="Arthur C. Clarke")
title_is("Leaving Las Vegas by John O'Brien", "Leaving Las Vegas", author="John O'Brien")
title_is("LAST RITES by Ozzy Osbourne (Audiobook)(Nonfiction)", "Last Rites", author="Ozzy Osbourne")
title_is("Some Book by Jane Doe (Unabridged)", "Some Book", author="Jane Doe")
title_is("THE MARTIAN by andy weir", "The Martian", author="Andy Weir", loose=True)

print("\n== Calibre ids and author-folder suffixes (#50) ==")
title_is("Stephen King/The Shining (123)", "The Shining", author="Stephen King")
title_is("Peter F. Hamilton (123)/Pandora's Star", "Pandora's Star", author="Peter F. Hamilton")
title_is("Stephen King Collection - The Shining", "The Shining", author="Stephen King")
title_is("Brandon Sanderson Anthology - Mistborn", "Mistborn", author="Brandon Sanderson")
title_is("Isaac Asimov Complete Works/Foundation", "Foundation", author="Isaac Asimov")

print("\n== Placeholder folders are never authors (#46, #59) ==")
for junk in ("Unknown", "Various", "Unknown Author", "Various Authors", "watch", "downloads",
             "incoming", "import", "new", "tmp", "metadata"):
    b = scan_one(f"{junk}/Some Real Title")
    check(f"{junk!r:20} is not an author", b is not None and b.author == "", (b.author, b.title) if b else None)

print("\n== Author clean-up ==")
for raw, want in [("Weir, Andy", "Andy Weir"), ("Le Guin, Ursula K.", "Ursula K. Le Guin"),
                  ("Neil Gaiman & Terry Pratchett", "Neil Gaiman"),
                  ("Larry Niven and Jerry Pournelle", "Larry Niven"),
                  ("Smith, John, Jr.", "John Smith Jr."), ("Freida McFadden", "Freida McFadden"),
                  ("Mary O'Brien", "Mary O'Brien")]:
    got = core.clean_author(raw)
    check(f"clean_author({raw!r}) == {want!r}", got == want, got)

if failures:
    print(f"\n{len(failures)} case(s) FAILED")
    sys.exit(1)
print("\nAll real-world cases passed.")
