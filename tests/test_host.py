import time

import loggerai
from loggerai import host


PROC = {
    "/proc/meminfo": "MemTotal:        8000000 kB\nMemFree:          500000 kB\nMemAvailable:    2000000 kB\nSwapTotal:       1000000 kB\nSwapFree:         750000 kB\n",
    "/proc/self/status": "Name:\tpython\nVmRSS:\t  204800 kB\n",
}


def fake_fs(monkeypatch, files):
    monkeypatch.setattr(host, "_read", lambda p: files.get(p))
    monkeypatch.setattr(host, "psutil", None)


def test_linux_proc(monkeypatch):
    files = dict(PROC, **{"/proc/stat": "cpu  100 0 100 800 0 0 0 0 0 0\n"})
    fake_fs(monkeypatch, files)
    s = host.HostSampler("/")
    files["/proc/stat"] = "cpu  250 0 150 900 0 0 0 0 0 0\n"  # +200 busy, +100 idle
    r = s.sample()
    assert r["cpu_pct"] == 66.7
    assert r["mem_used_pct"] == 75.0 and r["mem_total_mb"] == 7812
    assert r["swap_used_pct"] == 25.0
    assert r["process_mem_mb"] == 200.0
    assert 0 <= r["disk_used_pct"] <= 100


def test_container_limits(monkeypatch):
    """In Docker with limits, the container's CPU quota and memory limit count, not the machine's."""
    files = dict(PROC, **{
        "/sys/fs/cgroup/cpu.max": "200000 100000\n",  # 2 CPUs
        "/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n",
        "/sys/fs/cgroup/memory.max": str(1024 * 1048576),
        "/sys/fs/cgroup/memory.current": str(600 * 1048576),
        "/sys/fs/cgroup/memory.stat": f"anon 1\ninactive_file {88 * 1048576}\n",
    })
    fake_fs(monkeypatch, files)
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(clock))
    s = host.HostSampler("/")
    files["/sys/fs/cgroup/cpu.stat"] = "usage_usec 11000000\n"  # 10 s of CPU in 10 s, of 2 CPUs
    r = s.sample()
    assert r["cpus"] == 2 and r["cpu_pct"] == 50.0
    assert r["mem_used_pct"] == 50.0 and r["mem_total_mb"] == 1024


def test_nothing_readable(monkeypatch):
    fake_fs(monkeypatch, {})
    r = host.HostSampler("/does/not/exist").sample()
    assert "ts" in r and "cpu_pct" not in r and "mem_used_pct" not in r


def test_client_sends_hosts(ingest):
    c = loggerai.init("sk_live_test", endpoint=ingest.url + "/ingest", flush_interval=1, host_interval=10)
    try:
        c._next_host = 0.0
        deadline = time.time() + 5
        while time.time() < deadline and not ingest.all("hosts"):
            c._next_host = 0.0  # don't wait the 10 s minimum in a test
            time.sleep(0.2)
        assert ingest.all("hosts"), "no hosts record sent"
        assert "disk_used_pct" in ingest.all("hosts")[0]
    finally:
        c.close()


def test_host_metrics_off(ingest, monkeypatch):
    monkeypatch.setenv("LOGGERAI_HOST_METRICS", "false")
    c = loggerai.init("sk_live_test", endpoint=ingest.url + "/ingest", flush_interval=1)
    try:
        assert c.host_metrics is False
        c.flush()
        assert not ingest.all("hosts")
    finally:
        c.close()
