#!/usr/bin/env python3
"""Scrape Indeed UK jobs in/around Sheffield for Ben, and merge into cache/.

Search terms are derived from Ben's CV (outdoor education, hospitality,
driving/logistics, retail/customer service, marine/mechanical engineering).
Indeed's search is description-wide, so short generic terms work well.

Usage:  python3 indeed_scraper.py
Output: cache/cached_jobs.json  (merged, deduped by job_url)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import pandas as pd
from jobspy import scrape_jobs

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "cached_jobs.json")

LOCATION = "Sheffield"
COUNTRY = "UK"
DISTANCE_MILES = 5
HOURS_OLD = 72          # Indeed: last 3 days
RESULTS_PER_SEARCH = 40
SLEEP_BETWEEN_S = 1.5
DESC_MAX_CHARS = 6000   # trim descriptions so the cache stays lean

# (search_term, primary_category)
SEARCHES = [
    ("activity instructor", "Outdoor & Education"),
    ("outdoor instructor", "Outdoor & Education"),
    ("playworker", "Outdoor & Education"),
    ("warehouse operative", "Warehouse"),
    ("picker packer", "Warehouse"),
    ("delivery driver", "Driving & Logistics"),
    ("courier driver", "Driving & Logistics"),
    ("customer service assistant", "Retail & Customer Service"),
    ("retail assistant", "Retail & Customer Service"),
    ("receptionist", "Retail & Customer Service"),
    ("kitchen assistant", "Hospitality"),
    ("kitchen porter", "Hospitality"),
    ("barista", "Hospitality"),
    ("waiting staff", "Hospitality"),
    ("maintenance operative", "Engineering & Maintenance"),
    ("maintenance technician", "Engineering & Maintenance"),
    ("fitter", "Engineering & Maintenance"),
    ("caretaker", "Engineering & Maintenance"),
]

# Title keyword -> category (adds secondary tags beyond the search's category).
TITLE_KEYWORDS = [
    (r"instructor|outdoor|activity|forest school|playwork|education", "Outdoor & Education"),
    (r"kitchen|chef|barista|waiter|waitress|bar ?staff|catering|hospitality|front of house|pot ?wash|food", "Hospitality"),
    (r"driver|delivery|courier|logistics", "Driving & Logistics"),
    (r"warehouse|picker|packer|stockroom", "Warehouse"),
    (r"retail|sales assistant|customer service|cashier|store assistant|receptionist|front desk", "Retail & Customer Service"),
    (r"maintenance|fitter|technician|engineer|mechanic|electrician|plumber|caretaker|grounds", "Engineering & Maintenance"),
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                return data
        except Exception as e:
            print(f"! could not read cache ({e}); starting fresh")
    return {"meta": {}, "jobs": {}}


def job_key(row):
    url = row.get("job_url")
    if url:
        return url
    return f"{row.get('company', '')}|{row.get('title', '')}".lower()


def clean_text(val, max_chars=None):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text or None


def normalize_date_posted(val):
    """Return 'YYYY-MM-DD' when possible, else the raw string, else None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        ts = pd.Timestamp(val)
        if pd.notna(ts):
            fmt = "%Y-%m-%d %H:%M" if (ts.hour or ts.minute) else "%Y-%m-%d"
            return ts.strftime(fmt)
    except Exception:
        pass
    return clean_text(val, 60)


def categories_for(row, primary):
    cats = {primary}
    title = (row.get("title") or "").lower()
    for pattern, cat in TITLE_KEYWORDS:
        if re.search(pattern, title):
            cats.add(cat)
    return sorted(cats)


def scrape_one(search_term):
    kwargs = dict(
        site_name=["indeed"],
        search_term=search_term,
        location=LOCATION,
        country_indeed=COUNTRY,
        distance=DISTANCE_MILES,
        hours_old=HOURS_OLD,
        results_wanted=RESULTS_PER_SEARCH,
        description_format="markdown",
        verbose=1,
    )
    df = scrape_jobs(**kwargs)
    if len(df) == 0:
        # Indeed's date filter hides many niche/local postings; retry unfenced.
        kwargs.pop("hours_old")
        df = scrape_jobs(**kwargs)
    return df


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = load_cache()
    jobs = cache["jobs"]
    run_started = now_iso()
    report = {"started": run_started, "searches": [], "new": 0, "updated": 0}

    for i, (term, primary_cat) in enumerate(SEARCHES, 1):
        entry = {"term": term, "found": 0, "new": 0}
        try:
            df = scrape_one(term)
            entry["found"] = len(df)
            for _, row in df.iterrows():
                record = {
                    "title": clean_text(row.get("title"), 200),
                    "company": clean_text(row.get("company"), 120),
                    "location": clean_text(row.get("location"), 120),
                    "job_type": clean_text(row.get("job_type"), 40),
                    "is_remote": bool(row.get("is_remote")) if pd.notna(row.get("is_remote")) else False,
                    "min_amount": None if pd.isna(row.get("min_amount")) else float(row["min_amount"]),
                    "max_amount": None if pd.isna(row.get("max_amount")) else float(row["max_amount"]),
                    "interval": clean_text(row.get("interval"), 20),
                    "currency": clean_text(row.get("currency"), 8) or "GBP",
                    "date_posted": normalize_date_posted(row.get("date_posted")),
                    "description": clean_text(row.get("description"), DESC_MAX_CHARS),
                    "job_url": clean_text(row.get("job_url"), 500),
                }
                key = job_key(record)
                if not key:
                    continue
                existing = jobs.get(key)
                if existing:
                    record["categories"] = sorted(
                        set(record_categories(record, primary_cat)) | set(existing.get("categories", []))
                    )
                    record["first_seen"] = existing.get("first_seen", run_started)
                    record["last_seen"] = run_started
                    jobs[key] = record
                    report["updated"] += 1
                else:
                    record["categories"] = record_categories(record, primary_cat)
                    record["first_seen"] = run_started
                    record["last_seen"] = run_started
                    jobs[key] = record
                    report["new"] += 1
                    entry["new"] += 1
        except Exception as e:
            entry["error"] = str(e)[:300]
        report["searches"].append(entry)
        status = entry.get("error") or f"{entry['found']} found, {entry['new']} new"
        print(f"[{i}/{len(SEARCHES)}] '{term}': {status}")
        time.sleep(SLEEP_BETWEEN_S)

    # Prune jobs not seen in the last 45 days to keep the cache lean.
    cutoff = datetime.now(timezone.utc).timestamp() - 45 * 86400
    pruned = 0
    for key in list(jobs):
        last_seen = jobs[key].get("last_seen", "")
        try:
            seen_ts = datetime.strptime(last_seen, "%Y-%m-%dT%H:%M:%SZ").timestamp()
        except Exception:
            continue
        if seen_ts < cutoff:
            del jobs[key]
            pruned += 1

    cache["meta"] = {
        "last_run": run_started,
        "location": LOCATION,
        "country": COUNTRY,
        "hours_old": HOURS_OLD,
        "distance_miles": DISTANCE_MILES,
    }
    cache["last_run_report"] = report

    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)

    print(f"\nDone: {len(jobs)} total jobs in cache "
          f"({report['new']} new, {report['updated']} refreshed, {pruned} pruned)")
    print(f"Cache: {CACHE_FILE}")
    return 0


def record_categories(record, primary_cat):
    row = {"title": record.get("title") or ""}
    return categories_for(row, primary_cat)


if __name__ == "__main__":
    sys.exit(main())
