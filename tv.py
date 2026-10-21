#!/usr/bin/env python3
"""
tv.py: the shuffle TV, run locally.

Put tv.py, index.html, fullscreen.html and channels.csv in the same folder, then run:

    python tv.py --key YOUR_API_KEY

(or save your key in a file called key.txt next to this script and just run
`python tv.py`). Your browser opens to http://localhost:8000 (the TV set);
http://localhost:8000/fullscreen.html is the edge-to-edge version. Leave the
terminal window open; closing it turns the TV off.

The public website runs `python tv.py --once` on a schedule instead: one
check, write live.json, exit.

What it does:
  * Reads channels.csv. Every channel NOT marked "n" in the approved column is
    on the dial (use --approved-only once you've reviewed, to only use "y").
  * Every --every minutes it figures out which of those channels are live:
      1. re-checks the last live video it knew about for each channel, and
      2. reads each channel's free RSS feed to catch newly started streams,
    then confirms them all with cheap YouTube API calls (1 unit per 50 videos).
  * Skips streams whose owners disabled embedding, since those can't play.
  * Writes live.json and serves it plus the page.
  * In the TV, press X to reject the channel you're on (it's marked "n" in
    channels.csv and skipped) and U to undo the last reject.

Quota: about 60-100 units per check for ~1,000 channels. At the default of
every 20 minutes that's roughly 5,000-7,000 of your 10,000 daily units, so
don't run a big seed_channels.py the same day as a full day of TV, or raise
--every to 30.
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://www.googleapis.com/youtube/v3/videos"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id="
STATE_FILE = os.path.join(HERE, "live_state.json")   # last known live video per channel
LIVE_FILE = os.path.join(HERE, "live.json")          # what the page plays
PENDING_FILE = os.path.join(HERE, "rejected.txt")    # rejects not yet written to the CSV

live_lock = threading.Lock()
csv_lock = threading.Lock()
CHANNELS = {}        # what's on the dial (shared with the checker)
REMOVED = {}         # channels rejected this session, kept so U can undo
CSV_PATH = None
live_data = {"updated": None, "checking": False, "progress": "", "streams": []}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- inputs ----------

def load_channels(path, approved_only):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        cid = (r.get("channel_id") or "").strip()
        verdict = (r.get("approved") or "").strip().lower()
        if not cid or verdict.startswith("n") or cid in pending_rejects():
            continue
        if approved_only and not verdict.startswith("y"):
            continue
        out[cid] = {
            "channel_id": cid,
            "channel_title": r.get("channel_title", ""),
            "category": (r.get("category") or "other").strip().lower(),
            "country": (r.get("country") or "").strip().upper(),
            "csv_video": (r.get("last_video_id") or "").strip(),
        }
    return out


def pending_rejects():
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except OSError:
        return set()


def save_pending(ids):
    if ids:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(ids)) + "\n")
    elif os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)


def write_verdicts(changes):
    """changes: {channel_id: (approved, note)}. Rewrites channels.csv. Returns True if saved."""
    with csv_lock:
        try:
            with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fields, rows = reader.fieldnames, list(reader)
            for r in rows:
                if r.get("channel_id") in changes:
                    r["approved"], r["notes"] = changes[r["channel_id"]]
            tmp = CSV_PATH + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
            os.replace(tmp, CSV_PATH)
            return True
        except OSError:
            return False


def flush_pending():
    """Write any rejects that couldn't be saved earlier (e.g. the CSV was open in Excel)."""
    ids = pending_rejects()
    if ids and write_verdicts({cid: ("n", "rejected from TV") for cid in ids}):
        save_pending(set())
        log(f"Saved {len(ids)} earlier reject(s) to channels.csv.")


def reject(cid):
    ch = CHANNELS.pop(cid, None)
    if ch is None:
        return "not on the dial"
    with live_lock:
        gone = [s for s in live_data["streams"] if s["channel_id"] == cid]
        live_data["streams"] = [s for s in live_data["streams"] if s["channel_id"] != cid]
        save_json(LIVE_FILE, live_data)
    REMOVED[cid] = (ch, gone)
    if write_verdicts({cid: ("n", "rejected from TV")}):
        log(f"Rejected: {ch['channel_title']}")
        return "saved"
    save_pending(pending_rejects() | {cid})
    log(f"Rejected: {ch['channel_title']} (channels.csv is open elsewhere; saved to rejected.txt, "
        "it'll be written in when the file is free)")
    return "pending"


