#!/usr/bin/env python3
"""
seed_channels.py: find YouTube channels that are live right now and
collect them into a CSV you can review.

Setup (one time, ~5 minutes):
  1. Go to https://console.cloud.google.com, create a project.
  2. APIs & Services -> Library -> enable "YouTube Data API v3".
  3. APIs & Services -> Credentials -> Create credentials -> API key.

Run:
  python3 seed_channels.py --key YOUR_API_KEY
  (or set the env var YT_API_KEY and just run python3 seed_channels.py)

  python3 seed_channels.py --dry-run          # show queries + quota cost, no API calls
  python3 seed_channels.py --only worship animals

Output: channels.csv. Open it in Sheets/Excel, mark the "approved" column
y or n, and add notes. Re-running merges new finds into the same file and
keeps your approvals, notes and category edits. Run it at different times
and days (Sunday mornings catch masses across timezones) to grow the list.

Quota: the free tier is 10,000 units/day. Each search costs 100, so the
default query list (~60 searches) uses about 6,000. The extra detail
lookups cost 1 unit per 50 results.
"""

import argparse
import csv
import html
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

API = "https://www.googleapis.com/youtube/v3/"
COST = {"search": 100, "channels": 1, "videos": 1}

# Edit freely. Category -> search phrases (any language).
QUERIES = {
    "worship": [
        "holy mass live", "live church service", "divine liturgy live",
        "perpetual adoration live", "santa misa en vivo", "adoración eucarística en vivo",
        "missa ao vivo", "msza święta na żywo", "messe en direct",
        "heilige messe live", "santa messa in diretta", "banal na misa live",
        "masjid live", "temple live darshan", "gurdwara live kirtan",
    ],
    "animals": [
        "zoo live cam", "aquarium live cam", "bird feeder live cam",
        "eagle nest cam", "panda live cam", "farm live cam",
        "kitten cam live", "zoo en vivo", "動物園 ライブ", "zoo live kamera",
    ],
    "civic": [
        "city council meeting live", "board of supervisors meeting live",
        "school board meeting live", "town hall meeting live",
        "sesión ordinaria en vivo", "câmara municipal ao vivo",
        "conseil municipal en direct",
    ],
    "scenic": [
        "live webcam", "harbor cam live", "beach cam live", "ski resort live cam",
        "volcano live cam", "northern lights live", "ライブカメラ",
        "cámara en vivo", "webcam ao vivo", "webcam live meteo",
    ],
    "industrial": [
        "railfan live cam", "train cam live", "airport live cam",
        "port live cam ships", "canal locks live cam", "construction cam live",
        "traffic cam live", "bridge cam live",
    ],
    "oddities": [
        "local tv live", "public access tv live", "radio studio live cam",
        "livestock auction live", "bingo live", "aquarium jellyfish live",
        "laundromat live", "fish tank live stream",
    ],
}

FIELDS = [
    "approved", "category", "notes",
    "channel_title", "country", "subscribers",
    "last_live_title", "concurrent_viewers",
    "matched_queries", "times_seen", "first_seen", "last_seen",
    "channel_url", "last_live_url", "channel_id", "last_video_id",
]

# Columns you edit by hand; never overwritten by a re-run.
USER_FIELDS = {"approved", "category", "notes"}


class QuotaExceeded(Exception):
    pass


