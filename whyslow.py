#!/usr/bin/env python3
"""whyslow: work out why this Mac is slow, and offer to reclaim what it can.

Read only by default. Nothing is killed or deleted unless you pass --fix and
then say yes to each action individually.
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------- severities

CRIT, HIGH, MED, LOW, OK = 0, 1, 2, 3, 4
SEV_NAME = {CRIT: "CRITICAL", HIGH: "HIGH", MED: "MEDIUM", LOW: "MINOR", OK: "OK"}
SEV_COLOR = {
    CRIT: "\033[38;2;255;60;60m",
    HIGH: "\033[38;2;255;150;40m",
    MED: "\033[38;2;255;220;60m",
    LOW: "\033[38;2;120;190;255m",
    OK: "\033[38;2;90;220;140m",
}
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREY = "\033[38;2;140;140;150m"
WHITE = "\033[38;2;235;235;240m"


class Finding:
    def __init__(self, sev, title, detail, fix=None, evidence=None):
        self.sev = sev
        self.title = title
        self.detail = detail
        self.fix = fix
        self.evidence = evidence or []


class Fix:
    """A reclaim action. `cmd` is a list, or a callable returning (ok, message)."""

    def __init__(self, label, cmd, gain=None, caution=None):
        self.label = label
        self.cmd = cmd
        self.gain = gain
        self.caution = caution


# ------------------------------------------------------------------ plumbing


def sh(cmd, timeout=20):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=isinstance(cmd, str),
        )
        return out.stdout
    except Exception:
        return ""


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path, timeout=25):
    """Size of a directory in bytes, via du. Returns None if it cannot be read."""
    if not os.path.isdir(path):
        return None
    out = sh(["du", "-sk", path], timeout=timeout)
    m = re.match(r"\s*(\d+)", out)
    return int(m.group(1)) * 1024 if m else None


def etime_seconds(etime):
    """Parse ps etime (dd-hh:mm:ss, hh:mm:ss or mm:ss) into seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def duration(secs):
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def processes():
    """Every process as a dict. Cached for the life of one run."""
    if getattr(processes, "_cache", None) is not None:
        return processes._cache
    out = sh(["ps", "-Ao", "pid=,ppid=,%cpu=,rss=,etime=,ucomm=,command="])
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, cpu, rss, etime, ucomm, command = parts
        try:
            exe = command.split()[0] if command.split() else ucomm
            rows.append({
                "pid": int(pid),
                "ppid": int(ppid),
                "cpu": float(cpu),
                "rss": int(rss) * 1024,
                "age": etime_seconds(etime),
                "name": os.path.basename(exe),
                "ucomm": ucomm,
                "cmd": command,
            })
        except ValueError:
            continue
    processes._cache = rows
    return rows


# -------------------------------------------------------------------- checks


def check_disk():
    """Free space. On this machine this is the usual answer."""
    findings = []
    st = os.statvfs("/System/Volumes/Data")
    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    pct = 100.0 * free / total if total else 0

    if free < 2 * 1024**3:
        sev = CRIT
        detail = (
            f"{human(free)} free of {human(total)}. macOS keeps swap on this volume, "
            "so with the disk this full it cannot page out and every app stalls. "
            "This is almost certainly the whole problem."
        )
    elif free < 10 * 1024**3:
        sev = HIGH
        detail = f"{human(free)} free of {human(total)}. Swap has very little room left."
    elif free < 25 * 1024**3:
        sev = MED
        detail = (f"{human(free)} free of {human(total)}. Room to work, but this volume "
                  "refills quickly.")
    else:
        return [Finding(OK, "Disk space", f"{human(free)} free of {human(total)}, {pct:.0f} percent.")]

    title = "Disk almost full" if sev in (CRIT, HIGH) else "Disk filling up"
    findings.append(Finding(sev, title, detail))
    findings.extend(_reclaimable())
    return findings


