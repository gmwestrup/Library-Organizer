# Changelog

All notable changes to Library Organizer, newest first. Both editions
(Windows and Docker/NAS) ship the same `core.py` / `app.py` / templates,
so every entry applies to both unless noted.

## v2.0.1 — folder picker fixes (Windows)

- **Browse... listed no drives on some PCs.** The picker checked every drive
  letter A–Z by touching it, and a disconnected mapped network drive can
  stall Windows for 20–30 s per letter. Drives now come from the Windows
  drive list, which never touches the drives.
- Opening a folder that doesn't answer (a dead network drive) now gives up
  after 10 s with a clear message instead of hanging.
- The picker has a path box: type or paste any path, including
  `\\server\share\folder` UNC paths, and press Go. Quoted paths pasted from
  Explorer's "Copy as path" work too.
- Errors show inside the picker instead of a pop-up; a "Loading..." line shows
  while a folder is being read.
- Windows: when running under Microsoft Store Python, the startup log now
  explains where the data folder really is (Store Python redirects AppData).

## v2.0.1 — Windows folder picker

- **Browse... no longer shows an empty list on Windows.** Drive letters are
  read from the Windows drive list instead of probing A:–Z: one by one,
  which could stall 20–30 s per disconnected mapped network drive (a NAS
  drive letter, typically). Opening a folder that doesn't answer within 10 s
  now shows a clear message instead of hanging.
- The folder picker has a **path box**: type or paste any path, including
  `\\server\share\folder` network paths, and press Go / Enter.
- Microsoft Store Python: the startup log now explains that its data folder
  is redirected to `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.*\LocalCache\Local\LibraryOrganizer`.

## v2.0.0 — one tool for the whole job

Library Organizer now does everything its sibling tools did, and more, in
one app. Originals are still never touched; both editions still run
byte-identical files.

**New pipeline:** Scan → Enrich → AI identify → Clean up → Copy, with an
**Autopilot** button that runs every step except the copy.

- **Enrich:** audio probe (real duration, bitrate, codec, chapters; empty or
  unreadable files flagged *damaged* and held back from the copy); narrator,
  year, publisher, description, genres, ASIN and ISBN read from tags,
  sidecars and the epub OPF; the ebook's title/copyright page is read for an
  ISBN; online match against Audible (**length-aware**: the edition whose
  runtime matches your file wins), **Audnexus**, Google Books and Open
  Library, including exact lookups by ISBN/ASIN.
- **Covers:** every candidate (embedded, folder images, Audible, Audnexus,
  Google, Open Library) is measured and ranked by resolution and shape; the
  best is written as `cover.jpg` and embedded in the copied audio. Pick a
  different one, or paste an image URL, in the book's detail panel.
- **Confidence score** (0-100) per book from how well independent sources
  agree; filter *low confidence* to review only what needs it.
- **AI identify** (optional): Claude, or any OpenAI-compatible endpoint
  (Ollama, LM Studio). Only uncertain books are sent, with all the evidence;
  answers are cached against the files' content; confident answers apply,
  the rest wait as suggestions you can accept with one click. Optional local
  speech-to-text of audiobook intros (`faster-whisper`).
- **Duplicates tab:** identical files (content fingerprint, even renamed),
  same ASIN/ISBN, same or similar title, same author + running time (from
  ABS Duplicate Finder). "Book 1" never pairs with "Book 2"; audiobooks
  never pair with ebooks. Recommended keeper scored on format, bitrate,
  chapters, cover and part count; epub + mobi + pdf of one book are
  **merged** into one folder for Calibre.
- **Authors tab:** variant clustering from calibre-author-cleanup (accents,
  initials, "Last, First", typos), with "Jr." kept apart on purpose.
- **Decisions database** (from calibre-library-cleaner): your edits, author
  merges, skips, duplicate and format-merge decisions are stored against the
  files' content and re-applied on every rescan. Copy journal exportable as CSV.
