#!/usr/bin/env python3
"""
multimodal.md candidate paper watcher
--------------------------------------
1) Pull recent papers from the arXiv API and Nature-family journal RSS feeds.
2) Skip anything already covered in multimodal.md (by arXiv ID / DOI / link).
3) Skip anything already surfaced before (tracked in candidates/watchlist.md).
4) Send the remaining candidates to the Claude API and ask "does this fit the
   repo's scope for a multimodal biomedical AI paper?"
5) For anything that passes, try to fetch the full text (arXiv PDF, the
   publisher's own HTML page for open-access journals, or a PubMed Central
   fallback) and have Claude draft a full multimodal.md-formatted entry from
   whatever text was actually retrieved — never inventing numbers for fields
   that aren't stated in the source text.
6) Both the short listing and the draft entry get appended to
   candidates/watchlist.md.

This script never edits multimodal.md directly, and never opens a PR against
the upstream repo. A human still reviews every draft (especially any field
NOT marked "—"/"Not disclosed") before copying it into multimodal.md and
opening a PR from a clean branch off `main`.
"""

import os
import re
import io
import json
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MULTIMODAL_MD = os.path.join(REPO_ROOT, "multimodal.md")
WATCHLIST_MD = os.path.join(REPO_ROOT, "candidates", "watchlist.md")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Cheap model for the yes/no screening step.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# A stronger model is worth it for the drafting step, since it has to read a
# full paper and format a detailed, numbers-heavy entry accurately.
ANTHROPIC_DRAFT_MODEL = os.environ.get("ANTHROPIC_DRAFT_MODEL", "claude-sonnet-5")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; multimodal-watch/1.0; "
        "+https://github.com/medfm-flare/AwesomeBiomedicalAI)"
    )
}

# Journals that are fully open access — full HTML text is expected to work.
OPEN_ACCESS_JOURNALS = {
    "Nature Communications",
    "npj Digital Medicine",
    "Signal Transduct. Target. Ther.",
    "Cell Rep. Med.",
}

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


# ---- 4. Full-text retrieval -------------------------------------------------