def unreject(cid):
    if cid not in REMOVED:
        return "nothing to undo"
    ch, streams = REMOVED.pop(cid)
    CHANNELS[cid] = ch
    with live_lock:
        live_data["streams"] = sort_streams({s["channel_id"]: s for s in live_data["streams"] + streams})
        save_json(LIVE_FILE, live_data)
    save_pending(pending_rejects() - {cid})
    write_verdicts({cid: ("", "")})
    log(f"Un-rejected: {ch['channel_title']}")
    return "restored"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------- YouTube ----------

class QuotaExceeded(Exception):
    pass


def http_get(url, timeout=20):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (shuffle-tv)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def rss_latest(cid, n):
    """Newest n video IDs from a channel's public feed. Free, no quota."""
    try:
        xml = http_get(RSS + cid).decode("utf-8", "replace")
    except (HTTPError, URLError, OSError):
        return []
    return re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", xml)[:n]


def videos_details(key, ids):
    """Batch-check videos, 50 per call, 1 quota unit per call."""
    out, calls = {}, 0
    ids = list(ids)
    for i in range(0, len(ids), 50):
        q = urlencode({"part": "snippet,liveStreamingDetails,status",
                       "id": ",".join(ids[i:i + 50]), "key": key})
        try:
            data = json.loads(http_get(f"{API}?{q}"))
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 403 and "quota" in body.lower():
                raise QuotaExceeded()
            if e.code in (400, 403):
                sys.exit(f"YouTube rejected the request ({e.code}). Is the API key right?\n{body[:300]}")
            log(f"  videos call failed ({e.code}), skipping a batch")
            continue
        except (URLError, OSError) as e:
            log(f"  network error ({e}), skipping a batch")
            continue
        calls += 1
        for item in data.get("items", []):
            out[item["id"]] = item
    return out, calls


# ---------- the check ----------

def confirm(key, channels, cands):
    """Ask YouTube which candidate videos are live + embeddable. Returns (streams by channel, units, blocked)."""
    details, calls = videos_details(key, cands)
    best, blocked = {}, 0
    for vid, item in details.items():
        sn = item.get("snippet", {})
        if sn.get("liveBroadcastContent") != "live":
            continue
        cid = cands.get(vid) or sn.get("channelId")
        if cid not in channels:
            continue
        if item.get("status", {}).get("embeddable") is False:
            blocked += 1
            continue
        lsd = item.get("liveStreamingDetails", {})
        viewers = int(lsd.get("concurrentViewers") or 0)
        prev = best.get(cid)
        if prev and prev["viewers"] >= viewers:
            continue
        ch = channels[cid]
        best[cid] = {
            "video_id": vid,
            "channel_id": cid,
            "channel_title": sn.get("channelTitle") or ch["channel_title"],
            "title": sn.get("title", ""),
            "category": ch["category"],
            "country": ch["country"],
            "viewers": viewers,
            "started": lsd.get("actualStartTime", ""),
        }
    return best, calls, blocked


def sort_streams(best):
    return sorted(best.values(), key=lambda s: (s["category"], s["channel_title"].lower()))


def check_once(key, channels, rss_n, workers, publish):
    started = time.time()
    channels = dict(channels)
    state = load_json(STATE_FILE, {})

    # Pass 1 (a few seconds): re-check the last live video we knew for each
    # channel. Most 24/7 cams keep the same video for weeks, so this alone
    # puts most of the dial on the air right away.
    known = {}
    for cid, ch in channels.items():
        for vid in (state.get(cid), ch["csv_video"]):
            if vid:
                known[vid] = cid
    set_progress("re-checking known streams")
    best, units, blocked = confirm(key, channels, known)
    publish(sort_streams(best))
    log(f"Quick pass: {len(best)} live from known streams ({time.time() - started:.0f}s).")

    # Pass 2 (a minute or two): read every channel's RSS feed to catch streams
    # that started since, like a church that just went live for mass.
    new = {}
    done = 0
    total = len(channels)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, vids in zip(channels, pool.map(lambda c: rss_latest(c, rss_n), channels)):
            done += 1
            if done % 50 == 0:
                set_progress(f"scanning feeds {done}/{total}")
            for vid in vids:
                if vid not in known:
                    new.setdefault(vid, cid)
    set_progress("confirming new streams")
    more, units2, blocked2 = confirm(key, channels, new)
    for cid, s in more.items():
        if cid not in best or s["viewers"] > best[cid]["viewers"]:
            best[cid] = s
    units += units2
    blocked += blocked2

    for cid, s in best.items():
        state[cid] = s["video_id"]
    save_json(STATE_FILE, state)

    streams = sort_streams(best)
    publish(streams)
    log(f"{len(streams)} of {len(channels)} channels live "
        f"({blocked} skipped: embedding off). {units} quota units, {time.time() - started:.0f}s.")
    return streams


