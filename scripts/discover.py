#!/usr/bin/env python3
"""
Stage 1/4: discover candidates from arXiv + Nature-family RSS, and drop
anything already in multimodal.md or already suggested before (tracked in
candidates/.seen.json on the tooling branch).

Usage:
  python discover.py --multimodal-md path/to/multimodal.md \
                      --seen-file candidates/.seen.json \
                      --out candidates.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher_lib as lib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--multimodal-md", required=True)
    parser.add_argument("--seen-file", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    seen = set()
    if os.path.exists(args.multimodal_md):
        with open(args.multimodal_md, "r", encoding="utf-8") as f:
            seen |= lib.extract_identifiers(f.read())
    if os.path.exists(args.seen_file):
        with open(args.seen_file, "r", encoding="utf-8") as f:
            seen |= set(json.load(f))

    raw_candidates = lib.fetch_arxiv_candidates() + lib.fetch_nature_family_candidates()
    fresh_candidates = lib.dedupe(raw_candidates, seen)

    print(f"Collected: {len(raw_candidates)} / New candidates: {len(fresh_candidates)}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(fresh_candidates, f, ensure_ascii=False, indent=2)

    # Job outputs so the workflow can skip downstream stages and pass stats along.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_candidates={'true' if fresh_candidates else 'false'}\n")
            f.write(f"raw_count={len(raw_candidates)}\n")
            f.write(f"new_count={len(fresh_candidates)}\n")


if __name__ == "__main__":
    main()
