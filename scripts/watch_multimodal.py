#!/usr/bin/env python3
"""
multimodal.md candidate paper watcher
--------------------------------------
1) Pull recent papers from the arXiv API and Nature-family journal RSS feeds.
2) Skip anything already covered in multimodal.md (by arXiv ID / DOI / link).
3) Skip anything already surfaced before (tracked in candidates/watchlist.md).
4) Send the remaining candidates to the Claude API and ask "does this fit the
   repo's scope for a multimodal biomedical AI paper?"
5) Only candidates that pass get appended to candidates/watchlist.md.

This script never edits multimodal.md directly. Writing the actual, fully
detailed entry (backbone, pre-training scheme, benchmark numbers, etc.) and
opening the PR against medfm-flare/AwesomeBiomedicalAI is left to a human.
"""

import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

import feedparser
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MULTIMODAL_MD = os.path.join(REPO_ROOT, "multimodal.md")
WATCHLIST_MD = os.path.join(REPO_ROOT, "candidates", "watchlist.md")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# ---- Search targets -------------------------------------------------------

ARXIV_CATEGORIES = ["cs.CV", "cs.CL", "cs.LG", "q-bio.QM", "eess.IV"]
ARXIV_KEYWORDS = [
    "multimodal", "vision-language", "foundation model",
]
ARXIV_DOMAIN_KEYWORDS = [
    "biomedical", "clinical", "pathology", "radiology", "histopathology",
    "genomics", "EHR", "medical imaging", "proteomics",
]

# Nature-family + related journal RSS feeds (add/remove as needed)
NATURE_FAMILY_FEEDS = {
    "Nature": "https://www.nature.com/nature.rss",
    "Nature Medicine": "https://www.nature.com/nm.rss",
    "Nature Communications": "https://www.nature.com/ncomms.rss",
    "Nature Cancer": "https://www.nature.com/natcancer.rss",
    "Nature Biomedical Engineering": "https://www.nature.com/natbiomedeng.rss",
    "Nature Methods": "https://www.nature.com/nmeth.rss",
    "npj Digital Medicine": "https://www.nature.com/npjdigitalmed.rss",
}

MAX_ARXIV_RESULTS = 40
LOOKBACK_DAYS = 8  # a bit more than the weekly run cadence, for safety margin


# ---- 1. Collect candidates -------------------------------------------------

def fetch_arxiv_candidates():
    """Pull recently submitted arXiv papers matching multimodal + biomedical keywords."""
    query_terms = []
    for kw in ARXIV_KEYWORDS:
        query_terms.append(f'abs:"{kw}"')
    kw_query = "(" + " OR ".join(query_terms) + ")"

    domain_terms = [f'abs:"{kw}"' for kw in ARXIV_DOMAIN_KEYWORDS]
    domain_query = "(" + " OR ".join(domain_terms) + ")"

    cat_terms = [f"cat:{c}" for c in ARXIV_CATEGORIES]
    cat_query = "(" + " OR ".join(cat_terms) + ")"

    search_query = f"{cat_query} AND {kw_query} AND {domain_query}"

    params = {
        "search_query": search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(MAX_ARXIV_RESULTS),
    }
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=LOOKBACK_DAYS)
    candidates = []
    for entry in root.findall("atom:entry", ns):
        arxiv_id = entry.find("atom:id", ns).text.strip().split("/abs/")[-1]
        title = entry.find("atom:title", ns).text.strip().replace("\n", " ")
        summary = entry.find("atom:summary", ns).text.strip().replace("\n", " ")
        published = entry.find("atom:published", ns).text.strip()
        published_dt = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
        if published_dt < cutoff:
            continue
        link = f"https://arxiv.org/abs/{arxiv_id}"
        candidates.append({
            "source": "arXiv",
            "id": arxiv_id,
            "title": title,
            "summary": summary,
            "link": link,
            "date": published_dt.date().isoformat(),
        })
    return candidates


