# GDrive Organizer

**Reorganise a Google Drive you're afraid to touch.**

A single-file Python tool that reads your entire Drive, shows you what's
actually in it, lets you decide where things belong, and then moves them —
without ever being able to permanently delete anything.

Built for the Drive that got away from you: the one with 190,000 files, six
copies of the same project folder, and a `_DESKTOP-CLEANUP` directory you've
been scared to open since 2022.

---

## Why this exists

Every Drive cleanup tool asks you to trust it. This one is built so you
don't have to.

**It cannot permanently delete a file.** Not as a policy — as a capability.
The strongest things it can do are:

| Action | Reversible by | Notes |
| --- | --- | --- |
| Move a file or folder | `undo <log>` | The only action `apply` performs |
| Send a file or folder to the trash | Trash tab, or `undo` | Same as deleting in Drive: kept 30 days, restorable the whole time |
| Trash a duplicate you confirmed | Trash tab, or `undo` | One explicit click per item, in `review` |
| Remove one sharing permission | `undo` (it's re-created) | Records who lost access, so it can be restored |
| Upload from this computer | `undo` (removes the copy) | Adds to Drive; originals untouched unless you pick Move |
| Replace a file when uploading | `undo` (previous version restored) | Same file gets new contents — keeps its id, link and sharing |
| Move (upload, then remove local) | your **recycle bin** | Only after the upload is verified; never a direct delete |

Everything else is read-only. Every run writes a JSON-lines undo log before
its first change. Nothing writes without `--execute`, and the live UI shows
you a full preview you have to confirm.

---

## What it does

### Sees your Drive honestly

`scan` crawls everything and produces a report: total size, how much sits in
one folder, duplicate sets, and a proposed structure — where every proposal
carries the **evidence** it was based on.

Classification uses file types, modification dates, duplication, and the
proportion of machine-generated content (`node_modules`, `.git`, `vendor`).
It never guesses what a folder means from its name. Anything without a clear
signal goes to **Review**, which is a no-op bucket: items left there stay
exactly where they are.

### Finds duplicates that are genuinely safe to remove

Duplicate detection is content-hash based, with three rules that matter:

- **Never trade down.** A surviving copy must live somewhere at least as
  good as the one being removed. Without this, a live working folder gets
  quarantined because a stale copy exists in an old backup — the clean copy
  dies and the scratch copy survives.
- **Only files you own.** A copy owned by someone else can vanish the moment
  they unshare it, so it's never quarantined and never counted as a survivor.
- **No content hash may lose every copy.** Enforced as a hard assertion that
  aborts the run rather than proceeding.

Duplicates are moved to a quarantine folder with a manifest recording where
each original lives — never deleted. You review them side by side and trash
only what you confirm.

### Lets you organise live

`ui` starts a local web app that reads your Drive as you expand folders, so
what you see is what's actually there — not a snapshot.

- **Organise** — browse, assign destinations, preview, execute. Rows you've
  annotated are tinted; anything that will be carried along by a planned
  parent is tinted lighter. **Drag any row onto a folder** to plan a move —
  it becomes a planned move exactly as choosing that folder from the
  dropdown does, with impossible drops refused and the reason shown.
  Rename anything in place, and **search your whole Drive by name**: press
  Enter in the filter box and results come back as real rows you can plan,
  rename, delete or jump to.
- **Sharing** — an exposure audit. It opens by telling you what's actually
  at risk: **how many items are public on the web**, how many people and
  groups can reach your files, and who they are — ranked by how much each
  can see. Click a person to get the exact list of everything they can
  open, and revoke access from that list. Folders show how many shared
  items are anywhere beneath them, so you follow the trail down instead of
  clicking blindly.
- **Trash** — a table of what you've thrown away, newest first, with
  search and one-click type filters. Restore only: deliberately **no**
  empty-trash or permanent-delete capability.
- **From this PC** — browse your computer, pick a Drive folder for
  anything you want up there, and upload. Folder structure is recreated,
  and hidden files and build folders (`node_modules`, `.git`, `dist`,
  `venv`…) are skipped by default. Choose **Copy** to leave everything
  local, or **Move** to send each original to the **recycle bin** — but
  only after its upload is confirmed in Drive at the right size, and never
  by deleting outright. Where a file of the same name is already in Drive,
  **Replace gives that file the new contents** — it keeps its id, link,
  sharing and comments, and the old contents remain as a previous version
  that undo can restore. Folders of the same name are merged into, never
  replaced. Uploads are resumable, so a dropped connection
  continues rather than restarting, and a long transfer can be stopped
  between files. Undo removes what was uploaded.

Any row can be sent to the trash from the tree. The confirmation tells you
what it costs before you agree: for a folder, how many items go with it,
counted live rather than from the last scan — and that Drive keeps it for
30 days, restorable the whole time, before removing it itself.

Files carry an icon for their actual type — images, video, audio, PDFs,
Docs/Sheets/Slides, archives, code, fonts, keys, databases — resolved from
the MIME type with the file extension as a fallback, since Drive names
aren't required to have one.

**Click any file to view it.** Images, PDFs, video, audio and text or code
files are streamed through the local server and shown inline, and Google
Docs, Sheets and Slides are rendered to PDF for preview. **Word `.docx`
files are rendered as readable text** — headings, bold and italic, lists
and tables — using the standard library, or
[mammoth](https://github.com/mwilliamson/python-mammoth) for better
fidelity if you install it. The converted markup is filtered to a small
set of tags with no attributes, because a document is untrusted input.
**ZIP archives list their contents** — read from the archive's own
directory, so nothing is extracted and nothing is written to disk. Every file also offers **Copy
link**, **Copy path**, **Download**, and **Open in Drive**. Previews are
capped at 25 MB; anything larger or in a format the browser can't render
says so and links out instead.

Destination search indexes every folder in your Drive, so you can file
something into a folder 12 levels deep that you've never opened. Search
matches fragments of any level in any order — `nokia phones`,
`phones nokia` and `personal/phones` all find `Personal/Phones/Nokia`.

The index is built once and then **kept up to date by reading only what
changed**, using Drive's change feed. A first run reads everything; after
that, relaunching with nothing changed costs a single API call instead of
a crawl, and a rename or a move costs one more. A full read happens again
only if the change token expires.

Name collisions are surfaced before execution, with an explicit choice
between keeping both (Drive allows same-named siblings) and replacing —
where the replaced item goes to trash and is restored if you undo.

---

## Install

Requires **Python 3.8+**.

```bash
git clone https://github.com/imsaeedafzal/GDrive-Organizer.git
cd GDrive-Organizer
pip install -r requirements.txt
```

### Get Google Drive API credentials

The tool talks to your Drive as *you*, using your own OAuth client. No
credentials ship with it and nothing is sent anywhere except Google.

1. **Enable the Drive API** —
   [console.cloud.google.com/apis/library/drive.googleapis.com](https://console.cloud.google.com/apis/library/drive.googleapis.com)
2. **Configure the consent screen** —
   [console.cloud.google.com/auth/branding](https://console.cloud.google.com/auth/branding)
   (External is fine for personal use)
3. **Add yourself as a test user** —
   [console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)
4. **Create an OAuth client**, type **Desktop app** —
   [console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients)
5. **Download the JSON**, rename it to `credentials.json`, and place it
   beside the script.

The first run opens a browser to authorise, then caches the grant in
`token.json`. Both files are gitignored — they grant full read/write access
to your Drive, so never commit them. If one ever leaks, revoke the OAuth
client in Google Cloud Console; deleting the commit isn't enough.

> **Windows:** run `python gdrive_organizer.py ...`, never
> `./gdrive_organizer.py` — the shebang picks up MSYS python3, which won't
> have the dependencies.

---

## Use it

### The quick way

```bash
python gdrive_organizer.py ui
```

Opens the live interface. Browse, assign destinations, preview, execute.
Every run is undoable from the Revisions panel, which lists each run with
its details and its own undo action.

### The deliberate way

```bash
# 1. Read-only crawl. Writes inventory.csv (keep it), report.html,
#    proposed_structure.html and duplicates.csv
python gdrive_organizer.py scan

# 2. Open report.html, then the interactive tree.
#    Adjust destinations, export mapping.csv.

# 3. See exactly what would happen. Changes nothing.
python gdrive_organizer.py preview mapping.csv

# 4. Do it, with a progress bar, an audit log, and verification after
python gdrive_organizer.py apply mapping.csv --execute

# 5. Move duplicates aside — nothing is deleted
python gdrive_organizer.py quarantine --execute

# 6. Inspect each quarantined item next to its original, trash what you confirm
python gdrive_organizer.py review
```

Any run is reversible:

```bash
python gdrive_organizer.py undo logs/apply_20260805_143036.jsonl --execute
```

### All commands

| Command | What it does | |
| --- | --- | --- |
| `ui` | Live interface — browse your real Drive and organise it | |
| `scan` | Crawl and produce the report + inventory baseline | *read only* |
| `retree` | Rebuild report and tree from the last scan, no crawl | *read only* |
| `preview` | Turn a mapping into `preview.html` | *read only* |
| `verify` | Check every baseline file ID still exists | *read only* |
| `apply` | Execute a mapping | needs `--execute` |
| `quarantine` | Move duplicates to a review folder | needs `--execute` |
| `review` | Browse quarantined duplicates, trash what you confirm | |
| `undo` | Reverse any run | needs `--execute` |

Useful flags:

- `--max-ops N` — safety cap, default **15**. Large batches are hard to
  review and hard to reason about when something goes wrong. Raise it
  deliberately, or override once with `--allow-large`.
- `--dup-min-size BYTES` — ignore small files in duplicate analysis. Useful
  when thousands of 27-byte `index.php` stubs are drowning out the 100 MB
  folders.
- `--credentials` / `--token` — point at different credential files.

---

## Where things go

```text
credentials.json      your OAuth client         — gitignored
token.json            your cached grant         — gitignored
drive_scan/           inventory + reports       — gitignored
logs/                 undo logs, manifests,
                      folder index              — gitignored
```

Everything generated describes a real Drive — file names, folder structure,
file IDs, owners — so all of it is private even though only two files are
credentials. The shipped `.gitignore` covers all of it.

---

## Safety design

The things worth knowing before you trust it with 190,000 files:

- **Dry run by default.** Every write command needs `--execute`.
- **Operation cap.** Refuses more than `--max-ops` in one batch.
- **Undo logs.** Written before the first change and flushed per operation,
  so an interrupted run is still fully reversible. Undo is idempotent —
  items already back in place are skipped, so it's safe to re-run.
- **Post-run verification.** Every item a run touched is confirmed present
  and untrashed afterwards. `verify` does the full comparison against your
  scan baseline.
- **Self-move guards.** A folder can't be moved into itself or its own
  subtree — blocked in the picker, at planning time, and again at execution.
- **Orphan-aware.** Folders with no path from My Drive's root are excluded
  from destinations, because resolving one would silently *create* a new
  folder of that name instead of finding it.
- **Local servers are locked down.** The UI and review servers require a
  session token and a localhost `Host` header, so no other page in your
  browser can drive them.

---

## Limitations

- **My Drive only.** Shared drives aren't supported.
- **The scan is a snapshot**, but the live UI no longer depends on it:
  folder totals, emptiness and contents are all measured from your Drive as
  it is now. The scan's figures are used only as a stand-in until the live
  count finishes, and the tooltip says which you're looking at.
- **Duplicate detection needs content hashes.** Google-native files (Docs,
  Sheets, Slides) don't have them and are never treated as duplicates.
- **Some files can't be typed by anyone.** Drive reports no MIME and the
  name has no extension — those get a generic icon and no preview. In a
  190k-file Drive that was about 14% of files, nearly all build artefacts.
- **Same-named sibling folders collapse** to one entry in destination
  search, and the resolver picks the first match.

---

## License

MIT — see [LICENSE](LICENSE).