def set_progress(msg):
    with live_lock:
        live_data["progress"] = msg


def checker_loop(args, channels):
    def publish(streams):
        with live_lock:
            live_data["streams"] = [s for s in streams if s["channel_id"] in CHANNELS]
            live_data["updated"] = datetime.now(timezone.utc).isoformat()
            save_json(LIVE_FILE, live_data)

    while True:
        with live_lock:
            live_data["checking"] = True
        flush_pending()
        try:
            check_once(args.key, channels, args.rss, args.workers, publish)
        except QuotaExceeded:
            log("YouTube daily quota used up. Keeping the last list; it resets at midnight Pacific.")
        except Exception as e:  # keep the TV on whatever happens
            log(f"Check failed: {e!r}. Keeping the last list.")
        finally:
            with live_lock:
                live_data["checking"] = False
                live_data["progress"] = ""
        time.sleep(args.every * 60)


# ---------- web server ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/live.json":
            with live_lock:
                body = json.dumps(live_data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/", "/index.html", "/fullscreen.html", "/privacy.html", "/favicon.ico"):
            self.send_error(404)   # don't serve the rest of the folder (key.txt!)
            return
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/reject", "/unreject"):
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            cid = json.loads(self.rfile.read(n) or b"{}").get("channel_id", "")
        except ValueError:
            cid = ""
        result = (reject if path == "/reject" else unreject)(cid) if cid else "no channel"
        body = json.dumps({"result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="YouTube Data API key (or put it in key.txt)")
    ap.add_argument("--csv", default=os.path.join(HERE, "channels.csv"))
    ap.add_argument("--every", type=float, default=20, help="minutes between checks (default 20)")
    ap.add_argument("--rss", type=int, default=3, help="newest uploads to check per channel (default 3)")
    ap.add_argument("--approved-only", action="store_true", help='only channels marked "y"')
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--once", action="store_true", help="run one check, write live.json and exit (used by the website's scheduled job)")
    args = ap.parse_args()

    if not args.key:
        kf = os.path.join(HERE, "key.txt")
        args.key = (open(kf).read().strip() if os.path.exists(kf) else os.environ.get("YT_API_KEY"))
    if not args.key:
        sys.exit("No API key. Run: python tv.py --key YOUR_KEY  (or save it in key.txt next to tv.py)")
    if not os.path.exists(args.csv):
        sys.exit(f"Can't find {args.csv}. Put channels.csv next to tv.py.")
    if not os.path.exists(os.path.join(HERE, "index.html")):
        sys.exit("Can't find index.html. Put it next to tv.py.")

    global CSV_PATH
    CSV_PATH = args.csv
    flush_pending()
    channels = CHANNELS
    channels.update(load_channels(args.csv, args.approved_only))
    if not channels:
        sys.exit("No channels to watch. Check the approved column in channels.csv.")
    log(f"{len(channels)} channels on the dial. Checking every {args.every:g} min.")

    cached = load_json(LIVE_FILE, None)
    if cached and cached.get("streams"):
        live_data["streams"] = [s for s in cached["streams"] if s["channel_id"] in channels]
        log(f"Loaded {len(cached['streams'])} streams from last time while the first check runs.")

    if args.once:
        def publish(streams):
            live_data["streams"] = streams
            live_data["updated"] = datetime.now(timezone.utc).isoformat()
        try:
            check_once(args.key, channels, args.rss, args.workers, publish)
        except QuotaExceeded:
            log("YouTube daily quota used up. Keeping the last list.")
        live_data["checking"], live_data["progress"] = False, ""
        save_json(LIVE_FILE, live_data)
        return

    threading.Thread(target=checker_loop, args=(args, channels), daemon=True).start()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"Port {args.port} is busy. Try: python tv.py --port 8001")
    url = f"http://localhost:{args.port}"
    log(f"TV is on at {url}  (Ctrl+C to turn it off)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Off.")


if __name__ == "__main__":
    main()