def _extract_readable_text(html):
    """Best-effort extraction of the main article body from a journal HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # Springer Nature journals render the body in a <div class="c-article-body">;
    # fall back to <article>, then to the whole page if neither is found.
    body = soup.find("div", class_="c-article-body") or soup.find("article") or soup.body
    if body is None:
        return ""
    text = body.get_text(separator="\n", strip=True)
    return text


def _fetch_arxiv_fulltext(arxiv_id):
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    resp = requests.get(pdf_url, headers=HTTP_HEADERS, timeout=60)
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _doi_to_pmcid(doi):
    """Ask NCBI's ID converter whether this DOI has a PubMed Central copy."""
    try:
        resp = requests.get(
            "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
            params={"ids": doi, "format": "json", "tool": "multimodal-watch"},
            headers=HTTP_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if records and "pmcid" in records[0]:
            return records[0]["pmcid"]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    return None


def _fetch_pmc_fulltext(pmcid):
    url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return _extract_readable_text(resp.text)


def fetch_full_text(candidate):
    """
    Try, in order: arXiv PDF -> the publisher's own HTML page -> PubMed
    Central. Returns (text, source_label) where source_label describes what
    was actually retrieved, or (None, None) if nothing worked.
    """
    if candidate["source"] == "arXiv":
        try:
            text = _fetch_arxiv_fulltext(candidate["id"])
            if len(text) > 2000:
                return text, "arXiv PDF (full text)"
        except Exception as e:
            print(f"    arXiv fetch failed: {e}")
        return None, None

    # Journal entries: try the publisher page directly first.
    try:
        resp = requests.get(candidate["link"], headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
        text = _extract_readable_text(resp.text)
        # A paywalled teaser page is usually short; a real article body is not.
        if len(text) > 3000:
            return text, f"{candidate['source']} article page (full text)"
    except Exception as e:
        print(f"    Publisher page fetch failed: {e}")

    # Fall back to PubMed Central if this DOI has a deposited copy.
    doi_match = re.search(r"doi\.org/(\S+)$", candidate["link"])
    if doi_match:
        pmcid = _doi_to_pmcid(doi_match.group(1))
        if pmcid:
            try:
                text = _fetch_pmc_fulltext(pmcid)
                if len(text) > 3000:
                    return text, f"PubMed Central {pmcid} (full text)"
            except Exception as e:
                print(f"    PMC fetch failed: {e}")

    return None, None


# ---- 5. Draft a full multimodal.md-formatted entry --------------------------

DRAFT_SYSTEM_PROMPT = """\
You draft candidate entries for the "AwesomeBiomedicalAI" repository's \
multimodal.md file, in EXACTLY this format (this is one real example from \
the file):

**VirTues — The Virtual Tissues foundation model resolves spatial proteomics across scales *(Nature 202608)***

**[The Virtual Tissues foundation model resolves spatial proteomics across scales](https://doi.org/10.1038/s41586-026-10884-y)**

*Nature* · 202608 · [Author One](scholar-link) & [Author Two](scholar-link) · [doi:...](https://doi.org/...)

| | |
|---|---|
| **Parameters** | ... |
| **Backbone** | ... |
| **Pre-training** | category   One sentence of specifics. |
| **Training data** | description   key numbers |
| **Downstream tasks** | comma list   One sentence of specifics. |
| **Modalities** | ... |
| **Code** | [github.com/...](...) |
| **Weights** | ... |
| **License** | ... |

**Reported performance**

| Benchmark | Metric | Value | Note |
|---|---|---|---|
| ... | ... | ... | ... |

CRITICAL RULES:
- Only state a fact if it is explicitly present in the text you were given below. \
Never estimate, infer, or invent a parameter count, benchmark number, dataset \
size, or architecture detail.
- If a field is not stated in the text, write exactly "Not disclosed" (for \
Parameters/Backbone/etc.) or "—" (for table cells), matching this repo's own \
convention: "A dash (—) means the value has not been confirmed from the paper \
or an official release."
- Omit the Code/Weights/License rows entirely if no link or statement about \
them appears in the text — do not guess a GitHub URL.
- If you were only given an abstract (not the full paper), most detail fields \
will legitimately be "Not disclosed" — that's expected and correct, not a \
failure. Say so plainly rather than padding the entry.
- Keep the "Pre-training" and "Downstream tasks" cells in this repo's style: \
a short category/list on its own line, then one sentence of specifics below it.
- Output ONLY the markdown entry. No preamble, no closing remarks.
"""


def draft_entry_with_claude(candidate, text, text_source):
    if text is None:
        text = candidate["summary"]
        text_source = "abstract/summary only (full text not retrievable)"

    prompt = (
        f"Source: {candidate['source']}\n"
        f"Link: {candidate['link']}\n"
        f"Text basis: {text_source}\n\n"
        f"--- TEXT ---\n{text[:60000]}\n--- END TEXT ---\n"
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_DRAFT_MODEL,
            "max_tokens": 2000,
            "system": DRAFT_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    draft_md = "".join(block.get("text", "") for block in data.get("content", []))
    return draft_md.strip(), text_source


# ---- 6. Write to watchlist.md ----------------------------------------------

def append_to_watchlist(accepted):
    os.makedirs(os.path.dirname(WATCHLIST_MD), exist_ok=True)
    today = datetime.date.today().isoformat()

    lines = [f"\n## Auto-collected candidates — {today}\n"]
    for c, verdict, draft_md, text_source in accepted:
        lines.append(f"- **[{c['title']}]({c['link']})** — {c['source']}, {c['date']}")
        lines.append(f"  - Priority: `{verdict['priority']}`")
        lines.append(f"  - Reason: {verdict['reason']}")
        lines.append(f"  - Draft basis: {text_source}")
        lines.append("")
        lines.append(
            "  > ⚠️ **AUTO-DRAFTED — verify every field before merging**, "
            "especially anything not marked `—`/`Not disclosed`. Any field "
            "not explicitly stated in the retrieved text should already say "
            "so, but double-check numbers against the paper yourself."
        )
        lines.append("")
        lines.append("  <details><summary>Draft entry (click to expand)</summary>\n")
        lines.append("  ```markdown")
        for draft_line in draft_md.splitlines():
            lines.append(f"  {draft_line}")
        lines.append("  ```")
        lines.append("  </details>")
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
        if not verdict.get("relevant"):
            print(f"  [rejected] {c['title']} — {verdict.get('reason', '')}")
            continue

        print(f"  [ACCEPTED:{verdict.get('priority')}] {c['title']}")
        print("    Fetching full text...")
        text, text_source = fetch_full_text(c)
        print(f"    Basis for draft: {text_source or 'abstract only'}")
        draft_md, text_source = draft_entry_with_claude(c, text, text_source)
        time.sleep(1)
        accepted.append((c, verdict, draft_md, text_source))

    if not accepted:
        print("No new candidates this run.")
        return

    append_to_watchlist(accepted)
    print(f"Added {len(accepted)} entries to watchlist.md")


if __name__ == "__main__":
    main()