- **Richer output:** `metadata.opf` and `metadata.json` now carry narrator,
  ASIN/ISBN, year, publisher, genres, description and the cover; embedded
  tags gain year, genre, description, ASIN/ISBN and cover art.
- Copy holds back damaged books and books still missing an author or title
  (both switchable), plus an optional minimum confidence.
- Data folder: `/config` (Docker) or `%LOCALAPPDATA%\LibraryOrganizer`
  (Windows). A v1 plan file is picked up automatically.

**Parser fixes** (many found by porting real-world cases from
deucebucket/library-manager's regression suite into `tests/test_realworld_names.py`):

- `clean_author("Neil Gaiman & Terry Pratchett")` returned "Terry Pratchett
  Neil Gaiman" — co-authors now give the primary author. `Smith, John, Jr.`
  → John Smith Jr. (was "Smith").
- `THE MARTIAN by andy weir` and `LAST RITES by Ozzy Osbourne
  (Audiobook)(Nonfiction)` now parse; `Stand by Me` is not "by" author "Me".
- Release and encoder junk stripped: `[MAM]`, `[bitsearch.to]`, `128k`,
  `{465mb}`, `mono vbr`, `stereo lame`, `Full Audiobook`, `audiobook_…_full`.
  Only leading tags and trailing junk runs are removed, so `11.22.63`,
  `1984` and `[Dune] …` survive.
- `The Three-Body Problem` no longer becomes author "The Three".
- `Stephen King Collection`, `Isaac Asimov Complete Works` → the author.
- `Unknown`, `Various`, `Various Authors` folders are never authors.
- `George Orwell/1984/` is a title, not a year folder.
- Library-wide pass: junk values ("calibre", "Unknown") cleared; `Title -
  Author` swaps fixed using the authors the rest of the library knows;
  narrators filed as authors flagged (and fixed when an online match
  confirms who narrated).

**Tests:** new `tests/test_realworld_names.py` and `tests/test_pipeline.py`
(the whole pipeline end to end with online services and AI mocked, real
audio via ffmpeg). CI runs all three suites on Python 3.9 and 3.12.

## v1.11 — scanner tuning against a real 1,200-book library

The scanner was dry-run against a complete real Audiobookshelf library
listing (1,209 books, ~41,000 files), using folder names as the only
available information. Before tuning it produced 15,627 "books" and 10,587
false duplicates; after tuning: 1,192 books, 1,159 exact matches, 0 books
missed, and every merge a genuine disc/part folder.

- Numbered chapter files are recognized as ONE book: `Title - 01 - Opening
  Credits.mp3`, `Title - 02 - Chapter 1.mp3`, `03 - Beauty.mp3` ... are
  grouped by the text before the track number; chapter names after it are
  ignored.
- Series index vs. part number disambiguation: `Lee Child - Reacher 27 -
  Personal 12` (27 = series position, 12 = part), `Jack Reacher Series,
  Book 18`, `MR 23 Capture or Kill`, and `The Expanse 2.5` are all read
  correctly.
- More recognized layouts: `(6of17)` and `D01`/`D12` disc folders, `The
  Last Juror 01..10` sibling folders, `[The Expanse 1.0] Leviathan Wakes`
  and `[Series 01] - Title` bracket forms, `Author Trilogy Series
  1-Title`, `Series by Author` top folders, numbered prefix codes
  (`LNSR01-95`, `01-10 DFOA`), `01 to 14` and `3 of 12` unit forms, and
  dotted names (`Long.Shadows.[Unabridged].-.006`).
- A numbered folder (`01/`) that merely *contains* a book folder is never
  treated as a disc of its parent.
- Cleanup: Amazon ASINs and bare ISBNs glued to folder names, `(10 MP3s -
  U)`, `(4 Discs - A)`, `Unb`/`Unabridged`, `A Novel`, `Collection-`,
  author-name prefixes (`Larry Niven-Footfall`), double extensions
  (`Lasher.doc`), `word- word` colon artifacts, and subtitle echoes of the
  series (`If I Had a Nickel: Roy Ballard Mysteries, Book 3`).
- The folder name arbitrates when a filename hint has title and series
  swapped (`Transfer of Power - Rapp 01`).

Sidecar `metadata.opf` / `metadata.json` files still win over all of this
— the tuning above covers books with no sidecar.

## v1.10.3 — hotfix

- v1.10.2's page could load with **no working buttons**: the "Number
  folders" checkbox was missing from the page while its script handler
  was not, which crashed the script at load. Fixed, and the UI is now
  exercised by a headless-browser test (every button clicked) before
  release.
- Browser storage (remembering rows-per-page, etc.) can no longer break
  the page if a browser blocks site data for a plain-http LAN address.

## v1.10.2

- Book folders now carry just the title (`Author\Series\The Final
  Empire`); the series position is **not** prefixed to the folder name
  any more. It lives in the series field and is written to
  `metadata.opf`, `metadata.json`, and the embedded tags — which is where
  Audiobookshelf and Calibre actually read it from. Tick "Number folders
  by series position" to get the old `01 - Title` folders back. The
  structure dropdown reads accordingly.

## v1.10.1

- Checkbox selections now persist across pages: check books on several
  pages and bulk-edit / skip them together; the bulk bar stays visible
  and shows "(across pages)". Applying an action clears the selection.
- "Rows per page" selector (50–2000) next to the pager; remembered
  between visits.

## v1.10 — one app, two launchers

The Windows edition is now the same web app as the Docker edition,
started by a small launcher (`LibraryOrganizer.pyw`) that serves it on
`127.0.0.1` and opens your browser. The old Tkinter desktop window is
retired: it had only the engine improvements but none of the interface
features (layouts, duplicate policies, skip, bulk edit, filters, Stop,
persistence...) and had drifted out of sync. Now both editions are
byte-identical application files — a fix in one is a fix in both. Windows
specifics: the folder picker starts at "My Computer" (drive letters,
mapped shares), the plan auto-saves under
`%LOCALAPPDATA%\LibraryOrganizer`, and the server binds to localhost
only.

## v1.9.2

- "Use only as last resort" scan options, generalized to three
  independent switches: `.opf`, `metadata.json`, and embedded tags. Tick
  any of them to demote that source behind folders and filenames — it
  then only fills in what nothing else could, and the Meta column reads
  e.g. `tags (last resort)`. Use whichever source was polluted by a bad
  import in *your* library (json from an early Audiobookshelf scan, tags
  from a sloppy ripper, opf from a mismatched Calibre import). "Trust
  folder names over embedded tags" still governs the folders-vs-tags
  order when tags aren't demoted.
- A demoted source still fills in blanks when it's the only information
  available (true last resort, not a full block).
- Fixed: a purely numeric placeholder title (e.g. `01` from `01.mp3`) no
  longer blocks a last-resort source from supplying the real title.

## v1.9.1 — box sets, bad `metadata.json`

- Box sets: a number range on the folder (`Box Set (5-8)`, `Books
  17-20`) is carried into a generic sidecar/tag title, so ten box sets no
  longer collapse into one title. Ranges and volume markers inside `(...)`
  are content and survive even "Strip all `(...)`". If a series has no
  position yet, the range becomes it (`5-8`) — Audiobookshelf sorts that
  correctly among the single books.
- Duplicate detection is series-aware: the same title at different
  series positions (box sets 02 and 03) is **not** a duplicate.
- Rows whose sidecar disagrees with the folder path are marked "(path
  differs)" — e.g. a `metadata.json` claiming "Addison Cain" inside
  `Wilbur Smith/The Dark of the Sun`.
