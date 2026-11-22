"""Entry point for Windows Task Scheduler (run with pythonw.exe, so no window opens).

Runs an unattended sync of the last 7 days and appends its output to logs/scheduled.log.
Already-sorted mail is skipped, so a 7-day window also covers days the PC was off.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
(ROOT / "logs").mkdir(exist_ok=True)
log = (ROOT / "logs" / "scheduled.log").open("a", encoding="utf-8")
sys.stdout = sys.stderr = log
print(f"\n=== scheduled sync {datetime.now():%Y-%m-%d %H:%M} ===")

from app.cli import main  # noqa: E402

try:
    main(["sync", "--scheduled", "--days", "7"])
except SystemExit as e:
    print(f"exit: {e}")
except Exception as e:  # keep a record of anything unexpected
    print(f"crashed: {type(e).__name__}: {e}")
finally:
    log.flush()
