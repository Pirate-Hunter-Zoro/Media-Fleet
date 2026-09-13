#!/usr/bin/env python3
"""fleet_doctor.py -- act on `fleet_health.json` without a person, and without guessing.

    python3 scripts/fleet_doctor.py --once          # one pass
    python3 scripts/fleet_doctor.py --once --dry-run  # decide, apply nothing
    python3 scripts/fleet_doctor.py                 # loop (launchd)

WHAT IT IS FOR. The fleet already detects its own faults; what it lacked was anything that
ACTS on them, so every finding waited for a person to read a report and type the fix. This
closes that loop for the faults that can be closed safely, and -- just as importantly --
says plainly which ones cannot be, instead of leaving them to look unattended.

HOW IT DECIDES, AND WHAT THE AI IS ALLOWED TO DO
------------------------------------------------
Each finding carries the NAME OF THE CHECK that produced it, and remedies are registered
against those names. That is the whole matcher for a known finding: no prose parsing, no
model call, no ambiguity. `fleet_health`'s messages get reworded whenever they read badly
on a phone, and a matcher keyed on wording would turn every such edit into a silent
behaviour change -- §4.26's fault, reading the label instead of the thing.

The free model is called for exactly one thing: a finding whose check name is in NO
remedy's `answers`. It is shown the fault and the CLOSED LIST of remedy ids and asked to
pick one or answer NONE. Its reply is then looked up in the registry, and anything that is
not a known id becomes NONE. So the worst a wrong, confused or hostile model answer can do
is run a remedy that is already reviewed, tested and non-destructive, or nothing at all.
It cannot author an action, name a path, or compose a command -- there is nowhere in this
program to put one. That is the §4.4 rule made structural rather than promised.

It also spends the model's budget through `config.aux_ai_attempts()`, so it yields to
identify: a self-healer that drained the account the filing path needs would create more
faults than it closed.

WHAT IT WILL NOT DO
-------------------
It does not delete media, write `~/Media`, touch `mediafs_deletions.jsonl`, restart the
reaper, or clear a heartbeat that has not been earned. Those are not omissions to be
tidied up later; `test_remedies.py` fails the build if a remedy acquires the ability.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import remedies                                                      # noqa: E402

HEALTH_JSON = config.STATE_DIR / "fleet_health.json"
REPORT_FILE = config.TORRENTS_DIR / "fleet_doctor.txt"
LOG_FILE = config.STATE_DIR / "fleet_doctor.log"
CYCLE_SEC = int(os.environ.get("FLEET_DOCTOR_CYCLE_SEC", "900"))     # 15 min
#: A health report older than this is not acted on. Acting on a stale snapshot is how a
#: fixed fault gets "fixed" again, and the remedies' own `detect()` is the second guard.
MAX_REPORT_AGE_SEC = int(os.environ.get("FLEET_DOCTOR_MAX_AGE_SEC", "3600"))


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_findings() -> tuple[list[dict], str]:
    """The current findings, or an empty list with the reason there are none."""
    try:
        data = json.loads(HEALTH_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], (f"{HEALTH_JSON.name} does not exist yet -- fleet_health has not run "
                    f"since this was installed.")
    except (OSError, ValueError) as exc:
        return [], f"{HEALTH_JSON.name} could not be read ({exc})."
    age = time.time() - float(data.get("generated_ts") or 0)
    if age > MAX_REPORT_AGE_SEC:
        return [], (f"{HEALTH_JSON.name} is {age / 3600:.1f}h old (limit "
                    f"{MAX_REPORT_AGE_SEC / 3600:.1f}h) -- fleet_health may have stopped. "
                    f"Acting on a stale snapshot is worse than not acting.")
    findings = data.get("findings")
    return (findings if isinstance(findings, list) else []), ""


# --- the one place a model is consulted --------------------------------------

_ROUTE_PROMPT = """\
A media-server fleet reported a health problem. Choose which ONE of the pre-written \
repairs below is the right one, or answer NONE.

PROBLEM
  severity: {severity}
  detector: {check}
  message: {message}

THE ONLY REPAIRS THAT EXIST
{catalogue}

Rules:
- Answer with a single repair id from the list, exactly as written, or the word NONE.
- Answer NONE unless the repair clearly addresses this exact problem. NONE is a good
  answer and is expected most of the time.
- Do not explain. Do not suggest anything that is not in the list.