- Initial single-switch "Distrust metadata.json" option (later
  generalized in v1.9.2).

## v1.9 — metadata sync (fix bad embedded data for good)

The reviewed table becomes the single source of truth, and at copy time
it's written **everywhere**, so all three metadata layers agree in the
output:

- `metadata.opf` (Calibre) — title, author, series, narrator
- `metadata.json` (Audiobookshelf) — title, authors, narrators,
  `series #N`
- Embedded tags in the **copied** files (originals are never touched):
  - MP3 — album/artist/albumartist = title/author, composer = narrator,
    `SERIES` + `SERIES-PART` (and `MVNM`/`MVIN`), track numbers `1/N..N/N`
    in playback order, junk "grouping" tag cleared
  - M4B/M4A — same via iTunes atoms (movement name/index, SERIES atoms)
  - FLAC/OGG/OPUS — Vorbis comments
  - EPUB — the OPF inside the epub is rewritten (title, author, series)

Toggles next to Copy: "Write metadata.opf", "Write metadata.json", "Fix
embedded tags in copies" — all on by default. Reading is bidirectional:
rescanning the *output* folder (with "Trust folder names" unticked)
reproduces the reviewed data exactly from the tags.

Also new: "Trust folder names over embedded tags" (on by default). When
a structured `Author\Series\Book` path and the embedded tags disagree,
the folders win and the row's Meta column shows "(tags differ)". Untick
to trust the tags instead. Sidecar files always win over both.

