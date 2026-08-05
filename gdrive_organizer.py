#!/usr/bin/env python3
"""
gdrive_organizer.py - scan, propose, apply and review a Google Drive
reorganisation.

WHAT THIS TOOL WILL NOT DO
--------------------------
It has no permanent-delete capability. The strongest action available is:
  * moving a file or folder,
  * on an explicit click in `review`, moving it to Drive's trash, which is
    recoverable for 30 days,
  * on an explicit click in the ui's Sharing tab, removing one sharing
    permission (who it was is recorded in logs/share_actions.jsonl, and
    `undo logs/share_actions.jsonl --execute` restores it),
  * on an explicit click in the ui, trashing a folder that is verifiably
    empty at that moment (recorded in logs/folder_actions.jsonl; also
    restorable via undo), or
  * during `undo`, trashing a folder that the run itself created and that
    is now empty.
Nothing is ever destroyed, and nothing writes without --execute. The ui's
Trash tab is strictly read-only plus restore — it cannot empty the trash or
permanently delete anything. All generated logs and manifests live in logs/.

WORKFLOW
--------
  1.  python gdrive_organizer.py scan
      Read-only crawl. Writes inventory.csv (your baseline - keep it),
      report.html, proposed_structure.html and duplicates.csv.

  2.  Open report.html, then the interactive tree. Adjust destinations,
      export mapping.csv.

  3.  python gdrive_organizer.py preview mapping.csv
      Writes preview.html listing every operation. Changes nothing.

  4.  python gdrive_organizer.py apply mapping.csv --execute
      Executes, with a live progress bar, a full audit log, and an automatic
      check that every moved item is still present and untrashed. (The
      full-crawl comparison against the baseline is the `verify` command.)

  5.  python gdrive_organizer.py quarantine --execute
      Moves duplicates into "Duplicates/" with a manifest recording where
      each original lives. Nothing is deleted.

  6.  python gdrive_organizer.py review
      Opens a local page to inspect each quarantined item next to its
      original, and trash the ones you confirm.

Any run is reversible:
      python gdrive_organizer.py undo <log>.jsonl --execute

SETUP
-----
  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

  Google Cloud Console:
    1. Enable the Drive API
       https://console.cloud.google.com/apis/library/drive.googleapis.com
    2. Consent screen  https://console.cloud.google.com/auth/branding
    3. Add yourself as a test user
       https://console.cloud.google.com/auth/audience
    4. Create an OAuth client, type "Desktop app"
       https://console.cloud.google.com/auth/clients
    5. Download JSON, rename to credentials.json, place beside this script.

  On Windows run it as "python gdrive_organizer.py ...", never
  "./gdrive_organizer.py" - the shebang picks the MSYS python3, which does
  not have the dependencies.
"""


# ========================================================================
# core
# ========================================================================


from __future__ import annotations

import argparse
import bisect
import csv
import html
import io
import json
import os
import random
import re
import secrets
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Every generated file — undo logs, action logs, manifests — lives here,
# not in the root. It is the tool's database.
LOG_DIR = "logs"

FIELDS = (
    "nextPageToken, files("
    "id, name, mimeType, parents, size, quotaBytesUsed, modifiedTime, "
    "createdTime, owners(emailAddress,displayName), trashed, shared, "
    "md5Checksum, webViewLink)"
)

_IMPORT_ERROR: Optional[str] = None
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError as _e:
    _IMPORT_ERROR = str(_e)

    class HttpError(Exception):  # type: ignore[no-redef]
        pass


def require_sdk() -> None:
    if _IMPORT_ERROR is None:
        return
    sys.exit(
        f"This command needs the Google Drive SDK, not importable from this "
        f"interpreter.\n  interpreter: {sys.executable}\n"
        f"  error:       {_IMPORT_ERROR}\n\n"
        f"  pip install google-api-python-client google-auth-httplib2 "
        f"google-auth-oauthlib\n\n"
        f"On Windows/Git Bash, if pip says 'already satisfied' you are running "
        f"the wrong Python.\nUse 'python gdrive_organizer.py ...', not "
        f"'./gdrive_organizer.py'."
    )


def get_service(credentials_file: str, token_file: str):
    require_sdk()
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as err:
                print(f"  stored token could not be refreshed "
                      f"({type(err).__name__}) — signing in again")
                creds = None
        if not creds or not creds.valid:
            if not os.path.exists(credentials_file):
                sys.exit(f"Missing {credentials_file}. See SETUP in the "
                         f"script header.")
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def with_backoff(fn, *args, _max_tries: int = 8, **kwargs):
    """Retry rate limits, 5xx, and network failures with exponential backoff."""
    import socket
    import ssl
    NET = (TimeoutError, socket.timeout, socket.gaierror,
           ConnectionError, ssl.SSLError, OSError)
    for attempt in range(_max_tries):
        try:
            return fn(*args, **kwargs)
        except NET as err:
            if attempt == _max_tries - 1:
                raise
            sleep = (2 ** attempt) + random.uniform(0, 1)
            Progress.note(f"network {type(err).__name__}; retry in {sleep:.0f}s")
            time.sleep(sleep)
        except HttpError as err:
            status = getattr(err.resp, "status", None)
            text = str(err)
            retriable = status in (429, 500, 502, 503, 504) or (
                status == 403 and ("ateLimit" in text or "uotaExceeded" in text))
            if not retriable or attempt == _max_tries - 1:
                raise
            sleep = (2 ** attempt) + random.uniform(0, 1)
            Progress.note(f"rate limit {status}; retry in {sleep:.0f}s")
            time.sleep(sleep)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Live terminal progress
# ---------------------------------------------------------------------------

