"""MediaERP Gateway API client — the outbound-only link.

Speaks the Phase 1 contract:
    POST /gateway/lease      → { operations: [...], lease_ttl }
    POST /gateway/ack        { results: [...] }
    POST /gateway/heartbeat  { gateway_id, healthy, live? } → { urgent: [] }

Authenticated with the station's gateway_key via the X-Gateway-Key header.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlsplit

import requests

log = logging.getLogger("gateway.erp")


class ErpError(Exception):
    pass


class ErpClient:
    def __init__(self, base_url: str, gateway_key: str, gateway_id: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.gateway_id = gateway_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Gateway-Key": gateway_key, "Accept": "application/json"}
        )

    def _post(self, path: str, json_body: dict) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self.session.post(url, json=json_body, timeout=self.timeout)
        except requests.RequestException as exc:
            # Wrap transport failures (DNS / refused / TLS / timeout / dropped
            # connection) as ErpError so the worker's graceful "uplink down"
            # handling runs — instead of a raw traceback escaping every tick.
            raise ErpError(f"{path}: {exc}") from exc
        if not resp.ok:
            raise ErpError(f"{path} → HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ErpError(f"{path}: invalid JSON response") from exc

    def lease(self, limit: int) -> list[dict]:
        data = self._post("gateway/lease", {"gateway_id": self.gateway_id, "limit": limit})
        return data.get("operations", [])

    def ack(self, results: list[dict]) -> dict:
        return self._post("gateway/ack", {"results": results})

    def heartbeat(self, healthy: bool, live: dict | None, telemetry: dict | None = None) -> dict:
        body: dict = {"gateway_id": self.gateway_id, "healthy": healthy, "live": live}
        if telemetry:
            body["telemetry"] = telemetry
        return self._post("gateway/heartbeat", body)

    def download_file_to(self, asset_id, dest_path: str) -> tuple[str, str | None]:
        """Stream a creative to `dest_path`, hashing as we write (Phase 3).

        Streaming (vs buffering the whole file in memory) keeps a large creative
        from spiking memory, and hashing in the same pass avoids a second read.
        Returns (sha256_hex, X-Checksum header or None). Raises ErpError on any
        transport/HTTP failure — the caller decides whether to keep the .part.
        """
        url = f"{self.base_url}/gateway/files/{asset_id}"
        h = hashlib.sha256()
        try:
            with self.session.get(url, timeout=max(30.0, self.timeout), stream=True) as resp:
                if not resp.ok:
                    raise ErpError(f"download {asset_id} → HTTP {resp.status_code}")
                x_checksum = resp.headers.get("X-Checksum")
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
                            h.update(chunk)
        except requests.RequestException as exc:
            raise ErpError(f"download {asset_id}: {exc}") from exc
        return h.hexdigest(), x_checksum

    def download_file(self, asset_id) -> tuple[bytes | None, str | None]:
        """Fetch a creative's bytes + its X-Checksum (buffered; kept for callers
        that want the bytes directly). Prefer download_file_to for large files."""
        url = f"{self.base_url}/gateway/files/{asset_id}"
        try:
            resp = self.session.get(url, timeout=max(30.0, self.timeout))
        except requests.RequestException as exc:
            raise ErpError(f"download {asset_id}: {exc}") from exc
        if not resp.ok:
            raise ErpError(f"download {asset_id} → HTTP {resp.status_code}")
        return resp.content, resp.headers.get("X-Checksum")

    def get_schedule(self, date: str) -> dict:
        """The approved day's schedule for this station (Phase 4)."""
        url = f"{self.base_url}/gateway/schedule"
        try:
            resp = self.session.get(url, params={"date": date}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ErpError(f"schedule {date}: {exc}") from exc
        if not resp.ok:
            raise ErpError(f"schedule {date} → HTTP {resp.status_code}")
        return resp.json()

    def get_manifest(self) -> dict:
        """The approved-content manifest + governance policy (Phase 6)."""
        url = f"{self.base_url}/gateway/manifest"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ErpError(f"manifest: {exc}") from exc
        if not resp.ok:
            raise ErpError(f"manifest → HTTP {resp.status_code}")
        return resp.json()

    def report_violations(self, violations: list[dict]) -> dict:
        """Report compliance violations detected on-site (Phase 6)."""
        return self._post("gateway/violations", {"violations": violations})

    def report_interactions(self, interactions: list[dict]) -> dict:
        """Register inbound listener messages as audience reach."""
        return self._post("gateway/interactions", {"interactions": interactions})

    def report_recordings(self, recordings: list[dict]) -> dict:
        """Log finalized as-aired recording segments for the compliance audit."""
        return self._post("gateway/recordings", {"recordings": recordings})

    def report_transcripts(self, transcripts: list[dict]) -> dict:
        """Attach Whisper transcripts (+ flagged language) to recorded segments."""
        return self._post("gateway/transcripts", {"transcripts": transcripts})

    def report_ad_reads(self, reads: list[dict]) -> dict:
        """Report live-read ad verifications (transcript vs required script)."""
        return self._post("gateway/ad-reads", {"reads": reads})

    def report_play_events(self, endpoint: str, events: list[dict]) -> list[str]:
        """Opt-in v1 contract. A 200 without explicit event ids is NOT an ACK."""
        try:
            path = urlsplit(endpoint)
        except ValueError as exc:
            raise ErpError("invalid play-event endpoint") from exc
        if (not endpoint or endpoint.startswith("/") or path.scheme or path.netloc
                or path.query or path.fragment or "\\" in endpoint or "%" in endpoint
                or any(p in ("", ".", "..") for p in endpoint.split("/"))):
            raise ErpError("play-event endpoint must be a relative API path")
        try:
            resp = self.session.post(
                f"{self.base_url}/{endpoint}",
                json={"schema_version": 1, "gateway_id": self.gateway_id, "events": events},
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ErpError("play-event transport failure") from exc
        if not 200 <= resp.status_code < 300:
            raise ErpError(f"play-event HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ErpError("invalid play-event acknowledgement") from exc
        ids = body.get("accepted_event_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise ErpError("missing accepted_event_ids acknowledgement")
        return ids
