#!/usr/bin/env python3
"""whyslow: work out why this Mac is slow, and offer to reclaim what it can.

By default the measurements go to Claude, which writes a short plan, and one
Enter runs the steps whyslow can do itself. Anything that deletes data asks
again on its own. --plain skips Claude and prints the rule based report.
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

    def __init__(self, label, cmd, gain=None, caution=None, heavy=False):
        self.label = label
        self.cmd = cmd
        self.gain = gain
        self.caution = caution
        # heavy fixes delete data or take a long time, so they always ask on their own
        self.heavy = heavy
        self.id = None


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
                    caution="reclaim asks again and makes you type the word delete.", heavy=True),
        ))

    runtimes = _simulator_runtimes()
    # The newest iOS runtime is also the device build platform for the iPhone,
    # so it is never offered for deletion.
    ios = [r for r in runtimes if r["name"].startswith("iOS") and r["uuid"]]
    keep = max(ios, key=lambda r: [int(x) for x in re.findall(r"\d+", r["name"])[:3]]) if ios else None
    spare = [r for r in runtimes if r["uuid"] and r is not keep]
    total = sum(r["bytes"] for r in spare)
    if spare and total > 3 * 1024**3:
        out.append(Finding(
            HIGH, "Simulator runtimes",
            f"{human(total)} of simulator runtimes besides the newest iOS one, which stays "
            "because device builds need it.",
            evidence=[f"{human(r['bytes']):>9}  {r['name']}" for r in spare],
            fix=Fix(f"Delete {len(spare)} spare simulator runtime(s)",
                    " && ".join(f"xcrun simctl runtime delete {r['uuid']}" for r in spare),
                    gain=total, heavy=True,
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
                    caution="Removes local restore points. Your real backups are untouched.",
                    heavy=True),
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
        m = re.match(r"\s*((?:iOS|watchOS|tvOS|visionOS) [\d.]+ \(([\w.]+)\))(?: - ([0-9A-F-]{36}))?", line)
        if m:
            runtimes.append({"name": m.group(1), "build": m.group(2), "uuid": m.group(3), "bytes": 0})
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
        runtimes.append({"name": "simulator caches", "build": "", "uuid": None, "bytes": unaccounted})
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


def total_ram():
    return int(sh(["sysctl", "-n", "hw.memsize"]).strip() or 0) or 8 * 1024**3


def pressure_level():
    # The kernel's own verdict: 1 normal, 2 warning, 4 critical.
    try:
        return int(sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    except ValueError:
        return 1


def check_memory():
    findings = []
    ram = total_ram()
    pressure = pressure_level()
    # Swap and compressed pages linger long after the demand that caused them.
    # On a machine with room to spare they are history, not a slowdown, so they
    # only count when the kernel says memory is actually tight.
    tight = pressure >= 2
    if pressure >= 4:
        findings.append(Finding(
            CRIT, "Memory pressure critical",
            f"macOS reports critical memory pressure on {human(ram)} of RAM.",
        ))
    elif pressure == 2:
        findings.append(Finding(
            HIGH, "Memory pressure elevated",
            f"macOS reports memory pressure at warning level on {human(ram)} of RAM.",
        ))

    swap = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap)
    if m:
        total_s, used_s = float(m.group(1)), float(m.group(2))
        pct = 100.0 * used_s / total_s if total_s else 0
        if not tight:
            pass
        elif pct > 88:
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
    if tight and occupied * pagesize > 0.25 * ram:
        ratio = compressed / occupied if occupied else 0
        findings.append(Finding(
            HIGH, "Memory compressor working hard",
            f"{human(occupied * pagesize)} of RAM is holding {human(compressed * pagesize)} "
            f"of compressed pages, a {ratio:.1f} to 1 squeeze. "
            "Everything you touch has to be decompressed first.",
        ))

    hogs = sorted(processes(), key=lambda p: -p["rss"])[:6]
    if hogs and hogs[0]["rss"] > 0.15 * ram:
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

    idle = cpu_idle()
    if per_core > 1.6:
        sev, word = CRIT, "far more work queued than it can run"
    elif per_core > 1.0:
        sev, word = HIGH, "more work queued than cores to run it"
    elif per_core > 0.7:
        sev, word = MED, "busy"
    else:
        return [Finding(OK, "CPU load", f"Load {la:.1f} across {cores} cores.")]

    # macOS load average also counts threads waiting on disk and locks, so a
    # spike with idle cores is a stall, not a CPU shortage.
    if idle is not None and idle > 50:
        return [Finding(MED if sev == CRIT else LOW, "Load spike with idle CPU",
                        f"Load average {la:.1f} across {cores} cores while the CPU is "
                        f"{idle:.0f} percent idle. Threads are waiting, not computing.",
                        evidence=ev)]

    detail = f"Load average {la:.1f} across {cores} cores, {word}."
    if idle is not None:
        detail += f" CPU {idle:.0f} percent idle."
    return [Finding(sev, "CPU is saturated", detail, evidence=ev)]


def live_cpu():
    """(idle percent, {pid: cpu percent}) over one second, from top's second sample.

    ps reports a decaying average that can show a process busy a minute after it
    stopped, so anything that names a busy process uses this instead."""
    if getattr(live_cpu, "_cache", None) is not None:
        return live_cpu._cache
    out = sh(["top", "-l", "2", "-s", "1", "-n", "60", "-o", "cpu", "-stats", "pid,cpu"], timeout=15)
    idle, per = None, {}
    samples = out.split("Processes:")
    if len(samples) >= 3:
        last = samples[-1]
        m = re.search(r"([\d.]+)% idle", last)
        idle = float(m.group(1)) if m else None
        for line in last.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                try:
                    per[int(parts[0])] = float(parts[1])
                except ValueError:
                    pass
    live_cpu._cache = (idle, per)
    if per:
        for p in processes():
            p["cpu"] = per.get(p["pid"], 0.0)
    return live_cpu._cache



def cpu_idle():
    return live_cpu()[0]


def launchd_pids():
    """Pids launchd keeps alive. Killing one only restarts it."""
    pids = set()
    for line in sh(["launchctl", "list"]).splitlines():
        parts = line.split(None, 2)
        if parts and parts[0].isdigit():
            pids.add(int(parts[0]))
    return pids


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
    managed = launchd_pids()
    stale = []
    for p in processes():
        if p["age"] < 6 * 3600 or p["pid"] in managed or p["ppid"] in managed:
            continue
        for pat, label in patterns:
            if re.search(pat, p["cmd"], re.I):
                stale.append((p, label))
                break

    if not stale:
        return [Finding(OK, "Background servers", "Nothing left running from an old session.")]

    # One finding per project, so each can be stopped on its own. A launcher and
    # the server it started (uv run and uvicorn) land in the same group.
    groups = {}
    for p, label in stale:
        pm = re.search(r"/Code/([^/]+)", p["cmd"]) or re.search(r"--port[ =](\d+)", p["cmd"])
        key = pm.group(1) if pm else str(p["pid"])
        port = re.search(r"--port[ =](\d+)", p["cmd"])
        for k, g in groups.items():
            if port and port.group(1) in g["ports"]:
                key = k
        g = groups.setdefault(key, {"procs": [], "label": label, "ports": set(), "project": None})
        g["procs"].append(p)
        if port:
            g["ports"].add(port.group(1))
        proj = re.search(r"/Code/([^/]+)", p["cmd"])
        if proj:
            g["project"] = proj.group(1)

    findings = []
    for key, g in groups.items():
        procs = sorted(g["procs"], key=lambda p: -p["age"])
        name = g["project"] or g["label"]
        ram = sum(p["rss"] for p in procs)
        ports = ", ".join(sorted(g["ports"]))
        findings.append(Finding(
            HIGH if ram > 0.10 * total_ram() else MED if ram > 0.02 * total_ram() else LOW,
            f"Old dev server: {name}",
            f"{g['label']} up for {duration(procs[0]['age'])}"
            + (f" on port {ports}" if ports else "") + f", holding {human(ram)}.",
            evidence=[f"pid {p['pid']:<7} {p['cmd'][:90]}" for p in procs],
            fix=Fix(f"Stop the {name} server" if g["project"] else f"Stop the {name}", ["kill"] + [str(p["pid"]) for p in procs],
                    gain=ram, caution="Restart it with the project's usual command."),
        ))
    return findings


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

    for k, v in seen.items():
        pids = {p["pid"] for p in v}
        seen[k] = [p for p in v if p["ppid"] not in pids]
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
    share = ram / total_ram()
    sev = HIGH if share > 0.25 else MED if share > 0.10 else LOW
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
        HIGH if ram > 0.25 * total_ram() else MED, "A virtual machine is running",
        f"{len(heavy)} VM or container process(es) holding {human(ram)} "
        f"of {human(total_ram())} RAM.",
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


# ------------------------------------------------------------------- advisor

ADVISOR_PROMPT = """You are the advisor inside whyslow, a command line tool on Mustafa's Mac.
He runs it when the Mac feels slow. It has already measured everything and ran
rule based checks (the findings). The rules are crude, so you decide what is
actually going on and what he should do right now.

