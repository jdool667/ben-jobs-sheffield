#!/usr/bin/env python3
"""Add a 2-sentence summary + fit verdict to every job in cache/ via OpenRouter.

Only jobs missing summary/fit are sent (new additions on each re-scrape).
Falls back to cleaned Indeed description if the API fails or no key.
Fit: good / maybe / stretch (stretch = hidden from the site).

Usage:  python3 summarize.py
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CACHE = os.path.join(HERE, "cache", "cached_jobs.json")
MODEL = "minimax/minimax-m3:free"

BEN_PROFILE = """Ben, based near Norfolk Road, Sheffield S2. Immediately available, needs ANY job.
Experience: outdoor activity instructor (2 seasons, First Aid + safeguarding, group leadership);
delivery riding work (Uber deliveries on bike, newspaper round); customer service assistant
(POS, cash handling, food safety, allergen protocols); maritime engineering motorman (RFA, 2 years
— fault diagnosis and maintenance of mechanical/electrical systems); volunteer construction/farming abroad.
Qualifications: Level 3 Engineering Diploma (Distinction), 9 GCSEs incl. Maths 8.
IMPORTANT: he does NOT hold a driving licence, so ANY role that involves driving or requires a
driving licence is unsuitable. He also lacks HGV/LGV, Class 1/2, C+E, SIA badge and forklift licences."""

COMBINED_PROMPT = f"""You assess UK job ads for Ben (profile below). Return EXACTLY 3 lines in this format, nothing else:
SUMMARY: <two short sentences: 1) role + employer + pay/location if stated, 2) key duty or notable point>
FIT: <good|maybe|stretch>
REASON: <one short line explaining the fit for Ben>

Rules: good = matches his experience and needs no qualifications he lacks. maybe = plausible with on-the-job training or experience preferred. stretch = requires a licence/certification he does not have (HGV, Class 1/2, C+E, SIA, forklift, trade ticket), years of experience well beyond his, or is clearly senior/management.

BEN'S PROFILE:
{BEN_PROFILE}

JOB AD:
{{ad}}"""

FIT_ONLY_PROMPT = f"""You assess UK job ads for Ben (profile below). Return EXACTLY 2 lines, nothing else:
FIT: <good|maybe|stretch>
REASON: <one short line explaining the fit for Ben>

Rules: good = matches his experience and needs no qualifications he lacks. maybe = plausible with on-the-job training or experience preferred. stretch = requires a licence/certification he does not have (HGV, Class 1/2, C+E, SIA, forklift, trade ticket), years of experience well beyond his, or is clearly senior/management.

BEN'S PROFILE:
{BEN_PROFILE}

