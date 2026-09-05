#!/usr/bin/env python3
"""
Stage 2/4: ask Claude whether each candidate fits multimodal.md's scope.

Usage:
  python screen.py --in candidates.json --out accepted.json
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher_lib as lib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.in_path, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    accepted = []
    for c in candidates:
        verdict = lib.judge_with_claude(c)
        time.sleep(1)
        if verdict.get("relevant"):
            accepted.append({"candidate": c, "verdict": verdict})
            print(f"  [ACCEPTED:{verdict.get('priority')}] {c['title']}")
        else:
            print(f"  [rejected] {c['title']} — {verdict.get('reason', '')}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(accepted, f, ensure_ascii=False, indent=2)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_accepted={'true' if accepted else 'false'}\n")


if __name__ == "__main__":
    main()
