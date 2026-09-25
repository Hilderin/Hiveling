"""HTTP client for a worker."""

from __future__ import annotations

import httpx

from .plan import WorkerEndpoint


class WorkerError(Exception):
    """Communication error with a worker."""


class WorkerBusy(Exception):
    """The worker rejected the job because it is busy."""


class WorkerClient:
    def __init__(self, endpoint: WorkerEndpoint, timeout: float = 30.0):
        self.endpoint = endpoint
        self.base_url = endpoint.url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def __repr__(self) -> str:
        return f"<WorkerClient {self.endpoint.name} {self.base_url}>"

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise WorkerError(f"{self.endpoint.name}: {exc}") from exc
        if response.status_code == 409:
            raise WorkerBusy(response.text)
        if response.status_code >= 400:
            raise WorkerError(f"{self.endpoint.name}: HTTP {response.status_code} - {response.text}")
        return response

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    def create_job(self, spec: dict) -> dict:
        return self._request("POST", "/jobs", json=spec).json()

    def upload_files(self, job_id: str, data: bytes) -> dict:
        return self._request(
            "PUT",
            f"/jobs/{job_id}/files",
            content=data,
            headers={"Content-Type": "application/zip"},
        ).json()

    def start_job(self, job_id: str) -> dict:
        return self._request("POST", f"/jobs/{job_id}/start").json()

    def get_job(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}").json()

    def download_files(self, job_id: str, which: str = "modified") -> bytes:
        response = self._request("GET", f"/jobs/{job_id}/files", params={"which": which})
        return response.content

    def logs(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}/logs").json()

    def cancel(self, job_id: str) -> dict:
        return self._request("DELETE", f"/jobs/{job_id}").json()
