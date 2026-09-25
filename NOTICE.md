# Third-Party Notices

Library Organizer's own code is MIT-licensed (see `LICENSE`). It depends
on the following third-party packages, installed separately by the user
(via `pip install` / the Docker build) rather than bundled into this
repository:

| Package | License | Used for |
|---|---|---|
| [mutagen](https://github.com/quodlibet/mutagen) | **GPL-2.0-or-later** | reading and writing embedded audio tags (MP3/M4B/M4A/FLAC/OGG) |
| [Flask](https://github.com/pallets/flask) | BSD-3-Clause | the web application / API |
| [Waitress](https://github.com/Pylons/waitress) | ZPL 2.1 | production WSGI server |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) | cover conversion and resizing (optional) |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | fuzzy matching for duplicates and authors (optional) |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | reading PDF front pages for ISBNs (optional) |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | local speech-to-text of audiobook intros (optional) |
| [FFmpeg](https://ffmpeg.org) | LGPL/GPL | audio probing via `ffprobe` (optional; installed in the Docker image from Debian packages) |

**Why mutagen matters:** it's GPL-2.0, a copyleft license. This project
does not bundle mutagen's source or binaries — the app imports it as an
ordinary Python dependency that the user installs themselves (via `pip`,
or inside the Docker image built from the public PyPI package). That's
the model used throughout this project's setup instructions.

If you fork this project to build and **distribute a bundled binary**
(e.g. a single-file PyInstaller `.exe`, or a Docker image you publish
rather than one users build themselves from this Dockerfile), you would
be distributing mutagen's GPL-licensed code as part of your build, which
carries GPL obligations. This is a meaningfully different situation from
"my app imports a library the user installed separately," and you should
get your own legal advice before doing so — especially for a commercial
/ closed-source distribution. A common alternative is swapping mutagen
for a permissively-licensed tagging library (read-only tagging has
MIT/Apache options; writing tags may require more custom code).

## Borrowed ideas and data

Some word lists, heuristics and real-world test cases were adapted from
[deucebucket/library-manager](https://github.com/deucebucket/library-manager):
the known-narrator list and generic-title list (`enrich.py`), the
generic-title caution in the AI prompt (`ai.py`), and most naming cases in
`tests/test_realworld_names.py`. That project is MIT-licensed; its full license text is included at
[`docs/licenses/library-manager-LICENSE.txt`](docs/licenses/library-manager-LICENSE.txt).

Online metadata comes from Audible's public catalog API, Audnexus, Google
Books and Open Library, queried at run time; no data from them is bundled.

Fonts and any other assets used by the marketing pages (not part of the
application itself) carry their own licenses noted where they're used
(e.g. the Fraunces font, SIL Open Font License).

This file is informational, not legal advice.