def _reclaimable():
    """Where the space went. Build artifact is delegated to reclaim, which already
    knows how to find it safely; this only covers what reclaim does not touch."""
    out = []

    found, eligible = _reclaim_totals()
    if found and found > 2 * 1024**3:
        detail = f"{human(found)} of regenerable build artifact under ~/Code and the Xcode stores."
        if eligible:
            detail += f" {human(eligible)} of it is older than 60 days and safe to clear now."
        out.append(Finding(
            HIGH if found > 8 * 1024**3 else MED, "Build artifact",
            detail + " reclaim finds it and knows which directories are really source.",
            fix=Fix("Hand it to reclaim", ["reclaim", "--clean"], gain=eligible,
                    caution="reclaim asks again and makes you type the word delete."),
        ))

    runtimes = _simulator_runtimes()
    if runtimes:
        total = sum(r["bytes"] for r in runtimes)
        if total > 3 * 1024**3:
            out.append(Finding(
                HIGH, "Simulator runtimes",
                f"{human(total)} of simulator runtimes. Builds here go to the physical "
                "iPhone, so these are only needed if you actually open a simulator. "
                "reclaim does not cover these, it handles simulator devices instead.",
                evidence=[f"{human(r['bytes']):>9}  {r['name']}" for r in runtimes],
                fix=Fix("Delete every installed simulator runtime",
                        ["xcrun", "simctl", "runtime", "delete", "all"],
                        gain=total,
                        caution="Xcode redownloads one on demand, which is a slow download."),
            ))

    trash = os.path.join(HOME, ".Trash")
    size = dir_size(trash)
    if size and size > 512 * 1024**2:
        out.append(Finding(
            MED, "Trash", f"{human(size)} still in the Trash.",
            evidence=_trash_top(trash),
            fix=Fix("Open the Trash so you can look before emptying it",
                    ["open", trash],
                    caution="Deliberately not automatic. The Trash is where things go "
                            "when you were not sure."),
        ))

    snaps = [ln.strip() for ln in sh(["tmutil", "listlocalsnapshots", "/"]).splitlines()
             if ln.strip().startswith("com.apple")]
    if snaps:
        out.append(Finding(
            MED, "Local snapshots",
            f"{len(snaps)} Time Machine snapshot(s) are pinning old copies of files on disk.",
            evidence=[s[:70] for s in snaps],
            fix=Fix("Thin local snapshots to reclaim up to 20 GB",
                    ["tmutil", "thinlocalsnapshots", "/", "21474836480", "4"],
                    caution="Removes local restore points. Your real backups are untouched."),
        ))

    big = []
    for name in ("Downloads", "Pictures", "Movies", "Screenshots", "Desktop"):
        path = os.path.join(HOME, name)
        size = dir_size(path, timeout=60)
        if size and size > 3 * 1024**3:
            big.append((name, size))
    if big:
        big.sort(key=lambda x: -x[1])
        out.append(Finding(
            LOW, "Large personal folders",
            "Nothing here is safe to delete automatically, but this is where the space is.",
            evidence=[f"{human(sz):>9}  ~/{n}" for n, sz in big],
        ))

    return out


def _simulator_runtimes():
    """Installed simulator runtimes, sized from the volumes they mount.

    These are not what reclaim looks at: it walks the per device directories in
    ~/Library/Developer/CoreSimulator/Devices, while the runtime images live in
    /Library/Developer/CoreSimulator/Volumes and are far larger.
    """
    out = sh(["xcrun", "simctl", "runtime", "list"], timeout=30)
    runtimes = []
    for line in out.splitlines():
        m = re.match(r"\s*((?:iOS|watchOS|tvOS|visionOS) [\d.]+ \(([\w.]+)\))", line)
        if m:
            runtimes.append({"name": m.group(1), "build": m.group(2), "bytes": 0})
    if not runtimes:
        return []
    root = "/Library/Developer/CoreSimulator"
    vols = os.path.join(root, "Volumes")
    total = dir_size(root, timeout=90) or 0
    if os.path.isdir(vols):
        for entry in os.listdir(vols):
            size = dir_size(os.path.join(vols, entry), timeout=90) or 0
            for r in runtimes:
                if r["build"] in entry:
                    r["bytes"] = size
    unaccounted = total - sum(r["bytes"] for r in runtimes)
    if unaccounted > 512 * 1024**2:
        runtimes.append({"name": "simulator caches", "build": "", "bytes": unaccounted})
    return [r for r in runtimes if r["bytes"] > 0]


