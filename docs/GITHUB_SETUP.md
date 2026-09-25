# Putting this on GitHub

You have a complete, ready-to-commit project folder. To publish it:

```bash
cd library-organizer          # this folder
git init
git add .
git commit -m "Library Organizer v1.10.3"
```

Then on GitHub: **New repository** → don't initialize with a README
(you already have one) → copy the two commands it gives you under
"...or push an existing repository from the command line":

```bash
git remote add origin https://github.com/<you>/library-organizer.git
git branch -M main
git push -u origin main
```

## Before you make it public, decide:

1. **Public vs. private.** If you intend to sell this rather than give it
   away, keep the repo private (or public code / paid binaries — an
   "open core" model). See `NOTICE.md` for why the license question isn't
   entirely settled by picking a repo visibility, though — mutagen's
   GPL-2.0 license has real implications for a bundled/paid distribution,
   worth a quick conversation with a lawyer before you charge for it.
2. **Repo description / topics** (GitHub sidebar): something like "Sort
   messy audiobook & ebook libraries into clean Author/Series/Title
   folders for Audiobookshelf and Calibre" with topics `audiobookshelf`,
   `calibre`, `self-hosted`, `docker`, `nas`.
3. **A release.** Tag `v1.10.3` (Releases → Draft a new release) and
   attach the two zips you already share from your blog
   (`library-organizer-docker.zip`, `library-organizer-windows.zip`) as
   release assets — that gives people a stable download link separate
   from "clone the whole repo."
4. Update the two download buttons on `george-westrup.com/organizer` to
   point at the GitHub release assets instead of (or in addition to)
   your own WordPress Media uploads, if you'd like GitHub to be the
   canonical download location going forward.

## Local git config, if this is a new machine

```bash
git config --global user.name  "George Westrup"
git config --global user.email "you@example.com"
```
