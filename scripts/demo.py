"""Stage 10: the three-minute demo path, scripted.

    python scripts/demo.py                  # open each stop in the browser, 30 s apiece
    python scripts/demo.py --pause 10       # a faster run-through
    python scripts/demo.py --check          # no browser: verify every stop, exit 1 on failure

Run it through scripts/demo.ps1 or scripts/demo.sh, which start the API first if
it is not already up. Every stop is a map URL (date, place, zoom and the panel to
open are all URL parameters), so the path needs no clicking; each stop also
checks the API record behind it, so a demo that would show an empty panel fails
here first rather than in front of an audience.

The stops are fixed ids from the 2024 replay (make inference events risk). If the
database is rebuilt, ids can move: --check says which stop broke.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field

BASE = "http://localhost:8000"


@dataclass
class Stop:
    title: str
    url: str
    say: str
    api: str | None = None
    expect: dict = field(default_factory=dict)       # key -> value, or key -> True (present)


STOPS = [
    Stop("India, stubble season",
         "/?date=2024-11-10&days=7&z=4.3",
         "A week of November 2024: every satellite fire detection over India, one colour "
         "per class. FIRMS shows all of these as the same red dot; here the Punjab-Haryana "
         "stubble burning, central India's forest fires and the industrial sites are "
         "already separated -- deliverable (i)."),
    Stop("HMEL Bathinda in the paddy belt",
         "/?date=2024-11-10&days=7&lat=29.93&lon=74.95&z=10&open=source:94",
         "A refinery surrounded by stubble fires. The registry holds the refinery as a "
         "persistent oil and gas source; raw clustering would have swallowed the whole "
         "paddy belt into it. Only 0.014% of the belt's detections land on a source.",
         api="/api/sources/94", expect={"cls": "oil_gas"}),
    Stop("Reliance Jamnagar, operating normally",
         "/?date=2024-11-06&days=7&lat=22.33&lon=69.87&z=11&open=detection:10839350",
         "The world's largest refinery complex burns every night. Each detection is "
         "judged against this site's own history -- median, MAD and p99 for this "
         "instrument, day or night, and season -- so normal flaring stays quiet (Road B).",
         api="/api/detections/10839350", expect={"road": 2, "reason": True}),
    Stop("A confirmed anomaly at a running refinery",
         "/?date=2024-04-16&days=3&lat=13.16&lon=80.29&z=11&open=event:667105",
         "Chennai, April 2024: two consecutive passes breached this refinery's own "
         "baseline, so it is a confirmed alert (Road C). The panel says why in words, and "
         "the risk score is broken down into hazard, exposure and vulnerability.",
         api="/api/events/667105",
         expect={"kind": "anomaly", "alert": "confirmed", "risk_breakdown": True}),
    Stop("A new industrial site the registry did not know",
         "/?date=2024-06-30&days=30&lat=19.09&lon=82.17&z=11&open=event:462123",
         "NMDC's Nagarnar steel plant, commissioned in 2023, has no fire history. It kept "
         "burning, so it was promoted to a provisional source -- and it keeps alerting "
         "until an analyst confirms it. The registry grows, but never silently absorbs "
         "an accident; Baghjan 2020 is the regression test.",
         api="/api/events/462123", expect={"kind": "new_source", "reason": True}),
    Stop("The highest-risk event of 2024",
         "/?date=2024-06-23&days=3&lat=22.34&lon=69.89&z=11&open=event:805832",
         "Eight pixels, 42 MW, inside the Reliance complex at a spot with no thermal "
         "history: no baseline exists, so Road A classifies it -- oil and gas, because it "
         "sits inside a mapped refinery. Risk multiplies hazard, people and vulnerability, "
         "so a big fire far from anyone scores low and this one scores 84. Every factor "
         "is shown, not just the number.",
         api="/api/events/805832", expect={"risk_breakdown": True}),
]


def get(path: str, timeout: float = 60) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def check(stop: Stop) -> list[str]:
    """What is wrong with this stop, if anything."""
    problems = []
    status, _ = get(stop.url)
    if status != 200:
        problems.append(f"page {stop.url} -> {status}")
    if stop.api:
        status, body = get(stop.api)
        if status != 200:
            return problems + [f"{stop.api} -> {status}"]
        record = json.loads(body)
        props = record.get("properties", record)
        for key, want in stop.expect.items():
            have = props.get(key)
            if want is True:
                if have in (None, "", {}, []):
                    problems.append(f"{stop.api}: no {key}")
            elif have != want:
                problems.append(f"{stop.api}: {key} = {have!r}, expected {want!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="The scripted demo path.")
    ap.add_argument("--check", action="store_true", help="verify every stop; open nothing")
    ap.add_argument("--pause", type=float, default=30, help="seconds per stop")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # narration keeps pace with the browser

    status, body = get("/api/health", timeout=10)
    if status != 200:
        print(f"FAIL: the API is not answering at {BASE} (start it: make api)")
        return 1
    if not json.loads(body).get("database"):
        print("FAIL: the API is up but cannot reach the database (make db)")
        return 1
    failures = 0
    for i, stop in enumerate(STOPS, 1):
        problems = check(stop)
        failures += bool(problems)
        print(f"\n[{i}/{len(STOPS)}] {stop.title}\n    {BASE}{stop.url}\n    {stop.say}")
        for p in problems:
            print(f"    FAIL: {p}")
        if not args.check:
            webbrowser.open(BASE + stop.url)
            time.sleep(args.pause)
    print("\nPASS: every stop answers" if not failures
          else f"\n{failures} of {len(STOPS)} stops failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
