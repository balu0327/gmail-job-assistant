"""Measure the classifier on labelled emails.

    python scripts/evaluate.py
    python scripts/evaluate.py --file data/my_labelled.jsonl

Each line: {"sender": "...", "sender_email": "...", "subject": "...", "body": "...", "category": "..."}
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.classify import CATEGORIES, classify  # noqa: E402
from app.gateway import Gateway, ModelError  # noqa: E402
from app.gmail_client import Email  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "data" / "labelled_emails.jsonl"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between model calls")
    args = ap.parse_args()

    rows = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    gw = Gateway(config.providers())
    correct, failed, latencies, confusion = 0, 0, [], Counter()
    for i, row in enumerate(rows, 1):
        email = Email(id=str(i), thread_id="", sender=row["sender"], sender_email=row["sender_email"], to="",
                      subject=row["subject"], date="", snippet="", body=row["body"], message_id="")
        start = time.time()
        try:
            got = classify(gw, email)["category"]
        except ModelError as e:
            failed += 1
            print(f"[{i}/{len(rows)}] FAILED: {e}")
            continue
        latencies.append(time.time() - start)
        ok = got == row["category"]
        correct += ok
        confusion[(row["category"], got)] += 1
        print(f"[{i}/{len(rows)}] {'ok  ' if ok else 'MISS'} expected {row['category']:<20} got {got:<20} {row['subject'][:50]}")
        time.sleep(args.delay)

    done = len(rows) - failed
    print("\n=== Results ===")
    print(f"model:     {gw.last_model}")
    print(f"emails:    {len(rows)}  (failed calls: {failed})")
    if done:
        print(f"accuracy:  {correct}/{done} = {correct / done:.0%}")
        print(f"avg secs:  {sum(latencies) / len(latencies):.1f}")
        print("\nper category (correct / total):")
        for cat in CATEGORIES:
            total = sum(n for (exp, _), n in confusion.items() if exp == cat)
            if total:
                print(f"  {cat:<22} {confusion[(cat, cat)]}/{total}")
        misses = [(e, g, n) for (e, g), n in confusion.items() if e != g]
        if misses:
            print("\nconfusions (expected -> got):")
            for e, g, n in sorted(misses, key=lambda x: -x[2]):
                print(f"  {e} -> {g}: {n}")


if __name__ == "__main__":
    main()