ANSWER:"""


def route_with_ai(finding: dict) -> tuple[str | None, str]:
    """Ask a free model to pick a registered remedy id for an unrecognised finding.

    Returns `(remedy_id_or_None, why)`. Every failure -- no budget, a timeout, an
    unparseable answer, an id that is not in the registry, a model that invents one --
    resolves to None. There is deliberately no path from this function's output to an
    action that is not already in `remedies.REGISTRY`.
    """
    catalogue = "\n".join(f"  {r.id}: {r.title}" for r in remedies.REGISTRY)
    prompt = _ROUTE_PROMPT.format(
        severity=finding.get("severity", "?"), check=finding.get("check", "?"),
        message=str(finding.get("message", ""))[:600], catalogue=catalogue)

    attempts = config.aux_ai_attempts()
    if not attempts:
        return None, f"no AI budget available ({config.aux_ai_verdict().why})"

    try:
        import ai_client
    except ImportError as exc:
        return None, f"ai_client unavailable ({exc})"

    for a in attempts:
        prov = config.ai_provider(a["provider"])
        if not prov:
            continue
        try:
            out = ai_client.run_agent(prompt, allowed_tools=[], max_turns=1,
                                      model=a["model"], base_url=prov["base_url"],
                                      key=prov["key"])
        except Exception as exc:                                      # noqa: BLE001
            if config.identify_account_capped(str(exc)):
                config.note_ai_account_capped(a["provider"])
            continue
        text = (out.get("result") or "").strip()
        if not text:
            continue
        # Take only a token that IS a registered id. A model that answers with a sentence,
        # a new id, or a shell command yields None -- the lookup is the filter.
        for token in text.replace(",", " ").split():
            token = token.strip(".`'\"*").lower()
            if remedies.by_id(token) is not None:
                return token, f"{a['provider']}/{a['model']} chose {token}"
        return None, f"{a['provider']}/{a['model']} answered {text[:60]!r} (not a repair id)"
    return None, "no provider answered"


# --- the pass ----------------------------------------------------------------

def handle(finding: dict, dry_run: bool) -> dict:
    """Decide and (unless dry-run) act on one finding. Returns a record for the report."""
    check = str(finding.get("check", ""))
    rec: dict = {"check": check, "severity": finding.get("severity", "?"),
                 "message": finding.get("message", ""), "outcome": "", "detail": "",
                 "remedy": ""}

    matches = remedies.for_check(check)
    if not matches:
        rid, why = route_with_ai(finding)
        rec["routed_by_ai"] = why
        if rid is None:
            rec["outcome"] = "needs you"
            rec["detail"] = ("No pre-written repair covers this, and the model did not "
                             f"recognise one. ({why})")
            return rec
        matches = [remedies.by_id(rid)]
        rec["remedy"] = rid

    applied_any = False
    for remedy in matches:
        still, evidence = remedy.detect()
        if not still:
            rec["outcome"] = rec["outcome"] or "already resolved"
            rec["detail"] = evidence
            rec["remedy"] = remedy.id
            continue

        rec["remedy"] = remedy.id
        if remedy.safety != "auto":
            rec["outcome"] = "needs you"
            rec["detail"] = f"{evidence}\n{remedy.owner_instruction}"
            return rec

        if dry_run:
            rec["outcome"] = "would fix"
            rec["detail"] = f"{evidence}\n(dry run: {remedy.title})"
            return rec

        changed, what = remedy.apply() if remedy.apply else (False, "no apply step")
        if not changed:
            rec["outcome"] = "could not fix"
            rec["detail"] = f"{evidence}\n{what}"
            return rec

        # PROVE it, never report the attempt. §4.20: a repair tool once reported 250
        # repairs it had not made, so "apply returned True" is not an outcome.
        ok, proof = remedy.verify() if remedy.verify else (False, "no verify step")
        rec["outcome"] = "FIXED" if ok else "applied but UNVERIFIED"
        rec["detail"] = f"{what}\nverified: {proof}" if ok else f"{what}\ncheck failed: {proof}"
        applied_any = True

    if not rec["outcome"]:
        rec["outcome"] = "needs you"
        rec["detail"] = "a repair exists but did not engage"
    _log(f"{rec['outcome']:>20}  {check}  [{rec['remedy'] or '-'}]")
    return rec if applied_any or rec["outcome"] else rec


def write_report(records: list[dict], note: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"FLEET DOCTOR  {now}", ""]
    if note:
        lines += [note, ""]
    if not records:
        lines.append("Nothing to act on -- fleet_health reported no issues.")
    else:
        fixed = [r for r in records if r["outcome"] == "FIXED"]
        owner = [r for r in records if r["outcome"] == "needs you"]
        other = [r for r in records if r not in fixed and r not in owner]
        lines.append(f"{len(fixed)} fixed automatically, {len(owner)} need you, "
                     f"{len(other)} other.")
        lines.append("")
        for label, group in (("NEEDS YOU", owner), ("FIXED", fixed), ("OTHER", other)):
            if not group:
                continue
            lines.append(f"--- {label} ---")
            for r in group:
                lines.append(f"  [{r['severity']}] {r['message']}")
                if r["remedy"]:
                    lines.append(f"      repair: {r['remedy']}")
                for ln in str(r["detail"]).splitlines():
                    lines.append(f"      {ln}")
                lines.append("")
    lines.append("This file is written by fleet_doctor. It only ever runs pre-written, "
                 "tested repairs; anything under NEEDS YOU is deliberately not automated.")
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def run_once(dry_run: bool) -> int:
    findings, why = load_findings()
    if why:
        _log(why)
        write_report([], why)
        return 0
    records = [handle(f, dry_run) for f in findings]
    write_report(records, "")
    fixed = sum(1 for r in records if r["outcome"] == "FIXED")
    _log(f"pass complete: {len(records)} finding(s), {fixed} fixed"
         + (" (dry run)" if dry_run else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Act on fleet_health findings, safely.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and report, but apply nothing")
    ap.add_argument("--list", action="store_true",
                    help="print the remedy registry and exit")
    args = ap.parse_args()

    if args.list:
        for r in remedies.REGISTRY:
            print(f"{r.safety:>5}  {r.id:<28} {r.title}")
            print(f"       answers: {', '.join(r.answers)}")
        print(f"\nnever restarted:")
        for label, why in remedies.NEVER_RESTART.items():
            print(f"  {label}: {why}")
        return 0

    _log(f"fleet_doctor up (cycle {CYCLE_SEC}s"
         + (", DRY RUN" if args.dry_run else "") + ")")
    while True:
        try:
            run_once(args.dry_run)
        except Exception as exc:                                      # noqa: BLE001
            # A doctor that dies is worse than one that skips a pass: nothing else would
            # notice it had stopped.
            _log(f"pass failed: {exc}")
        if args.once:
            return 0
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