class Client:
    def __init__(self, key, budget):
        self.key, self.budget, self.used = key, budget, 0

    def get(self, endpoint, **params):
        cost = COST[endpoint]
        if self.used + cost > self.budget:
            raise QuotaExceeded(f"local budget of {self.budget} units reached")
        params["key"] = self.key
        url = API + endpoint + "?" + urlencode(params)
        try:
            with urlopen(url, timeout=30) as r:
                data = json.load(r)
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 403 and "quota" in body.lower():
                raise QuotaExceeded("YouTube daily quota used up (resets at midnight Pacific)")
            raise RuntimeError(f"{endpoint} failed ({e.code}): {body[:400]}")
        except URLError as e:
            raise RuntimeError(f"network error: {e.reason}")
        self.used += cost
        return data


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def search_live(client, query, pages):
    out, token = [], None
    for _ in range(pages):
        params = dict(part="snippet", type="video", eventType="live",
                      q=query, maxResults=50)
        if token:
            params["pageToken"] = token
        data = client.get("search", **params)
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid:
                out.append((vid, item["snippet"]))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def load_existing(path):
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {row["channel_id"]: row for row in csv.DictReader(f) if row.get("channel_id")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default=os.environ.get("YT_API_KEY"), help="YouTube Data API key")
    ap.add_argument("--out", default="channels.csv")
    ap.add_argument("--budget", type=int, default=9000, help="max quota units to spend this run")
    ap.add_argument("--pages", type=int, default=1, help="result pages per query (50 each, 100 units each)")
    ap.add_argument("--only", nargs="*", help="limit to these categories")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queries = [(cat, q) for cat, qs in QUERIES.items()
               if not args.only or cat in args.only for q in qs]
    est = len(queries) * args.pages * 100

    if args.dry_run:
        for cat, q in queries:
            print(f"  [{cat}] {q}")
        print(f"\n{len(queries)} queries, ~{est} search units (+ a few for details). Budget: {args.budget}")
        return
    if not args.key:
        sys.exit("No API key. Pass --key YOUR_KEY or set YT_API_KEY (see the top of this file).")

    client = Client(args.key, args.budget)
    found = {}   # channel_id -> info
    videos = {}  # video_id -> channel_id

    print(f"Searching {len(queries)} queries (~{est} units)...")
    try:
        for i, (cat, q) in enumerate(queries, 1):
            hits = search_live(client, q, args.pages)
            print(f"  {i:>3}/{len(queries)} [{cat}] {q!r}: {len(hits)} live")
            for vid, sn in hits:
                cid = sn["channelId"]
                c = found.setdefault(cid, {"title": html.unescape(sn.get("channelTitle", "")),
                                           "cats": Counter(), "queries": set()})
                c["cats"][cat] += 1
                c["queries"].add(q)
                c["video_id"] = vid
                c["video_title"] = html.unescape(sn.get("title", ""))
                videos[vid] = cid
    except QuotaExceeded as e:
        print(f"Stopped searching early: {e}")

    # Cheap detail lookups: viewer counts and channel info.
    try:
        for batch in chunks(videos, 50):
            data = client.get("videos", part="liveStreamingDetails", id=",".join(batch))
            for item in data.get("items", []):
                cid = videos.get(item["id"])
                if cid and found[cid].get("video_id") == item["id"]:
                    found[cid]["viewers"] = item.get("liveStreamingDetails", {}).get("concurrentViewers", "")
        for batch in chunks(found, 50):
            data = client.get("channels", part="snippet,statistics", id=",".join(batch))
            for item in data.get("items", []):
                sn, st = item.get("snippet", {}), item.get("statistics", {})
                found[item["id"]]["country"] = sn.get("country", "")
                found[item["id"]]["subs"] = "" if st.get("hiddenSubscriberCount") else st.get("subscriberCount", "")
    except QuotaExceeded as e:
        print(f"Skipped some detail lookups: {e}")

    # Merge with the existing file, keeping hand edits.
    rows = load_existing(args.out)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new = 0
    for cid, c in found.items():
        row = rows.get(cid)
        if row is None:
            new += 1
            row = {k: "" for k in FIELDS}
            row.update(channel_id=cid, first_seen=now, times_seen="0",
                       category=c["cats"].most_common(1)[0][0])
            rows[cid] = row
        old_q = set(filter(None, row.get("matched_queries", "").split(" | ")))
        row.update(
            channel_title=c["title"],
            country=c.get("country", row.get("country", "")),
            subscribers=c.get("subs", row.get("subscribers", "")),
            last_live_title=c.get("video_title", ""),
            concurrent_viewers=c.get("viewers", ""),
            matched_queries=" | ".join(sorted(old_q | c["queries"])),
            times_seen=str(int(row.get("times_seen") or 0) + 1),
            last_seen=now,
            channel_url=f"https://www.youtube.com/channel/{cid}",
            last_live_url=f"https://www.youtube.com/watch?v={c['video_id']}",
            last_video_id=c["video_id"],
        )

    # Unreviewed first, then by category, then smallest channels first
    # (small channels are usually the "public access" gems).
    def sort_key(r):
        subs = int(r["subscribers"]) if (r.get("subscribers") or "").isdigit() else 10**12
        return (r.get("approved", "").strip() != "", r.get("category", ""), subs)

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=sort_key):
            w.writerow({k: r.get(k, "") for k in FIELDS})

    print(f"\nDone. {len(found)} live channels this run, {new} new. "
          f"{len(rows)} total in {args.out}. Quota used: {client.used} units.")


if __name__ == "__main__":
    main()
