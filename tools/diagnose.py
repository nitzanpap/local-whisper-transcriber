#!/usr/bin/env python3
"""What the app knows about what it recorded and transcribed.

    python3 tools/diagnose.py              the last recording, in full
    python3 tools/diagnose.py --all        every one still on file, in brief
    python3 tools/diagnose.py --json       the raw record, for pasting somewhere
    python3 tools/diagnose.py <name>       one recording by file name
    python3 tools/diagnose.py --backfill   measure recordings made before this existed

Written because every fault in this project so far was diagnosed by hand — a
person listening, an agent measuring the file afterwards, and hours spent on
questions the app could have answered in a line. The Teams-loopback fault was
"which device was actually opened". The two-track fault was "how many tracks did
the job have". The tap dying when the output device changed was "are there holes
in this channel". All three are printed below without anyone measuring anything.

The three questions at the top are the ones that have actually gone wrong. Read
them first.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnostics  # noqa: E402
from config import HISTORY  # noqa: E402

BOLD, DIM, RED, GREEN, OFF = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[0m"


def when(stamp: float | None) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S") if stamp else "—"


def jobs_for(rec_id: str, source: str | None) -> list[dict]:
    """Every transcription run of this recording, however it was started."""
    out = []
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if source and row.get("source") == source:
            out.append(row)
    return out


def verdict(rec: dict) -> list[str]:
    """The things that have actually gone wrong, asked directly."""
    said = []

    # First, because it is ground truth and everything below it is inference. The
    # helper knows how much it wrote in place of audio; a finished file never can.
    for side, said_about in (rec.get("padding") or {}).items():
        share = (said_about or {}).get("fraction", 0)
        if share >= 0.05:
            said.append(f"{RED}{share*100:.0f}% of the {side} side is silence the helper wrote "
                        f"in place of audio that never arrived{OFF} — that side is chopped and "
                        f"cannot be transcribed. This is measured by the capture itself, not "
                        f"guessed from the file.")
        elif share:
            said.append(f"{DIM}the {side} side padded {share*100:.1f}%, which is ordinary{OFF}")

    # 1. Did the computer's side stop mid-recording? The tap is built on one output
    #    device and never moves; if the machine's output changes, it goes deaf.
    for side, report in (rec.get("channels") or {}).items():
        if not report.get("measured"):
            continue
        if report.get("holes"):
            biggest = max(h["seconds"] for h in report["holes"])
            said.append(f"the {side} side has {len(report['holes'])} silence(s) inside its "
                        f"audio, the longest {biggest:.1f}s — {DIM}this cannot tell a capture "
                        f"that stopped from a spell when nothing was playing. The padding "
                        f"figures above can; read those first.{OFF}")
    output = rec.get("output_device")
    if output:
        said.append(f"{DIM}the tap was built on output device {output}{OFF}")

    # 2. Was the computer's side pointed at something that carries nothing?
    computer = (rec.get("devices") or {}).get("computer") or {}
    if computer.get("id") and computer["id"] != "system":
        said.append(f"{RED}the computer's side was {computer.get('name') or computer['id']}, "
                    f"not the built-in tap{OFF} — another app's loopback driver carries audio "
                    f"only while that app routes into it. See TRAPS §15.")

    # 3. Did what was asked for actually arrive?
    missing = [s for s in (rec.get("asked_for") or []) if s and s not in (rec.get("captured") or [])]
    if len(rec.get("asked_for") or []) > len(rec.get("captured") or []):
        said.append(f"{RED}asked for {len(rec.get('asked_for') or [])} source(s), "
                    f"captured {len(rec.get('captured') or [])}{OFF}"
                    + (f" — missing {', '.join(missing)}" if missing else ""))
    for side in rec.get("noisy") or []:
        ratio = (rec.get("snr") or {}).get(side)
        said.append(f"the {side} side is only {ratio} dB above its own background")
    if not said:
        said.append(f"{GREEN}nothing known to be wrong{OFF}")
    return said


def brief(rec: dict) -> str:
    channels = rec.get("channels") or {}
    holes = sum(len(c.get("holes") or []) for c in channels.values() if c.get("measured"))
    return (f"{when(rec.get('started_at')):<20} {Path(rec.get('path') or '—').name:<26} "
            f"{','.join(rec.get('captured') or []) or 'nothing':<18} "
            + (f"{RED}{holes} holes{OFF}" if holes else "clean"))


def full(rec: dict) -> None:
    print(f"\n{BOLD}{Path(rec.get('path') or '—').name}{OFF}   {when(rec.get('started_at'))}")
    print(f"{BOLD}what to look at first{OFF}")
    for line in verdict(rec):
        print(f"  · {line}")

    print(f"\n{BOLD}devices{OFF}")
    for side, device in (rec.get("devices") or {}).items():
        print(f"  {side:<10} {device.get('name') or '—'}  {DIM}{device.get('id') or ''}{OFF}")
    print(f"  {'output':<10} {rec.get('output_device') or '—'}   "
          f"{DIM}(what the tap was built on){OFF}")
    print(f"  {'helper':<10} used={rec.get('helper_used')}  exit={rec.get('helper_exit')}")

    padding = rec.get("padding") or {}
    if padding:
        print(f"\n{BOLD}what the capture itself reported{OFF}")
        for side, said in padding.items():
            share = (said or {}).get("fraction", 0)
            mark = RED if share >= 0.05 else ""
            print(f"  {side:<10} {mark}{said.get('seconds', 0):>7.1f}s of "
                  f"{said.get('of', 0):>7.1f}s written as padding ({share*100:>4.1f}%){OFF}")
    else:
        print(f"\n  {DIM}no padding figures — recorded before the capture reported them{OFF}")

    print(f"\n{BOLD}channels{OFF}")
    for side, report in (rec.get("channels") or {}).items():
        if not report.get("measured"):
            print(f"  {side:<10} not measurable")
            continue
        print(f"  {side:<10} {report['seconds']:>6.1f}s   speech {report['speech_db']:>6.1f} dB   "
              f"floor {report['floor_db']:>6.1f} dB   snr {report['snr_db']:>5.1f} dB   "
              f"silent {report['silent_seconds']:>5.1f}s")
        for hole in report.get("holes") or []:
            print(f"             {RED}hole at {hole['at']:.1f}s for {hole['seconds']:.1f}s{OFF}")

    seen, padded = rec.get("stalls_seen") or {}, rec.get("padded") or {}
    if seen or padded:
        print(f"\n{BOLD}stalls the app noticed while recording{OFF}")
        for side, runs in seen.items():
            for at, how in runs:
                print(f"  {side:<10} nothing arrived for {how:.1f}s, {at:.1f}s in")
        for side, runs in padded.items():
            print(f"  {side:<10} {len(runs)} gap(s) padded before saving")
    elif any((c.get("holes") for c in (rec.get("channels") or {}).values())):
        print(f"\n  {RED}holes in the file that the app did not notice while recording{OFF} — "
              f"the live warning is not covering this path")

    runs = jobs_for(rec.get("id") or "", rec.get("path"))
    print(f"\n{BOLD}transcribed {len(runs)} time(s){OFF}")
    for row in runs:
        tracks = row.get("tracks") or []
        shape = (", ".join(f"channel {t.get('channel')} as {t.get('label') or 'unnamed'}"
                           for t in tracks)) or "one unnamed track"
        flag = "" if len(tracks) == 2 or len(rec.get("captured") or []) < 2 else \
            f"  {RED}<- a two-channel recording transcribed as one track{OFF}"
        print(f"  {row.get('status'):<10} {shape}{flag}")

    print(f"\n{BOLD}versions{OFF}")
    for name, said in (rec.get("versions") or {}).items():
        print(f"  {name:<12} {said}")

    log = rec.get("log") or []
    if log:
        print(f"\n{BOLD}the recording's own log{OFF} ({len(log)} lines)")
        for line in log:
            print(f"  {DIM}{line}{OFF}")


def main(argv: list[str]) -> int:
    if "--backfill" in argv:
        from config import recording_config
        folder = Path(recording_config()["folder"]).expanduser()
        written = diagnostics.backfill(folder)
        print(f"measured {written} recording(s) in {folder} that had no record")
    found = diagnostics.recent(200)
    if not found:
        print(f"No recordings written down yet. {diagnostics.RECORDINGS} does not exist —\n"
              "it is written when a recording is saved, so make one and try again.")
        return 1
    if "--all" in argv:
        print(f"{BOLD}{'when':<20} {'file':<26} {'captured':<18} state{OFF}")
        for rec in found:
            print(brief(rec))
        return 0
    wanted = next((a for a in argv[1:] if not a.startswith("-")), None)
    rec = next((r for r in found if wanted and wanted in (r.get("path") or "")), found[0])
    if "--json" in argv:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    full(rec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
