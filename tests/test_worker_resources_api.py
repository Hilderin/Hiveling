"""The server exposes each worker's live machine resources (CPU/RAM)."""

from __future__ import annotations


from server.app.plan import WorkerEndpoint
from server.app.worker_client import WorkerClient
from server.app.workers import probe_workers


class _Registry:
    def __init__(self, endpoints):
        self._endpoints = endpoints

    def get(self):
        return list(self._endpoints)

    def error(self):
        return None


def test_probe_workers_exposes_live_resources(monkeypatch):
    resources = {
        "cpu_count": 8,
        "cpu_count_physical": 4,
        "cpu_speed_mhz": 3200.0,
        "cpu_model": "Test CPU",
        "ram_total_bytes": 16 * 1024**3,
        "ram_available_bytes": 4 * 1024**3,
        "load_average": [0.5, 0.4, 0.3],
    }

    def fake_health(self):
        return {
            "busy": False,
            "capabilities": {"os": "linux"},
            "resources": resources,
        }

    monkeypatch.setattr(WorkerClient, "health", fake_health)

    data = probe_workers(_Registry([WorkerEndpoint(name="w", url="http://w:1")]))

    worker = data["workers"][0]
    assert worker["reachable"] is True
    assert worker["resources"]["cpu_count"] == 8
    assert worker["resources"]["ram_total_bytes"] == 16 * 1024**3
    assert worker["resources"]["ram_available_bytes"] == 4 * 1024**3


def test_unreachable_worker_reports_empty_resources(monkeypatch):
    def failing_health(self):
        from server.app.worker_client import WorkerError

        raise WorkerError("down")

    monkeypatch.setattr(WorkerClient, "health", failing_health)

    data = probe_workers(_Registry([WorkerEndpoint(name="w", url="http://w:1")]))

    worker = data["workers"][0]
    assert worker["reachable"] is False
    assert worker["resources"] == {}