How to judge:
- Separate load he chose from waste. A video call, lecture stream, build or
  model run he is in the middle of is expected. Name it plainly as the cause and
  do not tell him to stop it unless it is clearly left over (for example a call
  tab still busy long after a meeting would have ended).
- Match browser content processes to the open tabs. A heavy browser content
  process plus coreaudiod and video decoder activity usually means a call or
  video in a tab; name the tab he would recognise.
- A server whose port appears in an open tab, or that the port registry says is
  a LaunchAgent, is in use. Never suggest stopping it.
- Process cpu_pct values are a live one second sample. macOS load average
  counts threads waiting on disk and locks, so trust cpu_idle_pct over it.
- A local server with connected_clients_now is being used by something.
- Swap and compressed memory linger for days. They only matter when
  memory_pressure is not normal.
- Findings marked severe can be wrong; overrule them. If nothing meaningful is
  slowing the Mac, say so and return no steps.

Output:
- headline: at most two sentences. What is making it slow right now, in terms
  he recognises (app, tab, project), with the key number.
- steps: at most 5, most impactful first, only ones worth doing. Each step's
  text is one short sentence saying what to do. Set fix to an id from
  available_fixes only when that exact action is the right thing; otherwise fix
  is null and he does the step himself.

Writing rules, strict:
- Facts only. No reassurance, no coaching, no explaining your reasoning, no
  "don't worry", no "that's fine", no "that's expected", no "nothing owed".
  State what is using the CPU and stop.
