"""Typed configuration (pydantic-settings), loaded from env / .env — same
convention as the team's other Python services (py_comm)."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8-sig",  # tolerate a UTF-8 BOM (some editors/installers add one)
        extra="ignore",
    )

    # ── MediaERP ────────────────────────────────────────────────────────
    erp_base_url: str
    gateway_key: str
    gateway_id: str = "gw-1"
    lease_limit: int = 50
    poll_interval: float = 5.0
    heartbeat_interval: float = 5.0
    http_timeout: float = 10.0

    # ── RadioBOSS (localhost) ───────────────────────────────────────────
    radioboss_base_url: str = "http://127.0.0.1:9000"
    radioboss_user: str | None = None
    radioboss_password: str = ""
    radioboss_timeout: float = 5.0
    insert_position: str = "-2"

    # Public broadcast stream URL the operator console can monitor ("Listen" —
    # what listeners hear). Optional; empty disables the button.
    monitor_stream_url: str = ""

    # ── Listener SMS ingest (studio-local) ──────────────────────────────
    # Read inbound listener SMS so the presenter can read them on-air and each
    # sender is registered as audience reach in MediaERP.
    sms_source: str = ""             # "" disabled | "adb" (phone) | "http" (app pushes SMS)
    sms_poll_interval: float = 15.0  # seconds between ADB inbox reads
    sms_adb_path: str = ""           # adb.exe path; blank → bundled/PATH
    sms_adb_device: str = ""         # specific device serial (optional)
    # HTTP receiver: an off-the-shelf Android SMS-forwarding app POSTs inbound
    # SMS here. We LISTEN on this host:port; token (optional) authenticates.
    sms_http_bind: str = "0.0.0.0:8099"
    sms_http_token: str = ""

    # ── Media (Phase 3) + schedule (Phase 4) ────────────────────────────
    # Where synced creatives are placed on the studio disk, and where the
    # published daily playlist (.m3u) is written.
    media_folder: str = "media"
    schedule_folder: str = "data/schedule"
    # Background creative downloads run off the hot path so a slow/large file
    # never stalls playout commands. How many can run at once.
    download_workers: int = 2
    # Pre-fetch every creative the manifest lists onto media_folder, so an ad
    # uploaded in MediaERP is on the studio disk before it's ever scheduled or
    # inserted. Already-present files (matching checksum) are skipped.
    prefetch_manifest: bool = True
    # A command that keeps failing (crashes, or its creative never downloads) is
    # dead-lettered after this many attempts so it can't block the queue forever.
    max_execute_attempts: int = 5

    # ── Must-air presenter cues (Phase B) ───────────────────────────────
    # How long the console keeps a must-air presenter-cue overlay up (from the
    # asset's air time) before it auto-clears. Roughly matches the server's
    # force_grace window so the prompt lingers through the airing.
    cue_display_seconds: int = 300

    # ── Audio monitoring (Tier C, sub-phase C1) — opt-in ────────────────
    # Meter the on-air signal and alarm on dead air. Needs the audio extra:
    # `pip install .[audio]` (numpy + sounddevice). Only computed levels leave
    # the studio, never audio.
    audio_enabled: bool = False
    audio_source: str = "loopback"       # "loopback" (default output, Windows) | "device"
    audio_device: str = ""               # input device name/index ("" = default)
    audio_samplerate: int = 44100
    audio_block_ms: int = 100            # metering window
    silence_dbfs: float = -50.0          # below this RMS level counts as silence
    silence_seconds: float = 10.0        # continuous silence this long → dead air

    # ── Compliance recorder (Tier C, sub-phase C2) — opt-in ─────────────
    # Log the on-air audio to rolling MP3 segments for the as-aired record.
    # Needs ffmpeg on the studio PC.
    record_enabled: bool = False
    record_source: str = "stream"        # "stream" (monitor_stream_url) | "device" (dshow)
    record_device: str = ""              # DirectShow device name when source=device
    record_folder: str = "data/recordings"
    record_bitrate: str = "64k"
    record_segment_seconds: int = 3600   # one file per hour
    record_retention_days: int = 14
    ffmpeg_path: str = ""                # blank → "ffmpeg" on PATH

    # ── As-aired transcription (Whisper) — opt-in, depends on the recorder ─
    # Local speech-to-text over recorded segments → searchable as-aired text +
    # banned-language flagging. Needs the [whisper] extra (faster-whisper).
    transcribe_enabled: bool = False
    whisper_model: str = "small"         # tiny | base | small | medium | large-v3
    whisper_device: str = "cpu"          # cpu | cuda
    whisper_compute_type: str = "int8"   # int8 | int8_float16 | float16 | float32
    whisper_language: str = ""           # blank = auto-detect; else e.g. sw, en
    transcribe_poll_seconds: float = 30.0
    transcribe_banned_words: str = ""    # comma-separated; flags them in the transcript

    # ── Fingerprinting / ACR (Tier C, sub-phase C3) — EXPERIMENTAL, opt-in ─
    # Verify the on-air sound actually matches the creative the automation says
    # is playing. Needs the [audio] extra (numpy) + ffmpeg (to decode refs).
    fingerprint_enabled: bool = False
    fp_sr: int = 11025                   # fingerprinting sample rate
    fp_verify_interval: float = 8.0      # seconds between live checks
    fp_window_seconds: float = 5.0       # live audio window per check
    fp_min_score: int = 5               # min aligned hash matches to call a detection
    fp_db_path: str = "data/fingerprints.db"

    # ── Local AI (Ollama) — optional on-site intelligence ───────────────
    # CAPACITY provisions: endpoint (placement — localhost or a LAN box), model
    # (size), timeout, concurrency, input cap, and a CPU guard that skips
    # inference while the studio PC is busy (protects real-time playout).
    ai_enabled: bool = False
    ai_base_url: str = "http://127.0.0.1:11434"
    ai_model: str = "qwen2.5:3b"
    ai_timeout: float = 20.0
    ai_max_concurrency: int = 1
    ai_max_input_chars: int = 2000
    ai_max_cpu_pct: float = 75.0         # skip AI when host CPU is above this
    # FEATURE toggles — flip independently.
    ai_sms_classify: bool = True         # category / sentiment / language
    ai_sms_moderate: bool = True         # flag profane/unsafe-to-air messages

    # ── Local store / logging ───────────────────────────────────────────
    db_path: str = "data/gateway.db"
    log_level: str = "INFO"


def _find_env_file() -> str | None:
    """Locate the .env robustly, whether run as a PyInstaller .exe or from source.

    Search order:
      1. next to the executable (where the installer writes it) — frozen builds
      2. the current working directory
      3. the project root (running from source)
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):  # PyInstaller .exe
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def load_settings() -> Settings:
    env_file = _find_env_file()
    try:
        return Settings(_env_file=env_file, _env_file_encoding="utf-8-sig")  # type: ignore[call-arg]
    except Exception as exc:  # missing required config → a clear message, not a traceback
        where = env_file or "no .env found next to the .exe or in the current folder"
        raise SystemExit(
            f"\nMediaCore Studio Gateway — configuration error.\n"
            f"  .env: {where}\n"
            f"  Put a filled-in .env next to the executable (the installer does this),\n"
            f"  or set the GATEWAY_* environment variables.\n\n{exc}\n"
        )
