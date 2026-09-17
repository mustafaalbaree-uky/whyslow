# whyslow

One command that answers "why is this Mac slow right now".

```
whyslow           # the report
whyslow --fix     # the report, then offer each reclaim one at a time
whyslow --watch   # live, refreshing
whyslow --all     # include the checks that passed
whyslow --json    # machine readable
```

Read only unless you pass `--fix`, and even then every action asks first and
shows you the exact command before it runs.

## What it checks

**Disk.** Free space on the data volume, installed simulator runtimes, the Trash,
Time Machine local snapshots, and the large folders in your home directory.

**Memory.** The kernel's memory pressure level, then swap and the compressor,
and the biggest resident process. Swap and compressor only count while pressure
is elevated, and every memory threshold is a share of installed RAM.

**CPU.** Load average against core count, and anything above 15 percent.

**Processes.** Dev servers left running from sessions that ended days ago,
scripts running more than one copy of themselves, and how many Claude Code
sessions are open at once.

**System.** Spotlight reindexing, photo and media analysis, Time Machine, iCloud
sync, thermal throttling, a stuck screenshot tool, running VMs, and uptime.

## How this differs from reclaim

`~/Code/reclaim` is the cleanup tool: it finds regenerable build artifact and
deletes it, with a careful safety model around what is really source. `whyslow`
is the diagnosis, and it covers memory, CPU, and processes as well as disk.

Where they touch, whyslow defers. It reports the artifact total by asking
`reclaim --json`, and its fix is to hand you to `reclaim --clean` rather than
delete anything itself.

The one disk thing whyslow owns is simulator runtimes. reclaim walks the per
device directories under `~/Library/Developer/CoreSimulator/Devices`, whereas
the runtime images sit in `/Library/Developer/CoreSimulator/Volumes` and are far
bigger. On 31 Aug 2026 the devices were negligible and the runtimes were 30 GB.

## Why these checks and not others

Written on an 8 GB M2 with a 228 GB disk that ran close to full. Those two
facts produce almost every slowdown here, and they produce it together: when
the disk fills, macOS cannot grow its swap file, so memory pressure that would
normally be invisible turns into a stall. A report that leads with disk space
is leading with the cause.

The thresholds are calibrated against a real bad state measured on 31 Aug 2026:
300 MB free on the data volume, swap 97 percent used, the compressor holding
22 GB of pages inside 3 GB of RAM, load average 10 on 8 cores, 43 days of
uptime, and dev servers still up from 15 days earlier.

## Notes

Simulator runtimes were 30 GB of that disk, and deleting them took the volume
from 300 MB free to 22 GB. Builds here go to the physical iPhone, so the fix
offers to delete them all. Xcode redownloads one on demand.

The Trash has no automatic fix on purpose. It opens the Trash instead, because
the Trash is where things go when you were not sure.

Nothing here runs on a schedule. It runs when you type it.

On 17 Sep 2026, on the 36 GB M3 Pro, the old fixed thresholds flagged 3.2 GB of
swap and 4.6 GB of compressor as HIGH while 18 GB of RAM sat unused and pressure
was normal. Swap lingers for days after the demand that caused it, so the memory
checks now gate on `kern.memorystatus_vm_pressure_level` and scale with
`hw.memsize`. Dev servers, Claude sessions and VMs are rated by their share of
RAM rather than by count.
