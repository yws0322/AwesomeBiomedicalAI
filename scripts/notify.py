#!/usr/bin/env python3
"""
Stage 3/3: format the accepted candidates into an email-ready subject/body,
and record them in candidates/.notified.json so the same paper isn't emailed
again next week.

Usage:
  python notify.py --in accepted.json --seen-file candidates/.notified.json \
                    --subject-out subject.txt --body-out body.txt
"""

import argparse
import datetime
import json
import os


def build_email(accepted):
    today = datetime.date.today().isoformat()
    subject = f"[multimodal.md watch] {len(accepted)} new candidate paper(s) \u2014 {today}"

    lines = [
        f"{len(accepted)} new candidate paper(s) found for multimodal.md ({today}).",
        "Paste this list into Claude Code and ask it to read each paper, draft an",
        "entry in multimodal.md's format, and open a PR when you're happy with it.",
        "",
    ]
    for item in accepted:
        c, verdict = item["candidate"], item["verdict"]
        lines.append(f"- [{verdict['priority'].upper()}] {c['title']}")
        lines.append(f"  Source: {c['source']}, {c['date']}")
        lines.append(f"  Link: {c['link']}")
        lines.append(f"  Why: {verdict['reason']}")
        lines.append("")

    return subject, "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--seen-file", required=True)
    parser.add_argument("--subject-out", required=True)
    parser.add_argument("--body-out", required=True)
    args = parser.parse_args()

    with open(args.in_path, "r", encoding="utf-8") as f:
        accepted = json.load(f)

    subject, body = build_email(accepted)
    with open(args.subject_out, "w", encoding="utf-8") as f:
        f.write(subject)
    with open(args.body_out, "w", encoding="utf-8") as f:
        f.write(body)

    # Record these as notified so they aren't emailed again next week.
    seen = set()
    if os.path.exists(args.seen_file):
        with open(args.seen_file, "r", encoding="utf-8") as f:
            seen = set(json.load(f))
    for item in accepted:
        c = item["candidate"]
        seen.add(c["id"])
        seen.add(c["link"])
    os.makedirs(os.path.dirname(args.seen_file) or ".", exist_ok=True)
    with open(args.seen_file, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

    print(f"Prepared email for {len(accepted)} candidates, updated {args.seen_file}")


if __name__ == "__main__":
    main()