- Every number and duration must come from the data. Uptime is the uptime
  field, not a process age.
- Plain, specific, short. Use app names and tab titles, not pids or internal
  process names like plugin-container.
- Never use dashes as punctuation: no em dash, no en dash, no hyphen between
  clauses. Rephrase with commas, colons or separate sentences.
"""

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "fix": {"type": ["string", "null"]},
                },
                "required": ["text", "fix"],
            },
        },
    },
    "required": ["headline", "steps"],
}


def _lz4_block(src, size):
    """Decode one raw LZ4 block. Zen's session file is LZ4 and python has no lz4 built in."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n or len(dst) >= size:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    return bytes(dst)


def browser_tabs(limit=80):
    """Open tabs in Zen, from its session recovery file."""
    import glob
    files = glob.glob(os.path.join(
        HOME, "Library/Application Support/zen/Profiles/*/sessionstore-backups/recovery.jsonlz4"))
    if not files:
        return []
    path = max(files, key=os.path.getmtime)
    try:
        raw = open(path, "rb").read()
        if raw[:8] != b"mozLz40\0":
            return []
        size = int.from_bytes(raw[8:12], "little")
        data = json.loads(_lz4_block(raw[12:], size))
    except Exception:
        return []
    tabs = []
    for w in data.get("windows", []):
        for t in w.get("tabs", []):
            entries = t.get("entries") or []
            if not entries:
                continue
            e = entries[min(max(t.get("index", 1), 1), len(entries)) - 1]
            url = e.get("url", "")
            if url.startswith("about:"):
                continue
            # host only: paths carry message ids and document names that add nothing here
            host = re.match(r"[a-z]+://([^/?#]+)", url)
            tabs.append({"title": e.get("title", "")[:100], "site": host.group(1) if host else url[:40],
                         "last_used_min_ago": int((time.time() * 1000 - t.get("lastAccessed", 0)) / 60000)
                         if t.get("lastAccessed") else None})
    tabs.sort(key=lambda t: t["last_used_min_ago"] if t["last_used_min_ago"] is not None else 1e9)
    return tabs[:limit]