class Progress:
    """Single-line live progress. Honest about what it can know.

    The Drive API does not reveal a total up front when crawling, so `total`
    is optional: without it you get a live count and rate, with it a real
    percentage bar. It never invents a percentage.
    """

    _active: Optional["Progress"] = None

    def __init__(self, label: str, total: Optional[int] = None,
                 quiet: bool = False):
        self.label = label
        self.total = total
        self.n = 0
        self.start = time.time()
        self.detail = ""
        self.quiet = quiet or not sys.stdout.isatty()
        self._last_draw = 0.0
        Progress._active = self

    @classmethod
    def note(cls, msg: str) -> None:
        """Print a message without corrupting the progress line."""
        if cls._active and not cls._active.quiet:
            sys.stdout.write("\r" + " " * 100 + "\r")
        print(f"    {msg}")
        if cls._active:
            cls._active.draw(force=True)

    def step(self, k: int = 1, detail: str = "") -> None:
        self.n += k
        if detail:
            self.detail = detail
        self.draw()

    def draw(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        el = now - self.start
        rate = self.n / el if el > 0 else 0
        if self.quiet:
            return
        if self.total:
            frac = min(self.n / self.total, 1.0) if self.total else 0
            width = 28
            filled = int(width * frac)
            bar = "#" * filled + "." * (width - filled)
            left = self.total - self.n
            eta = left / rate if rate > 0 else 0
            line = (f"\r  [{bar}] {frac*100:5.1f}%  "
                    f"{self.n:,}/{self.total:,}  "
                    f"{left:,} left  ETA {_dur(eta)}")
        else:
            line = (f"\r  {self.label}: {self.n:,} items  "
                    f"{rate:.0f}/s  elapsed {_dur(el)}")
        if self.detail:
            room = 118 - len(line)
            if room > 12:
                d = self.detail
                if len(d) > room:
                    d = "..." + d[-(room - 3):]
                line += "  " + d
        sys.stdout.write(line.ljust(119)[:119])
        sys.stdout.flush()

    def done(self, msg: str = "") -> None:
        el = time.time() - self.start
        if not self.quiet:
            sys.stdout.write("\r" + " " * 119 + "\r")
            sys.stdout.flush()
        print(f"  {msg or self.label}: {self.n:,} in {_dur(el)}")
        if Progress._active is self:
            Progress._active = None


def _dur(sec: float) -> str:
    sec = int(max(sec, 0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def crawl(service, quiet: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    root = with_backoff(
        service.files().get(fileId="root", fields="id").execute)["id"]
    files: List[Dict[str, Any]] = []
    token = None
    p = Progress("scanning Drive", quiet=quiet)
    while True:
        resp = with_backoff(
            service.files().list(
                q="trashed = false", fields=FIELDS, pageSize=1000,
                pageToken=token, corpora="user").execute)
        batch = resp.get("files", [])
        files.extend(batch)
        last = batch[-1]["name"] if batch else ""
        p.step(len(batch), detail=last)
        token = resp.get("nextPageToken")
        if not token:
            break
    p.done("scanned")
    return files, root


def build_paths(files: List[Dict[str, Any]], root_id: str) -> Dict[str, str]:
    by_id = {f["id"]: f for f in files}
    cache: Dict[str, str] = {root_id: ""}

    def resolve(fid: str, seen: Optional[Set[str]] = None) -> str:
        if fid in cache:
            return cache[fid]
        seen = seen or set()
        if fid in seen:
            cache[fid] = "<cycle>"
            return cache[fid]
        seen.add(fid)
        node = by_id.get(fid)
        if node is None:
            cache[fid] = "<external>"
            return cache[fid]
        parents = node.get("parents") or []
        if not parents:
            cache[fid] = f"<orphan>/{node['name']}"
            return cache[fid]
        pp = resolve(parents[0], seen)
        cache[fid] = f"{pp}/{node['name']}" if pp else node["name"]
        return cache[fid]

    p = Progress("resolving paths", total=len(files))
    for f in files:
        resolve(f["id"])
        p.step()
    p.done("paths resolved")
    return cache


def to_row(f: Dict[str, Any], path: str) -> Dict[str, Any]:
    owners = f.get("owners") or [{}]
    size = f.get("size") or f.get("quotaBytesUsed") or 0
    parents = f.get("parents") or []
    return {
        "file_id": f["id"],
        "name": f.get("name", ""),
        "path": path,
        "depth": path.count("/") if path else 0,
        "is_folder": f.get("mimeType") == FOLDER_MIME,
        "mime_type": f.get("mimeType", ""),
        "size_bytes": int(size),
        "created": f.get("createdTime", ""),
        "modified": f.get("modifiedTime", ""),
        "owner": owners[0].get("emailAddress", ""),
        "shared": bool(f.get("shared", False)),
        "md5": f.get("md5Checksum", ""),
        "parent_id": parents[0] if parents else "",
        "multi_parent": len(parents) > 1,
        "link": f.get("webViewLink", ""),
    }


def write_inventory(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def load_inventory(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        sys.exit(f"Inventory not found: {path}\nRun 'scan' first.")
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["is_folder"] = str(r.get("is_folder", "")).strip().lower() == "true"
        try:
            r["size_bytes"] = int(r.get("size_bytes") or 0)
        except ValueError:
            r["size_bytes"] = 0
        r["depth"] = int(r.get("depth") or 0)
    return rows


def mb(n: float) -> str:
    return f"{n / 1_048_576:.1f} MB"


def gb(n: float) -> str:
    return f"{n / 1_073_741_824:.2f} GB"


def human(n: float) -> str:
    if n >= 1_073_741_824:
        return gb(n)
    if n >= 1_048_576:
        return mb(n)
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{int(n)} B"


# ========================================================================
# analyze
# ========================================================================



# Path components that mark machine-generated content.
ARTIFACT_SEGMENTS = {
    "node_modules", ".git", "vendor", "bower_components", "dist", "build",
    ".next", ".nuxt", "__pycache__", ".cache", "venv", ".venv",
    "site-packages", ".idea", ".vscode", "obj", ".gradle", "target",
    "coverage", ".terraform", "pods", ".svn", "wp-includes", "wp-admin",
    ".pytest_cache", "elm-stuff", "bin", "lib",
}

# Locations that mark a scratch or throwaway copy. When the same content sits
# in two places, the copy in a folder like this loses.
JUNK_MARKERS = (
    "rearrange", "cleaning left", "___test", "desktop-cleanup", "--old",
    "unconfirmed", "/old/", " - copy", "copy of ", "/tmp/", "/temp/",
    "_backup/", "backup of ",
)

DOC_MIMES = {
    "application/pdf", "application/msword", "application/rtf", "text/rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.form",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "image/vnd.adobe.photoshop", "image/x-photoshop",
}

CODE_MIMES = {
    "text/javascript", "application/x-httpd-php", "text/css", "text/html",
    "text/texmacs", "application/json", "application/xml", "text/xml",
    "application/x-shellscript", "text/x-python", "application/x-sh",
}

# Starting categories. Plain names, no numeric prefixes — you rename, add and
# delete these in the tree page, and the names you choose are what appear in
# Drive. (Drive sorts folders alphabetically. If you want a specific order,
# the tree page has a toggle that adds 10/20/30-style prefixes.)
BUCKETS = {
    "review": "Review",
    "active": "Active",
    "projects": "Projects",
    "media": "Media",
    "reference": "Reference",
    "archive": "Archive",
    "duplicates": "Duplicates",
}

RECENT_DAYS = 365
OLD_DAYS = 365 * 3


def is_artifact(path: str) -> bool:
    return any(seg.lower() in ARTIFACT_SEGMENTS
               for seg in path.split("/")[:-1])


def junk_score(path: str) -> int:
    low = path.lower()
    return sum(1 for m in JUNK_MARKERS if m in low)


def name_has_slash(r: Dict[str, Any]) -> bool:
    return "/" in (r.get("name") or "")


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def find_duplicates(rows: List[Dict[str, Any]],
                    min_size: int = 0) -> Dict[str, Any]:
    """Every duplicate, plus the minimal set of moves that covers them.

    Only files owned by this account participate. A copy owned by someone
    else can vanish the moment they unshare it, so it can neither be
    quarantined (moving it would change what collaborators see) nor be
    counted on as a survivor. The account is inferred as the most common
    owner in the inventory, which also works offline in `retree`.

    `min_size` (bytes) excludes small files from the analysis entirely —
    both as removal candidates and as proof of a folder's redundancy.

    Returns:
      sets              all content-hash groups with >1 member (complete)
      redundant_folders folders whose every file survives elsewhere
      individual        duplicate files not covered by a redundant folder
      wasted_bytes      recoverable space
    """
    files = [r for r in rows if not r["is_folder"]]
    files.sort(key=lambda r: r["path"])
    paths = [r["path"] for r in files]

    owner_counts = Counter(r.get("owner") or "" for r in files
                           if r.get("owner"))
    me = owner_counts.most_common(1)[0][0] if owner_counts else ""

    def eligible(r: Dict[str, Any]) -> bool:
        return (bool(r.get("md5"))
                and r["size_bytes"] >= min_size
                and (not me or (r.get("owner") or "") == me))

    by_md5: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in files:
        if eligible(r):
            by_md5[r["md5"]].append(r)
    sets = {h: g for h, g in by_md5.items() if len(g) > 1}
    wasted = sum(g[0]["size_bytes"] * (len(g) - 1) for g in sets.values())

    folders = [r for r in rows if r["is_folder"] and r["path"]]
    candidates: List[Tuple[str, List[Dict[str, Any]], int]] = []
    p = Progress("examining folders", total=len(folders))
    for fol in folders:
        p.step()
        prefix = fol["path"] + "/"
        lo = bisect.bisect_left(paths, prefix)
        hi = bisect.bisect_left(paths, prefix + "￿")
        under = files[lo:hi]
        if not under:
            continue
        if any(not eligible(r) for r in under):
            continue           # unhashable / too small / not mine -> no proof
        if any(name_has_slash(r) for r in under):
            continue                       # '/' in a name breaks prefix logic
        candidates.append((fol["path"], under,
                           sum(r["size_bytes"] for r in under)))
    p.done("folders examined")

    # Offer scratch-named copies up first so the clean copy is the survivor.
    # Depth ascending as the final tie-break so a parent is always considered
    # before its own children — otherwise a child can be accepted and then its
    # parent accepted too, producing two overlapping moves for one subtree.
    candidates.sort(key=lambda c: (-junk_score(c[0]), -c[2],
                                   c[0].count("/")))

    accepted: List[Dict[str, Any]] = []
    doomed: Set[str] = set()
    for path, under, total in candidates:
        if any(path == a["path"] or path.startswith(a["path"] + "/")
               for a in accepted):
            continue
        prefix = path + "/"
        ok = True
        example = ""
        for r in under:
            mine = junk_score(r["path"])
            # NEVER TRADE DOWN. A surviving copy must live somewhere at least
            # as good as this one. Without this, a live working folder gets
            # quarantined because a stale copy of its contents exists in a
            # backup — the clean copy dies and the scratch copy survives.
            twins = [t for t in by_md5.get(r["md5"], [])
                     if t["file_id"] != r["file_id"]
                     and not t["path"].startswith(prefix)
                     and t["file_id"] not in doomed
                     and junk_score(t["path"]) <= mine]
            if not twins:
                ok = False
                break
            if not example:
                example = twins[0]["path"]
        if ok:
            accepted.append({"path": path, "files": len(under),
                             "bytes": total, "example_survivor": example,
                             "members": under})
            doomed.update(r["file_id"] for r in under)

    covered = [a["path"] + "/" for a in accepted]

    def is_covered(pth: str) -> bool:
        return any(pth.startswith(c) for c in covered)

    surviving: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in files:
        if eligible(r) and not is_covered(r["path"]):
            surviving[r["md5"]].append(r)

    individual: List[Dict[str, Any]] = []
    for h, group in surviving.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: (junk_score(r["path"]),
                                               r["depth"], r["path"]))
        keeper = ordered[0]
        for dup in ordered[1:]:
            individual.append({**dup, "survivor": keeper["path"],
                               "survivor_id": keeper["file_id"]})

    # Hard invariant: no content hash may lose every copy.
    gone = {d["file_id"] for d in individual}
    for a in accepted:
        gone.update(r["file_id"] for r in a["members"])
    for h, group in by_md5.items():
        if group and all(r["file_id"] in gone for r in group):
            raise AssertionError(
                f"internal error: hash {h} would lose every copy "
                f"({group[0]['path']})")

    return {
        "sets": sets,
        "redundant_folders": accepted,
        "individual": individual,
        "wasted_bytes": wasted,
        "covered_files": sum(a["files"] for a in accepted) + len(individual),
        "covered_bytes": (sum(a["bytes"] for a in accepted)
                          + sum(d["size_bytes"] for d in individual)),
        "operations": len(accepted) + len(individual),
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _stats(under: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(under)
    if not n:
        return {"files": 0, "bytes": 0, "artifact": 0.0, "doc": 0.0,
                "media": 0.0, "code": 0.0, "newest": None}
    art = sum(1 for r in under if is_artifact(r["path"]))
    doc = sum(1 for r in under if r.get("mime_type") in DOC_MIMES)
    med = sum(1 for r in under
              if (r.get("mime_type") or "").startswith(("image/", "video/",
                                                        "audio/")))
    cod = sum(1 for r in under if r.get("mime_type") in CODE_MIMES)
    dates = [d for d in (_parse(r.get("modified", "")) for r in under) if d]
    return {
        "files": n,
        "bytes": sum(r["size_bytes"] for r in under),
        "artifact": art / n,
        "doc": doc / n,
        "media": med / n,
        "code": cod / n,
        "newest": max(dates) if dates else None,
    }


def _bucket_for(name: str, st: Dict[str, Any], now: datetime,
                dup_ratio: float) -> Tuple[str, str, str]:
    """-> (bucket key, reason, confidence)"""
    age_days = (now - st["newest"]).days if st["newest"] else None
    recent = age_days is not None and age_days <= RECENT_DAYS
    ancient = age_days is not None and age_days > OLD_DAYS

    if st["files"] == 0:
        return "review", "empty folder", "high"

    if st["artifact"] >= 0.40 or st["code"] >= 0.60:
        return ("archive",
                f"{st['artifact']*100:.0f}% machine-generated files "
                f"(node_modules/.git/vendor), {st['code']*100:.0f}% source",
                "high")

    if dup_ratio >= 0.90:
        return ("duplicates",
                f"{dup_ratio*100:.0f}% of its content exists elsewhere",
                "high")

    if st["media"] >= 0.70:
        return ("media",
                f"{st['media']*100:.0f}% photo/video/audio, "
                f"{human(st['bytes'])}", "high")

    if st["doc"] >= 0.40 and recent:
        return ("active",
                f"document-heavy ({st['doc']*100:.0f}%), modified "
                f"{age_days} days ago", "high")

    if st["doc"] >= 0.40 and ancient:
        return ("reference",
                f"document-heavy ({st['doc']*100:.0f}%), untouched for "
                f"{age_days // 365} years", "medium")

    if recent and st["files"] >= 20:
        return ("projects",
                f"mixed content, active ({age_days} days ago), "
                f"{st['files']:,} files", "medium")

    if ancient:
        return ("archive",
                f"untouched for {age_days // 365} years", "medium")

    return ("review",
            f"no clear signal — {st['files']:,} files, "
            f"{st['doc']*100:.0f}% documents, {st['media']*100:.0f}% media",
            "low")


def folder_tree(rows: List[Dict[str, Any]],
                dup: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every folder worth showing, with rolled-up counts, for drill-down.

    Folders *inside* a machine-generated directory are omitted — nobody needs
    to browse into node_modules — but their contents still count towards the
    totals of the folder that contains them, so the numbers stay honest.
    """
    files = [r for r in rows if not r["is_folder"]]
    files.sort(key=lambda r: r["path"])
    paths = [r["path"] for r in files]

    dup_ids: Set[str] = set()
    for a in dup["redundant_folders"]:
        dup_ids.update(r["file_id"] for r in a["members"])
    dup_ids.update(d["file_id"] for d in dup["individual"])

    def visible(p: str) -> bool:
        # keep 'x/node_modules' itself, drop anything beneath it
        segs = p.split("/")
        return not any(s.lower() in ARTIFACT_SEGMENTS for s in segs[:-1])

    folders = [r for r in rows if r["is_folder"] and r["path"]
               and visible(r["path"])]
    folders.sort(key=lambda r: r["path"].lower())

    out: List[Dict[str, Any]] = []
    p = Progress("building tree", total=len(folders))
    for fol in folders:
        prefix = fol["path"] + "/"
        lo = bisect.bisect_left(paths, prefix)
        hi = bisect.bisect_left(paths, prefix + "￿")
        under = files[lo:hi]
        st = _stats(under)
        direct = sum(1 for r in under if "/" not in r["path"][len(prefix):])
        out.append({
            "id": fol["file_id"],
            "path": fol["path"],
            "name": fol["name"],
            "parent": fol["path"].rsplit("/", 1)[0] if "/" in fol["path"]
                      else "",
            "files": st["files"],
            "direct": direct,
            "bytes": st["bytes"],
            "dups": sum(1 for r in under if r["file_id"] in dup_ids),
            "newest": st["newest"].strftime("%Y-%m-%d") if st["newest"] else "",
            "doc": round(st["doc"] * 100),
            "media": round(st["media"] * 100),
            "art": round(st["artifact"] * 100),
        })
        p.step()
    p.done("tree built")
    return out


def classify(rows: List[Dict[str, Any]],
             dup: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Propose a destination for each movable unit.

    Movable units are top-level folders and loose files at the root — moving
    a folder carries everything inside it, so this keeps the operation count
    proportional to what you can actually review.
    """
    now = datetime.now(timezone.utc)
    files = [r for r in rows if not r["is_folder"]]
    files.sort(key=lambda r: r["path"])
    paths = [r["path"] for r in files]

    dup_ids: Set[str] = set()
    for a in dup["redundant_folders"]:
        dup_ids.update(r["file_id"] for r in a["members"])
    dup_ids.update(d["file_id"] for d in dup["individual"])

    tops = [r for r in rows if r["is_folder"] and r["path"]
            and "/" not in r["path"]]
    loose = [r for r in files if "/" not in r["path"]]

    proposals: List[Dict[str, Any]] = []

    for fol in sorted(tops, key=lambda r: r["path"].lower()):
        prefix = fol["path"] + "/"
        lo = bisect.bisect_left(paths, prefix)
        hi = bisect.bisect_left(paths, prefix + "￿")
        under = files[lo:hi]
        st = _stats(under)
        ndup = sum(1 for r in under if r["file_id"] in dup_ids)
        dr = ndup / st["files"] if st["files"] else 0.0
        key, reason, conf = _bucket_for(fol["name"], st, now, dr)
        proposals.append({
            "file_id": fol["file_id"],
            "name": fol["name"],
            "current_path": fol["path"],
            "kind": "folder",
            "bucket": key,
            "target_path": BUCKETS[key],
            "reason": reason,
            "confidence": conf,
            "files": st["files"],
            "bytes": st["bytes"],
            "duplicate_files": ndup,
            "newest": st["newest"].strftime("%Y-%m-%d") if st["newest"] else "",
            "artifact_pct": round(st["artifact"] * 100),
            "doc_pct": round(st["doc"] * 100),
            "media_pct": round(st["media"] * 100),
        })

    for r in sorted(loose, key=lambda r: r["name"].lower()):
        st = _stats([r])
        _, evidence, _ = _bucket_for(
            r["name"], st, now, 1.0 if r["file_id"] in dup_ids else 0.0)
        # A loose file at the root ALWAYS goes to Review, whatever its type
        # or age. One file carries no context about where it belongs — the
        # type tells you what it is, never what it is for. These are few and
        # individually meaningful, so they are worth a human decision each.
        # Auto-filing them is precisely the kind of confident guess that is
        # wrong in the cases that matter.
        key, conf = "review", "low"
        reason = f"loose file at root — needs your decision ({evidence})"
        proposals.append({
            "file_id": r["file_id"],
            "name": r["name"],
            "current_path": r["path"],
            "kind": "file",
            "bucket": key,
            "target_path": BUCKETS[key] + "/Root Files",
            "reason": reason,
            "confidence": conf,
            "files": 1,
            "bytes": r["size_bytes"],
            "duplicate_files": 1 if r["file_id"] in dup_ids else 0,
            "newest": (r.get("modified") or "")[:10],
            "artifact_pct": 0,
            "doc_pct": round(st["doc"] * 100),
            "media_pct": round(st["media"] * 100),
        })

    return proposals


# ========================================================================
# render
# ========================================================================



CSS = """
:root{color-scheme:light;--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;
--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--ring:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--warn:#fab219;--crit:#d03b3b;
--good:#0ca30c}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--plane:#0d0d0d;
--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
*{box-sizing:border-box}
body{margin:0;padding:36px 22px 80px;background:var(--plane);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:29px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:19px;margin:40px 0 12px;letter-spacing:-.01em}
h3{font-size:14px;margin:22px 0 8px;color:var(--ink2);text-transform:uppercase;
letter-spacing:.05em}
.sub{color:var(--ink2);margin:0 0 26px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
padding:20px;margin:14px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:11px;margin:18px 0}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:11px;
padding:15px}
.tile .v{font-size:24px;font-weight:650;letter-spacing:-.02em;display:block}
.tile .l{font-size:12px;color:var(--ink2);display:block;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:4px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--grid);
vertical-align:top}
th{color:var(--ink2);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.04em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--grid);padding:1.5px 5px;border-radius:4px}
pre{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
padding:14px;overflow-x:auto;font:12.5px/1.6 ui-monospace,Menlo,monospace}
a{color:var(--s1)}
.pill{display:inline-block;font-size:11px;font-weight:650;padding:2px 7px;
border-radius:5px;border:1px solid var(--ring);white-space:nowrap}
.p-high{color:#0a7a0a;background:rgba(12,163,12,.13)}
.p-medium{color:#8a5a00;background:rgba(250,178,25,.16)}
.p-low{color:#9c2b2b;background:rgba(208,59,59,.14)}
@media (prefers-color-scheme:dark){.p-high{color:#4ec94e}.p-medium{color:#fab219}
.p-low{color:#e88}}
.barwrap{display:flex;height:38px;border-radius:8px;overflow:hidden;gap:2px;
margin:10px 0 6px}
.seg{display:flex;align-items:center;justify-content:center;font-size:12px;
font-weight:600;color:#fff;white-space:nowrap;overflow:hidden}
.legend{display:flex;flex-wrap:wrap;gap:15px;font-size:12.5px;color:var(--ink2)}
.key{display:inline-flex;align-items:center;gap:6px}
.dot{width:10px;height:10px;border-radius:3px;flex:none}
.note{border-left:3px solid var(--s2);padding:2px 0 2px 13px;color:var(--ink2);
margin:13px 0}
.note .pbar{height:6px;max-width:420px}
.note .pfill{background:var(--s2)}
.foot{margin-top:52px;padding-top:16px;border-top:1px solid var(--grid);
color:var(--muted);font-size:12.5px}
.btn{display:inline-block;background:var(--s1);color:#fff;border:0;
border-radius:8px;padding:9px 16px;font:inherit;font-weight:600;cursor:pointer;
text-decoration:none}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--ring)}
input,select{font:inherit;background:var(--surface);color:var(--ink);
border:1px solid var(--ring);border-radius:7px;padding:5px 8px}
"""


def _h(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _page(title: str, body: str, extra_css: str = "",
          script: str = "") -> str:
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_h(title)}</title><style>{CSS}{extra_css}</style></head>"
            f"<body><div class=wrap>{body}</div>"
            f"{'<script>' + script + '</script>' if script else ''}"
            f"</body></html>")


# ---------------------------------------------------------------------------

def report_html(rows, dup, proposals, out_dir, tree_name) -> str:
    files = [r for r in rows if not r["is_folder"]]
    folders = [r for r in rows if r["is_folder"]]
    total = sum(r["size_bytes"] for r in files)
    now = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")

    tops = sorted((f for f in folders if "/" not in f["path"]),
                  key=lambda f: f["path"].lower())
    by_top = {p["current_path"]: p for p in proposals
              if p["kind"] == "folder"}

    b = []
    A = b.append
    A(f"<h1>Google Drive — Scan Report</h1>")
    A(f"<p class=sub>{now} &middot; read-only scan, nothing was modified</p>")

    A("<div class=tiles>")
    for v, l in ((f"{len(files):,}", "files"),
                 (f"{len(folders):,}", "folders"),
                 (gb(total), "total size"),
                 (f"{max((r['depth'] for r in rows), default=0)}",
                  "max nesting depth")):
        A(f"<div class=tile><span class=v>{v}</span>"
          f"<span class=l>{l}</span></div>")
    A("</div>")

    # ---- concentration
    if tops:
        ranked = sorted(
            ((t["path"], by_top.get(t["path"], {}).get("files", 0),
              by_top.get(t["path"], {}).get("bytes", 0)) for t in tops),
            key=lambda x: -x[1])
        biggest = ranked[0]
        share = biggest[1] / len(files) * 100 if files else 0
        if share > 40:
            rest = len(files) - biggest[1]
            A("<h2>Concentration</h2><div class=card>")
            A(f"<span style='font-size:44px;font-weight:680;letter-spacing:"
              f"-.03em;line-height:1'>{share:.1f}%</span>")
            A(f"<p style='margin:8px 0 0;color:var(--ink2)'>of all files sit "
              f"in one folder — <code>{_h(biggest[0])}</code></p>")
            A(f"<div class=barwrap role=img aria-label='{_h(biggest[0])} "
              f"{biggest[1]} files, everything else {rest} files'>")
            A(f"<div class=seg style='flex:0 0 {share:.1f}%;background:"
              f"var(--s1)'>{_h(biggest[0])} — {biggest[1]:,}</div>")
            A(f"<div class=seg style='flex:0 0 {100-share:.1f}%;background:"
              f"var(--s3)'>{rest:,}</div></div>")
            A(f"<div class=legend><span class=key><span class=dot "
              f"style='background:var(--s1)'></span>{_h(biggest[0])} — "
              f"{biggest[1]:,} files</span><span class=key><span class=dot "
              f"style='background:var(--s3)'></span>Everything else — "
              f"{rest:,} files</span></div></div>")

    # ---- duplicates
    A("<h2>Duplicates</h2>")
    A(f"<p style='color:var(--ink2);margin-top:0'>Every duplicate is listed "
      f"here regardless of size. Execution uses the largest safe unit, so "
      f"complete coverage costs "
      f"<strong>{dup['operations']:,} operations</strong>, not "
      f"{dup['covered_files']:,}.</p>")
    A("<div class=tiles>")
    for v, l in ((f"{len(dup['sets']):,}", "duplicate sets"),
                 (f"{dup['covered_files']:,}", "redundant copies"),
                 (gb(dup["wasted_bytes"]), "space they waste"),
                 (f"{dup['operations']:,}", "moves to quarantine all")):
        A(f"<div class=tile><span class=v>{v}</span>"
          f"<span class=l>{l}</span></div>")
    A("</div>")

    if dup["redundant_folders"]:
        A("<h3>Fully redundant folders</h3>")
        A("<p style='color:var(--ink2);margin-top:0'>Every file inside these "
          "provably exists elsewhere. Each is one move.</p>")
        A("<table><thead><tr><th>Folder</th><th class=num>Files</th>"
          "<th class=num>Size</th><th>A surviving copy lives at</th>"
          "</tr></thead><tbody>")
        for a in sorted(dup["redundant_folders"],
                        key=lambda a: -a["bytes"])[:60]:
            A(f"<tr><td><code>{_h(a['path'])}</code></td>"
              f"<td class=num>{a['files']:,}</td>"
              f"<td class=num>{human(a['bytes'])}</td>"
              f"<td><code>{_h(a['example_survivor'])}</code></td></tr>")
        A("</tbody></table>")
        if len(dup["redundant_folders"]) > 60:
            A(f"<p style='color:var(--muted);font-size:12.5px'>"
              f"{len(dup['redundant_folders'])-60} more in "
              f"duplicates.csv</p>")

    if dup["individual"]:
        A("<h3>Individual duplicate files</h3>")
        A("<table><thead><tr><th>File</th><th class=num>Size</th>"
          "<th>Original kept at</th></tr></thead><tbody>")
        for d in sorted(dup["individual"],
                        key=lambda d: -d["size_bytes"])[:60]:
            A(f"<tr><td><code>{_h(d['path'])}</code></td>"
              f"<td class=num>{human(d['size_bytes'])}</td>"
              f"<td><code>{_h(d['survivor'])}</code></td></tr>")
        A("</tbody></table>")
        if len(dup["individual"]) > 60:
            A(f"<p style='color:var(--muted);font-size:12.5px'>"
              f"{len(dup['individual'])-60} more in duplicates.csv</p>")

    # ---- proposal
    A("<h2>Proposed structure</h2>")
    conf = defaultdict(int)
    for p in proposals:
        conf[p["confidence"]] += 1
    A(f"<p style='color:var(--ink2);margin-top:0'>{len(proposals):,} movable "
      f"units — top-level folders and loose root files. Moving a folder "
      f"carries its contents, which is why this is a short list rather than "
      f"{len(files):,} decisions.</p>")
    A(f"<p><a class=btn href='{_h(tree_name)}'>Open the interactive tree "
      f"&rarr;</a></p>")
    A(f"<p style='color:var(--ink2)'>"
      f"<span class='pill p-high'>{conf['high']} confident</span> "
      f"<span class='pill p-medium'>{conf['medium']} probable</span> "
      f"<span class='pill p-low'>{conf['low']} needs your decision</span>"
      f"</p>")

    A("<div class=note><strong>How these were decided:</strong> from evidence "
      "only — file types, modification dates, duplication, and the proportion "
      "of machine-generated content. The script never guesses what a folder "
      "means from its name. Anything without a clear signal goes to "
      "<code>Review</code> for you to place.</div>")

    A("<table><thead><tr><th>Current</th><th>Proposed</th><th>Confidence</th>"
      "<th class=num>Files</th><th class=num>Size</th><th>Why</th></tr>"
      "</thead><tbody>")
    order = {"low": 0, "medium": 1, "high": 2}
    for p in sorted(proposals, key=lambda p: (order[p["confidence"]],
                                              -p["bytes"])):
        A(f"<tr><td><code>{_h(p['current_path'])}</code></td>"
          f"<td><code>{_h(p['target_path'])}</code></td>"
          f"<td><span class='pill p-{p['confidence']}'>"
          f"{p['confidence']}</span></td>"
          f"<td class=num>{p['files']:,}</td>"
          f"<td class=num>{human(p['bytes'])}</td>"
          f"<td style='color:var(--ink2)'>{_h(p['reason'])}"
          + (f"<br><span style='color:var(--s2)'>"
             f"{p['duplicate_files']:,} duplicate files inside</span>"
             if p["duplicate_files"] else "")
          + "</td></tr>")
    A("</tbody></table>")

    A("<h2>What happens next</h2>")
    A("<pre># 1. review and adjust assignments in the tree, export mapping.csv\n"
      "# 2. see exactly what would move — writes preview.html, changes nothing\n"
      "python gdrive_organizer.py preview mapping.csv\n\n"
      "# 3. run it (refuses more than --max-ops at once)\n"
      "python gdrive_organizer.py apply mapping.csv --execute\n\n"
      "# 4. review quarantined duplicates in a browser and trash what you "
      "confirm\n"
      "python gdrive_organizer.py review</pre>")

    A("<div class=foot>Read-only scan. No file was moved, renamed, trashed or "
      "deleted. Every duplicate above is reported for information — nothing "
      "acts on it until you run <code>apply</code>.</div>")
    return _page("Drive Scan Report", "".join(b))


# ---------------------------------------------------------------------------

TREE_CSS = """
.node{border-bottom:1px solid var(--grid)}
.row{display:flex;align-items:center;gap:10px;padding:8px 6px;cursor:pointer}
.row:hover{background:var(--grid)}
.tw{width:14px;color:var(--muted);flex:none;font-size:11px}
.nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.meta{color:var(--ink2);font-size:12.5px;white-space:nowrap;
font-variant-numeric:tabular-nums}
.kids{display:none;padding-left:22px}
.kids.open{display:block}
.bucket{background:var(--surface);border:1px solid var(--ring);
border-radius:11px;margin:10px 0;overflow:hidden}
.bhead{display:flex;align-items:center;gap:12px;padding:13px 15px;
background:var(--surface);border-bottom:1px solid var(--grid)}
.bhead .t{font-weight:650;flex:1}
.detail{padding:10px 14px 14px 40px;color:var(--ink2);font-size:13px;
display:none;background:var(--plane)}
.detail.open{display:block}
.toolbar{position:sticky;top:0;background:var(--plane);padding:12px 0;
border-bottom:1px solid var(--grid);z-index:5;display:flex;gap:10px;
align-items:center;flex-wrap:wrap}
.dupflag{color:var(--s2);font-size:12px}
.steps{display:flex;align-items:stretch;gap:8px;margin:18px 0;flex-wrap:wrap}
.step{flex:1;min-width:180px;display:flex;gap:10px;background:var(--surface);
border:1px solid var(--ring);border-radius:11px;padding:13px;font-size:13px;
line-height:1.45;color:var(--ink2)}
.step strong{color:var(--ink)}
.sn{flex:none;width:22px;height:22px;border-radius:50%;background:var(--s1);
color:#fff;font-weight:700;font-size:12px;display:flex;align-items:center;
justify-content:center}
.arrow{display:flex;align-items:center;color:var(--muted)}
.dirty{font-size:13px;font-weight:600;color:var(--s2)}
.assign{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.assign label{font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em}
.changed{box-shadow:inset 3px 0 0 var(--s2)}
@media (max-width:820px){.arrow{display:none}}
.catbox{background:var(--surface);border:1px solid var(--ring);
border-radius:12px;padding:16px;margin:18px 0}
.cathead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
margin-bottom:12px}
.cats{display:flex;flex-wrap:wrap;gap:9px}
.cat{display:flex;align-items:center;gap:8px;border:1px solid var(--ring);
border-radius:9px;padding:5px 8px;background:var(--plane)}
.cat input{border:0;background:transparent;width:130px;font-weight:600;
padding:2px 3px}
.cat input:focus{outline:2px solid var(--s1);border-radius:5px}
.cnt{font-size:12px;color:var(--muted);white-space:nowrap}
.cat .x{border:0;background:transparent;color:var(--muted);cursor:pointer;
font-size:17px;line-height:1;padding:0 2px}
.cat .x:hover{color:var(--crit)}
.lock{font-size:11px;color:var(--muted)}
.addrow{display:flex;gap:9px;margin-top:13px;align-items:center}
.stay{font-size:11px;font-weight:650;color:var(--muted);border:1px solid
var(--ring);border-radius:5px;padding:2px 7px}
.browse{margin-top:12px;border-top:1px solid var(--grid);padding-top:10px}
.bh{font-size:11.5px;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em;margin-bottom:6px}
.kid{border-left:1px solid var(--grid);margin-left:3px}
.krow{display:flex;align-items:center;gap:8px;padding:4px 0 4px 8px}
.krow:hover{background:var(--grid)}
.ktw{width:13px;color:var(--muted);cursor:pointer;font-size:11px;flex:none;
user-select:none}
.knm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-size:13px}
.kmeta{font-size:12px;color:var(--ink2);white-space:nowrap;
font-variant-numeric:tabular-nums}
.ksel{font-size:12px;padding:3px 5px;max-width:190px}
.kkids{padding-left:14px}
"""


def tree_html(proposals: List[Dict[str, Any]], dup: Dict[str, Any],
              nodes: Optional[List[Dict[str, Any]]] = None) -> str:
    data = json.dumps(proposals)
    buckets = json.dumps(list(BUCKETS.values()))
    tree = json.dumps(nodes or [])

    body = f"""
<h1>Organise your Drive</h1>
<p class=sub>A worksheet. Nothing here is connected to your Drive — define
your categories, assign things to them, then export your decisions.</p>

<div class=steps>
  <div class=step><span class=sn>1</span><div><strong>Name your categories
    </strong><br>Rename these, delete what you don't want, add your own —
    Finance, Clients, whatever fits.</div></div>
  <div class=arrow>&rarr;</div>
  <div class=step><span class=sn>2</span><div><strong>Assign</strong><br>
    Expand a row, pick its category. Leave anything you're unsure about.
    </div></div>
  <div class=arrow>&rarr;</div>
  <div class=step><span class=sn>3</span><div><strong>Export &amp; preview
    </strong><br>Writes <code>mapping.csv</code>; <code>preview</code> then
    shows the exact result for you to approve.</div></div>
  <div class=arrow>&rarr;</div>
  <div class=step><span class=sn>4</span><div><strong>Apply</strong><br>
    <code>apply --execute</code> is the only step that touches Drive.
    </div></div>
</div>

<div class=catbox>
  <div class=cathead>
    <strong>Your categories</strong>
    <span style="color:var(--ink2);font-size:13px">these become real folders
      in your Drive</span>
    <label style="margin-left:auto;font-size:13px;color:var(--ink2)">
      <input type=checkbox id=prefix onchange=togglePrefix()>
      number them to control sort order</label>
  </div>
  <div id=cats class=cats></div>
  <div class=addrow>
    <input id=newcat placeholder="new category, e.g. Finance"
      onkeydown="if(event.key==='Enter')addCat()" style="width:260px">
    <button class="btn ghost" onclick=addCat()>Add category</button>
  </div>
</div>

<div class=note><strong>Review is the safe default.</strong> Anything left
there produces no operation at all — it stays exactly where it is. You do not
have to empty it.</div>

<div class=toolbar>
  <button class=btn onclick=expandAll()>Expand all</button>
  <button class="btn ghost" onclick=collapseAll()>Collapse all</button>
  <label style="color:var(--ink2);font-size:13px">
    <input type=checkbox id=onlyunsure onchange=render()> only items needing
    a decision</label>
  <span id=dirty class=dirty style="margin-left:auto"></span>
  <button class=btn id=expbtn onclick=exportCsv()>Export decisions &rarr;
    mapping.csv</button>
</div>

<div id=out></div>

<div class=foot>The exported file lands in your Downloads folder. Move it next
to the script, then run
<code>python gdrive_organizer.py preview mapping.csv</code> — that produces
the final report for you to approve before anything runs.<br>
<code>python gdrive_organizer.py retree</code> rebuilds this page from the
existing scan without re-crawling Drive.</div>
"""

    script = f"""
const DATA = {data};
let CATS = {buckets};
const NODES = {tree};
const ORIG = DATA.map(p=>p.target_path);
const SUB = {{}};                       // explicit sub-folder assignments
const BYPARENT = {{}};
NODES.forEach(n=>{{ (BYPARENT[n.parent] = BYPARENT[n.parent]||[]).push(n); }});
Object.values(BYPARENT).forEach(a=>a.sort((x,y)=>y.bytes-x.bytes));
let dirty = false, exported = false, prefixed = false;

function human(n){{
  if(n>=1073741824) return (n/1073741824).toFixed(2)+' GB';
  if(n>=1048576) return (n/1048576).toFixed(1)+' MB';
  if(n>=1024) return Math.round(n/1024)+' KB';
  return n+' B';
}}
function esc(s){{return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);}}
function root(p){{ return String(p||'').split('/')[0]; }}
function opts(cur){{
  const list = CATS.slice();
  const r = root(cur);
  if(list.indexOf(r) === -1 && r) list.unshift(r);
  return list.map(b=>'<option'+(b===r?' selected':'')+'>'+esc(b)+
    '</option>').join('');
}}

// ---- categories -----------------------------------------------------------
function usage(c){{ return DATA.filter(p=>root(p.target_path)===c).length; }}
function drawCats(){{
  document.getElementById('cats').innerHTML = CATS.map((c,i)=>{{
    const n = usage(c);
    const locked = (stripNum(c)==='Review');
    return '<div class=cat><input value="'+esc(c)+'" onchange="renameCat('+i+
      ',this.value)"'+(locked?' title="Review is the untouched bucket"':'')+
      '><span class=cnt>'+n+' item'+(n===1?'':'s')+'</span>'+
      (locked?'<span class=lock>kept</span>'
             :'<button class=x onclick="delCat('+i+')" title="remove">&times;'+
              '</button>')+'</div>';
  }}).join('');
}}
function stripNum(s){{ return String(s||'').replace(/^\\d+\\s+/,''); }}
function renameCat(i,v){{
  v = (v||'').trim(); if(!v){{ drawCats(); return; }}
  const old = CATS[i];
  if(CATS.indexOf(v)!==-1 && CATS[i]!==v){{
    alert('A category called "'+v+'" already exists.'); drawCats(); return; }}
  CATS[i] = v;
  // Carry every assignment across, including nested paths like "Clients/HR".
  DATA.forEach(p=>{{
    if(root(p.target_path)===old){{
      const rest = p.target_path.slice(old.length);
      p.target_path = v + rest;
    }}
  }});
  dirty = true; drawCats(); render();
}}
function addCat(){{
  const el = document.getElementById('newcat');
  const v = (el.value||'').trim();
  if(!v) return;
  if(CATS.indexOf(v)!==-1){{ alert('That category already exists.'); return; }}
  CATS.push(v); el.value=''; drawCats(); render();
}}
function delCat(i){{
  const c = CATS[i], n = usage(c);
  if(stripNum(c)==='Review'){{ alert('Review is where untouched items live.');
    return; }}
  if(n && !confirm(n+' item'+(n===1?' is':'s are')+' assigned to "'+c+
     '".\\n\\nRemove it and send '+(n===1?'it':'them')+' back to Review?'))
    return;
  const rev = CATS.find(x=>stripNum(x)==='Review') || 'Review';
  DATA.forEach(p=>{{ if(root(p.target_path)===c) p.target_path = rev; }});
  CATS.splice(i,1); dirty = true; drawCats(); render();
}}
function togglePrefix(){{
  prefixed = document.getElementById('prefix').checked;
  // Number by position in the list: 00, 10, 20... Drag order is the list
  // order, so what you see here is the order they appear in Drive.
  CATS = CATS.map((c,i)=>{{
    const bare = stripNum(c);
    return prefixed ? String(i*10).padStart(2,'0') + ' ' + bare : bare;
  }});
  DATA.forEach(p=>{{
    const bare = stripNum(root(p.target_path));
    const match = CATS.find(c=>stripNum(c)===bare);
    if(match) p.target_path = match + p.target_path.slice(
      root(p.target_path).length);
  }});
  dirty = true; drawCats(); render();
}}

// ---- items ----------------------------------------------------------------
function render(){{
  const only = document.getElementById('onlyunsure').checked;
  const groups = {{}};
  DATA.forEach((p,i)=>{{
    if(only && p.confidence==='high') return;
    (groups[p.target_path] = groups[p.target_path]||[]).push([p,i]);
  }});
  // Folders you pulled out of somewhere else appear alongside the top-level
  // items, so the target structure you are building is visible in one place.
  const extra = {{}};
  Object.keys(SUB).forEach(path=>{{
    const t = SUB[path];
    if(!t || stripNum(root(t))==='Review') return;
    const n = NODES.find(x=>x.path===path);
    if(!n) return;
    (extra[t] = extra[t]||[]).push(n);
  }});
  const keys = Array.from(new Set(
    Object.keys(groups).concat(Object.keys(extra)))).sort();
  let h='';
  if(!keys.length) h='<p style="color:var(--ink2)">Nothing to show.</p>';
  keys.forEach(k=>{{
    const items = groups[k] || [];
    const pulled = extra[k] || [];
    const files = items.reduce((a,[p])=>a+p.files,0)
                + pulled.reduce((a,n)=>a+n.files,0);
    const bytes = items.reduce((a,[p])=>a+p.bytes,0)
                + pulled.reduce((a,n)=>a+n.bytes,0);
    const untouched = stripNum(root(k))==='Review';
    h += '<div class=bucket><div class=bhead><span class=t>'+esc(k)+
      '</span>'+(untouched?'<span class=stay>stays put</span>':'')+
      '<span class=meta>'+(items.length+pulled.length)+' items &middot; '+
      files.toLocaleString()+' files &middot; '+human(bytes)+'</span></div>';
    pulled.forEach(n=>{{
      h += '<div class=node><div class=row>'+
        '<span class=tw>&#8618;</span>'+
        '<span class=nm>'+esc(n.path)+'/</span>'+
        '<span class=meta>'+n.files.toLocaleString()+' files &middot; '+
        human(n.bytes)+'</span>'+
        '<span class="pill p-medium">pulled out</span>'+
        '<button class=x style="border:0;background:transparent;'+
        'color:var(--muted);cursor:pointer;font-size:17px" title="undo" '+
        'onclick="unassign(\\''+enc(n.path)+'\\')">&times;</button>'+
        '</div></div>';
    }});
    items.forEach(([p,i])=>{{
      h += '<div class=node><div class=row onclick="tog('+i+')">'+
        '<span class=tw id=tw'+i+'>&#9656;</span>'+
        '<span class=nm>'+esc(p.current_path)+
        (p.kind==='folder'?'/':'')+'</span>'+
        '<span class=meta>'+p.files.toLocaleString()+' files &middot; '+
        human(p.bytes)+'</span>'+
        '<span class="pill p-'+p.confidence+'">'+p.confidence+'</span>'+
        '</div><div class=detail id=d'+i+'>'+
        '<div><strong>Why:</strong> '+esc(p.reason)+'</div>'+
        (p.duplicate_files?'<div class=dupflag>'+
          p.duplicate_files.toLocaleString()+
          ' files inside are duplicates of content elsewhere</div>':'')+
        '<div style="margin-top:6px">Last modified '+esc(p.newest||'unknown')+
        ' &middot; '+p.doc_pct+'% documents &middot; '+p.media_pct+
        '% media &middot; '+p.artifact_pct+'% machine-generated</div>'+
        '<div class=assign onclick="event.stopPropagation()">'+
        '<label>Category</label>'+
        '<select onchange="setCat('+i+',this.value)">'+opts(p.target_path)+
        '</select>'+
        '<label>Subfolder (optional)</label>'+
        '<input value="'+esc(p.target_path.split('/').slice(1).join('/'))+
        '" onchange="setSub('+i+',this.value)" style="width:200px" '+
        'placeholder="e.g. Finance/2026">'+
        (ORIG[i]!==p.target_path?'<span class=dirty>was '+
          esc(ORIG[i])+'</span>':'')+
        '</div>'+
        (p.kind==='folder' && kidsOf(p.current_path).length
          ? '<div class=browse><div class=bh>Inside this folder — assign any '+
            'subfolder separately if you want it somewhere else</div>'+
            '<div id=k'+i+'></div></div>'
          : '')+
        '</div></div>';
    }});
    h += '</div>';
  }});
  document.getElementById('out').innerHTML = h;
  drawCats(); updateDirty();
}}
function tog(i){{
  const d = document.getElementById('d'+i);
  d.classList.toggle('open');
  const t = document.getElementById('tw'+i);
  const open = d.classList.contains('open');
  t.innerHTML = open ? '&#9662;' : '&#9656;';
  if(open){{
    const box = document.getElementById('k'+i);
    if(box && !box.dataset.done){{
      box.dataset.done = '1';
      drawKids(box, DATA[i].current_path);
    }}
  }}
}}

// ---- drill-down -----------------------------------------------------------
function kidsOf(path){{ return BYPARENT[path] || []; }}
function assignedAncestor(path){{
  // nearest ancestor with an explicit non-Review destination
  const parts = path.split('/');
  for(let n=parts.length-1; n>0; n--){{
    const anc = parts.slice(0,n).join('/');
    const t = SUB[anc] !== undefined ? SUB[anc] : topTarget(anc);
    if(t && stripNum(root(t))!=='Review') return {{path:anc, target:t}};
  }}
  return null;
}}
function topTarget(path){{
  const p = DATA.find(x=>x.current_path===path);
  return p ? p.target_path : null;
}}
function destOf(path){{
  if(SUB[path] !== undefined) return SUB[path];
  const t = topTarget(path);
  return t !== null ? t : '';
}}
function drawKids(box, parentPath){{
  const kids = kidsOf(parentPath);
  if(!kids.length){{ box.innerHTML=''; return; }}
  box.innerHTML = kids.map(k=>{{
    const dest = destOf(k.path);
    const anc = assignedAncestor(k.path);
    const moved = dest && stripNum(root(dest))!=='Review';
    const grand = kidsOf(k.path).length;
    return '<div class=kid>'+
      '<div class=krow>'+
      '<span class=ktw onclick="kidTog(this,\\''+enc(k.path)+'\\')">'+
        (grand?'&#9656;':'&nbsp;')+'</span>'+
      '<span class=knm title="'+esc(k.path)+'">'+esc(k.name)+'/</span>'+
      '<span class=kmeta>'+k.files.toLocaleString()+' files &middot; '+
        human(k.bytes)+(k.dups?' &middot; <span class=dupflag>'+
        k.dups.toLocaleString()+' dup</span>':'')+'</span>'+
      '<select class=ksel onchange="assignSub(\\''+enc(k.path)+
        '\\',this.value,this)">'+
        '<option value="">'+(anc?'moves with '+esc(anc.path.split('/').pop())
                                :'leave where it is')+'</option>'+
        CATS.map(c=>'<option'+(moved&&root(dest)===c?' selected':'')+'>'+
          esc(c)+'</option>').join('')+
      '</select>'+
      '</div><div class=kkids></div></div>';
  }}).join('');
}}
function enc(s){{ return String(s).replace(/\\\\/g,'\\\\\\\\')
  .replace(/'/g,"\\\\'"); }}
function kidTog(el, path){{
  const holder = el.closest('.kid').querySelector('.kkids');
  if(holder.dataset.done){{
    holder.style.display = holder.style.display==='none' ? '' : 'none';
    el.innerHTML = holder.style.display==='none' ? '&#9656;' : '&#9662;';
    return;
  }}
  holder.dataset.done='1'; el.innerHTML='&#9662;';
  drawKids(holder, path);
}}
function unassign(path){{ delete SUB[path]; dirty = true; render(); }}
function assignSub(path, val, el){{
  if(!val){{ delete SUB[path]; }}
  else {{
    SUB[path] = val;
    // The deepest decision wins. Any ancestor that was going to move is set
    // back to "stays put", otherwise it would carry this folder along with it
    // and your choice here would be silently ignored.
    const parts = path.split('/');
    const rev = CATS.find(x=>stripNum(x)==='Review') || 'Review';
    let cleared = [];
    for(let n=parts.length-1; n>0; n--){{
      const anc = parts.slice(0,n).join('/');
      if(SUB[anc] !== undefined && stripNum(root(SUB[anc]))!=='Review'){{
        SUB[anc] = rev; cleared.push(anc);
      }}
      const top = DATA.find(x=>x.current_path===anc);
      if(top && stripNum(root(top.target_path))!=='Review'){{
        top.target_path = rev; cleared.push(anc);
      }}
    }}
    if(cleared.length){{
      alert('"'+parts[parts.length-1]+'" will now move on its own.\\n\\n'+
        'Because of that, '+cleared.join(', ')+' is set to stay where it is '+
        '— otherwise it would carry this folder with it and your choice here '+
        'would be ignored.');
    }}
  }}
  dirty = true; render();
  const host = el.closest('.browse');
  if(host){{ const box = host.querySelector('div[id^=k]');
    if(box){{ box.dataset.done='1';
      drawKids(box, box.id.replace('k','') !== '' ?
        DATA[parseInt(box.id.slice(1))].current_path : ''); }} }}
}}
function setCat(i,v){{
  const sub = DATA[i].target_path.split('/').slice(1).join('/');
  DATA[i].target_path = sub ? v+'/'+sub : v;
  dirty = true; render();
}}
function setSub(i,v){{
  v = (v||'').replace(/^\\/+|\\/+$/g,'');
  DATA[i].target_path = v ? root(DATA[i].target_path)+'/'+v
                          : root(DATA[i].target_path);
  dirty = true; render();
}}
function expandAll(){{ document.querySelectorAll('.detail')
  .forEach(d=>d.classList.add('open')); }}
function collapseAll(){{ document.querySelectorAll('.detail')
  .forEach(d=>d.classList.remove('open')); }}
function changeCount(){{
  return DATA.filter((p,i)=>p.target_path!==ORIG[i]).length;
}}
function updateDirty(){{
  const n = changeCount(), el = document.getElementById('dirty');
  if(!el) return;
  if(exported && !dirty){{
    el.innerHTML = '<span style="color:var(--good)">exported \\u2713</span>';
  }} else if(n){{
    el.textContent = n + ' change' + (n===1?'':'s') + ' not yet exported';
  }} else {{ el.textContent = ''; }}
}}
window.addEventListener('beforeunload', function(e){{
  if(dirty && changeCount()){{ e.preventDefault(); e.returnValue=''; }}
}});
function exportCsv(){{
  const q = s => '"'+String(s==null?'':s).replace(/"/g,'""')+'"';
  let out = 'file_id,name,current_path,kind,target_path,confidence,reason\\n';
  DATA.forEach(p=>{{
    out += [q(p.file_id),q(p.name),q(p.current_path),q(p.kind),
            q(p.target_path),q(p.confidence),q(p.reason)].join(',')+'\\n';
  }});
  Object.keys(SUB).forEach(path=>{{
    const t = SUB[path];
    if(!t || stripNum(root(t))==='Review') return;
    const n = NODES.find(x=>x.path===path);
    if(!n) return;
    out += [q(n.id),q(n.name),q(path),q('folder'),q(t),q('manual'),
            q('assigned by you in the tree')].join(',')+'\\n';
  }});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([out],{{type:'text/csv'}}));
  a.download='mapping.csv'; a.click();
  exported = true; dirty = false; updateDirty();
}}
render();
"""
    return _page("Organise your Drive", body, TREE_CSS, script)


def preview_html(ops: List[Dict[str, Any]], creates: List[str],
                 warnings: List[str]) -> str:
    b = []
    A = b.append
    A("<h1>Preview</h1>")
    A(f"<p class=sub>{len(ops):,} operations &middot; nothing has been "
      f"modified</p>")
    A("<div class=tiles>")
    kinds = defaultdict(int)
    for o in ops:
        kinds[o["action"]] += 1
    for k, v in sorted(kinds.items()):
        A(f"<div class=tile><span class=v>{v:,}</span>"
          f"<span class=l>{k}</span></div>")
    A(f"<div class=tile><span class=v>{len(creates):,}</span>"
      f"<span class=l>folders created</span></div></div>")

    if warnings:
        A("<h2>Warnings</h2><div class=card>")
        for w in warnings[:40]:
            A(f"<div style='color:var(--crit)'>! {_h(w)}</div>")
        A("</div>")

    if creates:
        A("<h2>New folders</h2><pre>")
        for c in creates:
            A(_h(c) + "\n")
        A("</pre>")

    A("<h2>Every operation</h2>")
    A("<p style='color:var(--ink2);margin-top:0'>"
      "<input id=q oninput=flt() placeholder='filter…' style='width:320px'>"
      "</p>")
    A("<table id=t><thead><tr><th>#</th><th>Action</th><th>From</th>"
      "<th>To</th></tr></thead><tbody>")
    for i, o in enumerate(ops, start=1):
        A(f"<tr><td class=num>{i}</td><td>{_h(o['action'])}</td>"
          f"<td><code>{_h(o['current_path'])}</code></td>"
          f"<td><code>{_h(o['target_display'])}</code></td></tr>")
    A("</tbody></table>")
    A("<div class=foot>Nothing here has run. Add <code>--execute</code> to "
      "apply.</div>")
    script = ("function flt(){var v=document.getElementById('q').value"
              ".toLowerCase();document.querySelectorAll('#t tbody tr')"
              ".forEach(function(r){r.style.display="
              "r.textContent.toLowerCase().indexOf(v)>-1?'':'none'})}")
    return _page("Preview", "".join(b), "", script)


# ========================================================================
# execute
# ========================================================================



QUARANTINE = "Duplicates"

# Preview limits and conversions for the live UI's file viewer.
MAX_PREVIEW_BYTES = 25 * 1024 * 1024
MAX_ZIP_BYTES = 100 * 1024 * 1024      # listing only reads the directory
MAX_ZIP_ENTRIES = 2000
MAX_SHARE_DETAIL = 1500                # per-item permission lookups
EXPORT_AS = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/pdf",
    "application/vnd.google-apps.drawing": "image/png",
    "application/vnd.google-apps.script": "application/json",
}


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------

def load_mapping(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        sys.exit(f"Mapping not found: {path}\n"
                 f"Export it from the tree page produced by 'scan'.")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("Mapping file is empty.")
    need = {"file_id", "target_path"}
    missing = need - set(rows[0])
    if missing:
        sys.exit(f"Mapping missing columns: {sorted(missing)}")
    return rows


def validate(ops: List[Dict[str, Any]]) -> List[str]:
    problems: List[str] = []
    seen: Dict[str, int] = {}
    dest: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for i, o in enumerate(ops, start=2):
        fid = o["file_id"]
        if not fid:
            problems.append(f"row {i}: missing file_id")
            continue
        if fid in seen:
            problems.append(f"row {i}: file_id {fid} already on row {seen[fid]}")
        seen[fid] = i
        if not o["target_path"]:
            problems.append(f"row {i}: missing target_path")
        dest[(o["target_path"], (o["name"] or "").lower())].append(fid)
    for (t, n), ids in dest.items():
        if len(ids) > 1:
            problems.append(f"collision: {len(ids)} items land at '{t}/{n}'")
    return problems


def build_ops(mapping: List[Dict[str, str]],
              skip_buckets: Tuple[str, ...] = ("Review",)) -> List[Dict]:
    ops = []
    for m in mapping:
        target = (m.get("target_path") or "").strip("/ ")
        if not target:
            continue
        # The tree page can prefix categories with sort numbers ("10 Review"),
        # so compare with the number stripped — Review means "do not touch"
        # whatever it is currently labelled.
        top = re.sub(r"^\d+\s+", "", target.split("/")[0])
        if top in skip_buckets:
            continue
        name = (m.get("name") or "").strip()
        ops.append({
            "file_id": (m.get("file_id") or "").strip(),
            "name": name,
            "current_path": (m.get("current_path") or "").strip("\r\n"),
            "action": "move",
            "target_path": target,
            "target_display": f"{target}/{name}",
        })
    return ops


# ---------------------------------------------------------------------------

class Folders:
    """Resolves 'A/B/C' to a folder ID, creating what is missing.

    Existing folders are found and reused — a category named like a real
    folder resolves to that folder rather than creating a twin.

    `call` lets a caller serialize API access (the live UI passes a locked
    wrapper); `on_create` is invoked as on_create(path, folder_id) for every
    folder actually created, so runs can record creations in their undo log.
    """

    def __init__(self, service, root_id: str, execute: bool,
                 call: Optional[Callable[[Any], Dict[str, Any]]] = None,
                 on_create: Optional[Callable[[str, str], None]] = None):
        self.s = service
        self.root = root_id
        self.execute = execute
        self.call = call or (lambda req: with_backoff(req.execute))
        self.on_create = on_create
        self.cache: Dict[str, str] = {"": root_id}
        self.created: List[str] = []

    def resolve(self, path: str) -> str:
        path = path.strip("/ ")
        if path in self.cache:
            return self.cache[path]
        parent_path, _, leaf = path.rpartition("/")
        parent = self.resolve(parent_path) if parent_path else self.root
        safe = leaf.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"name = '{safe}' and mimeType = '{FOLDER_MIME}' "
             f"and '{parent}' in parents and trashed = false")
        hits = self.call(self.s.files().list(
            q=q, fields="files(id)", pageSize=5)).get("files", [])
        if hits:
            self.cache[path] = hits[0]["id"]
            return self.cache[path]
        self.created.append(path)
        if not self.execute:
            self.cache[path] = f"<new:{path}>"
            return self.cache[path]
        made = self.call(self.s.files().create(
            body={"name": leaf, "mimeType": FOLDER_MIME,
                  "parents": [parent]}, fields="id"))
        self.cache[path] = made["id"]
        if self.on_create:
            self.on_create(path, made["id"])
        return made["id"]


# ---------------------------------------------------------------------------

def run_apply(args, ops: List[Dict[str, Any]],
              log_prefix: str = "apply") -> Optional[str]:
    problems = validate(ops)
    if problems:
        print(f"\nPlan validation found {len(problems)} problem(s):")
        for p in problems[:40]:
            print(f"  ! {p}")
        if not getattr(args, "force", False):
            sys.exit("\nRefusing to continue. Fix the mapping, or --force.")
        print("\n--force given; continuing.\n")

    cap = getattr(args, "max_ops", 15)
    if cap and len(ops) > cap and not getattr(args, "allow_large", False):
        sys.exit(
            f"\nThis plan has {len(ops):,} operations, above the safety cap of "
            f"{cap}.\n\n"
            f"Large batches are hard to review and hard to reason about when "
            f"something\ngoes wrong. Either split the mapping into smaller "
            f"pieces, raise the cap\nwith --max-ops N, or override once with "
            f"--allow-large.\n"
        )

    service = get_service(args.credentials, args.token)
    root = with_backoff(
        service.files().get(fileId="root", fields="id").execute)["id"]
    folders = Folders(service, root, args.execute)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.abspath(
        os.path.join(LOG_DIR, f"{log_prefix}_{stamp}.jsonl"))
    log = open(log_path, "w", encoding="utf-8") if args.execute else None
    if log:
        def _log_mkdir(pth: str, fid: str) -> None:
            log.write(json.dumps({"op": "mkdir", "file_id": fid,
                                  "path": pth}) + "\n")
            log.flush()
        folders.on_create = _log_mkdir

    mode = "EXECUTING" if args.execute else "DRY RUN — nothing will change"
    print(f"\n{'=' * 70}\n{mode}  ({len(ops):,} operations)\n{'=' * 70}\n")

    p = Progress("applying", total=len(ops))
    done = 0
    errors: List[str] = []
    for o in ops:
        try:
            parent = folders.resolve(o["target_path"])
            if parent == o["file_id"]:
                raise ValueError(
                    "the destination resolves to this folder itself — "
                    "cannot move a folder into itself")
            if args.execute:
                meta = with_backoff(service.files().get(
                    fileId=o["file_id"], fields="id,name,parents").execute)
                old = meta.get("parents", [])
                params: Dict[str, Any] = {
                    "fileId": o["file_id"], "body": {}, "addParents": parent}
                if old:
                    params["removeParents"] = ",".join(old)
                with_backoff(service.files().update(**params).execute)
                log.write(json.dumps({
                    "op": "move", "file_id": o["file_id"],
                    "name": meta.get("name"), "old_parents": old,
                    "new_parent": parent,
                    "from": o["current_path"],
                    "to": o["target_display"]}) + "\n")
                log.flush()
            done += 1
        except Exception as err:
            errors.append(f"{o['current_path']}: {type(err).__name__}: {err}")
            Progress.note(f"ERROR {o['current_path']}: {err}")
        p.step(detail=o["current_path"])
    p.done("applied")

    if log:
        log.close()

    print(f"\n  completed        {done:,}/{len(ops):,}")
    print(f"  folders created  {len(folders.created):,}")
    for c in folders.created[:25]:
        print(f"      {c}")
    if errors:
        print(f"\n  {len(errors)} error(s):")
        for e in errors[:20]:
            print(f"      ! {e}")

    if not args.execute:
        print("\nDry run only. Nothing was modified.")
        return None

    print(f"\n  audit log        {log_path}")
    print(f"  reverse with     python {os.path.basename(sys.argv[0])} "
          f"undo {os.path.relpath(log_path)} --execute")

    print(f"\n{'=' * 70}\nVERIFYING the {len(ops):,} item(s) this run "
          f"touched\n{'=' * 70}")
    quick_verify(service, ops)
    print(f"  (a full baseline comparison is: python "
          f"{os.path.basename(sys.argv[0])} verify)")
    return log_path


def quick_verify(service, ops: List[Dict[str, Any]]) -> bool:
    """Confirm each item this run touched still exists and is not trashed.

    Scoped to the moved items only — checking 15 moves must not cost a
    full re-crawl of a 190k-file Drive. The complete comparison against
    the scan baseline remains available as the `verify` command.
    """
    p = Progress("verifying", total=len(ops))
    bad: List[str] = []
    for o in ops:
        try:
            meta = with_backoff(service.files().get(
                fileId=o["file_id"], fields="id,trashed").execute)
            if meta.get("trashed"):
                bad.append(f"{o['current_path']}: found in trash")
        except Exception as err:
            bad.append(f"{o['current_path']}: {type(err).__name__}: {err}")
        p.step()
    p.done("verified")
    if bad:
        print(f"\n  ATTENTION — {len(bad)} item(s) not accounted for:")
        for m in bad[:20]:
            print(f"      ! {m}")
        return False
    print(f"  PASS — all {len(ops):,} touched items are present and "
          f"untrashed.")
    return True


# ---------------------------------------------------------------------------

def verify_against(service, baseline_path: str) -> bool:
    base = load_inventory(baseline_path)
    base_files = {r["file_id"]: r for r in base if not r["is_folder"]}
    base_folders = {r["file_id"]: r for r in base if r["is_folder"]}

    current, _ = crawl(service)
    cur_ids = {f["id"] for f in current}

    missing = [r for i, r in {**base_files, **base_folders}.items()
               if i not in cur_ids]

    in_trash: List[Dict] = []
    gone: List[Dict] = []
    unknown: List[Dict] = []
    if missing:
        p = Progress("checking missing items", total=len(missing))
        for r in missing:
            try:
                meta = with_backoff(service.files().get(
                    fileId=r["file_id"], fields="id,trashed").execute)
                (in_trash if meta.get("trashed") else unknown).append(r)
            except HttpError as err:
                if getattr(err.resp, "status", None) == 404:
                    gone.append(r)
                else:
                    unknown.append(r)
            p.step()
        p.done("checked")

    mf = [r for r in missing if not r["is_folder"]]
    print(f"\n  baseline files             {len(base_files):,}")
    print(f"  present and not trashed    {len(base_files) - len(mf):,}")
    print(f"  in trash (recoverable)     "
          f"{sum(1 for r in in_trash if not r['is_folder']):,}")
    print(f"  PERMANENTLY GONE           "
          f"{sum(1 for r in gone if not r['is_folder']):,}")
    print(f"  indeterminate              {len(unknown):,}")

    if gone:
        os.makedirs(LOG_DIR, exist_ok=True)
        gone_csv = os.path.join(LOG_DIR, "verify_gone.csv")
        with open(gone_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file_id", "name", "path", "size_bytes"])
            for r in gone:
                w.writerow([r["file_id"], r["name"], r["path"],
                            r["size_bytes"]])
        print(f"\n  *** see {gone_csv}")

    print("\n" + "-" * 70)
    if not missing:
        print("  PASS — every baseline file and folder is present and "
              "untrashed.")
        ok = True
    elif not gone and not unknown:
        print(f"  RECOVERABLE — {len(in_trash)} item(s) in trash, nothing "
              f"permanently deleted.")
        ok = True
    else:
        print(f"  ATTENTION — {len(gone)} item(s) could not be found.")
        ok = False
    print("-" * 70)
    return ok


# ---------------------------------------------------------------------------

def run_undo(args) -> None:
    with open(args.log, encoding="utf-8") as fh:
        records = [json.loads(l) for l in fh if l.strip()]
    records.reverse()
    service = get_service(args.credentials, args.token)

    mode = "EXECUTING UNDO" if args.execute else "DRY RUN — nothing changes"
    print(f"\n{'=' * 70}\n{mode}  ({len(records):,} records)\n{'=' * 70}\n")

    p = Progress("reverting", total=len(records))
    ok = skipped = 0
    errors: List[str] = []
    for rec in records:
        fid = rec["file_id"]
        try:
            if rec.get("op") == "trash":
                if args.execute:
                    with_backoff(service.files().update(
                        fileId=fid, body={"trashed": False}).execute)
                ok += 1
            elif rec.get("op") == "untrash":
                # Reverse of a Trash-tab restore: put it back in the trash.
                if args.execute:
                    with_backoff(service.files().update(
                        fileId=fid, body={"trashed": True}).execute)
                ok += 1
            elif rec.get("op") == "unshare":
                # Restore a sharing permission removed in the Sharing tab.
                if args.execute:
                    perm = rec.get("permission") or {}
                    body_p: Dict[str, Any] = {
                        "role": perm.get("role", "reader"),
                        "type": perm.get("type", "user")}
                    if perm.get("emailAddress"):
                        body_p["emailAddress"] = perm["emailAddress"]
                    if perm.get("domain"):
                        body_p["domain"] = perm["domain"]
                    kwargs: Dict[str, Any] = {}
                    # Only valid for user/group shares; quietly restore.
                    if body_p["type"] in ("user", "group"):
                        kwargs["sendNotificationEmail"] = False
                    with_backoff(service.permissions().create(
                        fileId=fid, body=body_p, **kwargs).execute)
                ok += 1
            elif rec.get("op") == "mkdir":
                # A folder this run created: remove it again, but only if it
                # is empty — anything placed there since is left alone.
                if args.execute:
                    kids = with_backoff(service.files().list(
                        q=f"'{fid}' in parents and trashed = false",
                        fields="files(id)", pageSize=1).execute).get("files")
                    if kids:
                        skipped += 1
                        Progress.note(
                            f"kept folder {rec.get('path', '')} — not empty")
                    else:
                        with_backoff(service.files().update(
                            fileId=fid, body={"trashed": True}).execute)
                        ok += 1
                else:
                    ok += 1
            elif args.execute:
                cur = with_backoff(service.files().get(
                    fileId=fid, fields="id,name,parents").execute)
                cp = set(cur.get("parents") or [])
                op_ = set(rec.get("old_parents") or [])
                add, rem = op_ - cp, cp - op_
                if not add and not rem:
                    skipped += 1
                else:
                    params: Dict[str, Any] = {"fileId": fid, "body": {}}
                    if add:
                        params["addParents"] = ",".join(add)
                    if rem:
                        params["removeParents"] = ",".join(rem)
                    with_backoff(service.files().update(**params).execute)
                    ok += 1
            else:
                ok += 1
        except Exception as err:
            errors.append(f"{fid}: {type(err).__name__}: {err}")
            Progress.note(f"ERROR {fid}: {err}")
        p.step(detail=rec.get("from", ""))
    p.done("reverted")

    print(f"\n  reverted {ok:,}   already done {skipped:,}")
    if errors:
        print(f"  {len(errors)} failed — safe to re-run, completed records "
              f"are skipped:")
        for e in errors[:20]:
            print(f"      ! {e}")
    if not args.execute:
        print("\nDry run only. Add --execute to apply.")


# ---------------------------------------------------------------------------
# Duplicate review server
# ---------------------------------------------------------------------------

REVIEW_PAGE = """<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Quarantined duplicates</title><style>__CSS__
.item{background:var(--surface);border:1px solid var(--ring);border-radius:11px;
padding:14px 16px;margin:10px 0}
.item.done{opacity:.45}
.hd{display:flex;gap:12px;align-items:center}
.hd .n{flex:1;font-weight:600;min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.orig{margin-top:8px;font-size:13px;color:var(--ink2)}
.orig code{word-break:break-all}
.acts{display:flex;gap:8px;margin-top:11px}
.danger{background:var(--crit)}
.ok{color:var(--good);font-weight:600}
</style></head><body><div class=wrap>
<h1>Quarantined duplicates</h1>
<p class=sub>__COUNT__ items &middot; __SIZE__ &middot; every one has a
verified original elsewhere</p>
<div class=note><strong>Delete here means Drive trash</strong> — recoverable
for 30 days. This tool has no permanent-delete capability. Check the original
location first; open it in Drive if you want to be certain.</div>
<div class=toolbar style="position:sticky;top:0;background:var(--plane);
padding:12px 0;border-bottom:1px solid var(--grid);display:flex;gap:10px;
align-items:center">
<input id=q oninput=flt() placeholder="filter…" style="width:300px">
<span id=stat style="color:var(--ink2);font-size:13px"></span>
</div>
<div id=list></div>
<div class=foot>Close this tab and stop the script when you are done. Nothing
happens unless you click.</div>
</div><script>
const TOKEN = "__TOKEN__";
const ITEMS = __DATA__;
function human(n){if(n>=1073741824)return (n/1073741824).toFixed(2)+' GB';
if(n>=1048576)return (n/1048576).toFixed(1)+' MB';
if(n>=1024)return Math.round(n/1024)+' KB';return n+' B';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
function draw(){
  document.getElementById('list').innerHTML = ITEMS.map((it,i)=>
    '<div class=item id=i'+i+'><div class=hd><span class=n>'+esc(it.name)+
    '</span><span class=meta style="color:var(--ink2);font-size:13px">'+
    human(it.size)+'</span></div>'+
    '<div class=orig>quarantined from <code>'+esc(it.from)+'</code></div>'+
    '<div class=orig>original kept at <code>'+esc(it.original)+'</code></div>'+
    '<div class=acts>'+
    (it.link?'<a class="btn ghost" target=_blank href="'+esc(it.link)+
      '">Open in Drive</a>':'')+
    (it.original_link?'<a class="btn ghost" target=_blank href="'+
      esc(it.original_link)+'">Open the original</a>':'')+
    '<button class="btn danger" onclick="trash('+i+')">Move to trash</button>'+
    '<span id=s'+i+'></span></div></div>').join('');
  document.getElementById('stat').textContent =
    ITEMS.length+' remaining';
}
function trash(i){
  const it = ITEMS[i];
  if(!confirm('Move to Drive trash?\\n\\n'+it.name+
     '\\n\\nRecoverable for 30 days. The original at\\n'+it.original+
     '\\nis not affected.')) return;
  fetch('/trash',{method:'POST',headers:{'Content-Type':'application/json',
    'X-Auth':TOKEN},body:JSON.stringify({file_id:it.file_id})})
    .then(r=>r.json()).then(d=>{
      const s=document.getElementById('s'+i);
      if(d.ok){document.getElementById('i'+i).classList.add('done');
        s.innerHTML='<span class=ok>moved to trash</span>';}
      else{s.innerHTML='<span style="color:var(--crit)">'+esc(d.error)+
        '</span>';}
    }).catch(e=>{document.getElementById('s'+i).textContent='failed: '+e;});
}
function flt(){const v=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.item').forEach(function(r){
r.style.display=r.textContent.toLowerCase().indexOf(v)>-1?'':'none'})}
draw();
</script></body></html>"""


def run_review(args) -> None:
    import http.server
    import socketserver
    import threading
    import urllib.parse
    import webbrowser

    if not os.path.exists(args.manifest):
        sys.exit(f"No quarantine manifest at {args.manifest}.\n"
                 f"It is written by 'apply' when duplicates are quarantined.")
    with open(args.manifest, encoding="utf-8") as fh:
        items = json.load(fh)
    if not items:
        print("Manifest is empty — nothing to review.")
        return

    service = get_service(args.credentials, args.token)
    total = sum(i.get("size", 0) for i in items)
    # Session token: the page URL carries it once, every action must echo it.
    # Blocks other websites (CSRF) and DNS-rebinding pages from driving this
    # server; the Host check stops rebinding for reads too.
    auth = secrets.token_urlsafe(16)
    page = (REVIEW_PAGE.replace("__CSS__", CSS)
            .replace("__DATA__", json.dumps(items))
            .replace("__COUNT__", f"{len(items):,}")
            .replace("__SIZE__", gb(total))
            .replace("__TOKEN__", auth))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._local() or (q.get("t") or [""])[0] != auth:
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def do_POST(self):
            if not self._local() or self.headers.get("X-Auth") != auth:
                self.send_response(403)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            fid = body.get("file_id", "")
            out: Dict[str, Any]
            try:
                meta = with_backoff(service.files().get(
                    fileId=fid, fields="id,name").execute)
                with_backoff(service.files().update(
                    fileId=fid, body={"trashed": True}).execute)
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(os.path.join(LOG_DIR, "review_actions.jsonl"),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "op": "trash", "file_id": fid,
                        "name": meta.get("name"),
                        "at": datetime.now().isoformat(timespec="seconds"),
                    }) + "\n")
                print(f"  trashed  {meta.get('name')}")
                out = {"ok": True}
            except Exception as err:
                out = {"ok": False, "error": f"{type(err).__name__}: {err}"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/?t={auth}"
        print(f"\n  Review UI:  {url}")
        print(f"  {len(items):,} quarantined items, {gb(total)}")
        print("  Clicks move items to Drive trash (recoverable 30 days).")
        print("  Actions are logged to review_actions.jsonl.")
        print("\n  Press Ctrl+C when finished.\n")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Review closed.")


# ========================================================================
# main
# ========================================================================



HEADER = r"""
gdrive_organizer — scan, propose, apply, review.

  ui          live interface: browse your real Drive, annotate, execute
  scan        crawl Drive, write inventory + report.html + tree.html   READ ONLY
  preview     turn a mapping into preview.html                         READ ONLY
  apply       execute a mapping (needs --execute)
  quarantine  move duplicates to a review folder (needs --execute)
  review      browse quarantined duplicates and trash what you confirm
  verify      check every baseline file ID still exists                READ ONLY
  undo        reverse any apply or quarantine run

Nothing is ever permanently deleted. The strongest actions available are a
move, trashing a folder that is verifiably empty, and removing one sharing
permission — each on an explicit click, each logged, each reversible with
`undo`. Drive's trash is recoverable for 30 days.

All generated files (undo logs, manifests, the folder index) live in logs/.
"""


# ---------------------------------------------------------------------------

_GENERATED = (
    r"(?:ui|apply|quarantine)_\d{8}_\d{6}\.jsonl",
    r"(?:share|folder|review|trash)_actions\.jsonl",
    r"quarantine_manifest\.(?:json|md)",
    r"verify_gone\.csv",
)


def migrate_generated() -> None:
    """Move previously generated files out of the root into logs/."""
    moved = 0
    for fn in os.listdir("."):
        if not any(re.fullmatch(p, fn) for p in _GENERATED):
            continue
        os.makedirs(LOG_DIR, exist_ok=True)
        dest = os.path.join(LOG_DIR, fn)
        if not os.path.exists(dest):
            os.replace(fn, dest)
            moved += 1
    if moved:
        print(f"  (tidied {moved} generated file(s) into {LOG_DIR}/)")


def cmd_scan(args) -> None:
    os.makedirs(args.out_dir, exist_ok=True)
    service = get_service(args.credentials, args.token)
    print()
    files, root = crawl(service)
    paths = build_paths(files, root)
    rows = [to_row(f, paths.get(f["id"], "<unknown>")) for f in files]
    rows.sort(key=lambda r: r["path"].lower())
    for r in rows:
        r["is_folder"] = bool(r["is_folder"])

    inv = os.path.join(args.out_dir, "inventory.csv")
    write_inventory(rows, inv)

    print("\nAnalysing duplicates...")
    dup = find_duplicates(rows, args.dup_min_size)
    print("Classifying...")
    proposals = classify(rows, dup)
    nodes = folder_tree(rows, dup)

    dup_csv = os.path.join(args.out_dir, "duplicates.csv")
    with open(dup_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "path", "files", "size_bytes",
                    "original_survives_at"])
        for a in sorted(dup["redundant_folders"], key=lambda a: -a["bytes"]):
            w.writerow(["folder", a["path"], a["files"], a["bytes"],
                        a["example_survivor"]])
        for d in sorted(dup["individual"], key=lambda d: -d["size_bytes"]):
            w.writerow(["file", d["path"], 1, d["size_bytes"], d["survivor"]])

    tree_name = "proposed_structure.html"
    rep = os.path.join(args.out_dir, "report.html")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write(report_html(rows, dup, proposals, args.out_dir, tree_name))
    tre = os.path.join(args.out_dir, tree_name)
    with open(tre, "w", encoding="utf-8") as fh:
        fh.write(tree_html(proposals, dup, nodes))

    nf = sum(1 for r in rows if not r["is_folder"])
    print(f"\n{'=' * 70}")
    print(f"  {nf:,} files, {len(rows) - nf:,} folders, "
          f"{gb(sum(r['size_bytes'] for r in rows if not r['is_folder']))}")
    print(f"  {len(dup['sets']):,} duplicate sets wasting "
          f"{gb(dup['wasted_bytes'])} — "
          f"{dup['operations']:,} moves would quarantine all of them")
    print(f"  {len(proposals):,} movable units proposed")
    print(f"{'=' * 70}\n")
    for f in (inv, dup_csv, rep, tre):
        print(f"  {f}")
    print(f"\n  Open {rep} in your browser.")
    print(f"  KEEP {inv} — it is the baseline every later check uses.\n")


def cmd_retree(args) -> None:
    """Rebuild the report and tree from an existing scan. No crawl."""
    rows = load_inventory(args.inventory)
    print("\nAnalysing duplicates...")
    dup = find_duplicates(rows, args.dup_min_size)
    print("Classifying...")
    proposals = classify(rows, dup)
    nodes = folder_tree(rows, dup)
    out_dir = os.path.dirname(os.path.abspath(args.inventory))
    tree_name = "proposed_structure.html"
    rep = os.path.join(out_dir, "report.html")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write(report_html(rows, dup, proposals, out_dir, tree_name))
    tre = os.path.join(out_dir, tree_name)
    with open(tre, "w", encoding="utf-8") as fh:
        fh.write(tree_html(proposals, dup, nodes))
    print(f"\n  rebuilt from {args.inventory} — Drive was not contacted\n")
    print(f"  {rep}\n  {tre}\n")


def cmd_preview(args) -> None:
    ops = build_ops(load_mapping(args.mapping))
    if not ops:
        print("Nothing to do — every row targets Review, which is left "
              "untouched by design.")
        return
    warnings = validate(ops)
    creates = sorted({o["target_path"] for o in ops})
    out = args.out
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(preview_html(ops, creates, warnings))
    print(f"\n  {len(ops):,} operations, {len(creates)} destination folders")
    if warnings:
        print(f"  {len(warnings)} warning(s) — see the page")
    print(f"\n  {os.path.abspath(out)}")
    print("\n  Nothing has been modified. Open that file, then:")
    print(f"    python {os.path.basename(sys.argv[0])} apply "
          f"{args.mapping} --execute\n")


def cmd_apply(args) -> None:
    ops = build_ops(load_mapping(args.mapping))
    if not ops:
        print("Nothing to do.")
        return
    run_apply(args, ops, "apply")


def cmd_quarantine(args) -> None:
    rows = load_inventory(args.inventory)
    print("\nAnalysing duplicates...")
    dup = find_duplicates(rows, args.dup_min_size)
    by_path = {r["path"]: r for r in rows if r["is_folder"]}
    file_by_path = {r["path"]: r for r in rows if not r["is_folder"]}
    by_id = {r["file_id"]: r for r in rows}

    ops: List[Dict[str, Any]] = []
    manifest: List[Dict[str, Any]] = []
    for a in dup["redundant_folders"]:
        r = by_path.get(a["path"])
        if not r:
            continue
        surv = file_by_path.get(a["example_survivor"], {})
        ops.append({"file_id": r["file_id"], "name": r["name"],
                    "current_path": a["path"], "action": "move",
                    "target_path": f"{QUARANTINE}/folders",
                    "target_display": f"{QUARANTINE}/folders/{r['name']}"})
        manifest.append({"file_id": r["file_id"], "name": r["name"],
                         "from": a["path"], "original": a["example_survivor"],
                         "size": a["bytes"], "files": a["files"],
                         "link": r.get("link", ""),
                         "original_link": surv.get("link", "")})
    for d in dup["individual"]:
        surv = by_id.get(d.get("survivor_id", ""), {})
        ops.append({"file_id": d["file_id"], "name": d["name"],
                    "current_path": d["path"], "action": "move",
                    "target_path": f"{QUARANTINE}/files",
                    "target_display": f"{QUARANTINE}/files/{d['name']}"})
        manifest.append({"file_id": d["file_id"], "name": d["name"],
                         "from": d["path"], "original": d["survivor"],
                         "size": d["size_bytes"], "files": 1,
                         "link": d.get("link", ""),
                         "original_link": surv.get("link", "")})

    if not ops:
        print("No duplicates found.")
        return

    print(f"  {len(ops):,} items to quarantine, "
          f"{gb(sum(m['size'] for m in manifest))} recoverable")

    log = run_apply(args, ops, "quarantine")

    md_dir = os.path.dirname(os.path.abspath(args.manifest))
    os.makedirs(md_dir, exist_ok=True)
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    md = os.path.splitext(args.manifest)[0] + ".md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("# Quarantined duplicates\n\n")
        fh.write(f"{len(manifest):,} items, "
                 f"{gb(sum(m['size'] for m in manifest))}. "
                 f"Every one has a verified original elsewhere in your Drive. "
                 f"Nothing here has been deleted — these were moved to "
                 f"`{QUARANTINE}/`.\n\n")
        fh.write("| Item | Size | Was at | Original kept at |\n")
        fh.write("|---|---|---|---|\n")
        for m in sorted(manifest, key=lambda m: -m["size"]):
            fh.write(f"| {m['name']} | {human(m['size'])} | `{m['from']}` "
                     f"| `{m['original']}` |\n")
    print(f"\n  manifest   {os.path.abspath(args.manifest)}")
    print(f"  readable   {os.path.abspath(md)}")
    if args.execute:
        print(f"\n  Review and trash what you confirm:\n"
              f"    python {os.path.basename(sys.argv[0])} review\n")


def cmd_verify(args) -> None:
    service = get_service(args.credentials, args.token)
    ok = verify_against(service, args.baseline)
    sys.exit(0 if ok else 2)


# ---------------------------------------------------------------------------

# ===========================================================================
# Live UI — a local web app backed by real Drive queries
# ===========================================================================
#
# Every folder listing is fetched from Drive at the moment you expand it, so
# what you see is what is actually there, not a snapshot. Assignments live on
# the server. Execute runs from the same process, streaming progress back.
#
# It still cannot delete anything: the only Drive writes are folder creation
# and re-parenting.

UI_HTML = r"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Organise Drive</title><style>__CSS__
html,body{overflow-x:hidden}
.app{display:grid;grid-template-columns:1fr 340px;gap:18px;align-items:start}
@media(max-width:940px){.app{grid-template-columns:1fr}}
.app>*{min-width:0}
#tree{transition:opacity .25s}
#tree.updating{opacity:.45;pointer-events:none}
.panel{background:var(--surface);border:1px solid var(--ring);
border-radius:12px;padding:14px;min-width:0;overflow:hidden}
.tree{font-size:14px}
.tnode{}
.trow{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:7px;
min-width:0}
.trow:hover{background:var(--grid)}
.trow.planned{background:rgba(42,120,214,.12);
box-shadow:inset 3px 0 0 var(--s1)}
.trow.planned:hover{background:rgba(42,120,214,.18)}
.trow.carried{background:rgba(42,120,214,.05)}
.tabs{display:flex;gap:6px;margin-bottom:12px;
border-bottom:1px solid var(--grid);padding-bottom:9px}
.tab{border:1px solid var(--ring);background:transparent;color:var(--ink2);
border-radius:7px;padding:5px 13px;font:inherit;font-size:13px;
font-weight:600;cursor:pointer}
.tab.on{background:var(--s1);color:#fff;border-color:var(--s1)}
.srow{display:flex;align-items:center;gap:8px;padding:5px 6px;
border-radius:7px;min-width:0}
.srow:hover{background:var(--grid)}
.perms{display:flex;flex-wrap:wrap;gap:5px;min-width:0;
justify-content:flex-end}
.chip{display:inline-flex;align-items:center;gap:5px;
border:1px solid var(--ring);border-radius:6px;padding:1px 7px;
font-size:12px;background:var(--plane);white-space:nowrap}
.chip .x{border:0;background:transparent;color:var(--muted);cursor:pointer;
font-size:14px;line-height:1;padding:0}
.chip .x:hover{color:var(--crit)}
.btn.danger{background:var(--crit)}
.trow,.srow{cursor:pointer}
.rowx{border:0;background:transparent;color:var(--muted);cursor:pointer;
font-size:14px;line-height:1;padding:0 2px;flex:none}
.rowx:hover{color:var(--crit)}
.spin{display:inline-block;width:11px;height:11px;flex:none;
border:2px solid var(--grid);border-top-color:var(--s1);border-radius:50%;
animation:sp .7s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
/* One disabled/busy look for every control in the app. */
button:disabled,.btn:disabled{opacity:.45;cursor:not-allowed}
button:disabled:hover,.btn:disabled:hover{border-color:var(--ring)}
.btn:disabled .spin,.btn.busy .spin{border-color:rgba(255,255,255,.4);
border-top-color:#fff}
.btn.ghost:disabled .spin,.btn.ghost.busy .spin{border-color:var(--grid);
border-top-color:var(--s1)}
.busy{cursor:progress}
.menuwrap{position:relative;flex:none}
.dots{border:0;background:transparent;color:var(--muted);cursor:pointer;
font-size:16px;line-height:1;padding:2px 5px;border-radius:6px}
.dots:hover{background:var(--grid);color:var(--ink)}
.menu{position:absolute;right:0;top:100%;z-index:20;min-width:150px;
background:var(--surface);border:1px solid var(--ring);border-radius:9px;
box-shadow:0 6px 22px rgba(0,0,0,.18);padding:5px;display:none}
.menu.on{display:block}
.menu button{display:block;width:100%;text-align:left;border:0;
background:transparent;color:var(--ink);font:inherit;font-size:13px;
padding:7px 9px;border-radius:6px;cursor:pointer}
.menu button:hover{background:var(--grid)}
.destbtn{border:1px solid var(--ring);background:var(--plane);color:var(--ink2);
border-radius:7px;padding:3px 9px;font:inherit;font-size:12px;cursor:pointer;
max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
flex:none}
.destbtn:hover{border-color:var(--s1)}
.destbtn.set{border-color:var(--s1);color:var(--s1);font-weight:650;
background:rgba(42,120,214,.10)}
.picker{position:fixed;z-index:80;width:340px;max-width:92vw;
background:var(--surface);border:1px solid var(--ring);border-radius:11px;
box-shadow:0 12px 34px rgba(0,0,0,.26);padding:9px;display:none}
.picker.on{display:block}
.picker input{width:100%}
.picklist{max-height:270px;overflow:auto;margin-top:7px}
.pickitem{display:block;width:100%;text-align:left;border:0;
background:transparent;color:var(--ink);font:inherit;font-size:13px;
padding:6px 8px;border-radius:6px;cursor:pointer;overflow-wrap:anywhere}
.pickitem:hover,.pickitem.act{background:var(--grid)}
.hint{color:var(--muted);font-size:11.5px}
.pickitem .hint{color:var(--muted);font-size:11.5px}
.pickitem:disabled{opacity:.5;cursor:not-allowed;background:transparent}
.picksec{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);padding:8px 8px 3px;font-weight:650}
.pickempty{color:var(--muted);font-size:12.5px;padding:8px}
.picknote{color:var(--muted);font-size:11.5px;padding:7px 8px;
border-top:1px solid var(--grid);margin-top:5px}
.vwrap{text-align:center;margin:12px 0;background:var(--plane);
border:1px solid var(--ring);border-radius:10px;overflow:hidden}
.vimg{max-width:100%;max-height:62vh;display:block;margin:0 auto}
.vframe{width:100%;height:66vh;border:1px solid var(--ring);
border-radius:10px;margin:12px 0;background:#fff}
.vtext{background:var(--plane);border:1px solid var(--ring);
border-radius:10px;padding:13px;max-height:62vh;overflow:auto;margin:12px 0;
font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
white-space:pre-wrap;overflow-wrap:anywhere}
.dtable{width:100%;border-collapse:collapse;font-size:12.5px}
.dtable td{padding:5px 7px;border-bottom:1px solid var(--grid);
vertical-align:top;overflow-wrap:anywhere}
.dtable td.op{white-space:nowrap;color:var(--ink2);width:1%}
.tw{width:15px;text-align:center;color:var(--muted);cursor:pointer;flex:none;
user-select:none;font-size:11px}
.ic{flex:none;width:16px;text-align:center}
.nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;cursor:default}
.mt{font-size:12px;color:var(--ink2);white-space:nowrap;
font-variant-numeric:tabular-nums;flex-shrink:1;min-width:0;overflow:hidden;
text-overflow:ellipsis}
.kids{padding-left:19px;border-left:1px solid var(--grid);margin-left:11px}
.tag{font-size:11px;font-weight:650;padding:1px 6px;border-radius:5px;
background:rgba(42,120,214,.14);color:var(--s1);white-space:nowrap}
.tag.stay{background:transparent;color:var(--muted)}
.pend{max-height:320px;overflow:auto;margin:8px 0}
.pi{display:flex;gap:8px;align-items:center;padding:5px 0;
border-bottom:1px solid var(--grid);font-size:13px}
.pi .a{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.pi .x{border:0;background:transparent;color:var(--muted);cursor:pointer;
font-size:16px}
.cats{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}
.cat{display:flex;align-items:center;gap:6px;border:1px solid var(--ring);
border-radius:8px;padding:3px 7px;background:var(--plane);font-size:13px}
.cat input{border:0;background:transparent;width:110px;font-weight:600;
padding:1px 2px;font-size:13px}
.cat.inuse{border-color:var(--s1);background:rgba(42,120,214,.12);
color:var(--s1)}
.cat.inuse .cnt{background:var(--s1);color:#fff;border-radius:9px;
padding:0 6px;font-size:11px;font-weight:700}
.cat.inuse .x{color:var(--s1)}
.cat.inuse .x:hover{color:var(--crit)}
.cat.person{cursor:pointer;font:inherit;font-size:13px;color:var(--ink);
gap:7px}
.cat.person:hover{border-color:var(--s1);background:var(--grid)}
.cat.person .cnt{background:var(--grid);border-radius:9px;padding:0 6px;
font-size:11px;font-weight:700;color:var(--ink2)}
.cat.person.public{border-color:var(--crit)}
.cat.person.public .cnt{background:var(--crit);color:#fff}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
align-items:center;justify-content:center;z-index:50}
.ov.on{display:flex}
.ovbox{background:var(--surface);border-radius:14px;padding:24px;
width:min(680px,92vw);max-height:86vh;overflow:auto}
.pbar{height:12px;background:var(--grid);border-radius:7px;overflow:hidden;
margin:12px 0 6px}
.pfill{height:100%;background:var(--s1);width:0%;transition:width .25s}
.plog{font:12px/1.55 ui-monospace,Menlo,monospace;background:var(--plane);
border:1px solid var(--ring);border-radius:9px;padding:10px;max-height:260px;
overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:10px}
.warn{color:var(--crit)}
.muted{color:var(--ink2)}
.sticky{position:sticky;top:12px}
</style></head><body><div class=wrap>

<div style="display:flex;align-items:center;gap:14px;margin:0 0 6px">
  <img src="/logo" alt="Google Drive" style="height:30px;width:auto"
    onerror="this.style.display='none'">
  <h1 style="margin:0">Organise your Drive</h1>
</div>
<p class=sub id=sub>Reading your Drive live. Expand a folder, choose where it
should go, then execute.</p>

<div class=note><strong>Nothing here can delete any file.</strong> This page
creates folders, moves items, and — only on an explicit click in the Sharing
tab — removes a sharing permission. Every change is logged and reversible.
</div>

<div id=indexbar class=note style="display:none"></div>

<div class=app>
  <div class=panel>
    <div class=tabs>
      <button id=tab-org class="tab on" onclick="setView('org')">Organise
        </button>
      <button id=tab-share class=tab onclick="setView('share')">Sharing
        </button>
      <button id=tab-trash class=tab onclick="setView('trash')">Trash
        </button>
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
      <strong>My Drive</strong>
      <span id=treestat class=muted style="font-size:12px;flex:1"></span>
      <label id=onlyshared style="display:none;font-size:12.5px;
        color:var(--ink2);white-space:nowrap">
        <input type=checkbox id=hideunshared onchange=filterTree()>
        only shared</label>
      <input id=filter oninput=filterTree() placeholder="filter visible…"
        style="width:200px">
      <button class="btn ghost" onclick="refreshActive(this)">Refresh</button>
    </div>
    <div id=sharehead class=note style="display:none;margin:0 0 12px"></div>
    <div id=people style="display:none"></div>
    <div id=tree class=tree></div>
    <div id=stree class=tree style="display:none"></div>
    <div id=ttree class=tree style="display:none"></div>
  </div>

  <div class="panel sticky">
    <strong>Destinations</strong>
    <div class=muted style="font-size:12.5px">your existing root folders,
      read live — plus any new ones you add. New ones are only created on
      execute, and nested paths like Clients/Acme work.</div>
    <div id=cats class=cats></div>
    <div style="display:flex;gap:7px">
      <input id=newcat placeholder="new, e.g. Clients/Acme" style="flex:1"
        onkeydown="if(event.key==='Enter')addCat()">
      <button class="btn ghost" onclick=addCat()>Add</button>
    </div>

    <div style="margin-top:18px;display:flex;align-items:center">
      <strong style="flex:1">Planned moves</strong>
      <span id=cnt class=muted style="font-size:13px">0</span>
    </div>
    <div id=pend class=pend></div>
    <button class=btn id=go onclick=execute() disabled
      style="width:100%">Execute</button>
    <div class=muted style="font-size:12px;margin-top:8px">A preview appears
      first. Nothing runs until you confirm it there.</div>

    <div style="margin-top:18px;display:flex;align-items:center">
      <strong style="flex:1">Revisions</strong>
      <span class=muted style="font-size:12px">newest first</span>
    </div>
    <div id=runsum class=muted style="font-size:12.5px;margin:2px 0 4px">
      </div>
    <div id=runs class=pend></div>
    <div class=muted style="font-size:12px;margin-top:4px">Every run keeps its
      undo log. Undoing is safe to repeat — items already back in place are
      skipped.</div>
  </div>
</div>

<div class=foot id=foot></div>
</div>

<div class=ov id=ov><div class=ovbox id=ovbox></div></div>

<div class=picker id=picker>
  <input id=pickq placeholder="search any folder, at any depth…"
    autocomplete=off oninput="scheduleRemote();drawPicker()"
    onkeydown=pickKey(event)>
  <div class=picklist id=picklist></div>
</div>

<div class=ov id=cfm style="z-index:70"><div class=ovbox
  style="width:min(520px,92vw)">
  <h2 id=cfm-title style="margin-top:0">Please confirm</h2>
  <div id=cfm-msg style="line-height:1.55"></div>
  <div style="display:flex;gap:9px;margin-top:18px;justify-content:flex-end">
    <button class="btn ghost" id=cfm-no>Cancel</button>
    <button class=btn id=cfm-yes>Yes</button>
  </div>
</div></div>

<script>
const TOKEN = "__TOKEN__";
let CATS = [];          // existing root folders + your own additions
let ROOT_FOLDERS = [];  // folder names that already exist at Drive root
const DISCOVERED = new Set();   // nested folder paths seen while browsing
let USER_CATS = [];     // categories you added; kept in localStorage
try{ USER_CATS = JSON.parse(localStorage.getItem('gdo_cats')||'[]'); }
catch(e){ USER_CATS = []; }
// Destinations you actually used, most recent first — filing many things
// into the same folder should not mean searching for it every time.
let RECENTS = [];
try{ RECENTS = JSON.parse(localStorage.getItem('gdo_recent')||'[]'); }
catch(e){ RECENTS = []; }
function pushRecent(t){
  if(!t) return;
  RECENTS = [t].concat(RECENTS.filter(x=>x!==t)).slice(0,6);
  try{ localStorage.setItem('gdo_recent', JSON.stringify(RECENTS)); }
  catch(e){}
}
const PLAN = {};        // path -> {id,name,target}
const OPENSET = {};     // path -> id of folders currently expanded
const CACHE = {};       // id -> children
let ROOT = null;
let refreshing = false, refreshQueued = false;
let LAST_ACTION = '';   // 'run' | 'undo' — what the progress dialog is for
let LAST_RUN = {done:0, total:0, log:''};

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
function human(n){n=+n||0;
  if(n>=1073741824)return (n/1073741824).toFixed(2)+' GB';
  if(n>=1048576)return (n/1048576).toFixed(1)+' MB';
  if(n>=1024)return Math.round(n/1024)+' KB';
  return n?n+' B':'';}
async function api(p,body){
  const r = await fetch(p, body?{method:'POST',
    headers:{'Content-Type':'application/json','X-Auth':TOKEN},
    body:JSON.stringify(body)}:{headers:{'X-Auth':TOKEN}});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

// Branded yes/no dialog in the page's own stylesheet — no native popups.
// `msg` is HTML; callers esc() anything user-controlled they interpolate.
function uiConfirm(msg, opts){
  opts = opts || {};
  return new Promise(res=>{
    const ov = document.getElementById('cfm');
    document.getElementById('cfm-title').textContent =
      opts.title || 'Please confirm';
    document.getElementById('cfm-msg').innerHTML = msg;
    const yes = document.getElementById('cfm-yes');
    const no = document.getElementById('cfm-no');
    yes.textContent = opts.yes || 'Yes';
    no.textContent = opts.no || 'Cancel';
    no.style.display = '';          // a previous uiAlert may have hidden it
    yes.className = 'btn' + (opts.danger ? ' danger' : '');
    function done(v){
      ov.classList.remove('on');
      yes.onclick = no.onclick = null;
      ov.onkeydown = null;
      res(v);
    }
    yes.onclick = ()=>done(true);
    no.onclick = ()=>done(false);
    ov.classList.add('on');
    yes.focus();
    // Enter confirms, Escape cancels — same as every other dialog here.
    ov.onkeydown = (e)=>{
      if(e.key==='Escape'){ e.preventDefault(); done(false); }
      else if(e.key==='Enter'){ e.preventDefault(); done(true); }
    };
  });
}
// Branded message box — same dialog, single button. Used for every error
// and notice, so nothing falls back to the browser's native alert.
function uiAlert(msg, opts){
  opts = opts || {};
  const ov = document.getElementById('cfm');
  document.getElementById('cfm-title').textContent = opts.title || 'Notice';
  document.getElementById('cfm-msg').innerHTML = msg;
  const yes = document.getElementById('cfm-yes');
  const no = document.getElementById('cfm-no');
  yes.textContent = opts.ok || 'OK';
  yes.className = 'btn';
  no.style.display = 'none';
  return new Promise(res=>{
    function done(){
      ov.classList.remove('on');
      no.style.display = '';
      yes.onclick = null;
      ov.onkeydown = null;
      res();
    }
    yes.onclick = done;
    ov.onkeydown = (e)=>{
      if(e.key==='Escape' || e.key==='Enter'){ e.preventDefault(); done(); }
    };
    ov.classList.add('on');
    yes.focus();
  });
}
// One busy pattern for the whole app: any control that starts work is
// disabled and shows a spinner until that work settles, so nothing can be
// fired twice by an impatient second click. Returns the restore function.
function busy(el, label){
  if(!el) return function(){};
  const html = el.innerHTML, wasDisabled = el.disabled;
  el.disabled = true;
  el.classList.add('busy');
  el.innerHTML = '<span class=spin></span>' + (label ? ' '+esc(label) : '');
  let restored = false;
  return function(){
    if(restored) return;
    restored = true;
    el.innerHTML = html;
    el.disabled = wasDisabled;
    el.classList.remove('busy');
  };
}
// Drive's API errors are long and JSON-ish; show the human part.
function niceErr(e){
  const s = String(e && e.message ? e.message : e);
  let m = s.match(/"message":\s*"([^"]+)"/) || s.match(/'message':\s*'([^']+)'/);
  if(m) return m[1];
  m = s.match(/HttpError\s+(\d{3})/);
  if(m) return 'Drive returned an error (HTTP '+m[1]+').';
  return s.length > 300 ? s.slice(0,300)+'…' : s;
}

// ---- destinations ---------------------------------------------------------
// The list is your real root folders, read live from Drive, plus anything
// you add yourself. Your additions persist in this browser; new folders are
// only created when a move into them actually executes.
function saveCats(){
  try{ localStorage.setItem('gdo_cats', JSON.stringify(USER_CATS)); }
  catch(e){}
}
// Destinations are every folder we know exists — root folders, plus any
// nested folder seen while browsing the tree — and anything you add. The
// nested ones make it possible to file something into "Personal/Phones"
// without inventing a new top-level folder.
function rebuildCats(){
  const seen = {}, out = [];
  ROOT_FOLDERS.concat(Array.from(DISCOVERED), USER_CATS).forEach(c=>{
    if(c && !seen[c]){ seen[c]=1; out.push(c); }
  });
  // Always ascending, so a newly created folder slots into place instead of
  // dangling at the end of the dropdown.
  out.sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase()));
  CATS = out;
}
function chipList(){
  const seen = {}, out = [];
  ROOT_FOLDERS.concat(USER_CATS).forEach(c=>{
    if(c && !seen[c]){ seen[c]=1; out.push(c); }
  });
  return out.sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase()));
}
// How many planned moves target this destination (or anything under it).
function planCount(c){
  return Object.values(PLAN).filter(
    p=>p.target===c || p.target.indexOf(c+'/')===0).length;
}
function drawCats(){
  const chips = chipList();
  // Destinations in use that are not root folders or your own additions —
  // nested folders you picked from the search. They belong on the list too,
  // otherwise there is nowhere to see or clear that selection.
  const extra = [];
  Object.values(PLAN).forEach(p=>{
    const t = p.target;
    if(t && chips.indexOf(t)===-1 && extra.indexOf(t)===-1) extra.push(t);
  });
  const all = chips.concat(extra.sort((a,b)=>
    a.toLowerCase().localeCompare(b.toLowerCase())));
  const nested = CATS.length - chips.length;
  document.getElementById('cats').innerHTML = all.map(c=>{
    const isNew = ROOT_FOLDERS.indexOf(c)===-1;
    const n = planCount(c);
    // In use: highlighted, counted, and clearable with one click.
    const inner = n
      ? '<span style="font-weight:650">'+esc(c)+'</span>'+
        '<span class=cnt>'+n+'</span>'+
        '<button class=x title="clear '+n+' selection'+(n===1?'':'s')+
        ' for this destination" onclick="clearDest(\''+
        esc(c).replace(/'/g,"\\'")+'\')">&times;</button>'
      : (isNew
        ? '<input value="'+esc(c)+'" onchange="renameCat(\''+esc(c)+
          '\',this.value)">'+
          '<button class=x style="border:0;background:transparent;'+
          'color:var(--muted);cursor:pointer" onclick="delCat(\''+esc(c)+
          '\')">&times;</button>'
        : '<span style="font-weight:600">'+esc(c)+'</span>'+
          '<span class=muted style="font-size:10.5px">existing</span>');
    return '<div class="cat'+(n?' inuse':'')+'">'+inner+'</div>';
  }).join('') + (nested>0
    ? '<div class=muted style="font-size:11.5px;width:100%;margin-top:4px">'+
      '+ '+nested+' nested folder'+(nested===1?'':'s')+' you have browsed '+
      'to, selectable in the row dropdowns</div>' : '');
}
// Undo every selection pointing at a destination, from the chip.
async function clearDest(c){
  const hits = Object.keys(PLAN).filter(
    k=>PLAN[k].target===c || PLAN[k].target.indexOf(c+'/')===0);
  if(!hits.length) return;
  if(!await uiConfirm('Clear '+hits.length+' selection'+
      (hits.length===1?'':'s')+' pointing at <b>'+esc(c)+'</b>?'+
      '<br><span class=muted>Those items go back to staying where they '+
      'are. Nothing in your Drive changes.</span>',
      {title:'Clear selections', yes:'Clear'})) return;
  hits.forEach(k=>delete PLAN[k]);
  drawPlan(); refreshTags(); redrawSelects();
}
function addCat(){
  const el=document.getElementById('newcat');
  const v=(el.value||'').trim().replace(/^\/+|\/+$/g,'');
  if(!v) return;
  const dup = findCat(v);
  if(dup){ uiAlert('<b>'+esc(dup)+'</b> is already on the list.',
    {title:'Already listed'}); el.value=''; return; }
  USER_CATS.push(v); saveCats(); rebuildCats();
  el.value=''; drawCats(); redrawSelects();
}
function renameCat(old,v){
  v=(v||'').trim().replace(/^\/+|\/+$/g,'');
  if(!v){ drawCats(); return; }
  if(v.toLowerCase()!==old.toLowerCase() && findCat(v)){
    uiAlert('<b>'+esc(v)+'</b> is already on the list.',
      {title:'Already listed'}); drawCats(); return; }
  const ui=USER_CATS.indexOf(old);
  if(ui===-1){ drawCats(); return; }     // existing folders keep their name
  USER_CATS[ui]=v; saveCats(); rebuildCats();
  Object.values(PLAN).forEach(p=>{
    if(p.target===old||p.target.startsWith(old+'/'))
      p.target = v + p.target.slice(old.length);
  });
  drawCats(); redrawSelects(); drawPlan();
}
async function delCat(c){
  if(ROOT_FOLDERS.indexOf(c)!==-1) return;   // real folders stay listed
  const used=Object.values(PLAN).filter(p=>p.target===c||
    p.target.startsWith(c+'/')).length;
  if(used && !await uiConfirm(used+' planned move'+(used===1?'':'s')+
    ' use <b>'+esc(c)+'</b>. Remove '+(used===1?'it':'them')+' too?',
    {title:'Remove destination', yes:'Remove'})) return;
  Object.keys(PLAN).forEach(k=>{
    if(PLAN[k].target===c||PLAN[k].target.startsWith(c+'/')) delete PLAN[k];});
  const ui=USER_CATS.indexOf(c);
  if(ui!==-1){ USER_CATS.splice(ui,1); saveCats(); }
  rebuildCats(); drawCats(); redrawSelects(); drawPlan();
}

// ---- tree
// Icons come from the MIME type first and the extension only as a fallback,
// because Drive names are not required to have one.
const EXT_ICON = {
  pdf:'\u{1F4D5}', doc:'\u{1F4DD}', docx:'\u{1F4DD}', rtf:'\u{1F4DD}',
  odt:'\u{1F4DD}', txt:'\u{1F4C4}', md:'\u{1F4C4}', log:'\u{1F4C4}',
  xls:'\u{1F4CA}', xlsx:'\u{1F4CA}', csv:'\u{1F4CA}', ods:'\u{1F4CA}',
  ppt:'\u{1F4FD}', pptx:'\u{1F4FD}', odp:'\u{1F4FD}', key:'\u{1F4FD}',
  zip:'\u{1F5DC}', rar:'\u{1F5DC}', '7z':'\u{1F5DC}', tar:'\u{1F5DC}',
  gz:'\u{1F5DC}', bz2:'\u{1F5DC}', dmg:'\u{1F5DC}', iso:'\u{1F5DC}',
  jpg:'\u{1F5BC}', jpeg:'\u{1F5BC}', png:'\u{1F5BC}', gif:'\u{1F5BC}',
  webp:'\u{1F5BC}', svg:'\u{1F5BC}', bmp:'\u{1F5BC}', tif:'\u{1F5BC}',
  tiff:'\u{1F5BC}', heic:'\u{1F5BC}', ico:'\u{1F5BC}',
  psd:'\u{1F3A8}', ai:'\u{1F3A8}', eps:'\u{1F3A8}', sketch:'\u{1F3A8}',
  fig:'\u{1F3A8}', xd:'\u{1F3A8}', indd:'\u{1F3A8}',
  mp4:'\u{1F3AC}', mov:'\u{1F3AC}', avi:'\u{1F3AC}', mkv:'\u{1F3AC}',
  webm:'\u{1F3AC}', wmv:'\u{1F3AC}', flv:'\u{1F3AC}', m4v:'\u{1F3AC}',
  mp3:'\u{1F3B5}', wav:'\u{1F3B5}', flac:'\u{1F3B5}', aac:'\u{1F3B5}',
  ogg:'\u{1F3B5}', m4a:'\u{1F3B5}', wma:'\u{1F3B5}',
  js:'\u{1F4DC}', ts:'\u{1F4DC}', jsx:'\u{1F4DC}', tsx:'\u{1F4DC}',
  py:'\u{1F4DC}', rb:'\u{1F4DC}', php:'\u{1F4DC}', java:'\u{1F4DC}',
  c:'\u{1F4DC}', h:'\u{1F4DC}', cpp:'\u{1F4DC}', cs:'\u{1F4DC}',
  go:'\u{1F4DC}', rs:'\u{1F4DC}', swift:'\u{1F4DC}', kt:'\u{1F4DC}',
  sh:'\u{1F4DC}', bat:'\u{1F4DC}', ps1:'\u{1F4DC}', sql:'\u{1F4DC}',
  html:'\u{1F310}', htm:'\u{1F310}', css:'\u{1F3A8}', scss:'\u{1F3A8}',
  json:'\u{1F9E9}', xml:'\u{1F9E9}', yml:'\u{1F9E9}', yaml:'\u{1F9E9}',
  toml:'\u{1F9E9}', ini:'\u{1F9E9}', env:'\u{1F9E9}',
  ttf:'\u{1F524}', otf:'\u{1F524}', woff:'\u{1F524}', woff2:'\u{1F524}',
  eot:'\u{1F524}',
  exe:'\u{2699}', msi:'\u{2699}', apk:'\u{2699}', deb:'\u{2699}',
  bin:'\u{2699}', dat:'\u{2699}', pak:'\u{2699}', install:'\u{2699}',
  bunx:'\u{2699}', wasm:'\u{2699}', swf:'\u{2699}',
  pem:'\u{1F511}', crt:'\u{1F511}', p12:'\u{1F511}', pub:'\u{1F511}',
  ppk:'\u{1F511}', cer:'\u{1F511}', pfx:'\u{1F511}',
  // Web/build files, which dominate any Drive holding old project folders.
  mjs:'\u{1F4DC}', cjs:'\u{1F4DC}', cts:'\u{1F4DC}', mts:'\u{1F4DC}',
  twig:'\u{1F4DC}', jst:'\u{1F4DC}', stml:'\u{1F4DC}', tpl:'\u{1F4DC}',
  module:'\u{1F4DC}', inc:'\u{1F4DC}', phpt:'\u{1F4DC}', pl:'\u{1F4DC}',
  coffee:'\u{1F4DC}', cmd:'\u{1F4DC}', flow:'\u{1F4DC}', vue:'\u{1F4DC}',
  svelte:'\u{1F4DC}', lua:'\u{1F4DC}', r:'\u{1F4DC}', scala:'\u{1F4DC}',
  less:'\u{1F3A8}', sass:'\u{1F3A8}', styl:'\u{1F3A8}', drawio:'\u{1F3A8}',
  map:'\u{1F9E9}', lock:'\u{1F9E9}', cfg:'\u{1F9E9}', conf:'\u{1F9E9}',
  properties:'\u{1F9E9}', plist:'\u{1F9E9}', htaccess:'\u{1F9E9}',
  db:'\u{1F5C4}', sqlite:'\u{1F5C4}', sqlite3:'\u{1F5C4}', mdb:'\u{1F5C4}',
  po:'\u{1F4C4}', mo:'\u{1F4C4}', pot:'\u{1F4C4}', 'svn-base':'\u{1F4C4}'
};
const GOOGLE_ICON = {
  document:'\u{1F4DD}', spreadsheet:'\u{1F4CA}', presentation:'\u{1F4FD}',
  form:'\u{1F4CB}', drawing:'\u{1F3A8}', script:'\u{1F4DC}',
  'shortcut':'\u{1F517}', 'map':'\u{1F5FA}', 'site':'\u{1F310}',
  'jam':'\u{1F5D2}', 'fusiontable':'\u{1F4CA}'
};
// Types Drive reports as text/code even when the name carries no extension.
const TEXT_MIME = ['application/x-shellscript','application/x-sh',
  'application/x-perl','application/x-python','application/x-ruby',
  'application/x-httpd-php','application/javascript',
  'application/x-javascript','application/typescript','application/sql',
  'application/x-yaml','application/yaml','application/toml',
  'application/graphql','application/x-tex'];
function icon(n){
  if(n.folder) return '\u{1F4C1}';
  const m = String(n.mime||'');
  if(m.indexOf('application/vnd.google-apps.')===0){
    const kind = m.slice('application/vnd.google-apps.'.length);
    return GOOGLE_ICON[kind] || '\u{1F4C4}';
  }
  if(m.indexOf('image/')===0) return '\u{1F5BC}';
  if(m.indexOf('video/')===0) return '\u{1F3AC}';
  if(m.indexOf('audio/')===0) return '\u{1F3B5}';
  if(m==='application/pdf') return '\u{1F4D5}';
  if(m.indexOf('zip')>-1 || m.indexOf('compressed')>-1 ||
     m.indexOf('tar')>-1 || m.indexOf('rar')>-1) return '\u{1F5DC}';
  if(TEXT_MIME.indexOf(m)>-1) return '\u{1F4DC}';
  const name = String(n.name||'');
  const dot = name.lastIndexOf('.');
  if(dot>-1){
    const ext = name.slice(dot+1).toLowerCase();
    if(EXT_ICON[ext]) return EXT_ICON[ext];
  }
  if(m.indexOf('text/')===0) return '\u{1F4C4}';
  return '\u{1F4C4}';
}
// What the viewer can show inline, decided the same way as the icon.
function fileKind(n){
  const m = String(n.mime||'');
  const name = String(n.name||'');
  const ext = name.lastIndexOf('.')>-1
    ? name.slice(name.lastIndexOf('.')+1).toLowerCase() : '';
  if(m.indexOf('image/')===0) return 'image';
  if(m==='application/pdf') return 'pdf';
  if(m==='application/vnd.google-apps.drawing') return 'image';
  if(m==='application/vnd.google-apps.script') return 'text';
  if(m.indexOf('application/vnd.google-apps.')===0)
    return ['document','spreadsheet','presentation'].indexOf(
      m.slice(28))>-1 ? 'pdf' : 'none';
  if(m.indexOf('video/')===0) return 'video';
  if(m.indexOf('audio/')===0) return 'audio';
  if(m==='application/zip' || m==='application/x-zip-compressed' ||
     ['zip','jar','war','apk','xpi','epub','docx','xlsx','pptx'
     ].indexOf(ext)>-1) return 'zip';
  if(m.indexOf('text/')===0 || m==='application/json' ||
     m==='application/xml' || TEXT_MIME.indexOf(m)>-1 ||
     ['txt','md','log','csv','json','xml','yml','yaml','ini','env','toml',
      'js','ts','jsx','tsx','py','rb','php','java','c','h','cpp','cs','go',
      'rs','swift','kt','sh','bat','ps1','sql','css','scss','html','htm',
      'mjs','cjs','cts','mts','twig','jst','stml','tpl','module','inc',
      'phpt','pl','coffee','cmd','flow','vue','svelte','lua','r','scala',
      'less','sass','styl','map','lock','cfg','conf','properties','po',
      'pot','htaccess','svn-base'
     ].indexOf(ext)>-1) return 'text';
  return 'none';
}
async function children(id, path){
  if(CACHE[id]) return CACHE[id];
  const d = await api('/api/children?id='+encodeURIComponent(id)+
    '&path='+encodeURIComponent(path||''));
  CACHE[id]=d.items;
  // Every folder you browse past becomes an available destination.
  let added = false;
  d.items.forEach(i=>{
    if(i.folder && i.path && i.path.indexOf('/')>-1 &&
       !DISCOVERED.has(i.path)){ DISCOVERED.add(i.path); added = true; }
  });
  if(added){ rebuildCats(); drawCats(); redrawSelects(); }
  return d.items;
}
function ancestorPlanned(path){
  const parts=path.split('/');
  for(let n=parts.length-1;n>0;n--){
    const a=parts.slice(0,n).join('/');
    if(PLAN[a]) return a;
  }
  return null;
}
function rowHtml(n){
  const planned = PLAN[n.path];
  const anc = ancestorPlanned(n.path);
  const stats = (n.files!=null? n.files.toLocaleString()+' files':'') +
    (n.bytes? ' · '+human(n.bytes):'') +
    (!n.folder && n.size? human(n.size):'');
  const statTitle = (n.folder && n.files!=null)
    ? ' title="rolled-up totals from your last scan"' : '';
  return '<div class=tnode data-path="'+esc(n.path)+'" data-id="'+
    esc(n.id)+'" data-folder="'+(n.folder?1:0)+'">'+
    '<div class="trow'+(planned?' planned':(anc?' carried':''))+
    '" onclick="rowToggle(this,event)">'+
    '<span class=tw>'+(n.folder?'&#9656;':'')+'</span>'+
    '<span class=ic>'+icon(n)+'</span>'+
    '<span class=nm title="'+esc(n.path)+'">'+esc(n.name)+'</span>'+
    (n.owned===false?'<span class="tag stay" title="owned by someone else — '+
      'moving it changes what they see, and Drive may refuse">not yours'+
      '</span>':'')+
    (n.shared&&n.owned!==false?'<span class="tag stay">shared</span>':'')+
    (n.folder&&n.empty?'<span class="tag stay emptytag">empty</span>'+
      '<button class=rowx title="delete this empty folder" '+
      'onclick="event.stopPropagation();rmdir(\''+esc(n.id)+'\',\''+
      esc(n.name).replace(/'/g,"\\'")+'\',this)">&#128465;</button>':'')+
    '<span class=mt'+statTitle+'>'+esc(stats)+'</span>'+
    (anc? '<span class="tag stay">moves with '+esc(anc.split('/').pop())+
      '</span>'
     : destBtn(n.path, n.id, n.name, planned?planned.target:''))+
    '</div><div class=kids style="display:none"></div></div>';
}
// ---- destination picker ---------------------------------------------------
// A searchable popup rather than a <select>: the destination list grows with
// every folder you browse, and a native dropdown of hundreds of entries is
// unusable. One shared popup serves every row, so the tree stays light.
// A folder can never be a destination for itself or anything it contains,
// so those are filtered out rather than failing later.
function destOptions(own){
  return CATS.filter(c=>!own || (c!==own && c.indexOf(own+'/')!==0));
}
function destBtn(path, id, name, cur){
  const label = cur ? cur : 'leave where it is';
  return '<button class="destbtn'+(cur?' set':'')+'" title="'+esc(label)+
    '" onclick="openPicker(event,\''+esc(path).replace(/'/g,"\\'")+'\',\''+
    esc(id)+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">'+
    esc(label)+' &#9662;</button>';
}
function redrawSelects(){
  document.querySelectorAll('#tree .tnode').forEach(nd=>{
    const b = nd.querySelector(':scope > .trow > .destbtn');
    if(!b) return;
    const cur = (PLAN[nd.dataset.path]||{}).target||'';
    const label = cur ? cur : 'leave where it is';
    b.className = 'destbtn' + (cur?' set':'');
    b.title = label;
    b.innerHTML = esc(label)+' &#9662;';
  });
}
let PICK = null;
// Folders you have not expanded still have to be findable, so the search
// also asks the server, which indexes every folder in the Drive. Local
// matches render instantly; remote ones merge in when they arrive.
let REMOTE = {q:'', items:[]};
let FSTATUS = {ready:false, building:false, count:0, error:''};
let remoteTimer = null;
function scheduleRemote(){
  const q = (document.getElementById('pickq').value||'').trim();
  if(q.length < 2){ REMOTE = {q:'', items:[]}; return; }
  if(REMOTE.q === q) return;
  clearTimeout(remoteTimer);
  remoteTimer = setTimeout(async function(){
    let d;
    try{ d = await api('/api/folders?q='+encodeURIComponent(q)); }
    catch(e){ return; }
    const now = (document.getElementById('pickq').value||'').trim();
    if(now !== q) return;              // a newer keystroke already won
    FSTATUS = d;
    REMOTE = {q:q, items:d.items||[]};
    if(PICK) drawPicker();
  }, 180);
}
// Index status on the page itself, from load: the first build takes a
// while on a large Drive, and silently slow search is worse than a bar.
let indexPolling = false;
async function pollIndex(){
  if(indexPolling) return;
  indexPolling = true;
  try{
    while(true){
      let d;
      try{ d = await api('/api/folders'); }catch(e){ return; }
      FSTATUS = d;
      const bar = document.getElementById('indexbar');
      if(!bar) return;
      if(d.building){
        const pct = d.expected
          ? Math.min(99, Math.round(d.count/d.expected*100)) : 0;
        bar.style.display = '';
        bar.innerHTML = '<strong>Indexing your folders.</strong> '+
          'The destination search can already use the '+
          (d.usable||0).toLocaleString()+' folder'+
          (d.usable===1?'':'s')+' found so far, and the rest appear as '+
          'they are read'+
          (d.stale?', with the previous index still in use until this '+
            'finishes':'; the first run takes a minute or two, later runs '+
            'start instantly')+'. '+
          (d.count||0).toLocaleString()+
          (d.expected?' of about '+d.expected.toLocaleString():'')+
          ' read.'+
          (d.expected
            ? '<div class=pbar style="margin-top:9px"><div class=pfill '+
              'style="width:'+pct+'%"></div></div>' : '');
        // Keep an open picker filling up while the crawl runs.
        if(PICK && REMOTE.q){ REMOTE = {q:'', items:[]}; scheduleRemote(); }
        await new Promise(r=>setTimeout(r,700));
        continue;
      }
      if(d.error){
        bar.style.display = '';
        bar.innerHTML = '<strong>Folder search is unavailable.</strong> '+
          '<span class=muted>'+esc(d.error)+' — you can still type a '+
          'destination path by hand.</span>';
        return;
      }
      bar.style.display = 'none';
      if(PICK) drawPicker();
      return;
    }
  } finally { indexPolling = false; }
}
function openPicker(ev, path, id, name){
  ev.stopPropagation();
  PICK = {path:path, id:id, name:name, active:0};
  const p = document.getElementById('picker');
  const q = document.getElementById('pickq');
  q.value = '';
  REMOTE = {q:'', items:[]};
  api('/api/folders').then(d=>{ FSTATUS = d; if(PICK) drawPicker(); })
    .catch(e=>{});
  drawPicker();
  p.classList.add('on');
  // Keep it on screen: flip above the row when there is no room below.
  const r = ev.currentTarget.getBoundingClientRect();
  const h = p.offsetHeight || 300;
  let top = r.bottom + 6;
  if(top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
  let left = Math.min(r.left, window.innerWidth - p.offsetWidth - 10);
  p.style.top = top+'px';
  p.style.left = Math.max(8,left)+'px';
  q.focus();
}
function closePicker(){
  PICK = null;
  document.getElementById('picker').classList.remove('on');
}
function isFolderPath(p){
  const nd = findNode(p);
  return !nd || nd.dataset.folder==='1';
}
function parentOf(p){
  const i = String(p||'').lastIndexOf('/');
  return i>-1 ? p.slice(0,i) : '';
}
// Case-insensitive existence test: Drive would happily create "personal"
// next to "Personal", which is never what someone means.
function findCat(name){
  const n = String(name||'').toLowerCase();
  return CATS.find(c=>c.toLowerCase()===n) || null;
}
function leafOf(p){ return String(p||'').slice(p.lastIndexOf('/')+1); }
// Search matches fragments of ANY level, in any order: "nokia phones",
// "phones nokia" and "personal/phones" all find Personal/Phones/Nokia.
// Every token must appear somewhere in the path; where it appears only
// affects ranking, never whether it matches.
function pickTokens(q){
  return String(q||'').toLowerCase().split(/[\s\/]+/).filter(Boolean);
}
function pickMatch(path, tokens){
  const low = path.toLowerCase();
  return tokens.every(t=>low.indexOf(t)>-1);
}
function pickScore(path, tokens){
  const leaf = leafOf(path).toLowerCase();
  let s = 0;
  tokens.forEach(t=>{
    if(leaf.indexOf(t)===0) s += 3;       // starts the folder's own name
    else if(leaf.indexOf(t)>-1) s += 2;   // somewhere in its own name
  });
  return s;
}
// The rows of the picker, in display order. Selectable entries carry an
// index so the keyboard walks exactly what the eye sees.
function pickRows(){
  if(!PICK) return [];
  const raw = document.getElementById('pickq').value||'';
  const tokens = pickTokens(raw);
  const typed = raw.trim().replace(/^\/+|\/+$/g,'');
  const own = PICK.path && isFolderPath(PICK.path) ? PICK.path : '';
  const here = parentOf(PICK.path);      // where it already lives
  const cur = (PLAN[PICK.path]||{}).target||'';
  const rows = [];
  rows.push({kind:'item', value:'', label:'leave where it is',
             checked:!cur});
  let pool = CATS;
  if(tokens.length && REMOTE.q === raw.trim() && REMOTE.items.length){
    const seen = {};
    pool = CATS.concat(REMOTE.items).filter(c=>{
      if(seen[c]) return false; seen[c] = 1; return true; });
  }
  let list = pool.filter(c=>!own || (c!==own && c.indexOf(own+'/')!==0));
  if(tokens.length){
    list = list.filter(c=>pickMatch(c, tokens));
    // Best name matches first, then shallower folders, then alphabetical.
    list = list.map(c=>({c:c, s:pickScore(c, tokens)}))
      .sort((a,b)=> b.s-a.s
        || a.c.split('/').length-b.c.split('/').length
        || a.c.toLowerCase().localeCompare(b.c.toLowerCase()))
      .map(x=>x.c);
  }
  const row = c=>({kind:'item', value:c, label:leafOf(c),
                   sub:parentOf(c), checked:c===cur});
  if(!tokens.length && RECENTS.length){
    const rec = RECENTS.filter(r=>list.indexOf(r)>-1 && r!==here);
    if(rec.length){
      rows.push({kind:'sec', label:'Recent'});
      rec.forEach(r=>rows.push(row(r)));
      rows.push({kind:'sec', label:'All folders'});
    }
  }
  list.slice(0,300).forEach(c=>{
    if(c===here){
      // Its current location. Shown, so the list still makes sense, but
      // not selectable — moving something where it already is does nothing.
      rows.push({kind:'item', value:c, label:leafOf(c), sub:parentOf(c),
                 disabled:true, hint:'already here'});
    } else {
      rows.push(row(c));
    }
  });
  // Anything you type that is not an existing folder can still be used —
  // it is created on execute, exactly like typing a new destination.
  if(typed && !findCat(typed) && typed!==here && typed!==own){
    rows.push({kind:'item', value:typed, label:typed, isNew:true,
               hint:'create this folder'});
  }
  if(!list.length && !typed)
    rows.push({kind:'empty',
               label:'No folders yet — browse the tree, or type a name.'});
  // Be honest about what the search can currently see.
  if(FSTATUS.building)
    rows.push({kind:'note',
               label:'indexing every folder in your Drive ('+
                     (FSTATUS.count||0).toLocaleString()+' so far)'+
                     (FSTATUS.stale
                       ? ' — searching the previous index meanwhile'
                       : ' — results widen as it finishes')});
  else if(FSTATUS.error)
    rows.push({kind:'note', label:'folder index failed: '+FSTATUS.error});
  else if(FSTATUS.ready && tokens.length && raw.trim().length < 2)
    rows.push({kind:'note', label:'type two or more letters to search all '+
                                  FSTATUS.count.toLocaleString()+' folders'});
  else if(FSTATUS.ready && !tokens.length)
    rows.push({kind:'note',
               label:'searching all '+FSTATUS.count.toLocaleString()+
                     ' folders in your Drive'});
  return rows;
}
function pickSelectable(rows){
  return rows.filter(r=>r.kind==='item' && !r.disabled);
}
function drawPicker(){
  const rows = pickRows();
  const sel = pickSelectable(rows);
  if(PICK){
    if(PICK.active > sel.length-1) PICK.active = Math.max(0, sel.length-1);
    if(PICK.active < 0) PICK.active = 0;
  }
  let n = -1;
  const h = rows.map(r=>{
    if(r.kind==='sec') return '<div class=picksec>'+esc(r.label)+'</div>';
    if(r.kind==='empty') return '<div class=pickempty>'+esc(r.label)+'</div>';
    if(r.kind==='note') return '<div class=picknote>'+
      (FSTATUS.building?'<span class=spin></span> ':'')+esc(r.label)+'</div>';
    if(!r.disabled) n++;
    const act = (!r.disabled && PICK && n===PICK.active) ? ' act' : '';
    // A folder icon for real destinations, a sparkle for one that will be
    // created, and the "stay put" row keeps its own marker.
    const ic = r.value === ''
      ? '\u{21A9}️ ' : (r.isNew ? '\u{2728} ' : '\u{1F4C1} ');
    return '<button class="pickitem'+act+'"'+(r.disabled?' disabled':'')+
      ' title="'+esc(r.value||r.label)+'"'+
      (r.disabled?'':' onclick="choosePick('+(r.isNew?'1':'0')+','+n+')"')+
      '>'+(r.checked?'&#10003; ':'')+ic+esc(r.label)+
      (r.sub?' <span class=hint>in '+esc(r.sub)+'</span>':'')+
      (r.hint?' <span class=hint>— '+esc(r.hint)+'</span>':'')+'</button>';
  }).join('');
  document.getElementById('picklist').innerHTML = h;
}
function pickKey(ev){
  if(ev.key==='Escape'){ closePicker(); return; }
  const sel = pickSelectable(pickRows());
  if(ev.key==='ArrowDown' || ev.key==='ArrowUp'){
    ev.preventDefault();
    if(!PICK || !sel.length) return;
    PICK.active += (ev.key==='ArrowDown'?1:-1);
    if(PICK.active < 0) PICK.active = sel.length-1;
    if(PICK.active > sel.length-1) PICK.active = 0;
    drawPicker();
    const act = document.querySelector('.pickitem.act');
    if(act) act.scrollIntoView({block:'nearest'});
    return;
  }
  if(ev.key==='Enter'){
    ev.preventDefault();
    if(!PICK || !sel.length) return;
    const r = sel[Math.min(PICK.active, sel.length-1)];
    choosePick(r.isNew?1:0, PICK.active);
  }
}
function choosePick(isNew, idx){
  if(!PICK) return;
  const sel = pickSelectable(pickRows());
  const r = sel[idx];
  if(!r) return;
  const p = PICK, target = r.value;
  closePicker();
  if(isNew && target && !findCat(target)){
    USER_CATS.push(target); saveCats(); rebuildCats(); drawCats();
  } else if(target && !findCat(target)){
    // Came from the server-side index — keep it locally so it stays
    // listed without another round trip.
    DISCOVERED.add(target); rebuildCats(); drawCats();
  }
  pushRecent(target);
  setDest(p.path, p.id, p.name, target);
}
document.addEventListener('click', function(e){
  if(!document.getElementById('picker').contains(e.target)) closePicker();
});
async function tog(el,id,path){
  const holder = el.closest('.tnode').querySelector(':scope > .kids');
  if(holder.dataset.loaded){
    const show = holder.style.display==='none';
    holder.style.display = show?'':'none';
    el.innerHTML = show?'&#9662;':'&#9656;';
    if(show) OPENSET[path]=id; else delete OPENSET[path];
    return;
  }
  el.innerHTML='<span class=spin></span>';
  try{
    const items = await children(id, path);
    holder.innerHTML = items.length
      ? items.map(rowHtml).join('')
      : '<div class=muted style="padding:5px 6px;font-size:13px">empty</div>';
    holder.dataset.loaded='1'; holder.style.display='';
    el.innerHTML='&#9662;';
    OPENSET[path]=id;
    paintRows();
  }catch(e){ el.innerHTML='&#9656;';
    uiAlert(esc(niceErr(e)), {title:'Could not read that folder'}); }
}
async function setDest(path, id, name, target){
  if(!target){ delete PLAN[path]; }
  else{
    const anc = ancestorPlanned(path);
    if(anc){
      uiAlert('<b>'+esc(anc)+'</b> is already planned to move, and would '+
        'carry this with it.<br><br>Remove that one first if you want this '+
        'folder to move separately.', {title:'Already moving'});
      return;
    }
    if(target===path || target.indexOf(path+'/')===0){
      uiAlert('A folder cannot move into itself.', {title:'Impossible move'});
      return;
    }
    // Last line of defence: the picker greys this out, but a typed path
    // could still name the folder the item is already sitting in.
    if(target===parentOf(path)){
      uiAlert('<b>'+esc(name)+'</b> is already in <b>'+esc(target)+
        '</b> — that move would do nothing.', {title:'Already there'});
      return;
    }
    // a planned descendant would be carried along — clear those
    const drop = Object.keys(PLAN).filter(k=>k.startsWith(path+'/'));
    if(drop.length && !await uiConfirm(drop.length+' planned move'+
      (drop.length===1?'':'s')+' inside this folder will be removed, '+
      'because moving this folder carries them along.<br><br>Continue?',
      {title:'Overlapping plans', yes:'Continue'})){
      return; }
    drop.forEach(k=>delete PLAN[k]);
    PLAN[path] = {id:id, name:name, target:target, path:path};
  }
  drawPlan(); refreshTags(); redrawSelects();
}
function refreshTags(){
  // Organise tree only — sharing and trash rows have no destination select.
  document.querySelectorAll('#tree .tnode').forEach(nd=>{
    const path = nd.dataset.path;
    const row = nd.querySelector(':scope > .trow');
    if(!row) return;
    const anc = ancestorPlanned(path);
    const btn = row.querySelector(':scope > .destbtn');
    const tag = row.querySelector('.tag.stay');
    if(anc && btn){
      btn.outerHTML = '<span class="tag stay">moves with '+
        esc(anc.split('/').pop())+'</span>';
    } else if(!anc && tag && tag.textContent.indexOf('moves with')===0){
      tag.outerHTML = destBtn(path, nd.dataset.id||'',
        path.split('/').pop(), '');
    }
  });
  paintRows();
}
function savePlan(){
  try{ localStorage.setItem('gdo_plan', JSON.stringify(PLAN)); }catch(e){}
}
// Colour the rows so annotations are visible at a glance: a planned row gets
// an accent bar, everything that will be carried along by a planned ancestor
// gets a lighter tint.
function paintRows(){
  document.querySelectorAll('#tree .tnode').forEach(nd=>{
    const row = nd.querySelector(':scope > .trow');
    if(!row) return;
    const p = nd.dataset.path;
    row.classList.toggle('planned', !!PLAN[p]);
    row.classList.toggle('carried', !PLAN[p] && !!ancestorPlanned(p));
  });
}
function drawPlan(){
  const keys = Object.keys(PLAN).sort();
  document.getElementById('cnt').textContent = keys.length;
  const go = document.getElementById('go');
  go.disabled = !keys.length;
  go.title = keys.length
    ? 'Preview, then run '+keys.length+' move'+(keys.length===1?'':'s')
    : 'Choose a destination for at least one item first';
  go.textContent = keys.length
    ? 'Execute '+keys.length+' move'+(keys.length===1?'':'s')
    : 'Execute';
  document.getElementById('pend').innerHTML = keys.length
    ? keys.map(k=>'<div class=pi><span class=a title="'+esc(k)+'">'+
        esc(k)+'</span><span class=tag>'+esc(PLAN[k].target)+'</span>'+
        '<button class=x onclick="unplan(\''+esc(k).replace(/'/g,"\\'")+
        '\')">&times;</button></div>').join('')
    : '<div class=muted style="font-size:13px;padding:6px 0">Nothing planned '+
      'yet. Everything stays exactly where it is.</div>';
  savePlan(); paintRows(); drawCats();
}
function unplan(k){ delete PLAN[k]; drawPlan(); refreshTags(); redrawSelects(); }

// One click anywhere on a row: folders expand/collapse, files open a viewer.
// Clicks on controls (selects, buttons, chips) keep their own behaviour.
function rowToggle(row, ev){
  if(ev && ev.target && ev.target.closest(
      'select,button,input,a,label,.chip')) return;
  const nd = row.closest('.tnode');
  if(!nd) return;
  const tw = row.querySelector(':scope > .tw');
  if(nd.dataset.folder==='1'){
    if(row.classList.contains('srow'))
      stog(tw, nd.dataset.id, nd.dataset.path);
    else
      tog(tw, nd.dataset.id, nd.dataset.path);
  } else {
    viewFile(nd.dataset.id);
  }
}
function findItem(id){
  for(const k in CACHE){
    const f = CACHE[k].find(i=>i.id===id); if(f) return f; }
  for(const k in SCACHE){
    const f = SCACHE[k].find(i=>i.id===id); if(f) return f; }
  const t = TCACHE.find(i=>i.id===id); if(t) return t;
  return null;
}
// Copy to clipboard. The page is served from 127.0.0.1, which browsers
// treat as a secure context, so the async API is available; the textarea
// fallback covers older ones.
async function copyText(text, btn){
  let ok = false;
  try{
    await navigator.clipboard.writeText(text);
    ok = true;
  }catch(e){
    try{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }catch(e2){ ok = false; }
  }
  if(btn){
    const was = btn.innerHTML;
    btn.innerHTML = ok ? '\u2713 Copied' : 'Press Ctrl+C';
    if(!ok){ window.prompt('Copy this link:', text); }
    setTimeout(()=>{ btn.innerHTML = was; }, 1600);
  }
  return ok;
}
// Viewer: shows the file itself — images, PDFs (including Docs, Sheets and
// Slides rendered to PDF), video, audio and text are streamed through this
// server, which already holds the Drive credentials.
function fileUrl(id){
  return '/api/file?t='+encodeURIComponent(TOKEN)+
         '&id='+encodeURIComponent(id);
}
function viewFile(id){
  const it = findItem(id);
  if(!it) return;
  const kind = fileKind(it);
  const url = fileUrl(it.id);
  const box = document.getElementById('ovbox');
  const head = '<h2 style="margin-top:0;overflow-wrap:anywhere">'+
    icon(it)+' '+esc(it.name)+'</h2>'+
    '<p class=muted style="overflow-wrap:anywhere">'+
    esc(it.path||it.name)+
    (it.size?' &middot; '+human(it.size):'')+
    (it.modified?' &middot; modified '+esc(it.modified):'')+'</p>';
  const actions =
    '<div style="display:flex;gap:9px;margin-top:12px;flex-wrap:wrap">'+
    (it.link?'<a class=btn target=_blank rel=noopener href="'+esc(it.link)+
      '">Open in Drive</a>':'')+
    (it.link?'<button class="btn ghost" onclick="copyText(\''+
      esc(it.link).replace(/'/g,"\\'")+'\',this)">Copy link</button>':'')+
    '<button class="btn ghost" onclick="copyText(\''+
      esc(it.path||it.name).replace(/'/g,"\\'")+
      '\',this)">Copy path</button>'+
    '<a class="btn ghost" href="'+url+'" download="'+esc(it.name)+
      '">Download</a>'+
    '<button class="btn ghost" onclick=closeOv()>Close</button></div>';
  let body;
  if(kind==='image'){
    body = '<div class=vwrap><img src="'+url+'" alt="'+esc(it.name)+
      '" class=vimg onerror="viewFailed(this)"></div>';
  } else if(kind==='pdf'){
    body = '<iframe class=vframe src="'+url+'" title="'+esc(it.name)+
      '"></iframe>';
  } else if(kind==='video'){
    body = '<div class=vwrap><video class=vimg controls src="'+url+
      '" onerror="viewFailed(this)"></video></div>';
  } else if(kind==='audio'){
    body = '<div class=vwrap style="padding:14px"><audio controls '+
      'style="width:100%" src="'+url+'" onerror="viewFailed(this)">'+
      '</audio></div>';
  } else if(kind==='text'){
    body = '<pre class=vtext id=vtext><span class=spin></span> '+
      'loading&hellip;</pre>';
  } else if(kind==='zip'){
    body = '<div id=vzip class=vtext style="font:inherit">'+
      '<span class=spin></span> reading the archive&hellip;</div>';
  } else {
    body = '<p class=muted>No inline preview for this file type — '+
      'open it in Drive or download it.</p>';
  }
  box.innerHTML = head + body + actions;
  document.getElementById('ov').classList.add('on');
  if(kind==='text') loadText(url);
  if(kind==='zip') loadZip(it.id);
}
// Archive contents, read from the zip's own directory — nothing is
// extracted and nothing is written to disk.
async function loadZip(id){
  const host = document.getElementById('vzip');
  if(!host) return;
  let d;
  try{ d = await api('/api/zip?id='+encodeURIComponent(id)); }
  catch(e){
    host.innerHTML = '<span class=muted>Could not read the archive: '+
      esc(niceErr(e))+'</span>';
    return;
  }
  if(!d.entries.length){
    host.innerHTML = '<span class=muted>The archive is empty.</span>';
    return;
  }
  const files = d.entries.filter(e=>!e.dir);
  host.innerHTML =
    '<div class=muted style="font:13px/1.5 system-ui;margin-bottom:9px">'+
    files.length.toLocaleString()+' file'+(files.length===1?'':'s')+
    (d.entries.length-files.length
      ? ', '+(d.entries.length-files.length).toLocaleString()+' folder'+
        (d.entries.length-files.length===1?'':'s') : '')+
    ' &middot; '+human(d.bytes)+' uncompressed'+
    (d.truncated?' &middot; showing the first '+
      d.entries.length.toLocaleString()+' of '+d.total.toLocaleString():'')+
    '</div>'+
    '<table class=dtable style="font:12.5px/1.5 ui-monospace,Menlo,'+
    'monospace"><tbody>'+
    d.entries.map(e=>'<tr><td>'+(e.dir?'\u{1F4C1} ':'\u{1F4C4} ')+
      esc(e.name)+'</td>'+
      '<td class=op style="text-align:right">'+
      (e.dir?'':human(e.size))+'</td>'+
      '<td class=op>'+esc(e.date||'')+'</td></tr>').join('')+
    '</tbody></table>';
}
function viewFailed(el){
  const w = el.closest('.vwrap') || el.parentNode;
  w.innerHTML = '<p class=muted style="padding:14px">That file could not '+
    'be displayed here. It may be larger than the 25 MB preview limit, or '+
    'a format the browser cannot show — open it in Drive instead.</p>';
}
async function loadText(url){
  const pre = document.getElementById('vtext');
  if(!pre) return;
  try{
    const r = await fetch(url);
    if(!r.ok) throw new Error(await r.text());
    let t = await r.text();
    // Enough to read and copy from, without hanging the page on a huge log.
    const LIMIT = 200000;
    let clipped = false;
    if(t.length > LIMIT){ t = t.slice(0, LIMIT); clipped = true; }
    pre.textContent = t + (clipped ? '\n\n… truncated at '+
      LIMIT.toLocaleString()+' characters. Download for the whole file.'
      : '');
  }catch(e){
    pre.innerHTML = '<span class=muted>Could not load the text: '+
      esc(niceErr(e))+'</span>';
  }
}
// Delete an empty folder: confirmed, sent to Drive's trash, logged so the
// CLI can restore it (undo folder_actions.jsonl --execute).
async function rmdir(fid, name, btn){
  const ok = await uiConfirm('Delete the empty folder <b>'+esc(name)+
    '</b>?<br><span class=muted>It goes to the Drive trash — recoverable '+
    'for 30 days — and the action is recorded in '+
    'logs/folder_actions.jsonl.</span>',
    {title:'Delete empty folder', yes:'Delete', danger:true});
  if(!ok) return;
  const undo = busy(btn);
  try{
    await api('/api/rmdir',{id:fid});
    const nd = btn.closest('.tnode');
    const gone = nd ? nd.dataset.path : '';
    // The parent may itself be empty now — find it before detaching.
    const parentNode = nd && nd.parentElement
      ? nd.parentElement.closest('.tnode') : null;
    if(nd){
      if(PLAN[nd.dataset.path]){ delete PLAN[nd.dataset.path]; drawPlan(); }
      nd.remove();
    }
    Object.keys(CACHE).forEach(k=>{
      CACHE[k] = CACHE[k].filter(i=>i.id!==fid); });
    Object.keys(SCACHE).forEach(k=>{
      SCACHE[k] = SCACHE[k].filter(i=>i.id!==fid); });
    delete CACHE[fid];
    // A deleted folder is no longer a place anything can be filed into.
    if(gone){
      let changed = DISCOVERED.delete(gone);
      Array.from(DISCOVERED).forEach(p=>{
        if(p.indexOf(gone+'/')===0){ DISCOVERED.delete(p); changed = true; }
      });
      const ui = USER_CATS.indexOf(gone);
      if(ui!==-1){ USER_CATS.splice(ui,1); saveCats(); changed = true; }
      if(changed){ rebuildCats(); drawCats(); redrawSelects(); }
    }
    if(parentNode) refreshEmptyState(parentNode);
  }catch(e){
    undo();
    uiAlert(esc(niceErr(e)), {title:'Could not delete the folder'});
  }
}
// Give a row the "empty" badge and its delete button the moment its last
// child goes, so the tree stays truthful without pressing Refresh.
function refreshEmptyState(nd){
  const kids = nd.querySelector(':scope > .kids');
  const row = nd.querySelector(':scope > .trow');
  if(!kids || !row || nd.dataset.folder!=='1') return;
  if(!kids.dataset.loaded) return;          // contents unknown — say nothing
  const isEmpty = !kids.querySelector(':scope > .tnode');
  const has = !!row.querySelector('.emptytag');
  const id = nd.dataset.id;
  const item = findItem(id);
  if(item){
    item.empty = isEmpty;
    if(isEmpty){ item.files = null; item.bytes = null; }
  }
  if(isEmpty && !has){
    const mt = row.querySelector('.mt');
    const tag = document.createElement('span');
    tag.className = 'tag stay emptytag';
    tag.textContent = 'empty';
    const del = document.createElement('button');
    del.className = 'rowx';
    del.title = 'delete this empty folder';
    del.innerHTML = '&#128465;';
    del.onclick = function(e){
      e.stopPropagation();
      rmdir(id, (nd.dataset.path||'').split('/').pop(), del);
    };
    row.insertBefore(tag, mt);
    row.insertBefore(del, mt);
    if(mt) mt.textContent = '';            // stale totals no longer apply
    kids.innerHTML = '<div class=muted style="padding:5px 6px;'+
      'font-size:13px">empty</div>';
  } else if(!isEmpty && has){
    row.querySelectorAll('.emptytag, .rowx').forEach(el=>el.remove());
  }
}
function filterTree(){
  const v=document.getElementById('filter').value.toLowerCase();
  const hideUn = VIEW==='share' &&
    document.getElementById('hideunshared').checked;
  const host = document.getElementById(
    VIEW==='share'?'stree':(VIEW==='trash'?'ttree':'tree'));
  host.querySelectorAll('.tnode').forEach(nd=>{
    const nm = nd.querySelector(':scope > .trow > .nm, :scope > .srow > .nm');
    if(!nm) return;
    let ok = !v || nm.textContent.toLowerCase().includes(v);
    if(ok && hideUn){
      // A folder is worth keeping only if it is shared itself or leads to
      // something shared. Keeping every folder — as this used to — meant
      // the filter changed nothing at the top level, where every row is a
      // folder, so it looked broken.
      if(nd.dataset.folder==='1'){
        const inside = parseInt(nd.dataset.sharedin||'0', 10);
        if(nd.dataset.shared!=='1' && !inside) ok = false;
      } else if(nd.dataset.shared!=='1') ok = false;
    }
    nd.style.display = ok?'':'none';
  });
}

// ---- sharing view ---------------------------------------------------------
// Same live tree, but each shared item shows exactly who has access, and an
// x on a chip revokes that one permission (confirmed, logged, restorable).
let VIEW='org';
let SWEEP_DONE = false;
const SCACHE = {};
let TCACHE = [];
function setView(v){
  VIEW = v;
  ['org','share','trash'].forEach(t=>{
    document.getElementById('tab-'+t).classList.toggle('on', v===t);
  });
  document.getElementById('tree').style.display = v==='org'?'':'none';
  document.getElementById('stree').style.display = v==='share'?'':'none';
  document.getElementById('ttree').style.display = v==='trash'?'':'none';
  document.getElementById('onlyshared').style.display =
    v==='share'?'':'none';
  document.getElementById('sharehead').style.display =
    v==='share'?'':'none';
  if(v==='share' && !document.getElementById('stree').dataset.loaded)
    bootShare();
  if(v==='trash' && !document.getElementById('ttree').dataset.loaded)
    bootTrash();
  filterTree();
}
// The sweep: one light pass over the whole Drive so every folder can show
// how many shared items are anywhere beneath it — the top-down trail.
// Exactly one poll loop ever runs (sweepPolling), and a finished sweep
// paints badges into the existing rows rather than rebuilding the tree —
// switching tabs mid-sweep must never restart or lose anything.
let sweepPolling = false;
async function pollSweep(){
  if(sweepPolling) return;
  sweepPolling = true;
  try{
    while(true){
      let s;
      try{ s = await api('/api/sweep'); }
      catch(e){ return; }
      const head = document.getElementById('sharehead');
      if(s.running){
        if(head) head.innerHTML = '<strong>Scanning for shared items.</strong>'+
          ' '+(s.seen||0).toLocaleString()+' items checked so far. You can '+
          'keep browsing — folders will show how many shared items are '+
          'inside them once this finishes.';
        await new Promise(r=>setTimeout(r,900));
        continue;
      }
      if(s.error){
        if(head) head.innerHTML = '<strong>Sharing scan failed.</strong> '+
          esc(s.error);
        return;
      }
      if(s.done){
        // Lead with exposure, because that is the question the tab exists
        // to answer: what is out there, and who can see it.
        if(head){
          const bits = [];
          if(s.public) bits.push('<strong style="color:var(--crit)">'+
            s.public.toLocaleString()+' item'+(s.public===1?' is':'s are')+
            ' public on the web</strong> — anyone with the link can open '+
            (s.public===1?'it':'them')+'.');
          bits.push('<strong>'+s.shared_count.toLocaleString()+
            ' shared item'+(s.shared_count===1?'':'s')+'</strong> in your '+
            'Drive, with '+s.people_total.toLocaleString()+' '+
            (s.people_total===1?'person or group':'people or groups')+
            '. Scanned at '+esc(s.at)+'.');
          if(s.capped) bits.push('<span class=muted>Access details were '+
            'read for the first '+s.cap.toLocaleString()+' items.</span>');
          head.innerHTML = bits.join('<br>');
        }
        SWEEP = s;
        drawPeople(s);
        SWEEP_DONE = true;
        await paintShareBadges();
      }
      return;
    }
  } finally { sweepPolling = false; }
}
// Who can see your files, most exposure first. Clicking a person answers
// "what exactly can they see?" and lets you revoke it in one place.
let SWEEP = null;
function drawPeople(s){
  const el = document.getElementById('people');
  if(!el) return;
  if(!s.people || !s.people.length){ el.style.display='none'; return; }
  el.style.display = '';
  el.innerHTML = '<div class=panel style="margin:0 0 12px">'+
    '<div style="display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;'+
    'margin-bottom:9px"><strong>Who your files are shared with</strong>'+
    '<span class=muted style="font-size:12.5px">click a name to see '+
    'exactly what they can open</span></div>'+
    '<div class=cats>'+
    s.people.map(p=>'<button class="cat person'+
      (p.type==='anyone'?' public':'')+'" onclick="showPerson(\''+
      esc(p.who).replace(/'/g,"\\'")+'\')" title="'+
      esc(p.roles.join(', '))+'">'+
      (p.type==='anyone'?'\u{1F310} ':(p.type==='domain'?'\u{1F3E2} '
        :'\u{1F464} '))+
      esc(p.who)+'<span class=cnt>'+p.count.toLocaleString()+'</span>'+
      '</button>').join('')+
    '</div>'+
    (s.people_total>s.people.length
      ? '<div class=muted style="font-size:11.5px;margin-top:8px">showing '+
        'the '+s.people.length+' with the most access, of '+
        s.people_total.toLocaleString()+'</div>' : '')+
    '</div>';
}
// The flat list of everything one person can reach, with revoke in place.
async function showPerson(who){
  const st = document.getElementById('stree');
  st.innerHTML = '<div class=muted style="padding:6px"><span class=spin>'+
    '</span> gathering what '+esc(who)+' can see&hellip;</div>';
  let d;
  try{ d = await api('/api/shared?who='+encodeURIComponent(who)); }
  catch(e){
    st.innerHTML = '<div class=warn style="padding:6px">'+esc(niceErr(e))+
      '</div>';
    return;
  }
  st.innerHTML =
    '<div class=note style="margin:0 0 10px"><strong>'+esc(who)+
    '</strong> can open '+d.total.toLocaleString()+' item'+
    (d.total===1?'':'s')+'. Removing access here affects only this '+
    'person.<br><button class="btn ghost" style="margin-top:9px" '+
    'onclick="backToShareTree()">&larr; Back to the folder tree</button>'+
    '</div>'+
    d.items.map(it=>{
      const p = (it.perms||[]).find(x=>(x.email||x.domain||
        (x.type==='anyone'?'Anyone with the link':x.type))===who);
      return '<div class=tnode data-path="'+esc(it.path)+'" data-id="'+
        esc(it.id)+'" data-folder="'+(it.folder?1:0)+'" data-shared="1">'+
        '<div class=srow onclick="rowToggle(this,event)">'+
        '<span class=tw></span><span class=ic>'+icon(it)+'</span>'+
        '<span class=nm title="'+esc(it.path)+'">'+esc(it.name)+
        '<span class=hint style="margin-left:7px">'+
        esc(parentOf(it.path)||'My Drive')+'</span></span>'+
        (it.public?'<span class="tag stay" style="color:var(--crit)">'+
          'public</span>':'')+
        (p?'<span class=perms>'+permChip(it.id,p,it.can_share!==false)+
          '</span>':'')+
        '</div></div>';
    }).join('')+
    (d.total>d.items.length
      ? '<div class=muted style="padding:8px">showing the first '+
        d.items.length.toLocaleString()+' of '+d.total.toLocaleString()+
        '</div>' : '');
}
function backToShareTree(){
  document.getElementById('stree').dataset.loaded='';
  bootShare();
}
// Add/refresh the "N shared inside" badges on rows already rendered.
async function paintShareBadges(){
  const host = document.getElementById('stree');
  if(!host) return;
  const nodes = Array.from(host.querySelectorAll('.tnode'))
    .filter(nd=>nd.dataset.folder==='1' && nd.dataset.id);
  if(!nodes.length) return;
  let d;
  try{
    d = await api('/api/sweepcounts',
      {ids: nodes.map(nd=>nd.dataset.id)});
  }catch(e){ return; }
  if(!d.done) return;
  nodes.forEach(nd=>{
    const row = nd.querySelector(':scope > .srow');
    if(!row) return;
    const n = d.counts[nd.dataset.id] || 0;
    nd.dataset.sharedin = n;      // keeps the "only shared" filter honest
    let tag = row.querySelector('.sharedin');
    if(!n){ if(tag) tag.remove(); return; }
    if(!tag){
      tag = document.createElement('span');
      tag.className = 'tag sharedin';
      tag.title = 'shared items anywhere beneath this folder — '+
        'follow the trail down';
      const perms = row.querySelector('.perms');
      row.insertBefore(tag, perms);
    }
    tag.textContent = n.toLocaleString()+' shared inside';
  });
}
async function schildren(id,path){
  if(SCACHE[id]) return SCACHE[id];
  const d = await api('/api/children?id='+encodeURIComponent(id)+
    '&path='+encodeURIComponent(path||'')+'&perms=1');
  SCACHE[id]=d.items; return d.items;
}
function permChip(fid,p,canManage){
  const who = p.type==='anyone' ? 'Anyone with the link'
    : (p.email || p.name || p.domain || p.type);
  return '<span class=chip title="'+esc(who)+' — '+esc(p.role)+
    (canManage?'':' (you cannot change this — you do not own the item)')+
    '">'+esc(who)+' <span class=muted>'+esc(p.role)+'</span>'+
    (canManage
      ? ' <button class=x title="stop sharing with '+esc(who)+'" '+
        'onclick="unshare(\''+esc(fid)+'\',\''+esc(p.id)+'\',\''+esc(who)+
        '\',this)">&times;</button>'
      : ' <span class=muted title="only the owner can change this">'+
        '&#128274;</span>')+
    '</span>';
}
function srowHtml(n){
  const canManage = n.can_share !== false && n.owned !== false;
  const chips = (n.perms||[]).map(p=>permChip(n.id,p,canManage)).join('');
  return '<div class=tnode data-path="'+esc(n.path)+'" data-id="'+
    esc(n.id)+'" data-shared="'+(n.shared?1:0)+'" data-folder="'+
    (n.folder?1:0)+'" data-sharedin="'+(n.shared_inside||0)+'">'+
    '<div class=srow onclick="rowToggle(this,event)">'+
    '<span class=tw>'+(n.folder?'&#9656;':'')+'</span>'+
    '<span class=ic>'+icon(n)+'</span>'+
    '<span class=nm title="'+esc(n.path)+'">'+esc(n.name)+'</span>'+
    (n.owned===false?'<span class="tag stay">not yours</span>':'')+
    (n.folder&&n.empty?'<span class="tag stay">empty</span>':'')+
    (n.folder&&n.shared_inside?'<span class="tag sharedin" title="shared '+
      'items anywhere beneath this folder — follow the trail down">'+
      n.shared_inside.toLocaleString()+' shared inside</span>':'')+
    '<span class=perms>'+
      (n.shared ? (chips||'<span class=muted style="font-size:12px">'+
        'shared</span>') : '')+
    '</span>'+
    '</div><div class=kids style="display:none"></div></div>';
}
async function bootShare(){
  const st = document.getElementById('stree');
  st.dataset.loaded='1';
  st.innerHTML = '<div class=muted style="padding:6px;font-size:13px">'+
    '<span class=spin></span> reading sharing information&hellip;</div>';
  try{
    // Idempotent: the server refuses to start a second sweep, so flipping
    // tabs cannot restart one that is already under way.
    await api('/api/sweep',{});
    pollSweep();
  }catch(e){}
  try{
    const items = await schildren('root','');
    st.innerHTML = items.length ? items.map(srowHtml).join('')
      : '<div class=muted style="padding:6px">empty</div>';
    filterTree();
    if(SWEEP_DONE) paintShareBadges();
  }catch(e){
    st.innerHTML = '<div class=warn style="padding:6px">could not load: '+
      esc(e)+'</div>';
    st.dataset.loaded='';
  }
}
async function stog(el,id,path){
  const holder = el.closest('.tnode').querySelector(':scope > .kids');
  if(holder.dataset.loaded){
    const show = holder.style.display==='none';
    holder.style.display = show?'':'none';
    el.innerHTML = show?'&#9662;':'&#9656;';
    return;
  }
  el.innerHTML='<span class=spin></span>';
  try{
    const items = await schildren(id, path);
    holder.innerHTML = items.length ? items.map(srowHtml).join('')
      : '<div class=muted style="padding:5px 6px;font-size:13px">empty</div>';
    holder.dataset.loaded='1'; holder.style.display='';
    el.innerHTML='&#9662;';
    filterTree();
    if(SWEEP_DONE) paintShareBadges();
  }catch(e){ el.innerHTML='&#9656;';
    uiAlert(esc(niceErr(e)), {title:'Could not read that folder'}); }
}
async function unshare(fid, permId, who, btn){
  if(!await uiConfirm('Stop sharing with <b>'+esc(who)+'</b>?<br>'+
    '<span class=muted>They will lose access. The removal is recorded in '+
    'logs/share_actions.jsonl, and "undo logs/share_actions.jsonl '+
    '--execute" can restore it.</span>',
    {title:'Remove access', yes:'Remove access', danger:true})) return;
  const undo = busy(btn);
  try{
    await api('/api/unshare',{file_id:fid, permission_id:permId});
    const chip = btn.closest('.chip');
    if(chip) chip.remove();
    Object.keys(SCACHE).forEach(k=>{
      SCACHE[k].forEach(it=>{
        if(it.id===fid && it.perms)
          it.perms = it.perms.filter(p=>p.id!==permId);
      });
    });
  }catch(e){
    undo();
    const raw = String(e);
    const denied = raw.indexOf('insufficientFilePermissions')>-1 ||
                   raw.indexOf('403')>-1;
    uiAlert(esc(niceErr(e))+
      (denied
        ? '<br><br>Drive would not let you change this one. That normally '+
          'means either:<ul style="margin:8px 0 0 18px;padding:0">'+
          '<li>someone else owns it — only the owner can change who it is '+
          'shared with; or</li>'+
          '<li>the access comes from a parent folder that is shared. '+
          'Remove it on that folder and it disappears here too.</li></ul>'
        : ''),
      {title:'Could not remove access'});
  }
}
async function refreshShare(){
  for(const k in SCACHE) delete SCACHE[k];
  document.getElementById('stree').dataset.loaded='';
  await bootShare();
}

// ---- trash view -----------------------------------------------------------
// Strictly read-only plus restore. There is deliberately no way to empty
// the trash or permanently delete anything from here.
function trashRow(n){
  return '<div class=tnode data-id="'+esc(n.id)+'" data-folder="0" '+
    'data-path="'+esc(n.name)+'">'+
    '<div class=srow onclick="rowToggle(this,event)">'+
    '<span class=tw></span>'+
    '<span class=ic>'+icon(n)+'</span>'+
    '<span class=nm title="'+esc(n.name)+'">'+esc(n.name)+'</span>'+
    '<span class=mt>'+(n.size?human(n.size)+' &middot; ':'')+
    esc(n.modified||'')+'</span>'+
    '<button class="btn ghost" style="padding:2px 9px;font-size:12px" '+
    'onclick="restoreItem(\''+esc(n.id)+'\',\''+esc(n.name)+'\',this)">'+
    'Restore</button>'+
    '</div></div>';
}
async function bootTrash(){
  const tt = document.getElementById('ttree');
  tt.dataset.loaded='1';
  tt.innerHTML = '<div class=muted style="padding:6px;font-size:13px">'+
    '<span class=spin></span> reading the trash&hellip;</div>';
  try{
    const d = await api('/api/trash');
    TCACHE = d.items;
    tt.innerHTML = (d.items.length
      ? '<div class=muted style="font-size:12.5px;margin:0 0 8px">'+
        d.items.length.toLocaleString()+' item(s) in trash'+
        (d.truncated?' (showing the first '+d.items.length.toLocaleString()+
          ')':'')+' &middot; view and restore only — this page cannot '+
        'empty the trash</div>'+
        d.items.map(trashRow).join('')
      : '<div class=muted style="padding:6px">The trash is empty.</div>');
    filterTree();
  }catch(e){
    tt.innerHTML = '<div class=warn style="padding:6px">could not load: '+
      esc(e)+'</div>';
    tt.dataset.loaded='';
  }
}
async function restoreItem(fid, name, btn){
  const ok = await uiConfirm('Restore <b>'+esc(name)+
    '</b> from the trash?<br><span class=muted>It returns to where it was. '+
    'The action is recorded in logs/trash_actions.jsonl.</span>',
    {title:'Restore from trash', yes:'Restore'});
  if(!ok) return;
  const undo = busy(btn, 'Restoring…');
  try{
    await api('/api/restore',{id:fid});
    const nd = btn.closest('.tnode');
    if(nd) nd.remove();
    TCACHE = TCACHE.filter(i=>i.id!==fid);
  }catch(e){
    undo();
    uiAlert(esc(niceErr(e)), {title:'Could not restore'});
  }
}
async function refreshTrash(){
  document.getElementById('ttree').dataset.loaded='';
  await bootTrash();
}
async function refreshActive(btn){
  const done = busy(btn, 'Refreshing…');
  try{
    if(VIEW==='share') await refreshShare();
    else if(VIEW==='trash') await refreshTrash();
    else await refreshTree();
  } finally { done(); }
}
function findNode(p){
  const all = document.querySelectorAll('#tree .tnode');
  for(const nd of all){ if(nd.dataset.path===p) return nd; }
  return null;
}
// Re-read the tree from Drive in place: root folders, destination list and
// every folder you had expanded — without a page reload, keeping your spot.
async function refreshTree(){
  if(refreshing){ refreshQueued = true; return; }
  refreshing = true;
  const tree = document.getElementById('tree');
  const stat = document.getElementById('treestat');
  tree.classList.add('updating');
  if(stat) stat.textContent = 'updating from Drive…';
  const open = Object.assign({}, OPENSET);
  Object.keys(OPENSET).forEach(k=>delete OPENSET[k]);
  try{
    for(const k in CACHE) delete CACHE[k];
    const items = await children('root','');
    ROOT_FOLDERS = items.filter(i=>i.folder).map(i=>i.name);
    rebuildCats(); drawCats();
    tree.innerHTML = items.map(rowHtml).join('');
    // Re-expand what was open, parents before children. A folder that moved
    // away simply is not found any more — that is the update working.
    const paths = Object.keys(open).sort((a,b)=>
      a.split('/').length - b.split('/').length || (a<b?-1:1));
    for(const p of paths){
      const nd = findNode(p);
      if(!nd || !nd.dataset.id) continue;
      const tw = nd.querySelector(':scope > .trow > .tw');
      if(tw) await tog(tw, nd.dataset.id, p);
    }
    drawPlan(); filterTree();
    // Moves change paths and can change what is shared where — the sharing
    // view re-reads on its next visit (or right now if it is on screen).
    for(const k in SCACHE) delete SCACHE[k];
    const st = document.getElementById('stree');
    if(st){ st.dataset.loaded=''; if(VIEW==='share') bootShare(); }
    if(stat) stat.textContent = '';
  }catch(e){
    if(stat) stat.textContent = 'could not update: '+e;
  }finally{
    tree.classList.remove('updating');
    refreshing = false;
    if(refreshQueued){ refreshQueued = false; refreshTree(); }
  }
}

// ---- execute
async function execute(){
  const items = Object.values(PLAN);
  if(!items.length) return;
  for(const it of items){
    if(it.target===it.path || it.target.indexOf(it.path+'/')===0){
      await uiAlert('<b>'+esc(it.path)+'</b> cannot move into <b>'+
        esc(it.target)+'</b> — that is itself, or inside itself. Remove '+
        'that entry first.', {title:'Impossible move'});
      return;
    }
  }
  const done = busy(document.getElementById('go'), 'Checking…');
  let d;
  try{ d = await api('/api/preview',{plan:items}); }
  catch(e){ uiAlert(esc(niceErr(e)), {title:'Could not build the plan'});
    return; }
  finally{ done(); }
  const ov=document.getElementById('ov'), box=document.getElementById('ovbox');
  const cl = d.clashes || [];
  // Name clashes: Drive happily keeps two items with the same name in one
  // folder, so this must be an explicit decision rather than a silent one.
  const clashBlock = cl.length
    ? '<div class=card style="border-color:var(--warn)">'+
      '<strong>'+cl.length+' name'+(cl.length===1?'':'s')+
      ' already exist'+(cl.length===1?'s':'')+' at the destination</strong>'+
      '<div class=plog style="max-height:150px">'+
      cl.map(c=>esc(c.target+'/'+c.name)+'   (existing '+
        (c.existing.folder?'folder':'file')+
        (c.existing.modified?', modified '+esc(c.existing.modified):'')+
        ')').join('\n')+'</div>'+
      '<div style="margin-top:11px;display:flex;flex-direction:column;'+
      'gap:7px">'+
      '<label><input type=radio name=confl value=keep checked> '+
      '<b>Keep both</b> — Drive allows two items with the same name; you '+
      'end up with a duplicate side by side.</label>'+
      '<label><input type=radio name=confl value=replace> '+
      '<b>Replace</b> — the existing item is moved to Drive trash '+
      '(recoverable 30 days) and restored if you undo this run.</label>'+
      '</div></div>'
    : '';
  box.innerHTML = '<h2 style="margin-top:0">Confirm — '+d.ops.length+
    ' move'+(d.ops.length===1?'':'s')+'</h2>'+
    (d.warnings.length?'<div class=warn>'+d.warnings.map(esc).join('<br>')+
      '</div>':'')+
    clashBlock+
    '<p class=muted>New folders: '+(d.creates.length?
      d.creates.map(esc).join(', '):'none')+'</p>'+
    '<div class=plog>'+d.ops.map(o=>esc(o.from)+'  →  '+
      esc(o.to)).join('\n')+'</div>'+
    '<p class=muted style="margin-top:14px">No file is deleted. An undo log '+
    'is written before the first change.</p>'+
    '<div style="display:flex;gap:9px;margin-top:6px">'+
    '<button class=btn id=runbtn onclick="run(this)">Run these '+
      d.ops.length+' move'+(d.ops.length===1?'':'s')+'</button>'+
    '<button class="btn ghost" onclick=closeOv()>Cancel</button></div>';
  ov.classList.add('on');
}
function conflictChoice(){
  const el = document.querySelector('input[name=confl]:checked');
  return el ? el.value : 'keep';
}
function closeOv(){ document.getElementById('ov').classList.remove('on'); }
async function run(btn){
  const conflict = conflictChoice();
  LAST_ACTION = 'run';
  const done = busy(btn, 'Starting…');
  const box=document.getElementById('ovbox');
  try{ await api('/api/execute',
    {plan:Object.values(PLAN), on_conflict:conflict}); }
  catch(e){ done();
    box.innerHTML='<h2 class=warn style="margin-top:0">Could not start</h2>'+
    '<p>'+esc(niceErr(e))+'</p>'+
    '<button class=btn onclick=closeOv()>Close</button>'; return; }
  box.innerHTML='<h2 style="margin-top:0">Running</h2>'+
    '<div class=pbar><div class=pfill id=pf></div></div>'+
    '<div id=pt class=muted>starting…</div><div class=plog id=pl></div>';
  try{ localStorage.removeItem('gdo_plan'); }catch(e){}
  poll();
}
async function poll(){
  let s;
  try{ s = await api('/api/progress'); }catch(e){ setTimeout(poll,700); return; }
  const pf=document.getElementById('pf'), pt=document.getElementById('pt'),
        pl=document.getElementById('pl');
  if(pf){
    const pct = s.total? Math.round(s.done/s.total*100):0;
    pf.style.width = pct+'%';
    pt.textContent = s.done+' of '+s.total+' — '+(s.total-s.done)+
      ' left'+(s.current?' — '+s.current:'');
    pl.textContent = s.log.join('\n'); pl.scrollTop = pl.scrollHeight;
  }
  if(!s.finished){ setTimeout(poll,500); return; }
  const undone = LAST_ACTION==='undo';
  LAST_RUN = {done:s.done, total:s.total, log:s.undo_log||''};
  const box=document.getElementById('ovbox');
  box.innerHTML = '<h2 style="margin-top:0">'+
    (s.errors.length?'Finished with errors':(undone?'Undone':'Done'))+
    '</h2>'+
    '<p>'+s.done+' of '+s.total+(undone?' record':' move')+
    (s.total===1?'':'s')+(undone?' reversed.':' completed.')+
    (s.errors.length?' <span class=warn>'+s.errors.length+' failed.</span>':'')+
    '</p>'+
    (s.verify?'<p class=muted>'+esc(s.verify)+'</p>':'')+
    '<div class=plog>'+esc(s.log.join('\n'))+
    (s.errors.length?'\n\n'+esc(s.errors.join('\n')):'')+'</div>'+
    '<p class=muted>Log: <code>'+esc(s.undo_log||'')+'</code></p>'+
    '<p class=muted id=refnote>The tree behind this dialog is updating '+
    'itself&hellip;</p>'+
    '<div style="display:flex;gap:9px;margin-top:10px">'+
    '<button class=btn onclick=closeOv()>Close</button>'+
    ((s.undo_log && !undone)
      ? '<button class="btn ghost" onclick=undoRun()>Undo everything</button>'
      : '')+'</div>';
  Object.keys(PLAN).forEach(k=>delete PLAN[k]);
  drawPlan();
  loadRuns();
  pollIndex();          // moves changed paths; show the re-index
  refreshTree().then(()=>{
    const n=document.getElementById('refnote');
    if(n) n.textContent='The tree behind this dialog is up to date.';
  });
}
// Undo straight after a run: name what is about to be reversed, because
// this is the one people click while the results are still on screen.
async function undoRun(){
  const n = LAST_RUN.done || 0;
  const ok = await uiConfirm(
    'Reverse the '+n+' change'+(n===1?'':'s')+' this run just made?'+
    '<br><span class=muted>Everything goes back where it was. Items '+
    'already back in place are skipped, folders this run created are '+
    'removed if empty, and anything it replaced comes out of the trash.'+
    '</span>',
    {title:'Undo this run', yes:'Undo everything', danger:true});
  if(!ok) return;
  await startUndo('');
}
async function undoLog(log){
  if(!await uiConfirm('Reverse every change recorded in <b>'+
     esc(log||'the last run')+'</b>?<br><span class=muted>Items already '+
     'back in place are skipped, and empty folders that run created are '+
     'removed.</span>',
     {title:'Undo revision', yes:'Undo it', danger:true})) return;
  await startUndo(log);
}
async function startUndo(log){
  LAST_ACTION = 'undo';
  closeMenus();
  const ov=document.getElementById('ov'), box=document.getElementById('ovbox');
  box.innerHTML='<h2 style="margin-top:0">Undoing</h2>'+
    '<div class=pbar><div class=pfill id=pf></div></div>'+
    '<div id=pt class=muted>starting…</div><div class=plog id=pl></div>';
  ov.classList.add('on');
  try{ await api('/api/undo', log?{log:log}:{}); }
  catch(e){ box.innerHTML='<h2 class=warn style="margin-top:0">Could not '+
    'start</h2><p>'+esc(niceErr(e))+'</p>'+
    '<button class=btn onclick=closeOv()>Close</button>'; return; }
  poll();
}

// ---- revisions ------------------------------------------------------------
async function loadRuns(){
  let d;
  try{ d = await api('/api/runs'); }catch(e){ return; }
  const el = document.getElementById('runs');
  if(!el) return;
  const totalRuns = d.runs.length;
  const totalItems = d.runs.reduce((a,r)=>a+(r.moves||0),0);
  const sum = document.getElementById('runsum');
  if(sum) sum.textContent = totalRuns
    ? totalRuns.toLocaleString()+' revision'+(totalRuns===1?'':'s')+
      ' · '+totalItems.toLocaleString()+' item'+(totalItems===1?'':'s')+
      ' organized'
    : '';
  el.innerHTML = d.runs.length
    ? d.runs.map((r,i)=>
        '<div class=pi><span class=a title="'+esc(r.log)+'">'+esc(r.when)+
        (r.kind!=='ui'?' <span class=muted style="font-size:11px">('+
          esc(r.kind)+')</span>':'')+'</span>'+
        '<span class=muted style="font-size:12px;white-space:nowrap">'+
        r.moves+' organized</span>'+
        '<span class=menuwrap>'+
        '<button class=dots title="actions" onclick="toggleMenu(event,'+i+
          ')">&#8942;</button>'+
        '<span class=menu id=menu'+i+'>'+
        '<button onclick="runDetails(\''+esc(r.log)+'\')">View details'+
          '</button>'+
        '<button onclick="undoLog(\''+esc(r.log)+'\')">Undo this run'+
          '</button>'+
        '</span></span></div>').join('')
    : '<div class=muted style="font-size:13px;padding:6px 0">No runs yet. '+
      'Each execute will appear here with its own actions.</div>';
}
function closeMenus(){
  document.querySelectorAll('.menu.on').forEach(m=>m.classList.remove('on'));
}
function toggleMenu(ev, i){
  ev.stopPropagation();
  const m = document.getElementById('menu'+i);
  const was = m.classList.contains('on');
  closeMenus();
  if(!was) m.classList.add('on');
}
document.addEventListener('click', closeMenus);
// Full record-by-record view of what a run did — the audit trail, readable.
async function runDetails(log){
  closeMenus();
  const box = document.getElementById('ovbox');
  box.innerHTML = '<h2 style="margin-top:0">Revision details</h2>'+
    '<p class=muted><span class=spin></span> reading '+esc(log)+'&hellip;</p>';
  document.getElementById('ov').classList.add('on');
  let d;
  try{ d = await api('/api/run?log='+encodeURIComponent(log)); }
  catch(e){
    box.innerHTML = '<h2 style="margin-top:0">Revision details</h2>'+
      '<p class=warn>Could not read that log: '+esc(e)+'</p>'+
      '<button class=btn onclick=closeOv()>Close</button>';
    return;
  }
  const label = {move:'moved', mkdir:'created folder', trash:'replaced',
                 untrash:'restored', unshare:'removed access'};
  const rows = d.records.map(r=>{
    const op = label[r.op] || r.op || 'change';
    let what;
    if(r.op==='mkdir') what = esc(r.path||'');
    else if(r.op==='trash')
      what = esc(r.from||r.name||'')+
        (r.replaced_by?'  <span class=muted>— replaced by '+
          esc(r.replaced_by)+'</span>':'')+
        '  <span class=muted>(in trash)</span>';
    else if(r.op==='unshare')
      what = esc((r.permission||{}).emailAddress||
        (r.permission||{}).type||'')+' <span class=muted>on '+
        esc(r.file_id||'')+'</span>';
    else what = esc(r.from||r.name||'')+
      (r.to?'  <span class=muted>&rarr;</span>  '+esc(r.to):'');
    return '<tr><td class=op>'+esc(op)+'</td><td>'+what+'</td></tr>';
  }).join('');
  box.innerHTML = '<h2 style="margin-top:0">Revision details</h2>'+
    '<p class=muted style="overflow-wrap:anywhere">'+esc(log)+' &middot; '+
    d.total.toLocaleString()+' record'+(d.total===1?'':'s')+
    (d.total>d.records.length?' (showing the first '+
      d.records.length.toLocaleString()+')':'')+'</p>'+
    '<div style="max-height:52vh;overflow:auto;border:1px solid var(--ring);'+
    'border-radius:9px"><table class=dtable>'+rows+'</table></div>'+
    '<div style="display:flex;gap:9px;margin-top:14px">'+
    '<button class="btn ghost" onclick="undoLog(\''+esc(log)+
      '\')">Undo this run</button>'+
    '<button class=btn onclick=closeOv()>Close</button></div>';
}

// ---- boot
async function boot(){
  const d = await api('/api/root');
  ROOT = d.root;
  // Un-executed annotations survive a refresh or a closed tab; they are
  // cleared the moment a run starts, because the server owns them from then.
  try{
    const sp = JSON.parse(localStorage.getItem('gdo_plan')||'{}');
    Object.keys(sp).forEach(k=>{
      if(sp[k] && sp[k].id && sp[k].target) PLAN[k]=sp[k];
    });
  }catch(e){}
  // If a run is in flight — because the tab was refreshed or reopened — pick
  // the progress display back up instead of leaving it invisible. Finished
  // runs live in the Revisions panel, each with its own undo action.
  try{
    const s = await api('/api/progress');
    if(!s.finished && s.total){
      document.getElementById('ovbox').innerHTML =
        '<h2 style="margin-top:0">Reconnected to a run in progress</h2>'+
        '<p class=muted>This run started before the page was reloaded. It '+
        'kept going on the server.</p>'+
        '<div class=pbar><div class=pfill id=pf></div></div>'+
        '<div id=pt class=muted>…</div><div class=plog id=pl></div>';
      document.getElementById('ov').classList.add('on');
      poll();
    }
  }catch(e){}
  loadRuns();
  pollIndex();
  document.getElementById('sub').textContent =
    'Reading your Drive live' + (d.inventory?
      ' — folder totals from your last scan' : '') + '.';
  document.getElementById('foot').innerHTML =
    'Server running from this terminal. Close it with Ctrl+C when you are '+
    'done. Undo logs are written beside the script.';
  const items = await children('root', '');
  ROOT_FOLDERS = items.filter(i=>i.folder).map(i=>i.name);
  rebuildCats();
  drawCats();
  document.getElementById('tree').innerHTML = items.map(rowHtml).join('');
  drawPlan();
}
boot();
</script></body></html>"""


class _Job:
    def __init__(self) -> None:
        self.total = 0
        self.done = 0
        self.current = ""
        self.log: List[str] = []
        self.errors: List[str] = []
        self.finished = True
        self.undo_log = ""
        self.verify = ""


def run_ui(args) -> None:
    import http.server
    import socketserver
    import threading
    import urllib.parse
    import webbrowser

    service = get_service(args.credentials, args.token)
    root_id = with_backoff(
        service.files().get(fileId="root", fields="id").execute)["id"]

    me = ""
    try:
        me = with_backoff(service.about().get(
            fields="user(emailAddress)").execute)["user"]["emailAddress"]
    except Exception:
        pass

    # The discovery client is not thread-safe, and this server answers
    # requests on threads while a run may be executing. locked() serializes
    # the actual HTTP exchange; backoff sleeps happen outside the lock so a
    # rate-limited write does not freeze reads for the whole wait.
    api_lock = threading.Lock()

    def locked(req):
        def _run():
            with api_lock:
                return req.execute()
        return with_backoff(_run)

    # Session token: the page URL carries it once, every request must echo
    # it. Blocks other websites (CSRF) and DNS-rebinding pages from driving
    # this server.
    auth = secrets.token_urlsafe(16)

    logo_bytes = b""
    for cand in (os.path.join("assets", "logo.png"), "logo.png"):
        if os.path.exists(cand):
            with open(cand, "rb") as fh:
                logo_bytes = fh.read()
            break

    # Optional: rolled-up totals from the last scan, purely informational.
    stats: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(args.inventory):
        try:
            rows = load_inventory(args.inventory)
            files = [r for r in rows if not r["is_folder"]]
            files.sort(key=lambda r: r["path"])
            paths = [r["path"] for r in files]
            for r in rows:
                if not r["is_folder"] or not r["path"]:
                    continue
                pre = r["path"] + "/"
                lo = bisect.bisect_left(paths, pre)
                hi = bisect.bisect_left(paths, pre + "￿")
                under = files[lo:hi]
                stats[r["file_id"]] = {
                    "files": len(under),
                    "bytes": sum(x["size_bytes"] for x in under)}
            print(f"  loaded folder totals for {len(stats):,} folders "
                  f"from {args.inventory}")
        except Exception as err:
            print(f"  (could not read {args.inventory}: {err})")

    job = _Job()
    page = UI_HTML.replace("__CSS__", CSS).replace("__TOKEN__", auth)

    def list_children(fid: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = None
        while True:
            resp = locked(service.files().list(
                q=f"'{fid}' in parents and trashed = false",
                fields="nextPageToken, files(id,name,mimeType,size,"
                       "modifiedTime,shared,owners(emailAddress),"
                       "ownedByMe,capabilities(canShare),webViewLink)",
                pageSize=1000, pageToken=token,
                orderBy="folder,name"))
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return out

    def empty_flags(folder_ids: List[str]) -> Dict[str, bool]:
        """Which of these folders have no un-trashed children.

        Batched: one query asks about ~20 folders at once, so flagging a
        listing costs a couple of calls, not one per folder.
        """
        flags = {fid: True for fid in folder_ids}
        chunk_size = 20
        for i in range(0, len(folder_ids), chunk_size):
            chunk = folder_ids[i:i + chunk_size]
            q_or = " or ".join(f"'{c}' in parents" for c in chunk)
            pending = set(chunk)
            token = None
            while pending:
                resp = locked(service.files().list(
                    q=f"trashed = false and ({q_or})",
                    fields="nextPageToken, files(parents)",
                    pageSize=1000, pageToken=token))
                for f in resp.get("files", []):
                    for par in f.get("parents", []):
                        if par in pending:
                            flags[par] = False
                            pending.discard(par)
                token = resp.get("nextPageToken")
                if not token:
                    break
        return flags

    # ---- sharing sweep ----------------------------------------------------
    # One light pass over the whole Drive (id/parents/shared only) so every
    # folder can say how many shared items sit anywhere beneath it. That is
    # what lets the Sharing tab guide you top-down instead of making you
    # open everything.
    sweep: Dict[str, Any] = {"running": False, "done": False, "seen": 0,
                             "shared_count": 0, "folders": {}, "at": "",
                             "error": "", "people": [], "items": [],
                             "public": 0, "detailed": 0, "phase": ""}
    sweep_lock = threading.Lock()

    def start_sweep() -> bool:
        """Start a sweep unless one is already going. Returns True if
        this call started it.

        The running flag is set HERE, under a lock, not inside the thread:
        setting it in the worker leaves a window where two quick requests
        both see 'not running' and start two sweeps, which then reset each
        other's counters — the sweep appears to restart on every click.
        """
        with sweep_lock:
            if sweep["running"]:
                return False
            sweep.update(running=True, done=False, seen=0, shared_count=0,
                         folders={}, error="", people=[], items=[],
                         public=0, detailed=0, phase="scanning")
        threading.Thread(target=sweep_worker, daemon=True).start()
        return True

    def sweep_worker() -> None:
        parents_map: Dict[str, Optional[str]] = {}
        names: Dict[str, str] = {}
        shared: List[Dict[str, Any]] = []
        token = None
        try:
            while True:
                resp = locked(service.files().list(
                    q="trashed = false", corpora="user",
                    fields="nextPageToken, files(id,name,parents,shared,"
                           "mimeType,webViewLink,ownedByMe,"
                           "capabilities(canShare))",
                    pageSize=1000, pageToken=token))
                batch = resp.get("files", [])
                sweep["seen"] += len(batch)
                for f in batch:
                    parents_map[f["id"]] = (f.get("parents") or [None])[0]
                    names[f["id"]] = f.get("name", "")
                    if f.get("shared"):
                        shared.append(f)
                token = resp.get("nextPageToken")
                if not token:
                    break

            counts: Dict[str, int] = {}
            for f in shared:
                cur = parents_map.get(f["id"])
                hops = 0
                while cur and hops < 200:
                    counts[cur] = counts.get(cur, 0) + 1
                    cur = parents_map.get(cur)
                    hops += 1
            sweep["folders"] = counts
            sweep["shared_count"] = len(shared)

            def path_for(fid: str) -> str:
                parts: List[str] = []
                cur: Optional[str] = fid
                hops = 0
                while cur and hops < 200:
                    nm = names.get(cur)
                    if nm is None:
                        break
                    parts.append(nm)
                    cur = parents_map.get(cur)
                    hops += 1
                return "/".join(reversed(parts))

            # Who each shared item is exposed to. One permissions call per
            # shared item, so it is capped and reported honestly rather
            # than silently truncated.
            sweep["phase"] = "reading permissions"
            tally: Dict[str, Dict[str, Any]] = {}
            items: List[Dict[str, Any]] = []
            public = 0
            for f in shared[:MAX_SHARE_DETAIL]:
                if not sweep["running"]:
                    break
                perms = perms_of(f["id"])
                sweep["detailed"] += 1
                if not perms:
                    continue
                is_public = any(p["type"] == "anyone" for p in perms)
                if is_public:
                    public += 1
                for p in perms:
                    who = (p["email"] or p["domain"]
                           or ("Anyone with the link"
                               if p["type"] == "anyone" else p["type"]))
                    slot = tally.setdefault(who, {
                        "who": who, "type": p["type"], "count": 0,
                        "roles": set()})
                    slot["count"] += 1
                    slot["roles"].add(p["role"])
                items.append({
                    "id": f["id"], "name": f.get("name", ""),
                    "path": path_for(f["id"]),
                    "folder": f.get("mimeType") == FOLDER_MIME,
                    "mime": f.get("mimeType", ""),
                    "link": f.get("webViewLink", ""),
                    "public": is_public,
                    "can_share": bool(f.get("ownedByMe", True)) and bool(
                        (f.get("capabilities") or {}).get("canShare", True)),
                    "perms": perms})
            sweep["people"] = sorted(
                ({"who": v["who"], "type": v["type"], "count": v["count"],
                  "roles": sorted(v["roles"])} for v in tally.values()),
                key=lambda p: -p["count"])
            sweep["items"] = items
            sweep["public"] = public
            sweep["at"] = datetime.now().strftime("%H:%M")
            sweep["phase"] = ""
            sweep["done"] = True
        except Exception as err:
            sweep["error"] = f"{type(err).__name__}: {err}"
        finally:
            sweep["running"] = False

    def list_trash(limit: int = 2000) -> Tuple[List[Dict[str, Any]], bool]:
        out: List[Dict[str, Any]] = []
        token = None
        while True:
            resp = locked(service.files().list(
                q="trashed = true and 'me' in owners",
                fields="nextPageToken, files(id,name,mimeType,size,"
                       "modifiedTime,webViewLink)",
                pageSize=1000, pageToken=token))
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token or len(out) >= limit:
                break
        return out[:limit], bool(token)

    # ---- folder index -----------------------------------------------------
    # Every folder in the Drive, by full path, so the destination search can
    # find a folder you have never expanded. Folders only — far lighter than
    # a full crawl — built once in the background at start-up.
    folder_index: Dict[str, Any] = {"ready": False, "building": False,
                                    "paths": [], "known": set(), "count": 0,
                                    "at": "", "error": "", "expected": 0,
                                    "stale": False, "again": False}
    findex_lock = threading.Lock()
    FINDEX_FILE = os.path.join(LOG_DIR, "folder_index.json")

    def load_folder_cache() -> None:
        """Last run's index, so search works the moment the page opens.

        Marked stale: it is served immediately while a fresh crawl runs
        behind it, and replaced when that finishes.
        """
        if not os.path.exists(FINDEX_FILE):
            return
        try:
            with open(FINDEX_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("root") != root_id:
                return              # a different account's index
            paths = d.get("paths") or []
            folder_index.update(paths=paths, known=set(paths),
                                count=len(paths), expected=len(paths),
                                at=d.get("at", ""), ready=bool(paths),
                                stale=True)
            print(f"  loaded {len(paths):,} folder paths from "
                  f"{FINDEX_FILE} (refreshing in the background)")
        except Exception as err:
            print(f"  (could not read {FINDEX_FILE}: {err})")

    def save_folder_cache() -> None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(FINDEX_FILE, "w", encoding="utf-8") as fh:
                json.dump({"root": root_id,
                           "at": folder_index["at"],
                           "paths": folder_index["paths"]}, fh)
        except Exception:
            pass

    def index_add(path: str) -> None:
        """Make a just-created folder searchable straight away.

        Order does not matter: search ranks its own results, so appending
        is enough and avoids re-sorting thousands of entries per folder.
        """
        if not path:
            return
        with findex_lock:
            if path not in folder_index["known"]:
                folder_index["known"].add(path)
                folder_index["paths"].append(path)
                folder_index["count"] = len(folder_index["paths"])

    def build_folder_index() -> None:
        with findex_lock:
            if folder_index["building"]:
                # A rebuild was asked for while one is already running —
                # two executions finishing close together. The running
                # crawl may have started BEFORE the second run's moves, so
                # its result would already be stale. Remember that another
                # pass is owed instead of dropping the request, and run it
                # when this one finishes.
                folder_index["again"] = True
                return
            folder_index.update(building=True, error="", again=False)
        try:
            by_id: Dict[str, Tuple[str, Optional[str]]] = {}
            pending: Set[str] = set()
            # A first build has nothing to serve, so it publishes results as
            # it goes. A refresh keeps serving the cached index untouched
            # and swaps it in one step at the end.
            progressive = not folder_index["ready"]

            def path_of(fid: str) -> Optional[str]:
                chain: List[str] = []
                cur: Optional[str] = fid
                seen: Set[str] = set()
                rooted = False
                while cur and cur not in seen:
                    seen.add(cur)
                    node = by_id.get(cur)
                    if node is None:
                        break          # parent outside My Drive (or shared)
                    name, par = node
                    chain.append(name)
                    if par == root_id:
                        rooted = True
                        break
                    if not par:
                        # Orphaned: reachable in Drive, but not from My
                        # Drive's root. Treating it as top level would hand
                        # back a path that resolve() cannot find and would
                        # instead CREATE as a new folder of the same name.
                        break
                    cur = par
                # Only paths that actually reach My Drive's root are usable
                # as destinations; anything else would be a fictional path.
                return "/".join(reversed(chain)) if rooted and chain else None

            def flush_resolved() -> None:
                """Publish every pending folder whose full path is known.

                Ones still waiting on an ancestor stay pending and are
                retried after the next page, so nothing is lost.
                """
                resolved = []
                for fid in pending:
                    p = path_of(fid)
                    if p:
                        resolved.append((fid, p))
                if not resolved:
                    return
                with findex_lock:
                    for fid, p in resolved:
                        if p not in folder_index["known"]:
                            folder_index["known"].add(p)
                            folder_index["paths"].append(p)
                    folder_index["ready"] = True
                pending.difference_update(fid for fid, _ in resolved)

            token = None
            while True:
                resp = locked(service.files().list(
                    q=f"mimeType = '{FOLDER_MIME}' and trashed = false",
                    corpora="user",
                    fields="nextPageToken, files(id,name,parents)",
                    pageSize=1000, pageToken=token))
                for f in resp.get("files", []):
                    fid = f["id"]
                    by_id[fid] = (f.get("name", ""),
                                  (f.get("parents") or [None])[0])
                    pending.add(fid)
                # Live count for the progress bar. When a cached index
                # exists its size is the expected total, which is what
                # makes a real percentage possible on later runs.
                folder_index["count"] = len(by_id)
                # Every folder whose ancestors have all arrived becomes
                # searchable now rather than at the end, so the picker fills
                # up as the crawl runs instead of staying empty. Skipped
                # when a cached index is already being served — mixing a
                # half-built list into it would show stale and fresh paths
                # at once.
                if progressive:
                    flush_resolved()
                token = resp.get("nextPageToken")
                if not token:
                    break
            paths = [p for p in (path_of(f) for f in by_id) if p]
            paths.sort(key=str.lower)
            with findex_lock:
                folder_index.update(paths=paths, known=set(paths),
                                    count=len(paths), expected=len(paths),
                                    at=datetime.now().strftime("%H:%M"),
                                    ready=True, stale=False)
            save_folder_cache()
        except Exception as err:
            folder_index["error"] = f"{type(err).__name__}: {err}"
        finally:
            with findex_lock:
                folder_index["building"] = False
                owed = folder_index.get("again", False)
                folder_index["again"] = False
            if owed:
                threading.Thread(target=build_folder_index,
                                 daemon=True).start()

    def search_folders(q: str, limit: int = 60) -> List[str]:
        """Same token rules as the picker: every token must appear, and a
        match on the folder's own name outranks one via an ancestor."""
        toks = [t for t in re.split(r"[\s/]+", q.lower()) if t]
        if not toks:
            return []
        scored: List[Tuple[int, int, str, str]] = []
        for p in folder_index["paths"]:
            low = p.lower()
            if not all(t in low for t in toks):
                continue
            leaf = low[low.rfind("/") + 1:]
            s = 0
            for t in toks:
                if leaf.startswith(t):
                    s += 3
                elif t in leaf:
                    s += 2
            scored.append((-s, p.count("/"), low, p))
        scored.sort()
        return [x[3] for x in scored[:limit]]

    def resolve_existing(path: str) -> Optional[str]:
        """Folder id for 'A/B/C' if it already exists — never creates."""
        cur = root_id
        for seg in [s for s in path.strip("/ ").split("/") if s]:
            safe = seg.replace("\\", "\\\\").replace("'", "\\'")
            hits = locked(service.files().list(
                q=(f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and "
                   f"'{cur}' in parents and trashed = false"),
                fields="files(id)", pageSize=1)).get("files", [])
            if not hits:
                return None
            cur = hits[0]["id"]
        return cur

    def find_clash(target: str, name: str,
                   moving_id: str) -> Optional[Dict[str, Any]]:
        """An item already called `name` inside `target`, if any."""
        parent = resolve_existing(target)
        if not parent:
            return None
        safe = (name or "").replace("\\", "\\\\").replace("'", "\\'")
        hits = locked(service.files().list(
            q=(f"name = '{safe}' and '{parent}' in parents and "
               f"trashed = false"),
            fields="files(id,name,mimeType,size,modifiedTime)",
            pageSize=5)).get("files", [])
        for h in hits:
            if h["id"] != moving_id:
                return {"id": h["id"], "name": h.get("name", ""),
                        "folder": h.get("mimeType") == FOLDER_MIME,
                        "size": int(h.get("size") or 0),
                        "modified": (h.get("modifiedTime") or "")[:10]}
        return None

    def perms_of(fid: str) -> List[Dict[str, Any]]:
        """Non-owner permissions on a file, for the Sharing tab."""
        try:
            resp = locked(service.permissions().list(
                fileId=fid,
                fields="permissions(id,role,type,emailAddress,"
                       "displayName,domain)"))
        except Exception:
            return []
        out = []
        for p in resp.get("permissions", []):
            if p.get("role") == "owner":
                continue
            out.append({
                "id": p.get("id", ""), "role": p.get("role", ""),
                "type": p.get("type", ""),
                "email": p.get("emailAddress", ""),
                "name": p.get("displayName", ""),
                "domain": p.get("domain", "")})
        return out

    def worker(plan: List[Dict[str, Any]],
               on_conflict: str = "keep") -> None:
        job.finished = False
        job.total = len(plan)
        job.done = 0
        job.log = []
        job.errors = []
        job.verify = ""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(LOG_DIR, exist_ok=True)
        job.undo_log = os.path.abspath(
            os.path.join(LOG_DIR, f"ui_{stamp}.jsonl"))
        folders: Optional[Folders] = None
        try:
            with open(job.undo_log, "w", encoding="utf-8") as log:
                def log_mkdir(pth: str, fid: str) -> None:
                    log.write(json.dumps({"op": "mkdir", "file_id": fid,
                                          "path": pth}) + "\n")
                    log.flush()
                    # Searchable immediately, without waiting for a rebuild.
                    index_add(pth)
                folders = Folders(service, root_id, True, call=locked,
                                  on_create=log_mkdir)
                for item in plan:
                    job.current = item["path"]
                    try:
                        tgt, pth = item["target"], item["path"]
                        if tgt == pth or tgt.startswith(pth + "/"):
                            raise ValueError(
                                "cannot move a folder into itself")
                        parent = folders.resolve(tgt)
                        if parent == item["id"]:
                            raise ValueError(
                                f"'{pth}' IS the destination folder — "
                                f"nothing to move")
                        meta = locked(service.files().get(
                            fileId=item["id"], fields="id,name,parents"))
                        old = meta.get("parents", [])
                        # Name clash at the destination. Re-checked here,
                        # not trusted from the preview, because the folder
                        # may have changed since you looked at it.
                        if on_conflict == "replace":
                            ex = find_clash(tgt, item.get("name") or "",
                                            item["id"])
                            if ex:
                                locked(service.files().update(
                                    fileId=ex["id"],
                                    body={"trashed": True}))
                                log.write(json.dumps({
                                    "op": "trash", "file_id": ex["id"],
                                    "name": ex["name"],
                                    "from": f"{tgt}/{ex['name']}",
                                    "replaced_by": pth}) + "\n")
                                log.flush()
                                job.log.append(
                                    f"replaced  {tgt}/{ex['name']}  "
                                    f"(moved to trash)")
                        if old == [parent]:
                            job.log.append(f"already in place  {pth}")
                        else:
                            params: Dict[str, Any] = {
                                "fileId": item["id"], "body": {},
                                "addParents": parent}
                            if old:
                                params["removeParents"] = ",".join(old)
                            locked(service.files().update(**params))
                            log.write(json.dumps({
                                "op": "move", "file_id": item["id"],
                                "name": meta.get("name"),
                                "old_parents": old,
                                "from": pth, "to": tgt}) + "\n")
                            log.flush()
                            job.log.append(f"moved  {pth}  ->  {tgt}")
                    except HttpError as err:
                        hint = ""
                        if getattr(err.resp, "status", None) == 400:
                            hint = ("  (Drive refused this — usually a "
                                    "folder moving into itself or its own "
                                    "subtree)")
                        msg = f"FAILED {item['path']}: {err}{hint}"
                        job.errors.append(msg)
                        job.log.append(msg)
                    except Exception as err:
                        msg = f"FAILED {item['path']}: {err}"
                        job.errors.append(msg)
                        job.log.append(msg)
                    job.done += 1
            if folders:
                for c in folders.created:
                    job.log.append(f"created folder  {c}")
            good = 0
            for item in plan:
                try:
                    meta = locked(service.files().get(
                        fileId=item["id"], fields="id,trashed"))
                    if not meta.get("trashed"):
                        good += 1
                except Exception:
                    pass
            job.verify = (f"verified: {good} of {len(plan)} planned items "
                          f"present and untrashed")
        finally:
            job.current = ""
            job.finished = True
            # Moves rewrite the paths of everything underneath them, so the
            # index is refreshed behind the scenes. The old one stays
            # searchable until the new one lands.
            threading.Thread(target=build_folder_index, daemon=True).start()

    def undo_worker(path: str) -> None:
        if not path or not os.path.exists(path):
            job.finished = True
            return
        job.undo_log = path
        with open(path, encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh if l.strip()]
        recs.reverse()
        job.finished = False
        job.total = len(recs)
        job.done = 0
        job.log = []
        job.errors = []
        for rec in recs:
            try:
                if rec.get("op") == "trash":
                    # An item this run replaced: bring it back out of the
                    # trash. Must be handled explicitly — falling through to
                    # the move branch would strip its parents.
                    locked(service.files().update(
                        fileId=rec["file_id"], body={"trashed": False}))
                    job.log.append(f"restored  {rec.get('from', '')}")
                elif rec.get("op") == "untrash":
                    locked(service.files().update(
                        fileId=rec["file_id"], body={"trashed": True}))
                    job.log.append(f"re-trashed  {rec.get('name', '')}")
                elif rec.get("op") == "mkdir":
                    # A folder this run created — remove it again, but only
                    # if it is empty now.
                    kids = locked(service.files().list(
                        q=f"'{rec['file_id']}' in parents and "
                          f"trashed = false",
                        fields="files(id)", pageSize=1)).get("files")
                    if kids:
                        job.log.append(f"kept folder  {rec.get('path', '')}"
                                       f"  — not empty")
                    else:
                        locked(service.files().update(
                            fileId=rec["file_id"], body={"trashed": True}))
                        job.log.append(
                            f"removed empty folder  {rec.get('path', '')}")
                else:
                    cur = locked(service.files().get(
                        fileId=rec["file_id"], fields="id,parents"))
                    cp = set(cur.get("parents") or [])
                    op = set(rec.get("old_parents") or [])
                    add, rem = op - cp, cp - op
                    if add or rem:
                        p: Dict[str, Any] = {"fileId": rec["file_id"],
                                             "body": {}}
                        if add:
                            p["addParents"] = ",".join(add)
                        if rem:
                            p["removeParents"] = ",".join(rem)
                        locked(service.files().update(**p))
                        job.log.append(f"reverted  {rec.get('from', '')}")
                    else:
                        job.log.append(f"already back  {rec.get('from', '')}")
            except Exception as err:
                job.errors.append(
                    f"{rec.get('from', rec.get('path', ''))}: {err}")
            job.done += 1
        job.finished = True

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authed(self) -> bool:
            return self._local() and self.headers.get("X-Auth") == auth

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                q = urllib.parse.parse_qs(u.query)
                if not self._local() or (q.get("t") or [""])[0] != auth:
                    self._send({"error": "forbidden"}, 403)
                    return
                b = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if u.path == "/logo":
                # <img> tags cannot send the auth header; a logo is not a
                # secret, so the Host check alone gates it.
                if not self._local() or not logo_bytes:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(logo_bytes)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(logo_bytes)
                return
            if u.path == "/api/zip":
                # Lists what is inside an archive without extracting it.
                # Nothing is written anywhere: the bytes are read into
                # memory, the central directory is parsed, and the listing
                # is returned.
                q = urllib.parse.parse_qs(u.query)
                if not self._authed():
                    self._send({"error": "forbidden"}, 403)
                    return
                fid = (q.get("id") or [""])[0]
                try:
                    meta = locked(service.files().get(
                        fileId=fid, fields="id,name,size"))
                    size = int(meta.get("size") or 0)
                    if size > MAX_ZIP_BYTES:
                        self._send({"error": f"archive is {human(size)}; "
                                             f"the listing limit is "
                                             f"{human(MAX_ZIP_BYTES)}"}, 413)
                        return
                    with api_lock:
                        blob = with_backoff(service.files().get_media(
                            fileId=fid).execute)
                    entries = []
                    with zipfile.ZipFile(io.BytesIO(blob)) as z:
                        for info in z.infolist()[:MAX_ZIP_ENTRIES]:
                            entries.append({
                                "name": info.filename,
                                "dir": info.is_dir(),
                                "size": info.file_size,
                                "packed": info.compress_size,
                                "date": ("%04d-%02d-%02d" % info.date_time[:3]
                                         if info.date_time[0] >= 1980
                                         else "")})
                        total = len(z.infolist())
                except zipfile.BadZipFile:
                    self._send({"error": "not a readable zip archive"}, 415)
                    return
                except Exception as err:
                    self._send({"error": f"{type(err).__name__}: {err}"},
                               500)
                    return
                self._send({"name": meta.get("name", ""), "entries": entries,
                            "total": total,
                            "truncated": total > len(entries),
                            "bytes": sum(e["size"] for e in entries)})
                return

            if u.path == "/api/file":
                # Streams a file's own bytes so previews show the real
                # thing. <img> and <iframe> cannot send headers, so this
                # one authenticates on the query token instead — same
                # secret, same localhost-only Host check.
                q = urllib.parse.parse_qs(u.query)
                if not self._local() or (q.get("t") or [""])[0] != auth:
                    self.send_response(403)
                    self.end_headers()
                    return
                fid = (q.get("id") or [""])[0]
                try:
                    meta = locked(service.files().get(
                        fileId=fid, fields="id,name,mimeType,size"))
                    mime = meta.get("mimeType", "")
                    size = int(meta.get("size") or 0)
                    if size > MAX_PREVIEW_BYTES:
                        self._send({"error": "too large to preview"}, 413)
                        return
                    if mime.startswith("application/vnd.google-apps."):
                        # Native Docs/Sheets/Slides have no bytes of their
                        # own; export a PDF rendering to preview instead.
                        export = EXPORT_AS.get(mime)
                        if not export:
                            self._send({"error": "no preview for this "
                                                 "Google file type"}, 415)
                            return
                        with api_lock:
                            data = with_backoff(service.files().export_media(
                                fileId=fid, mimeType=export).execute)
                        ctype = export
                    else:
                        with api_lock:
                            data = with_backoff(service.files().get_media(
                                fileId=fid).execute)
                        ctype = mime or "application/octet-stream"
                except Exception as err:
                    self._send({"error": f"{type(err).__name__}: {err}"},
                               500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", "inline")
                self.end_headers()
                self.wfile.write(data)
                return
            if not self._authed():
                self._send({"error": "forbidden"}, 403)
                return
            if u.path == "/api/root":
                self._send({"root": root_id, "inventory": bool(stats)})
                return
            if u.path == "/api/children":
                q = urllib.parse.parse_qs(u.query)
                fid = (q.get("id") or ["root"])[0]
                want_perms = (q.get("perms") or ["0"])[0] == "1"
                try:
                    kids = list_children(fid)
                except Exception as err:
                    self._send({"error": str(err)}, 500)
                    return
                parent_path = (q.get("path") or [""])[0]
                items = []
                for k in kids:
                    folder = k.get("mimeType") == FOLDER_MIME
                    st = stats.get(k["id"], {})
                    own = (k.get("owners") or [{}])[0].get(
                        "emailAddress", "")
                    item = {
                        "id": k["id"], "name": k.get("name", ""),
                        "path": (parent_path + "/" + k.get("name", ""))
                                if parent_path else k.get("name", ""),
                        "folder": folder,
                        "mime": k.get("mimeType", ""),
                        "size": int(k.get("size") or 0) if not folder else 0,
                        "files": st.get("files") if folder else None,
                        "bytes": st.get("bytes") if folder else None,
                        "owned": (not me) or own == me,
                        "shared": bool(k.get("shared")),
                        # Only the owner can change who a file is shared
                        # with; asking Drive up front means the × is not
                        # offered where it would only produce a 403.
                        "can_share": bool(k.get("ownedByMe", True)) and bool(
                            (k.get("capabilities") or {}).get(
                                "canShare", True)),
                        "link": k.get("webViewLink", ""),
                        "modified": (k.get("modifiedTime") or "")[:10]}
                    if want_perms and item["shared"]:
                        item["perms"] = perms_of(k["id"])
                    items.append(item)
                try:
                    flags = empty_flags(
                        [i["id"] for i in items if i["folder"]])
                except Exception:
                    flags = {}
                for i2 in items:
                    if i2["folder"]:
                        i2["empty"] = flags.get(i2["id"], False)
                        # files/bytes come from the last scan, emptiness is
                        # live. When they disagree the live answer wins —
                        # a folder emptied since the scan must not keep
                        # advertising the old contents.
                        if i2["empty"]:
                            i2["files"] = None
                            i2["bytes"] = None
                        if want_perms and sweep["done"]:
                            i2["shared_inside"] = sweep["folders"].get(
                                i2["id"], 0)
                self._send({"items": items})
                return
            if u.path == "/api/folders":
                q = urllib.parse.parse_qs(u.query)
                term = (q.get("q") or [""])[0]
                if not folder_index["ready"] and not folder_index["building"]:
                    threading.Thread(target=build_folder_index,
                                     daemon=True).start()
                # Search whatever is indexed so far — a half-built index is
                # still useful, and it fills in as the crawl proceeds.
                self._send({"ready": folder_index["ready"],
                            "building": folder_index["building"],
                            "count": folder_index["count"],
                            "usable": len(folder_index["paths"]),
                            "expected": folder_index["expected"],
                            "stale": folder_index["stale"],
                            "at": folder_index["at"],
                            "error": folder_index["error"],
                            "items": search_folders(term)})
                return
            if u.path == "/api/sweep":
                self._send({"running": sweep["running"],
                            "done": sweep["done"], "seen": sweep["seen"],
                            "shared_count": sweep["shared_count"],
                            "people": sweep["people"][:60],
                            "people_total": len(sweep["people"]),
                            "public": sweep["public"],
                            "detailed": sweep["detailed"],
                            "capped": sweep["shared_count"] >
                                      MAX_SHARE_DETAIL,
                            "cap": MAX_SHARE_DETAIL,
                            "phase": sweep["phase"],
                            "at": sweep["at"], "error": sweep["error"]})
                return
            if u.path == "/api/shared":
                # The flat list behind the exposure summary: every shared
                # item, optionally narrowed to one person.
                q = urllib.parse.parse_qs(u.query)
                who = (q.get("who") or [""])[0]
                items = sweep["items"]
                if who:
                    items = [it for it in items
                             if any((p["email"] or p["domain"]
                                     or ("Anyone with the link"
                                         if p["type"] == "anyone"
                                         else p["type"])) == who
                                    for p in it["perms"])]
                self._send({"items": items[:1000],
                            "total": len(items),
                            "who": who})
                return
            if u.path == "/api/run":
                q = urllib.parse.parse_qs(u.query)
                name = (q.get("log") or [""])[0]
                if not re.fullmatch(
                        r"(ui|apply|quarantine)_\d{8}_\d{6}\.jsonl", name):
                    self._send({"error": "unknown log"}, 400)
                    return
                cand = os.path.join(LOG_DIR, name)
                if not os.path.exists(cand):
                    cand = name
                if not os.path.exists(cand):
                    self._send({"error": "unknown log"}, 400)
                    return
                recs = []
                try:
                    with open(cand, encoding="utf-8") as fh:
                        for line in fh:
                            if line.strip():
                                recs.append(json.loads(line))
                except Exception as err:
                    self._send({"error": str(err)}, 500)
                    return
                self._send({"log": name, "records": recs[:2000],
                            "total": len(recs)})
                return

            if u.path == "/api/trash":
                try:
                    raw, truncated = list_trash()
                except Exception as err:
                    self._send({"error": str(err)}, 500)
                    return
                titems = [{
                    "id": t["id"], "name": t.get("name", ""),
                    "folder": t.get("mimeType") == FOLDER_MIME,
                    "mime": t.get("mimeType", ""),
                    "size": int(t.get("size") or 0),
                    "modified": (t.get("modifiedTime") or "")[:10],
                    "link": t.get("webViewLink", "")} for t in raw]
                self._send({"items": titems, "truncated": truncated})
                return
            if u.path == "/api/progress":
                self._send({"total": job.total, "done": job.done,
                            "current": job.current, "log": job.log[-400:],
                            "errors": job.errors, "finished": job.finished,
                            "undo_log": job.undo_log, "verify": job.verify})
                return
            if u.path == "/api/runs":
                runs = []
                base = LOG_DIR if os.path.isdir(LOG_DIR) else "."
                for fn in os.listdir(base):
                    m = re.fullmatch(
                        r"(ui|apply|quarantine)_(\d{8})_(\d{6})\.jsonl", fn)
                    if not m:
                        continue
                    moves = folders = 0
                    try:
                        with open(os.path.join(base, fn),
                                  encoding="utf-8") as fh:
                            for line in fh:
                                if not line.strip():
                                    continue
                                rec = json.loads(line)
                                if rec.get("op") == "mkdir":
                                    folders += 1
                                else:
                                    moves += 1
                    except Exception:
                        continue
                    if not moves and not folders:
                        continue
                    d8, t6 = m.group(2), m.group(3)
                    runs.append({
                        "log": fn, "kind": m.group(1),
                        "stamp": f"{d8}_{t6}",
                        "when": f"{d8[:4]}-{d8[4:6]}-{d8[6:]} "
                                f"{t6[:2]}:{t6[2:4]}",
                        "moves": moves, "folders": folders})
                runs.sort(key=lambda r: r["stamp"], reverse=True)
                self._send({"runs": runs[:20]})
                return
            self._send({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authed():
                self._send({"error": "forbidden"}, 403)
                return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            u = urllib.parse.urlparse(self.path)

            if u.path == "/api/preview":
                plan = body.get("plan") or []
                warnings: List[str] = []
                seen: Dict[str, str] = {}
                for it in plan:
                    if it["id"] in seen:
                        warnings.append(f"{it['path']} listed twice")
                    seen[it["id"]] = it["path"]
                for it in plan:
                    tgt = it.get("target") or ""
                    pth = it.get("path") or ""
                    if tgt == pth or tgt.startswith(pth + "/"):
                        warnings.append(
                            f"{pth}: cannot move a folder into itself")
                    parent_dir = pth.rsplit("/", 1)[0] if "/" in pth else ""
                    if tgt and tgt == parent_dir:
                        warnings.append(f"{pth}: already inside {tgt} — "
                                        f"nothing will change")
                    if not it.get("id"):
                        warnings.append(f"{pth}: missing file id — remove "
                                        f"and re-add this entry")
                for a in plan:
                    for b2 in plan:
                        if a is not b2 and a["path"].startswith(
                                b2["path"] + "/"):
                            warnings.append(
                                f"{a['path']} sits inside {b2['path']}, "
                                f"which is also moving")
                clashes = []
                for it in plan:
                    try:
                        ex = find_clash(it.get("target") or "",
                                        it.get("name") or "",
                                        it.get("id") or "")
                    except Exception:
                        ex = None
                    if ex:
                        clashes.append({
                            "path": it.get("path") or "",
                            "target": it.get("target") or "",
                            "name": it.get("name") or "",
                            "existing": ex})
                ops = [{"from": it["path"],
                        "to": it["target"] + "/" + it["name"]}
                       for it in plan]
                self._send({"ops": ops,
                            "creates": sorted({it["target"] for it in plan}),
                            "clashes": clashes,
                            "warnings": sorted(set(warnings))})
                return

            if u.path == "/api/execute":
                if not job.finished:
                    self._send({"error": "a run is already in progress"}, 409)
                    return
                plan = body.get("plan") or []
                if not plan:
                    self._send({"error": "nothing to do"}, 400)
                    return
                conflict = body.get("on_conflict")
                if conflict not in ("keep", "replace"):
                    conflict = "keep"
                threading.Thread(target=worker, args=(plan, conflict),
                                 daemon=True).start()
                self._send({"started": True})
                return

            if u.path == "/api/sweep":
                self._send({"started": start_sweep()})
                return

            if u.path == "/api/sweepcounts":
                # Badge counts for folders already on screen, so a finished
                # sweep paints in place instead of rebuilding the tree.
                ids = body.get("ids") or []
                self._send({"done": sweep["done"],
                            "counts": {i: sweep["folders"].get(i, 0)
                                       for i in ids if isinstance(i, str)}})
                return

            if u.path == "/api/restore":
                fid = (body.get("id") or "").strip()
                if not fid:
                    self._send({"error": "missing id"}, 400)
                    return
                try:
                    meta = locked(service.files().get(
                        fileId=fid, fields="id,name,trashed"))
                    if not meta.get("trashed"):
                        self._send({"error": "not in trash"}, 400)
                        return
                    locked(service.files().update(
                        fileId=fid, body={"trashed": False}))
                except Exception as err:
                    self._send({"error": f"{type(err).__name__}: {err}"}, 500)
                    return
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(os.path.join(LOG_DIR, "trash_actions.jsonl"),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "op": "untrash", "file_id": fid,
                        "name": meta.get("name"),
                        "at": datetime.now().isoformat(
                            timespec="seconds")}) + "\n")
                self._send({"ok": True})
                return

            if u.path == "/api/rmdir":
                fid = (body.get("id") or "").strip()
                if not fid:
                    self._send({"error": "missing id"}, 400)
                    return
                try:
                    meta = locked(service.files().get(
                        fileId=fid, fields="id,name,mimeType"))
                    if meta.get("mimeType") != FOLDER_MIME:
                        self._send({"error": "not a folder"}, 400)
                        return
                    # Emptiness is re-checked server-side at this moment —
                    # the flag in the page could be stale.
                    kids = locked(service.files().list(
                        q=f"'{fid}' in parents and trashed = false",
                        fields="files(id)", pageSize=1)).get("files")
                    if kids:
                        self._send({"error": "folder is not empty"}, 400)
                        return
                    locked(service.files().update(
                        fileId=fid, body={"trashed": True}))
                except Exception as err:
                    self._send({"error": f"{type(err).__name__}: {err}"}, 500)
                    return
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(os.path.join(LOG_DIR, "folder_actions.jsonl"),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "op": "trash", "file_id": fid,
                        "name": meta.get("name"),
                        "at": datetime.now().isoformat(
                            timespec="seconds")}) + "\n")
                self._send({"ok": True})
                return

            if u.path == "/api/unshare":
                fid = (body.get("file_id") or "").strip()
                pid = (body.get("permission_id") or "").strip()
                if not fid or not pid:
                    self._send({"error": "missing file_id or permission_id"},
                               400)
                    return
                try:
                    # Read the permission first so the log records exactly
                    # who lost access — that is what makes undo possible.
                    detail = locked(service.permissions().get(
                        fileId=fid, permissionId=pid,
                        fields="id,role,type,emailAddress,domain"))
                    if detail.get("role") == "owner":
                        self._send({"error": "cannot remove the owner"}, 400)
                        return
                    locked(service.permissions().delete(
                        fileId=fid, permissionId=pid))
                except Exception as err:
                    self._send({"error": f"{type(err).__name__}: {err}"}, 500)
                    return
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(os.path.join(LOG_DIR, "share_actions.jsonl"),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "op": "unshare", "file_id": fid,
                        "permission": detail,
                        "at": datetime.now().isoformat(
                            timespec="seconds")}) + "\n")
                self._send({"ok": True})
                return

            if u.path == "/api/undo":
                if not job.finished:
                    self._send({"error": "a run is in progress"}, 409)
                    return
                name = (body.get("log") or "").strip()
                if name:
                    # Strict allowlist: a known log name, never a path.
                    if not re.fullmatch(
                            r"(ui|apply|quarantine)_\d{8}_\d{6}\.jsonl",
                            name):
                        self._send({"error": "unknown undo log"}, 400)
                        return
                    cand = os.path.join(LOG_DIR, name)
                    if not os.path.exists(cand):
                        cand = name          # legacy: still in the root
                    if not os.path.exists(cand):
                        self._send({"error": "unknown undo log"}, 400)
                        return
                    path = os.path.abspath(cand)
                else:
                    path = job.undo_log
                if not path:
                    self._send({"error": "nothing to undo yet"}, 400)
                    return
                threading.Thread(target=undo_worker, args=(path,),
                                 daemon=True).start()
                self._send({"started": True})
                return

            self._send({"error": "not found"}, 404)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    # Index folders up front so the destination search can reach anywhere in
    # the Drive, not just what has been expanded on screen. Last run's index
    # loads instantly and is refreshed in the background.
    load_folder_cache()
    threading.Thread(target=build_folder_index, daemon=True).start()

    with Server(("127.0.0.1", args.port), H) as httpd:
        url = f"http://127.0.0.1:{args.port}/?t={auth}"
        print(f"\n  Live interface:  {url}")
        print("  Reads your Drive as you expand folders.")
        print("  It can create folders and move things. It cannot delete.")
        print("\n  Press Ctrl+C to stop.\n")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")


def main() -> None:
    p = argparse.ArgumentParser(
        description=HEADER,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--credentials", default="credentials.json")
    p.add_argument("--token", default="token.json")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="crawl and produce the report [read-only]")
    s.add_argument("--out-dir", default="drive_scan")
    s.add_argument("--dup-min-size", type=int, default=0, metavar="BYTES",
                   help="ignore files smaller than this in duplicate "
                        "analysis (default 0 = complete)")
    s.set_defaults(func=cmd_scan)

    rb = sub.add_parser(
        "retree",
        help="rebuild report + tree from the last scan, no crawl [read-only]")
    rb.add_argument("--inventory", default="drive_scan/inventory.csv")
    rb.add_argument("--dup-min-size", type=int, default=0, metavar="BYTES",
                    help="ignore files smaller than this in duplicate "
                         "analysis (default 0 = complete)")
    rb.set_defaults(func=cmd_retree)

    v = sub.add_parser("preview", help="render a mapping [read-only]")
    v.add_argument("mapping")
    v.add_argument("--out", default="preview.html")
    v.set_defaults(func=cmd_preview)

    a = sub.add_parser("apply", help="execute a mapping")
    a.add_argument("mapping")
    a.add_argument("--execute", action="store_true")
    a.add_argument("--force", action="store_true")
    a.add_argument("--max-ops", type=int, default=15)
    a.add_argument("--allow-large", action="store_true")
    a.set_defaults(func=cmd_apply)

    q = sub.add_parser("quarantine", help="move duplicates aside for review")
    q.add_argument("--inventory", default="drive_scan/inventory.csv")
    q.add_argument("--manifest",
                   default=os.path.join(LOG_DIR, "quarantine_manifest.json"))
    q.add_argument("--execute", action="store_true")
    q.add_argument("--force", action="store_true")
    q.add_argument("--max-ops", type=int, default=15)
    q.add_argument("--allow-large", action="store_true")
    q.add_argument("--dup-min-size", type=int, default=0, metavar="BYTES",
                   help="ignore files smaller than this in duplicate "
                        "analysis (default 0 = complete)")
    q.set_defaults(func=cmd_quarantine)

    r = sub.add_parser("review", help="browse quarantined duplicates")
    r.add_argument("--manifest",
                   default=os.path.join(LOG_DIR, "quarantine_manifest.json"))
    r.add_argument("--port", type=int, default=8765)
    r.set_defaults(func=run_review)

    w = sub.add_parser("verify", help="check a baseline [read-only]")
    w.add_argument("--baseline", default="drive_scan/inventory.csv")
    w.set_defaults(func=cmd_verify)

    g = sub.add_parser(
        "ui", help="live interface — browse your real Drive and organise it")
    g.add_argument("--port", type=int, default=8777)
    g.add_argument("--inventory", default="drive_scan/inventory.csv",
                   help="optional: folder totals from a previous scan")
    g.set_defaults(func=run_ui)

    u = sub.add_parser("undo", help="reverse a run")
    u.add_argument("log")
    u.add_argument("--execute", action="store_true")
    u.set_defaults(func=run_undo)

    args = p.parse_args()
    migrate_generated()
    args.func(args)


if __name__ == "__main__":
    main()
