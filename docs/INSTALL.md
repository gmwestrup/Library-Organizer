# Library Organizer v2 — Docker / NAS Install Guide

Library Organizer scans a messy audiobook & ebook collection, lets you review
and fix every title in a browser, then copies everything into a clean
`Author / Series / Title` structure for Audiobookshelf or Calibre.
Originals are never modified — the source is even mounted read-only.

This guide covers installing it as a Docker container on a NAS, two ways.
**Pick ONE method and stick with it** — if Portainer and an SSH compose file
both define the same container, they fight over it (stack updates fail with
500 errors, and edits appear to have no effect because you edited the copy
that isn't in charge).

---

## What you need

* A NAS (or any Linux box) running Docker. Portainer optional.
* Internet access on the NAS for the **first start only** (it installs two
  small Python packages; restarts after that are instant and offline).
* The unzipped app package in a folder on the NAS, e.g.
  `/share/Docker/library-organizer`, containing:

```
app.py   core.py   enrich.py   ai.py   dedupe.py   authors.py   library_db.py
requirements.txt   templates/   Dockerfile   docker-compose.yml
docker-compose.host.yml   docker-compose.build.yml   INSTALL.md
```

* Four paths decided:
  1. **App folder** — where the files above live
  2. **Source** — your messy library (will be mounted read-only)
  3. **Destination** — an empty-ish folder for the clean output
     (must NOT be inside the source)
  4. **Config** — a small folder for the decisions database, covers and
     settings (e.g. `/share/Docker/library-organizer/config`). This is
     what remembers your edits between scans: **back it up**.

## The NAS path rule (read before anything else)

Docker needs the path **as the NAS's Docker daemon sees it**, and it is
**case-sensitive**.

* **Asustor:** every shared folder lives under `/share/<ShareName>`,
  regardless of which physical volume holds it. Run `ls /share` and copy the
  spelling exactly. `/share/YourShare/Audiobooks` works;
  `/volume10/Audiobooks` fails with
  `error while creating mount source path ... operation not permitted`.
* **Synology:** use `/volume1/<ShareName>` style paths.
* **QNAP:** use `/share/<ShareName>` style paths.

When in doubt, SSH in and `ls` the path — if `ls` can't see it, Docker can't
mount it.

---

## Method A — Portainer stack (no SSH needed)

1. Put the unzipped app folder on the NAS (File Explorer / SMB is fine).
2. Portainer → **Stacks → Add stack**, name it `library-organizer`.
3. Paste the contents of `docker-compose.yml` into the web editor.
4. Edit the four volume lines to your real paths (path rule above):

```yaml
    volumes:
      - /path/to/library-organizer/docker:/app          # app folder
      - /path/to/your/messy-library:/source:ro          # messy library
      - /path/to/your/clean-output:/dest                # clean output
      - /path/to/library-organizer/config:/config       # remembered decisions
```
(the example under "The NAS path rule" above shows what a real Asustor
path looks like, e.g. `/share/YourShare/Audiobooks`)

5. **Deploy the stack.** First start takes ~30–60 s (package install).
6. Verify (see **First start** below), then browse to
   `http://NAS-IP:8765`.

> Do **not** use `docker-compose.build.yml` in Portainer — pasted stacks
> have no build folder, so `build: .` fails with
> `failed to read dockerfile: no such file or directory`. The normal
> compose needs no build step at all.

## Method B — SSH / docker compose

1. Put the unzipped app folder on the NAS and edit the volume paths in
   `docker-compose.yml` (same three lines as above):

```
cd /path/to/library-organizer/docker      # wherever you put this folder
nano docker-compose.yml
```

2. Launch (Docker needs root on most NAS — hence `sudo`):

```
sudo docker compose up -d
```

3. Watch it start:

```
sudo docker logs -f library-organizer
```

4. Verify (next section), then browse to `http://NAS-IP:8765`.
   The container has `restart: unless-stopped`, so it survives reboots.

*Optional:* stop needing `sudo` with
`sudo usermod -aG docker YOURUSER`, then log out of SSH and back in.

---

## First start — how to know it worked

In `docker logs` you should see the package install finish, then:

```
Library Organizer web UI on http://0.0.0.0:8765
```

Open `http://NAS-IP:8765` (your NAS's **LAN** IP — not the 172.x /
192.168.112.x addresses in the logs; those are Docker-internal). Check:

1. The header shows the version (e.g. **v1.10.3**) — confirms the mounted app
   files are the ones running.
2. The log pane at the bottom shows the mount report:

```
Source mount /source: OK - 214 entries visible
Destination mount /dest: OK - 1 entry visible
```

   `MISSING` or `EMPTY` here means the matching volume line points at the
   wrong NAS path — the message names which line to fix.
3. Press **Scan** — the log lists the top-level folders it can see, so you
   immediately know it's reading the right library.

---

## Can't reach the page?

Work down this list — it's ordered by how often each one is the culprit:

1. **Wrong IP.** Use the NAS's LAN IP (the one you SSH to / open ADM with).
2. **Browser forced HTTPS.** Type `http://` explicitly:
   `http://<NAS-IP>:8765`.
3. **Docker's port-forwarding is broken** (common on NAS with many
   container networks). Diagnose on the NAS:

```
curl -I http://localhost:8765        # app itself
curl -I http://NAS-IP:8765           # through Docker's port mapping
```

   If the first works and the second fails, switch to **host networking**:
   deploy with `docker-compose.host.yml` instead
   (`sudo docker rm -f library-organizer` then
   `sudo docker compose -f docker-compose.host.yml up -d`).
   Host mode skips Docker's NAT entirely; the only requirement is that
   port 8765 is free on the NAS (if the logs say "address already in use",
   change `PORT=8765` to `8766`).
4. **NAS firewall.** On Asustor: Settings → ADM Defender → Firewall — add
   an allow rule for TCP 8765.

## Optional: AI and listening to intros

**AI** is set up in the web UI (**AI & Settings** tab), not in the compose
file. Two choices:

* **Anthropic (Claude):** paste an API key from console.anthropic.com. The
  default model is Claude Haiku, which costs very little per book. You can
  also pass the key as `ANTHROPIC_API_KEY` in the compose `environment:`.
* **Local model (free, private):** choose *OpenAI-compatible* and enter your
  Ollama or LM Studio address, e.g. `http://192.168.1.10:11434/v1`, and a
  model name such as `llama3.1:8b`. From inside Docker, use the NAS or PC's
  IP address, not `localhost`.

Press **Test connection**. Only books below the confidence threshold are
ever sent, answers are cached, and *Max books per AI run* caps the cost.

**Listening to intros** (speech-to-text, runs on the NAS CPU, nothing is
uploaded) needs the `faster-whisper` package, so it's a build option:

```bash
sudo docker-compose -f docker-compose.build.yml build --build-arg WITH_WHISPER=1
sudo docker-compose -f docker-compose.build.yml up -d
```

Then tick **Transcribe audiobook intros** in AI & Settings. The first use
downloads the speech model (~150 MB for `base.en`) into the container.
On a small NAS CPU expect several seconds per book; it only runs for books
that nothing else could identify.

---

## Troubleshooting — real errors, real fixes

| Error / symptom | Cause | Fix |
|---|---|---|
| `failed to read dockerfile: no such file or directory` | Portainer stack used the `build:` compose | Use the normal `docker-compose.yml` (no build step) |
| `permission denied ... docker.sock` | SSH user can't talk to Docker | Prefix commands with `sudo` |
| `mount source path '/share/volumeX' ... not permitted` | Wrong path style or capitalization | Use the exact path from `ls /share` (Asustor) — case matters |
| `container name "/library-organizer" is already in use` | Leftover container from an earlier attempt | `sudo docker rm -f library-organizer`, retry |
| Stack update fails with a 500 in Portainer | Portainer and an SSH compose both define the container | Pick one method; delete the other definition |
| Edits to the stack seem to change nothing | You edited the copy that isn't managing the container | Same as above — one source of truth |
| Page loads but Scan finds 0 books | `/source` mounted to a wrong or empty path | Read the mount report in the log pane; fix that volume line |
| Header shows an old version number | Container running old files | Confirm `/app` points at the new folder, then recreate the container (not just restart) |
| Works from the NAS (`curl localhost`) but not from your PC | Docker NAT or firewall | Steps 3–4 in "Can't reach the page?" |

## Upgrading from v1

* Add the `/config` volume line (see above) before starting v2.
* v1 kept its review plan in the destination (`.organizer-plan.json`);
  v2 picks it up automatically on first start and saves to `/config` from
  then on.
* The quick compose file now installs from `requirements.txt`; the first
  start after upgrading takes a little longer.

## Updating to a new version

1. Replace the files in the app folder with the new release.
2. Recreate the container (a plain restart also works for app-file changes,
   but recreation is the sure thing):
   * SSH: `sudo docker compose up -d --force-recreate`
   * Portainer: Stacks → your stack → **Update the stack**
3. Hard-refresh the browser (Ctrl+F5) and confirm the new version number in
   the header.

Your review plan is safe across updates — it auto-saves to
`/dest/.organizer-plan.json`.

## Uninstall

```
sudo docker rm -f library-organizer
sudo docker rmi python:3.12-slim        # optional, if nothing else uses it
```

Delete the app folder. Your library, the clean output, and the plan file
are all just normal folders — nothing else was installed anywhere.