JOB AD:
{{ad}}"""

# Hard keyword rules -> force stretch regardless of LLM verdict.
FORCE_STRETCH = [
    r"\bHGV\b", r"\bLGV\b", r"\bClass\s*[12]\b", r"\bC\+E\b",
    r"\bSIA\s+(?:badge|licence|license)\b", r"\bcounterbalance\b.*\blicen[cs]e\b",
    r"\bforklift\s+licen[cs]e\b", r"\bPSV\b", r"\bPCV\b",
    # own-vehicle requirements (company vehicle provided does NOT count)
    r"\bown\s+(?:car|van|vehicle|transport)\b", r"\bown\s+means\s+of\s+transport\b",
    r"use\s+of\s+own\s+(?:car|vehicle)", r"must\s+have\s+(?:your\s+own|a)\s+(?:car|van|vehicle)\b",
    r"own\s+car\s+(?:essential|required|necessary)",
]
VEHICLE_PROVIDED = re.compile(r"company\s+(?:van|vehicle|car)\s+(?:is\s+)?(?:provided|supplied)", re.I)

def _key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for path in (os.path.join(HERE, ".env"), os.path.expanduser("~/Projects/holidays/.env")):
        try:
            for line in open(path):
                if line.startswith("OPENROUTER_API_KEY=") and len(line) > 30:
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None

def _fallback_summary(desc):
    """No LLM available -> cleaned Indeed description as the preview."""
    from build_site import clean_desc
    text = clean_desc(desc)
    if len(text) > 300:
        text = text[:300].rsplit(" ", 1)[0] + "…"
    return text

def _llm(key, prompt, max_tokens=160):
    import requests
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "temperature": 0.2, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def hard_stretch(job):
    from experience_filter import extract_years_experience
    blob = f"{job.get('title') or ''} {job.get('description') or ''}"
    # Ben has NO driving licence — any non-negated driving-licence requirement is out.
    for m in re.finditer(r"driving\s+licen[cs]e|full\s+(?:uk\s+)?licen[cs]e|\blicen[cs]e\b[^.]{0,30}\bdriv", blob, re.I):
        window = blob[max(0, m.start() - 60):m.end() + 80]
        if not re.search(r"not\s+(?:essential|required|needed)|is\s+(?:a\s+)?(?:bonus|advantageous)|desirable|not\s+a\s+requirement|no\s+licen[cs]e", window, re.I):
            return "driving licence required"
    if any(re.search(p, blob, re.I) for p in FORCE_STRETCH):
        if not (re.search(r"\bown\s+(?:car|van|vehicle|transport)\b", blob, re.I)
                and VEHICLE_PROVIDED.search(blob)):
            return "licence/own-vehicle requirement"
    if (extract_years_experience(job.get("description")) or 0) >= 3:
        return "3+ years experience required"
    return None

def _parse_verdict(content):
    fit, reason = None, ""
    for line in content.splitlines():
        m = re.match(r"^\s*(?:\d[.)]\s*)?(FIT|REASON|SUMMARY)\s*[:\-]\s*(.+)$", line.strip(), re.I)
        if not m: continue
        tag, val = m.group(1).lower(), m.group(2).strip()
        if tag == "fit":
            v = val.lower()
            fit = "good" if "good" in v else "stretch" if "stretch" in v else "maybe"
        elif tag == "reason":
            reason = re.sub(r"^[\"“]| [\"”]$", "", val)[:200]
    return fit, reason

def process(job, key, want_summary):
    ad = f"Title: {job.get('title')}\nCompany: {job.get('company')}\nLocation: {job.get('location')}\n\n{(job.get('description') or '')[:3500]}"
    prompt = (COMBINED_PROMPT if want_summary else FIT_ONLY_PROMPT).replace("{ad}", ad)
    content = _llm(key, prompt, max_tokens=200 if want_summary else 80)
    out = {"fit": None, "reason": ""}
    if want_summary:
        m = re.search(r"SUMMARY\s*[:\-]\s*(.+?)(?:\n|$)", content, re.I)
        if m:
            out["summary"] = re.sub(r"^\s*Summary:\s*", "", m.group(1), flags=re.I).strip()[:400]
    fit, reason = _parse_verdict(content)
    out["fit"], out["reason"] = fit, reason
    return out

def main():
    with open(CACHE) as f:
        cache = json.load(f)
    jobs = cache["jobs"]

    # Hard rules first (free, no LLM).
    forced = 0
    for k, j in jobs.items():
        why = hard_stretch(j)
        if why:
            j["fit"], j["fit_reason"], j["fit_source"] = "stretch", why, "rule"
            forced += 1

    todo = {k: j for k, j in jobs.items()
            if not j.get("fit") or (not j.get("summary") and j.get("fit") != "stretch")}
    print(f"{len(todo)} jobs need LLM work ({forced} already stretch by rules, of {len(jobs)})")

    key = _key()
    if not key:
        print("! no OPENROUTER_API_KEY -> fallback for all")

    def work(item):
        k, j = item
        want_summary = not j.get("summary")
        if key:
            for attempt in (1, 2, 3):
                try:
                    res = process(j, key, want_summary)
                    if res.get("summary"): j["summary"] = res["summary"]
                    if res.get("fit"):
                        j["fit"] = res["fit"]
                        j["fit_reason"] = res.get("reason", "")
                        j["fit_source"] = "llm"
                    if res.get("summary"): j["summary_source"] = "llm"
                    if res.get("fit") or res.get("summary"):
                        return
                    raise ValueError("unparseable response")
                except Exception as e:
                    if attempt == 3:
                        last_err = str(e)[:120]
                    time.sleep(2 * attempt)
        # fallback: only fill summary (fit stays unknown -> 'maybe' at build time)
        if want_summary and not j.get("summary"):
            j["summary"] = _fallback_summary(j.get("description"))
            j["summary_source"] = "fallback"
        if not j.get("fit"):
            j["fit"], j["fit_reason"], j["fit_source"] = "maybe", "no fit data", "fallback"

    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(work, it) for it in todo.items()]
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(todo)}")

    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)

    counts = {}
    for j in jobs.values():
        counts[j.get("fit", "?")] = counts.get(j.get("fit", "?"), 0) + 1
    print(f"Done. Fit counts: {counts}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