def listening_servers():
    out = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    by_pid = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9 or not parts[1].isdigit():
            continue
        port = parts[8].rsplit(":", 1)[-1]
        by_pid.setdefault(int(parts[1]), set()).add(port)
    clients = {}
    for line in sh(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9 and "->" in parts[8]:
            local, remote = parts[8].split("->", 1)
            # the client end: its remote side is the server's port
            port = remote.rsplit(":", 1)[-1]
            clients.setdefault(port, set()).add(parts[0])
    procs = {p["pid"]: p for p in processes()}
    managed = launchd_pids()
    rows = []
    for pid, ports in by_pid.items():
        p = procs.get(pid)
        if not p or p["cmd"].startswith(("/System", "/usr/libexec", "/usr/sbin")):
            continue
        cwd = ""
        for ln in sh(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
            if ln.startswith("n"):
                cwd = ln[1:].replace(HOME, "~")
        rows.append({"pid": pid, "ports": sorted(ports), "cwd": cwd, "cmd": p["cmd"][:120],
                     "age": duration(p["age"]), "launchd_managed": pid in managed or p["ppid"] in managed,
                     "connected_clients_now": sorted(set().union(*(clients.get(pt, set()) for pt in ports)))})
    return rows


def gather_context(findings):
    procs = processes()
    top_cpu = sorted(procs, key=lambda p: -p["cpu"])[:20]
    top_mem = sorted(procs, key=lambda p: -p["rss"])[:12]
    seen, plist = set(), []
    for p in top_cpu + top_mem:
        if p["pid"] in seen:
            continue
        seen.add(p["pid"])
        plist.append({"pid": p["pid"], "ppid": p["ppid"], "cpu_pct": round(p["cpu"]),
                      "rss": human(p["rss"]), "age": duration(p["age"]), "cmd": p["cmd"][:160]})
    counts = {}
    for p in procs:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    st = os.statvfs("/System/Volumes/Data")
    boot = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    registry = os.path.join(HOME, ".claude/reference/local-ports.md")
    return {
        "now": time.strftime("%A %d %b %Y %H:%M"),
        "machine": {
            "model": sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip(),
            "ram": human(total_ram()),
            "cores": int(sh(["sysctl", "-n", "hw.ncpu"]).strip() or 0),
        },
        "load_average_1_5_15": [round(x, 1) for x in os.getloadavg()],
        "cpu_idle_pct": round(cpu_idle()) if cpu_idle() is not None else None,
        "memory_pressure": {1: "normal", 2: "warning", 4: "critical"}.get(pressure_level(), "unknown"),
        "swap": sh(["sysctl", "-n", "vm.swapusage"]).strip(),
        "disk_free": human(st.f_bavail * st.f_frsize),
        "uptime": duration(int(time.time() - int(boot.group(1)))) if boot else None,
        "findings": [{"severity": SEV_NAME[f.sev], "title": f.title, "detail": f.detail,
                      "evidence": f.evidence[:8]} for f in findings if f.sev != OK],
        "available_fixes": [{"id": f.fix.id, "action": f.fix.label, "for_finding": f.title,
                             "command": f.fix.cmd if isinstance(f.fix.cmd, str) else " ".join(f.fix.cmd)}
                            for f in findings if f.fix and f.sev != OK],
        "processes": plist,
        "process_counts": {k: v for k, v in sorted(counts.items(), key=lambda x: -x[1])[:15] if v > 2},
        "listening_servers": listening_servers(),
        "zen_tabs_most_recent_first": browser_tabs(),
        "port_registry": open(registry).read()[:12000] if os.path.exists(registry) else None,
    }


def ask_claude(ctx, timeout=120):
    exe = shutil.which("claude") or os.path.join(HOME, ".local/bin/claude")
    if not os.path.exists(exe):
        raise RuntimeError("the claude command is not installed")
    r = subprocess.run(
        [exe, "-p", "--model", "sonnet", "--system-prompt", ADVISOR_PROMPT,
         "--tools", "", "--setting-sources", "", "--no-session-persistence",
         "--output-format", "json", "--json-schema", json.dumps(ADVICE_SCHEMA)],
        input=json.dumps(ctx), capture_output=True, text=True, timeout=timeout,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "no response")
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(str(out.get("result") or "no structured answer")[:200])
    return out["structured_output"]


def with_spinner(label, fn):
    """Run fn in a thread while a rainbow sweeps, so the wait is visibly alive."""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    started = time.time()
    tty = sys.stdout.isatty()
    while t.is_alive():
        if tty:
            dots = "".join("█" if (i + int((time.time() - started) * 8)) % 10 < 5 else "░" for i in range(10))
            sys.stdout.write(f"\r  {rainbow(dots, time.time() * 0.35)} {GREY}{label} "
                             f"{int(time.time() - started)}s{RESET}   ")
            sys.stdout.flush()
        t.join(0.1)
    if tty:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    if "error" in box:
        raise box["error"]
    return box["value"]


def advise(findings):
    """Claude's plan, then one keypress for the steps whyslow can run."""
    ctx = gather_context(findings)
    try:
        advice = with_spinner("asking Claude", lambda: ask_claude(ctx))
    except Exception as e:
        report(findings)
        print(f"  {GREY}Claude unavailable ({str(e)[:120]}). This is the rule based report.{RESET}")
        print()
        return

    fixes = {f.fix.id: f for f in findings if f.fix}
    print()
    print(rainbow("  why this mac is slow  ", time.time() * 0.2))
    print()
    for line in wrap(advice.get("headline", ""), 76):
        print(f"  {BOLD}{WHITE}{line}{RESET}")
    print()

    steps = advice.get("steps") or []
    runnable = []
    for i, step in enumerate(steps, 1):
        f = fixes.get(step.get("fix"))
        lines = wrap(step.get("text", ""), 72)
        print(f"  {WHITE}{i}.{RESET} {WHITE}{lines[0] if lines else ''}{RESET}")
        for line in lines[1:]:
            print(f"     {WHITE}{line}{RESET}")
        if f:
            runnable.append((i, f))
            gain = f", frees about {human(f.fix.gain)}" if f.fix.gain else ""
            who = "whyslow asks before this one" if f.fix.heavy else "whyslow does this"
            print(f"     {SEV_COLOR[OK]}{who}{RESET}{GREY}{gain}{RESET}")
        else:
            print(f"     {GREY}yours{RESET}")
        print()

    if not steps:
        print(f"  {GREY}No steps.{RESET}")
        print()
    if not runnable or not sys.stdin.isatty():
        return

    nums = [str(i) for i, _ in runnable]
    which = nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " and " + nums[-1]
    try:
        ans = input(f"  {GREY}Press {WHITE}enter{GREY} to do {which}, or {WHITE}q{GREY} to leave it.{RESET} ")
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans.strip().lower() not in ("", "y", "yes"):
        return
    print()
    for i, f in runnable:
        fix = f.fix
        if fix.heavy:
            shown = fix.cmd if isinstance(fix.cmd, str) else " ".join(fix.cmd)
            print(f"  {WHITE}{i}.{RESET} {fix.label}")
            print(f"     {DIM}{GREY}{shown[:200]}{RESET}")
            if fix.caution:
                print(f"     {SEV_COLOR[MED]}{fix.caution}{RESET}")
            try:
                if input("     do it? [y/N] ").strip().lower() != "y":
                    print(f"     {GREY}skipped{RESET}\n")
                    continue
            except (EOFError, KeyboardInterrupt):
                print()
                return
        run_fix(i, fix)


def run_fix(i, fix):
    # reclaim is interactive, so it gets the terminal instead of captured output
    interactive = not isinstance(fix.cmd, str) and fix.cmd[:1] == ["reclaim"]
    r = subprocess.run(fix.cmd, shell=isinstance(fix.cmd, str),
                       capture_output=not interactive, text=True)
    if r.returncode == 0:
        print(f"  {WHITE}{i}.{RESET} {fix.label}: {SEV_COLOR[OK]}done{RESET}")
    else:
        err = (r.stderr or "").strip()[:200] if not interactive else f"exit {r.returncode}"
        print(f"  {WHITE}{i}.{RESET} {fix.label}: {SEV_COLOR[CRIT]}failed{RESET} {GREY}{err}{RESET}")
    print()


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
    live_cpu._cache = None
    processes._cache = None
    processes()
    live_cpu()
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
    n = 0
    for f in findings:
        if f.fix:
            n += 1
            f.fix.id = f"f{n}"
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

    if problems and any(f.fix for f in problems) and "--fix" not in sys.argv:
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
    ap.add_argument("--plain", action="store_true",
                    help="skip Claude and print the rule based report")
    ap.add_argument("--fix", action="store_true",
                    help="rule based report, then offer each fix one at a time")
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

    if args.plain or args.fix or args.all:
        report(findings, show_ok=args.all)
        if args.fix:
            apply_fixes(sorted(findings, key=lambda f: f.sev), assume_yes=args.yes)
    else:
        advise(findings)

    worst = min((f.sev for f in findings), default=OK)
    sys.exit(1 if worst <= HIGH else 0)


if __name__ == "__main__":
    main()
