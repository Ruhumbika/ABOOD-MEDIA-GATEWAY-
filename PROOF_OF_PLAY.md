# Gateway proof-of-play observations

This feature belongs to the studio gateway and its operator console. It does not
change MediaCore booking statuses or require changes to existing lease/ACK APIs.
A successful `inserttrack` only confirms playlist insertion. It never creates a
play event. The Schedule tab remains the server's planned schedule; the new Play
History tab displays RadioBOSS observations and delivery status separately.

## Enable on the studio PC

Install this branch's changed Python files in `C:\ABOOD-GATEWAY` with the service
stopped. No new dependency is needed. Add these lines to the existing `.env`
without replacing the ERP key, URL, gateway ID or RadioBOSS password:

```dotenv
GATEWAY_PROOF_OF_PLAY_ENABLED=true
GATEWAY_PROOF_OF_PLAY_TIMEZONE=Africa/Dar_es_Salaam
GATEWAY_PROOF_OF_PLAY_ENDPOINT=
```

Capture is disabled by default. A blank endpoint enables local capture without
sending events to ERP. Restart the service and close/reopen the operator console.
The worker adds its table to the existing `data/gateway.db`; it does not create
another database or copy audio files. No backup folder is created by this feature.

Administrator PowerShell, from `C:\ABOOD-GATEWAY`:

```powershell
.\.venv\Scripts\python.exe tests\test_proof_of_play.py
.\.venv\Scripts\python.exe tests\test_uplink_status.py
.\.venv\Scripts\python.exe tests\test_gateway_fixes.py
Start-Service mediacore-studio-gateway
```

## Studio acceptance test

1. Open **Play History**. It should say **Local capture only**.
2. Start a short test track in RadioBOSS. Allow the next gateway poll to capture
   its history entry. If RadioBOSS exposes entries only after a transition, let
   the track finish. Check title and source start time; delivery stays Pending.
3. Replay that same file at a different second: it should create another event.
4. Restart the gateway service: existing history must not create duplicate rows.
5. Confirm playback, command processing and Uplink still behave normally.

First activation imports whatever history RadioBOSS currently exposes, including
older entries. A newly captured row does not mean it played after activation.
If nothing appears, inspect `data/gateway.log`: failed history requests and rows
without valid STARTTIME/filename are reported. Do not infer playback from a
playlist, schedule, successful command, or missing history.

## Evidence and identity

The source is RadioBOSS `getlastplayed` XML, specifically TRACK's STARTTIME and
FILENAME. RadioBOSS API reference:
https://manual.djsoft.net/radioboss/en/remote_controlapi.htm

An event means **playout start observed**, not a completed advert, an audio
fingerprint match, or successful transmitter output. `source_duration` is the
file duration reported by RadioBOSS, not measured airtime. No booking ID or asset
ID is guessed from a filename/title. Backend correlation and any Scheduled →
Aired business rule remain backend responsibilities.

The event ID hashes the destination/station/source scope, normalized Windows
filename and original STARTTIME. Repeated polls, restarts and retries keep that
ID; a different start time creates a new event. Source timestamps have second
precision, so identical files with identical source timestamps cannot be
separated reliably. RadioBOSS retains a finite history: events lost from that
history during long gateway outages cannot be reconstructed. Set the studio PC
clock correctly. Clock resets and daylight-saving repeated local times are
source limitations.

`started_at_local` is preserved exactly. The configured timezone is a label;
no guessed UTC conversion is performed. `observed_at` is UTC and records when
the gateway first persisted the observation. Set the correct source timezone
before enabling capture; changing it does not rewrite existing audit events.

The outbox uses the existing SQLite database, separate from command work/ACKs.
Sent rows are retained as deduplication evidence. There is no automatic deletion
of pending events or sent identities. Disk usage grows with text history, not
media copies. Changing the ERP base URL, gateway ID or RadioBOSS URL creates a
separate scope: old events are retained but not rerouted. Key rotation keeps the
scope and pending delivery. Do not delete the database to clear Pending counts.

## Optional backend contract — not an existing endpoint claim

Do **not** set the endpoint on the real ERP until its backend team confirms this
contract. An example relative path is `gateway/play-events`, resolved under the
configured ERP API base URL. Absolute URLs, redirects and traversal are rejected.
Authentication uses the existing X-Gateway-Key header. Request:

```json
{
  "schema_version": 1,
  "gateway_id": "studio-01",
  "events": [{
    "event_id": "stable-sha256-id",
    "gateway_id": "studio-01",
    "source": "radioboss.getlastplayed",
    "evidence": "playout_start_observed",
    "started_at_local": "2026-10-30 10:00:00",
    "source_timezone": "Africa/Dar_es_Salaam",
    "observed_at": "2026-10-30T07:00:05+00:00",
    "filename": "C:\\ABOOD-GATEWAY\\media\\spot.mp3",
    "title": "Spot",
    "artist": "ABOOD",
    "casttitle": "Spot",
    "source_duration": "00:30"
  }]
}
```

After durable storage the backend responds with HTTP 2xx and explicit IDs:

```json
{"accepted_event_ids": ["stable-sha256-id"]}
```

The backend must authorize the station and deduplicate event IDs, acknowledging
already-stored IDs too. A response lost after remote storage causes a retry with
the same ID. This is at-least-once delivery, not exactly-once network delivery.
Partial ACKs mark only matching events from that request as Sent. Generic 200,
invalid JSON, redirects, 401/403/404, timeouts and server errors leave events
Pending. Up to 50 due events are sent per worker tick; failed events back off
5, 10, 20, 40, 80, 160, then 300 seconds. Sent in the console means acknowledged
by the endpoint, never a claim that the backend marked a booking Aired.

## Validation in this branch

- 24 proof-of-play tests pass and cover XML parsing, replay identity, durable restart dedup,
  outage/recovery/backoff, partial ACKs, lost responses, scope changes, key
  rotation, bounded delivery, read-only console data and unchanged command ACKs.
- A real local HTTP fixture checks authenticated request/response delivery.
- All 14 uplink regression tests pass.
- Offscreen PyQt6 console smoke check renders persisted Pending → Sent events.
- Existing gateway suite has one pre-existing Linux failure for Windows basename
  handling (`test_manifest_keys_by_basename_and_classifier_matches`). The same
  suite previously passed on Windows. Windows studio acceptance is still required.