## v1.8 — roman-numeral parts, title cleanup, narrator capture

- Part folders like `1_ Part I - The World of Jeremy Walker`, `7_ Part
  VII`, `Disc 1 of 5`, or plain `01`/`02` are recognized as parts of the
  parent book (roman numerals, numeric prefixes, and subtitles all
  handled), kept in order I, II ... VII.
- Leading years on book folders (`1986 - Belinda`) are dropped.
- Title/series cleanup: `(read by X)`, `(Unabridged)`, format, bitrate,
  and year tags are removed automatically; an author-fragment tag such as
  `(Jyr)` on `Star Force Universe (Jyr)` is removed when it matches the
  author. Anything else in `(...)` is kept — unless "Strip all `(...)`
  from titles" is ticked before scanning.
- Narrators found in `(read by ...)` are captured (see the CSV export)
  and written into `metadata.opf` as a contributor, so Audiobookshelf/
  Calibre get the narrator without it polluting the title.
- Colons in titles become ` - ` in folder names (`Star Force - Origin`),
  instead of a jammed `Star Force- Origin`.
- Tag hygiene: the ID3 "content group"/Vorbis "grouping" fields are no
  longer treated as a series (they usually hold chapter/part names). A
  series identical to the title is discarded.
- Windows edition: fixed a latent crash on Copy and brought its copy
  engine to full parity (metadata.opf, layouts, part renaming).

## v1.7 — disc / part awareness

Multi-disc and multi-part books are recognized as ONE book instead of
several (which previously showed up as false "duplicates"):

- Nested disc folders: `Title\Disc 1\`, `Title\CD 2\`, `Title\Part 3\` ...
- Sibling disc folders: `Title - Disc 1`, `Title - Disc 2` side by side
- Numbered files sharing one folder: `Dune Part 1.mp3`, `Dune Part
  2.mp3` next to `Martian CD1.mp3`, `Martian CD2.mp3` → correctly two
  books, not one

Recognized markers: disc, disk, cd, part, pt, side, tape (with or
without a number separator, `3 of 12` forms too), plus numbered prefix
codes such as `LNSR01-95 Larry Niven - Saturn's Race.mp3` (v1.7.1). When
filenames carry a clean `Author - Title`, that's used in preference to a
sloppy folder name. `Book 1` / `Vol 2` are **not** disc markers — they
stay series positions. Titles containing numbers (`Fahrenheit 451`,
`1984`) are left intact.

Parts are kept in playback order (disc 1 first, natural sort so disc 10
follows disc 9). When a book spans several disc folders, copied parts
are prefixed with the disc folder (`Disc 1 - 01.mp3`) so nothing clashes;
with "Rename multi-part files" on, they become `Title - 01.mp3` ...
numbered straight through.

## v1.6 — duplicate policies

A "duplicate" is a book whose author + title + type (audio/ebook)
matches another book found elsewhere in the scan. They're highlighted
purple, counted at the top, and have their own filter.

