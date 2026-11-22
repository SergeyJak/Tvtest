"""Read-only VPS and IPTV process health for authenticated admin overview.

Uses Linux /proc, os.statvfs and a bounded systemctl query. No extra
dependencies, background threads, external requests, shell or credentials.
All CPU and network percentages are deltas between admin refreshes.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path


def _cpu_ticks() -> tuple[int, int]:
    values = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
    if not values or values[0] != "cpu":
        raise ValueError("No Linux CPU counters")
    ticks = [int(v) for v in values[1:]]
    return sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)


def _memory() -> dict:
    rows = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            rows[key] = int(rest.strip().split()[0]) * 1024
    total = rows["MemTotal"]
    available = rows["MemAvailable"]
    used = max(0, total - available)
    return {"used_bytes": used, "total_bytes": total,
            "percent": round(100.0 * used / total, 1) if total else None}


def _disk() -> dict:
    disk = os.statvfs("/")
    total = disk.f_blocks * disk.f_frsize
    # f_bavail is space accessible to non-root users.
    available = disk.f_bavail * disk.f_frsize
    used = max(0, total - available)
    return {"used_bytes": used, "total_bytes": total,
            "percent": round(100.0 * used / total, 1) if total else None}


def _network_bytes() -> tuple[int, int]:
    rx = tx = 0
    for row in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]:
        if ":" not in row:
            continue
        interface, data = row.rsplit(":", 1)
        if interface.strip() == "lo":
            continue  # Local TV gateway/admin traffic is not external bandwidth.
        columns = data.split()
        rx += int(columns[0])
        tx += int(columns[8])
    return rx, tx


def _process_counters(pid: int) -> tuple[int, int, int]:
    """Return (start tick, accumulated CPU ticks, RSS bytes); no command line."""
    if pid < 1:
        raise ValueError("Invalid process")
    source = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    values = source.rsplit(")", 1)[1].strip().split()
    # After the command name, index 0 is Linux field 3 (process state).
    start_tick = int(values[19])  # field 22
    cpu_ticks = int(values[11]) + int(values[12])  # fields 14, 15
    rss = int(values[21]) * os.sysconf("SC_PAGE_SIZE")  # field 24
    return start_tick, cpu_ticks, rss


def _gateway_pid() -> int | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", "--value", "--property=MainPID",
             "iptv-gateway.service"],
            capture_output=True, text=True, timeout=0.8, check=False,
        )
        if result.returncode != 0:
            return None
        pid = int(result.stdout.strip())
        return pid if pid > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _percent_delta(current, previous) -> float | None:
    if not previous or current[0] <= previous[0]:
        return None
    total = current[0] - previous[0]
    idle = max(0, current[1] - previous[1])
    return round(max(0, min(100, 100 * (total - idle) / total)), 1)


class ResourceSampler:
    def __init__(self, *, interval: float = 5.0):
        self.interval = interval
        self.lock = threading.Lock()
        self.previous_cpu = None
        self.previous_network = None
        self.previous_processes = {}
        self.last_sample_at = 0.0
        self.last_result = None

    def snapshot(self) -> dict:
        """At most one /proc sample per interval across concurrent requests."""
        with self.lock:
            now = time.monotonic()
            if self.last_result is not None and now - self.last_sample_at < self.interval:
                return self.last_result
            output = {
                "sampled_at": int(time.time()),
                "vps": {
                    "cpu_percent": None, "cores": os.cpu_count(),
                    "load_1m": None, "memory": None, "disk": None,
                    "rx_bytes_per_second": None, "tx_bytes_per_second": None,
                },
                "service": {
                    "gateway": {"cpu_percent": None, "rss_bytes": None},
                    "admin": {"cpu_percent": None, "rss_bytes": None},
                },
            }
            vps = output["vps"]
            try:
                clock = _cpu_ticks()
                vps["cpu_percent"] = _percent_delta(clock, self.previous_cpu)
                self.previous_cpu = clock
            except (OSError, ValueError, IndexError, ZeroDivisionError):
                self.previous_cpu = None
            for name, reader in (("memory", _memory), ("disk", _disk)):
                try:
                    vps[name] = reader()
                except (OSError, ValueError, KeyError, ZeroDivisionError):
                    pass
            try:
                vps["load_1m"] = round(os.getloadavg()[0], 2)
            except OSError:
                pass
            try:
                received, sent = _network_bytes()
                if self.previous_network:
                    old_time, old_rx, old_tx = self.previous_network
                    elapsed = now - old_time
                    if elapsed > 0 and received >= old_rx and sent >= old_tx:
                        vps["rx_bytes_per_second"] = round((received - old_rx) / elapsed)
                        vps["tx_bytes_per_second"] = round((sent - old_tx) / elapsed)
                self.previous_network = (now, received, sent)
            except (OSError, ValueError, IndexError):
                self.previous_network = None
            for name, pid in (("gateway", _gateway_pid()), ("admin", os.getpid())):
                try:
                    start_tick, ticks, rss = _process_counters(pid)
                    output["service"][name]["rss_bytes"] = rss
                    previous = self.previous_processes.get(name)
                    if previous and (pid, start_tick) == previous[:2]:
                        elapsed = now - previous[2]
                        delta = ticks - previous[3]
                        if elapsed > 0 and delta >= 0:
                            ticks_per_sec = os.sysconf("SC_CLK_TCK")
                            output["service"][name]["cpu_percent"] = round(
                                100.0 * delta / ticks_per_sec / elapsed, 1)
                    self.previous_processes[name] = (pid, start_tick, now, ticks)
                except (OSError, ValueError, TypeError, IndexError):
                    self.previous_processes.pop(name, None)
            self.last_sample_at = now
            self.last_result = output
            return output
