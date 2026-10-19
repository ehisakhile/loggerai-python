"""
The server the app runs on: CPU, memory and disk, sampled from the sender
thread every 30 seconds. Inside a container (Docker, Kubernetes) the
container's own limits count, not the whole machine's. Uses psutil when it's
installed, otherwise reads Linux's /proc and /sys/fs/cgroup; on other systems
it sends what it can (load and disk). Never raises.
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Dict, Optional, Tuple

try:  # optional: more accurate, and works on macOS and Windows
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

CGROUP = "/sys/fs/cgroup"


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _meminfo() -> Dict[str, float]:
    out: Dict[str, float] = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            out[k] = float(parts[0]) / 1024  # kB → MB
    return out


def _proc_cpu() -> Optional[Tuple[float, float]]:
    """(busy, total) jiffies for the whole machine."""
    line = (_read("/proc/stat") or "").split("\n", 1)[0].split()
    if len(line) < 8 or line[0] != "cpu":
        return None
    v = [float(x) for x in line[1:9]]  # user nice system idle iowait irq softirq steal
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


def _cgroup_cpu_limit() -> Optional[float]:
    """CPUs the container may use (cgroup v2), or None when it has no limit."""
    raw = (_read(f"{CGROUP}/cpu.max") or "").split()
    if len(raw) == 2 and raw[0] != "max":
        try:
            return float(raw[0]) / float(raw[1])
        except (ValueError, ZeroDivisionError):
            return None
    return None


def _cgroup_cpu_usec() -> Optional[float]:
    for line in (_read(f"{CGROUP}/cpu.stat") or "").splitlines():
        if line.startswith("usage_usec "):
            return float(line.split()[1])
    return None


def _cgroup_memory() -> Optional[Tuple[float, float]]:
    """(used, limit) MB for a container with a memory limit (cgroup v2)."""
    limit, current = (_read(f"{CGROUP}/memory.max") or "").strip(), (_read(f"{CGROUP}/memory.current") or "").strip()
    if not limit or limit == "max" or not current.isdigit():
        return None
    used = float(current)
    for line in (_read(f"{CGROUP}/memory.stat") or "").splitlines():
        if line.startswith("inactive_file "):  # page cache the kernel can drop: not really in use
            used -= float(line.split()[1])
            break
    return max(used, 0.0) / 1048576, float(limit) / 1048576


class HostSampler:
    def __init__(self, root: str = "/"):
        self.root = root if os.path.exists(root) else "/"
        self._prev: Optional[Tuple[float, float, float]] = None  # (wall, busy/usage, total)
        self.sample()  # the first reading only primes the CPU counters

    def _cpu(self) -> Tuple[Optional[float], Optional[float]]:
        """(cpu_pct since the last sample, cpus)."""
        limit = _cgroup_cpu_limit()
        now = time.monotonic()
        if limit:
            usec = _cgroup_cpu_usec()
            if usec is None:
                return None, limit
            prev, self._prev = self._prev, (now, usec, 0.0)
            if not prev or now <= prev[0]:
                return None, limit
            return min(100.0, (usec - prev[1]) / 1e6 / (now - prev[0]) / limit * 100), limit
        cpus = float(os.cpu_count() or 1)
        if psutil is not None:
            return psutil.cpu_percent(interval=None), cpus
        stat = _proc_cpu()
        if stat is None:
            return None, cpus
        prev, self._prev = self._prev, (now, stat[0], stat[1])
        if not prev or stat[1] <= prev[2]:
            return None, cpus
        return (stat[0] - prev[1]) / (stat[1] - prev[2]) * 100, cpus

    def sample(self) -> dict:
        r: dict = {"ts": round(time.time(), 3)}
        try:
            cpu, cpus = self._cpu()
            if cpu is not None:
                r["cpu_pct"] = round(max(cpu, 0.0), 1)
            if cpus:
                r["cpus"] = round(cpus, 2)
        except Exception:
            pass
        try:
            r["load1"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        try:
            cg = _cgroup_memory()
            if cg:
                r["mem_used_pct"], r["mem_total_mb"] = round(cg[0] / cg[1] * 100, 1), round(cg[1])
            elif psutil is not None:
                vm, sw = psutil.virtual_memory(), psutil.swap_memory()
                r["mem_used_pct"], r["mem_total_mb"], r["swap_used_pct"] = round(vm.percent, 1), round(vm.total / 1048576), round(sw.percent, 1)
            else:
                mi = _meminfo()
                if mi.get("MemTotal") and "MemAvailable" in mi:
                    r["mem_used_pct"] = round((1 - mi["MemAvailable"] / mi["MemTotal"]) * 100, 1)
                    r["mem_total_mb"] = round(mi["MemTotal"])
                if mi.get("SwapTotal"):
                    r["swap_used_pct"] = round((1 - mi.get("SwapFree", 0) / mi["SwapTotal"]) * 100, 1)
        except Exception:
            pass
        try:
            du = shutil.disk_usage(self.root)
            r["disk_used_pct"] = round(du.used / du.total * 100, 1)
        except (OSError, ZeroDivisionError):
            pass
        try:
            if psutil is not None:
                r["process_mem_mb"] = round(psutil.Process().memory_info().rss / 1048576, 1)
            else:
                for line in (_read("/proc/self/status") or "").splitlines():
                    if line.startswith("VmRSS:"):
                        r["process_mem_mb"] = round(float(line.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass
        return r