def fetch_nature_family_candidates():
    """Filter Nature-family RSS entries down to ones matching multimodal keywords."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=LOOKBACK_DAYS)
    all_keywords = [k.lower() for k in ARXIV_KEYWORDS + ARXIV_DOMAIN_KEYWORDS]

    candidates = []
    for journal, feed_url in NATURE_FAMILY_FEEDS.items():
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")
            text = (title + " " + summary).lower()
            if not any(kw in text for kw in all_keywords):
                continue
            if not any(kw in text for kw in ["multimodal", "multi-modal", "vision-language"]):
                continue

            published_dt = None
            if getattr(entry, "published_parsed", None):
                published_dt = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
            if published_dt and published_dt < cutoff:
                continue

            candidates.append({
                "source": journal,
                "id": getattr(entry, "id", entry.link),
                "title": title.strip(),
                "summary": re.sub("<[^<]+?>", "", summary).strip(),
                "link": entry.link,
                "date": published_dt.date().isoformat() if published_dt else "unknown",
            })
    return candidates


# ---- 2. De-duplication -----------------------------------------------------

def load_seen_identifiers():
    """Collect arXiv IDs / DOIs / links already covered in multimodal.md and watchlist.md."""
    seen = set()
    for path in (MULTIMODAL_MD, WATCHLIST_MD):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        seen.update(re.findall(r"(?:arxiv\.org/(?:abs|pdf)/)([0-9]{4}\.[0-9]{4,5})", content))
        seen.update(re.findall(r"doi\.org/(\S+?)[)\]\s]", content))
        seen.update(re.findall(r"https?://\S+", content))
    return seen


def dedupe(candidates, seen):
    fresh = []
    for c in candidates:
        if c["id"] in seen or c["link"] in seen:
            continue
        fresh.append(c)
    return fresh


# ---- 3. Judge with Claude ---------------------------------------------------

JUDGE_SYSTEM_PROMPT = """\
You are a screening assistant helping curate the "AwesomeBiomedicalAI" \
repository's multimodal.md file. That file only covers papers that meet ALL \
of these criteria:
- Clearly "multimodal" biomedical AI (combines 2+ distinct modalities: \
imaging, text, genomics, EHR, omics, etc.)
- Published in a journal (Nature-family, Cell-family, npj, JCO, Signal \
Transduct Target Ther, etc.) OR a preprint of one (arXiv/medRxiv/bioRxiv)
- Conference proceedings (MICCAI, IEEE, etc.) are OUT OF SCOPE and must be \
rejected
- Single-modality papers are rejected
- Give higher priority to major releases from well-known labs (e.g. Faisal \
Mahmood, Jakob Nikolas Kather, and similar pathology/multimodal foundation \
model groups), Nature-family publications, or papers with standout \
benchmark results.

Given the paper info below (title, abstract, source), respond with ONLY the \
following JSON. No other text.
{"relevant": true or false, "priority": "high" or "medium" or "low", "reason": "1-2 sentences in English explaining why this is or isn't a good candidate"}
"""


def judge_with_claude(candidate):
    prompt = (
        f"Source: {candidate['source']}\n"
        f"Title: {candidate['title']}\n"
        f"Abstract/summary: {candidate['summary'][:1200]}\n"
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 300,
            "system": JUDGE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(block.get("text", "") for block in data.get("content", []))
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"relevant": False, "priority": "low", "reason": "Failed to parse the judge response"}


# ---- 4. Write to watchlist.md ----------------------------------------------

def append_to_watchlist(accepted):
    os.makedirs(os.path.dirname(WATCHLIST_MD), exist_ok=True)
    today = datetime.date.today().isoformat()

    lines = [f"\n## Auto-collected candidates — {today}\n"]
    for c, verdict in accepted:
        lines.append(f"- **[{c['title']}]({c['link']})** — {c['source']}, {c['date']}")
        lines.append(f"  - Priority: `{verdict['priority']}`")
        lines.append(f"  - Reason: {verdict['reason']}")
        lines.append("")

    header = (
        "# Candidate paper watchlist (auto-generated)\n\n"
        "This file only holds candidates found by the automation script. "
        "It is never reflected into multimodal.md directly — a human must "
        "review these, write a properly formatted entry, and open a PR "
        "against the upstream repo (medfm-flare/AwesomeBiomedicalAI).\n"
    )

    if os.path.exists(WATCHLIST_MD):
        with open(WATCHLIST_MD, "r", encoding="utf-8") as f:
            existing = f.read()
        content = existing + "\n".join(lines)
    else:
        content = header + "\n".join(lines)

    with open(WATCHLIST_MD, "w", encoding="utf-8") as f:
        f.write(content)


# ---- main -------------------------------------------------------------------

def main():
    seen = load_seen_identifiers()

    raw_candidates = fetch_arxiv_candidates() + fetch_nature_family_candidates()
    fresh_candidates = dedupe(raw_candidates, seen)

    print(f"Collected: {len(raw_candidates)} / New candidates: {len(fresh_candidates)}")

    accepted = []
    for c in fresh_candidates:
        verdict = judge_with_claude(c)
        time.sleep(1)  # rate limit headroom
        if verdict.get("relevant"):
            accepted.append((c, verdict))
            print(f"  [ACCEPTED:{verdict.get('priority')}] {c['title']}")
        else:
            print(f"  [rejected] {c['title']} — {verdict.get('reason', '')}")

    if not accepted:
        print("No new candidates this run.")
        return

    append_to_watchlist(accepted)
    print(f"Added {len(accepted)} entries to watchlist.md")


if __name__ == "__main__":
    main()