Resolve them with the Duplicates dropdown + "Apply to duplicates":

| Policy | Keeps |
|---|---|
| decide manually (default) | nothing automatic — skip by hand |
| keep best format | m4b > m4a > flac > opus > ogg > mp3 (audio); epub > azw3 > azw > mobi > pdf (ebooks) |
| keep largest | most total bytes (usually the best-quality rip) |
| keep newest files | most recently modified files |
| keep oldest files | oldest files |
| keep fewest files | single-file over multi-part (ties → best format) |
| keep ALL | nothing skipped; extra copies get numbered folders: `Title`, `Title (2)`, ... |

Applying a policy keeps one book per duplicate group and marks the rest
"skipped" (grey) — nothing is deleted or copied by this step. Applying a
different policy later re-resolves every group from scratch.

Also: single-letter or alphabetical-bin folders (`A`, `B`, `M-Z`) are no
longer mistaken for author names.

## v1.5

- Sidecar metadata files became the **top** source: a `metadata.json`
  (Audiobookshelf) or `metadata.opf` / any `.opf` file (Calibre) sitting
  in the book's folder beats everything else. The Meta column shows
  `opf` or `metadata.json` when this happened.
- Multi-file tag fix: for multi-part books the per-track TITLE tag is a
  chapter name; only the ALBUM tag is trusted as the book title (no more
  books called "Chapter 1").
- New "Rename multi-part files" option (off by default): parts of
  multi-file audiobooks renamed to `Title - 01`, `Title - 02`... in
  their original sort order.
- A book's own sidecar `.opf` is preserved as-is in the output; the tool
  only writes its own `metadata.opf` when the book didn't bring one.

## v1.4 — review pass

- **Performance fix**: the status endpoint was recomputing the duplicate
  map per book on every poll (O(n²)); with 100k books the UI would have
  crawled. Now computed once per poll.
- **Collision guard**: before copying, the plan is checked for two
  different books resolving to the same output folder (easy with the
  flat/Title-only structures, or unresolved duplicates). You're warned
  and can review, skip, or force.
- "Last, First" author flipping now handles two-word surnames: "Le Guin,
  Ursula K." becomes "Ursula K. Le Guin" (previously truncated).
- Serves through Waitress, a production WSGI server.
- Diagnostics also print to `docker logs`, not just the web UI's log
  pane.

## v1.0 – v1.3 — initial feature set

The original round of improvements, added iteratively before explicit
version numbers were introduced:

- Review plan auto-saves (survives a container restart).
- Bulk-edit: check rows to set author/series/# across many books at once.
- "Export CSV" downloads the whole plan as a spreadsheet.
- Audiobook online lookups hit Audible's catalog first for real series
  names and positions, with Google Books / Open Library as fallback.
- "Write metadata.opf" (on by default).
- Disk-space pre-check before copying.
- Possible duplicates highlighted purple and counted; "possible
  duplicates" filter.
- Skip button/status: parks a book without removing it from the plan.
- Every copied file is size-verified against the original.
- Time-remaining estimate during long scans/copies.
- Output-structure dropdown (nested by series, flat variants, title
  only) with a live "will be filed under" preview.
- Stop button for any running scan/lookup/copy.
- Folder picker (Browse... buttons) to scan a single subfolder instead
  of the whole library.
- Series detection from top-level folder names (`Dresden Files`,
  `Stormlight Archive`), not mistaken for authors; numbered book folders
  (`Book 2 - Title`) fill in the series position.

## v0.x — original release

- Windows desktop app (Tkinter): scan a folder tree, detect audiobooks
  and ebooks, read embedded/sidecar metadata, parse folder and file
  names, optional online lookup, review table with inline editing, copy
  (never move) into `Author\Series\NN - Title`.
- Docker/NAS web edition added: same engine behind a Flask web UI,
  designed to run beside the files on a NAS so copying happens at full
  disk speed instead of over the network twice.