def _reclaim_totals():
    """Ask reclaim what it can see. Returns (found, eligible) bytes, or (None, None)."""
    exe = shutil.which("reclaim") or os.path.join(HOME, "Code/reclaim/reclaim.py")
    if not os.path.exists(exe) and not shutil.which("reclaim"):
        return None, None
    cmd = [exe, "--json"] if shutil.which("reclaim") else ["python3", exe, "--json"]
    out = sh(cmd, timeout=90)
    try:
        totals = json.loads(out)["totals"]
        return totals.get("found_bytes"), totals.get("eligible_bytes")
    except Exception:
        return None, None


def _trash_top(trash, n=4):
    out = sh(f"du -sk {trash!r}/* 2>/dev/null | sort -rn | head -{n}")
    ev = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        kb, path = line.split("\t", 1)
        ev.append(f"{human(int(kb) * 1024):>9}  {os.path.basename(path)}")
    return ev


def check_memory():
    findings = []
    swap = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap)
    if m:
        total_s, used_s = float(m.group(1)), float(m.group(2))
        pct = 100.0 * used_s / total_s if total_s else 0
        if pct > 88:
            findings.append(Finding(
                CRIT, "Swap is full",
                f"{used_s / 1024:.1f} GB of {total_s / 1024:.1f} GB swap in use, {pct:.0f} percent. "
                "The machine is spending its time moving memory to disk instead of working.",
            ))
        elif pct > 65:
            findings.append(Finding(
                HIGH, "Heavy swapping",
                f"{used_s / 1024:.1f} GB of {total_s / 1024:.1f} GB swap in use, {pct:.0f} percent.",
            ))

    vm = sh(["vm_stat"])
    pagesize = 16384
    pm = re.search(r"page size of (\d+)", vm)
    if pm:
        pagesize = int(pm.group(1))
    stats = dict(re.findall(r'"?([A-Za-z ,\-]+?)"?:\s+(\d+)', vm))

    def pages(key):
        return int(stats.get(key, 0))

    compressed = pages("Pages stored in compressor")
    occupied = pages("Pages occupied by compressor")
    if occupied * pagesize > 2 * 1024**3:
        ratio = compressed / occupied if occupied else 0
        findings.append(Finding(
            HIGH, "Memory compressor working hard",
            f"{human(occupied * pagesize)} of RAM is holding {human(compressed * pagesize)} "
            f"of compressed pages, a {ratio:.1f} to 1 squeeze. "
            "Everything you touch has to be decompressed first.",
        ))

    hogs = sorted(processes(), key=lambda p: -p["rss"])[:6]
    total_ram = int(sh(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    if hogs and hogs[0]["rss"] > 0.15 * total_ram:
        findings.append(Finding(
            MED, "Largest memory user",
            f"{os.path.basename(hogs[0]['cmd'].split()[0])} is holding {human(hogs[0]['rss'])}.",
            evidence=[f"{human(p['rss']):>10}  pid {p['pid']:<7} {p['name']}" for p in hogs],
        ))

    if not findings:
        findings.append(Finding(OK, "Memory", "No unusual pressure."))
    return findings


def check_load():
    cores = int(sh(["sysctl", "-n", "hw.ncpu"]).strip() or 8)
    la = os.getloadavg()[0]
    per_core = la / cores
    busy = sorted([p for p in processes() if p["cpu"] > 15], key=lambda p: -p["cpu"])[:6]
    ev = [f"{p['cpu']:>5.1f}%  pid {p['pid']:<7} {p['name']}" for p in busy]

    if per_core > 1.6:
        sev, word = CRIT, "far more work queued than it can run"
    elif per_core > 1.0:
        sev, word = HIGH, "more work queued than cores to run it"
    elif per_core > 0.7:
        sev, word = MED, "busy"
    else:
        return [Finding(OK, "CPU load", f"Load {la:.1f} across {cores} cores.")]

    return [Finding(sev, "CPU is saturated",
                    f"Load average {la:.1f} across {cores} cores, {word}.",
                    evidence=ev)]


def check_stale_servers():
    """Dev servers and dashboards left running from sessions that ended days ago."""
    patterns = [
        (r"vite", "vite dev server"),
        (r"server\.js", "node server"),
        (r"npm run dev", "npm dev script"),
        (r"webpack", "webpack"),
        (r"next dev", "next dev server"),
        (r"http\.server", "python http server"),
        (r"flask|uvicorn|gunicorn", "python web server"),
        (r"claude-usage.*dashboard", "claude-usage dashboard"),
        (r"jekyll|hugo serve", "static site server"),
    ]
    stale = []
    for p in processes():
        if p["age"] < 6 * 3600:
            continue
        for pat, label in patterns:
            if re.search(pat, p["cmd"], re.I):
                stale.append((p, label))
                break

    if not stale:
        return [Finding(OK, "Background servers", "Nothing left running from an old session.")]

    ev = []
    pids = []
    for p, label in sorted(stale, key=lambda x: -x[0]["age"]):
        proj = ""
        pm = re.search(r"/Code/([^/]+)", p["cmd"])
        if pm:
            proj = f" ({pm.group(1)})"
        ev.append(f"{duration(p['age']):>5} old  pid {p['pid']:<7} {label}{proj}")
        pids.append(str(p["pid"]))

    ram = sum(p["rss"] for p, _ in stale)
    return [Finding(
        HIGH if len(stale) >= 3 else MED,
        "Dev servers still running",
        f"{len(stale)} server(s) from earlier sessions are still up, holding {human(ram)}.",
        evidence=ev,
        fix=Fix(f"Stop all {len(stale)} of them", ["kill"] + pids,
                gain=ram, caution="Restart any you still want with the project's usual command."),
    )]


def check_duplicates():
    """The same script running more than once, which is nearly always accidental."""
    seen = {}
    for p in processes():
        key = None
        for pat in (r"claude-usage\.mjs \w+", r"/Code/[^/]+/[\w./]+\.(js|mjs|py)"):
            m = re.search(pat, p["cmd"])
            if m:
                key = m.group(0)
                break
        if key:
            seen.setdefault(key, []).append(p)

    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if not dupes:
        return []

    ev, kill = [], []
    for key, procs in dupes.items():
        procs.sort(key=lambda p: p["age"])
        ev.append(f"{len(procs)}x  {os.path.basename(key)}")
        for p in procs:
            ev.append(f"      {duration(p['age']):>5} old  pid {p['pid']}")
        kill += [str(p["pid"]) for p in procs[1:]]

    return [Finding(
        MED, "Duplicate processes",
        f"{len(dupes)} script(s) are running more than one copy of themselves.",
        evidence=ev,
        fix=Fix("Keep the newest of each, stop the rest", ["kill"] + kill),
    )]


def check_claude_sessions():
    sessions = [p for p in processes()
                if p["name"] == "claude" and not p["cmd"].startswith("/Applications/Claude Status")]
    if len(sessions) < 4:
        return []
    ram = sum(p["rss"] for p in sessions)
    ev = [f"{human(p['rss']):>9}  {duration(p['age']):>5} old  pid {p['pid']}"
          for p in sorted(sessions, key=lambda p: -p["rss"])[:8]]
    old = [p for p in sessions if p["age"] > 12 * 3600]
    sev = HIGH if len(sessions) >= 7 else MED
    detail = f"{len(sessions)} Claude Code sessions open, holding {human(ram)} between them."
    if old:
        detail += f" {len(old)} of them have been open longer than 12 hours."
    return [Finding(sev, "Many Claude sessions", detail, evidence=ev)]


def check_system_daemons():
    """Indexing, photo analysis, backups: heavy, temporary, and worth knowing about."""
    watch = {
        "mds_stores": "Spotlight is rebuilding its index",
        "mdworker_shared": "Spotlight is indexing files",
        "photoanalysisd": "Photos is analysing your library",
        "mediaanalysisd": "Media analysis is running",
        "backupd": "Time Machine is backing up",
        "bird": "iCloud Drive is syncing",
        "cloudd": "iCloud is syncing",
        "itunescloudd": "Music library is syncing with iCloud",
        "kernel_task": "The kernel is throttling the CPU to shed heat",
        "screencaptureui": "The screenshot tool is still open",
    }
    findings = []
    for p in processes():
        label = watch.get(p["name"])
        if not label:
            continue
        if p["name"] == "screencaptureui":
            findings.append(Finding(
                MED, "Screenshot tool stuck",
                f"{label}, {duration(p['age'])} old. Left open it holds a full screen window "
                "on the menu bar's own layer.",
                fix=Fix("Quit it (it relaunches when you next press it)",
                        ["kill", str(p["pid"])]),
            ))
        elif p["cpu"] > 25:
            findings.append(Finding(
                MED if p["name"] != "kernel_task" else HIGH,
                label.split(" is ")[0] if " is " in label else p["name"],
                f"{label}, using {p['cpu']:.0f} percent CPU. "
                + ("This finishes on its own. It is not something to fix."
                   if p["name"] != "kernel_task" else
                   "Check for anything blocking the vents, and unplug heavy USB devices."),
            ))
    return findings


def check_uptime():
    out = sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec = (\d+)", out)
    if not m:
        return []
    up = time.time() - int(m.group(1))
    days = up / 86400
    if days < 10:
        return [Finding(OK, "Uptime", f"Up {duration(int(up))}.")]
    return [Finding(
        MED if days < 25 else HIGH, "Long uptime",
        f"Up {int(days)} days. Leaked memory and stuck system UI accumulate over a stretch "
        "like this, and a restart clears both.",
    )]


def check_thermal():
    out = sh(["pmset", "-g", "therm"])
    findings = []
    m = re.search(r"CPU_Speed_Limit\s+=\s+(\d+)", out)
    if m and int(m.group(1)) < 100:
        findings.append(Finding(
            HIGH, "CPU is throttled",
            f"The CPU is capped at {m.group(1)} percent of its speed, because of heat or power.",
        ))
    return findings


def check_containers():
    heavy = []
    for p in processes():
        if re.search(r"colima|qemu|com\.docker\.backend|VirtualBox|VBoxHeadless|Parallels", p["cmd"], re.I):
            heavy.append(p)
    if not heavy:
        return []
    ram = sum(p["rss"] for p in heavy)
    return [Finding(
        HIGH if ram > 1024**3 else MED, "A virtual machine is running",
        f"{len(heavy)} VM or container process(es) holding {human(ram)}. "
        "On 8 GB of RAM this is usually the difference between fine and unusable.",
        evidence=[f"{human(p['rss']):>9}  pid {p['pid']:<7} {p['name']}" for p in heavy],
    )]


CHECKS = [
    ("disk", check_disk),
    ("memory", check_memory),
    ("cpu", check_load),
    ("servers", check_stale_servers),
    ("duplicates", check_duplicates),
    ("sessions", check_claude_sessions),
    ("daemons", check_system_daemons),
    ("containers", check_containers),
    ("thermal", check_thermal),
    ("uptime", check_uptime),
]


# ------------------------------------------------------------------- display


def rainbow(text, phase=0.0):
    """A hue sweep across a string. The one bright thing on the page."""
    import colorsys
    out = []
    n = max(len(text), 1)
    for i, ch in enumerate(text):
        h = ((i / n) * 0.8 + phase) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.75, 1.0)
        out.append(f"\033[38;2;{int(r * 255)};{int(g * 255)};{int(b * 255)}m{ch}")
    return "".join(out) + RESET


def scan(animate=True):
    """Run every check, sweeping a rainbow while it works."""
    findings = []
    tty = sys.stdout.isatty() and animate
    for i, (name, fn) in enumerate(CHECKS):
        if tty:
            bar = "".join("█" if j <= i else "░" for j in range(len(CHECKS)))
            sys.stdout.write(f"\r  {rainbow(bar, time.time() * 0.35)} {GREY}{name}{RESET}   ")
            sys.stdout.flush()
        try:
            findings += fn() or []
        except Exception as e:
            findings.append(Finding(LOW, f"Check failed: {name}", str(e)))
    if tty:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    return findings


def report(findings, show_ok=False):
    findings.sort(key=lambda f: f.sev)
    problems = [f for f in findings if f.sev != OK]

    title = "  why this mac is slow  "
    print()
    print(rainbow(title, time.time() * 0.2))
    print()

    if not problems:
        print(f"  {SEV_COLOR[OK]}Nothing is wrong.{RESET} Everything measured is in normal range.")
        print()
        show_ok = True
    else:
        worst = problems[0]
        print(f"  {BOLD}{WHITE}{worst.title}.{RESET} {GREY}That is the one to deal with first.{RESET}")
        print()

    shown = findings if show_ok else problems
    for f in shown:
        c = SEV_COLOR[f.sev]
        print(f"  {c}●{RESET} {BOLD}{WHITE}{f.title}{RESET} {c}{DIM}{SEV_NAME[f.sev]}{RESET}")
        for line in wrap(f.detail, 76):
            print(f"    {GREY}{line}{RESET}")
        for e in f.evidence:
            print(f"      {DIM}{GREY}{e}{RESET}")
        if f.fix:
            gain = f" frees about {human(f.fix.gain)}" if f.fix.gain else ""
            print(f"    {SEV_COLOR[OK]}fix:{RESET} {WHITE}{f.fix.label}{RESET}{GREY}{gain}{RESET}")
        print()

    if problems and any(f.fix for f in problems):
        print(f"  {GREY}Run {WHITE}whyslow --fix{GREY} to be walked through the fixes one at a time.{RESET}")
        print()


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def apply_fixes(findings, assume_yes=False):
    fixable = [f for f in findings if f.fix and f.sev != OK]
    if not fixable:
        print(f"  {GREY}Nothing here has an automatic fix.{RESET}\n")
        return
    for f in fixable:
        fix = f.fix
        print(f"  {BOLD}{WHITE}{f.title}{RESET}")
        print(f"    {fix.label}" + (f" {GREY}(about {human(fix.gain)}){RESET}" if fix.gain else ""))
        shown = fix.cmd if isinstance(fix.cmd, str) else " ".join(fix.cmd)
        print(f"    {DIM}{GREY}{shown[:200]}{RESET}")
        if fix.caution:
            print(f"    {SEV_COLOR[MED]}{fix.caution}{RESET}")
        if assume_yes:
            ans = "y"
        else:
            try:
                ans = input(f"    do it? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if ans == "y":
            r = subprocess.run(fix.cmd, shell=isinstance(fix.cmd, str),
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"    {SEV_COLOR[OK]}done{RESET}\n")
            else:
                print(f"    {SEV_COLOR[CRIT]}failed:{RESET} {r.stderr.strip()[:200]}\n")
        else:
            print(f"    {GREY}skipped{RESET}\n")


def main():
    ap = argparse.ArgumentParser(
        prog="whyslow",
        description="Work out why this Mac is slow.")
    ap.add_argument("--fix", action="store_true",
                    help="after the report, offer each fix one at a time")
    ap.add_argument("--yes", action="store_true",
                    help="with --fix, apply every offered fix without asking")
    ap.add_argument("--all", action="store_true",
                    help="show the checks that passed as well")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("--watch", type=float, nargs="?", const=5.0, default=None,
                    metavar="SECS", help="re-run continuously")
    args = ap.parse_args()

    if args.watch:
        try:
            while True:
                processes._cache = None
                fs = scan()
                os.system("clear")
                report(fs, show_ok=args.all)
                print(f"  {DIM}{GREY}refreshing every {args.watch:.0f}s, ctrl+c to stop{RESET}")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print()
        return

    findings = scan(animate=not args.json)

    if args.json:
        print(json.dumps([{
            "severity": SEV_NAME[f.sev],
            "title": f.title,
            "detail": f.detail,
            "evidence": f.evidence,
            "fix": f.fix.label if f.fix else None,
        } for f in sorted(findings, key=lambda f: f.sev)], indent=2))
        return

    report(findings, show_ok=args.all)
    if args.fix:
        apply_fixes(sorted(findings, key=lambda f: f.sev), assume_yes=args.yes)

    worst = min((f.sev for f in findings), default=OK)
    sys.exit(1 if worst <= HIGH else 0)


if __name__ == "__main__":
    main()
