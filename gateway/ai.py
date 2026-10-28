"""Local AI (Ollama) — optional on-site intelligence for the gateway.

A guarded client for a local LLM served by Ollama (default localhost:11434, or a
dedicated box on the studio LAN). Keeps the gateway's local-first / offline
ethos: listener text and audio never leave the building, and inference keeps
working when the uplink is down.

Provisions the operator controls (all in config / .env):
  • CAPACITY — endpoint (placement), model (size), per-request timeout, max
    concurrency, input cap, and a host-load guard that SKIPS inference when the
    studio PC is busy so RadioBOSS never glitches.
  • FEATURES — independent toggles per capability (SMS classify / moderate …).

Everything degrades to a clean no-op when disabled, when Ollama is unreachable,
or when the host is under load — AI can never stall playout, a command, or a
heartbeat.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("gateway.ai")

try:  # host-load guard (shared with telemetry)
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore


class AiClient:
    def __init__(self, settings) -> None:
        self.s = settings
        self._sem = threading.Semaphore(max(1, int(getattr(settings, "ai_max_concurrency", 1))))
        self._healthy: bool | None = None
        self._last_health = 0.0

    # ── provisioning gates ──────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(getattr(self.s, "ai_enabled", False))

    def _host_ok(self) -> bool:
        """Capacity guard — skip AI while the studio PC is loaded (protect
        real-time playout). Passes when psutil is unavailable."""
        if psutil is None:
            return True
        try:
            return psutil.cpu_percent(interval=None) < float(self.s.ai_max_cpu_pct)
        except Exception:  # noqa: BLE001
            return True

    def health(self) -> bool | None:
        """Is the local model runtime reachable? Cached ~30 s."""
        if not self.enabled():
            return None
        now = time.monotonic()
        if self._healthy is not None and now - self._last_health < 30.0:
            return self._healthy
        self._last_health = now
        try:
            r = requests.get(f"{self.s.ai_base_url}/api/tags", timeout=min(5.0, self.s.ai_timeout))
            self._healthy = r.ok
        except Exception:  # noqa: BLE001
            self._healthy = False
        return self._healthy

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "endpoint": self.s.ai_base_url,
            "model": self.s.ai_model,
            "healthy": self.health(),
            "features": {
                "sms_classify": bool(self.s.ai_sms_classify),
                "sms_moderate": bool(self.s.ai_sms_moderate),
            },
        }

    # ── inference ───────────────────────────────────────────────────────
    def _generate(self, system: str, prompt: str, json_mode: bool = True):
        if not self.enabled() or not self._host_ok():
            return None
        if not self._sem.acquire(blocking=False):  # at capacity — skip, don't queue
            return None
        try:
            body: dict = {
                "model": self.s.ai_model,
                "system": system,
                "prompt": prompt[: int(self.s.ai_max_input_chars)],
                "stream": False,
                "options": {"temperature": 0},
            }
            if json_mode:
                body["format"] = "json"
            r = requests.post(f"{self.s.ai_base_url}/api/generate", json=body, timeout=self.s.ai_timeout)
            if not r.ok:
                log.debug("ai generate HTTP %s", r.status_code)
                return None
            resp = (r.json() or {}).get("response", "")
            return json.loads(resp) if json_mode else resp
        except Exception as exc:  # noqa: BLE001
            log.debug("ai generate failed: %s", exc)
            return None
        finally:
            self._sem.release()

    # ── feature: inbound listener SMS triage ────────────────────────────
    def enrich_message(self, text: str | None) -> dict | None:
        """Classify + moderate one inbound listener message. Returns only the
        fields whose feature toggle is on, or None if nothing to do / no result."""
        classify = bool(self.s.ai_sms_classify)
        moderate = bool(self.s.ai_sms_moderate)
        if not text or not (classify or moderate):
            return None

        system = "You triage inbound radio-station listener SMS. Reply with COMPACT JSON only, no prose."
        prompt = (
            "Analyse the listener message and return JSON with keys: "
            "category (one of REQUEST, SHOUTOUT, COMPLAINT, COMPETITION, QUESTION, AD_INQUIRY, SPAM, OTHER), "
            "sentiment (POSITIVE, NEUTRAL, NEGATIVE), "
            "language (ISO 639-1 code, e.g. sw, en), "
            "flagged (true only if profane/abusive/unsafe to read on air), "
            "reason (short; only when flagged).\n"
            f'Message: """{text}"""'
        )
        out = self._generate(system, prompt, json_mode=True)
        if not isinstance(out, dict):
            return None

        result: dict = {}
        if classify:
            result["category"] = (str(out.get("category") or "").upper()[:24] or None)
            result["sentiment"] = (str(out.get("sentiment") or "").upper()[:12] or None)
            result["language"] = (str(out.get("language") or "").lower()[:8] or None)
        if moderate:
            result["flagged"] = bool(out.get("flagged"))
            result["ai_reason"] = (str(out.get("reason") or "")[:200] or None)
        return {k: v for k, v in result.items() if v is not None} or None

    # ── feature: live-read ad verification (transcript vs required script) ─
    def verify_ad_read(self, script: str, transcript: str) -> dict | None:
        """Judge whether a required ad script was actually read, given a transcript
        snippet of what aired. Returns {read, coverage, verdict, missing} or None."""
        if not script or not transcript:
            return None
        system = ("You verify radio advertising compliance: was a REQUIRED ad script actually read on air, "
                  "given a transcript of what aired? Reply with COMPACT JSON only.")
        prompt = (
            "Compare the required script to the aired transcript and return JSON with keys: "
            "read (bool — was the script substantially read), "
            "coverage (integer 0-100 — percent of the required script actually spoken), "
            "verdict (READ if faithfully read, PARTIAL if incomplete/altered, MISSED if not read at all), "
            "missing (array of required phrases that were NOT spoken).\n"
            f'REQUIRED SCRIPT: """{script}"""\n'
            f'AIRED TRANSCRIPT: """{transcript}"""'
        )
        out = self._generate(system, prompt, json_mode=True)
        if not isinstance(out, dict):
            return None
        verdict = str(out.get("verdict") or "").upper()
        if verdict not in ("READ", "PARTIAL", "MISSED"):
            verdict = "READ" if out.get("read") else "MISSED"
        cov = out.get("coverage")
        return {
            "read": bool(out.get("read")),
            "coverage": max(0, min(100, int(cov))) if isinstance(cov, (int, float)) else None,
            "verdict": verdict,
            "missing": [str(x)[:80] for x in (out.get("missing") or [])][:10],
        }
