# MediaCore Studio Gateway

A studio-resident playout agent (Python, native Windows). It **pulls** playout
commands from MediaERP, applies them to **RadioBOSS over localhost**, confirms
each in the playlist, and reports the outcome back — so RadioBOSS never needs a
public IP and playout survives an internet outage.

This is **Phase 2 — the delivery core**. File transfer (Phase 3), schedule
publication (Phase 4) and governance/compliance (Phase 6) build on top.

## How it works

```
MediaERP  ──lease──►  Gateway ──localhost──►  RadioBOSS
   ▲                    │  (local SQLite queue — survives uplink loss)
   └───ack / heartbeat──┘
```

Each tick: **lease → execute → confirm → ack → heartbeat**. Leased work and
un-posted acks are persisted locally (`data/gateway.db`), so a dropped link
never stops the studio — it keeps airing and syncs when the link returns.

The retry/backoff/DEAD brain lives in MediaERP and reacts to the acks. For
`inserttrack`, this daemon does the **confirm-in-playlist** check locally
(with a pre-check so a re-leased command that already landed is never inserted
twice) and reports `confirmed` — the server only marks a spot `SCHEDULED` when
`confirmed` is true.

## Setup

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env       # then edit .env (see below)
```

Set in `.env`:
- `GATEWAY_ERP_BASE_URL` — MediaERP API base, e.g. `https://media-api.abooderp.com/api/v1`
- `GATEWAY_GATEWAY_KEY` — the station's `gateway_key` (set on the station in MediaERP; the station must be in `gateway` delivery mode)
- `GATEWAY_RADIOBOSS_BASE_URL` / `GATEWAY_RADIOBOSS_PASSWORD` — the localhost RadioBOSS Remote Control API

## Run

```powershell
.venv\Scripts\python -m gateway      # foreground (Ctrl+C to stop)
```

## Install as a Windows service (WinSW)

1. Download WinSW, rename its exe to `mediacore-studio-gateway.exe`, place it in `winsw\` beside `gateway-service.xml`.
2. Rename `gateway-service.xml` to `mediacore-studio-gateway.xml`.
3. From `winsw\`: `mediacore-studio-gateway.exe install` then `... start`.

Run the service under a **dedicated, restricted account** and remove the
operator's ability to stop/edit it — this daemon becomes the compliance
enforcement point in Phase 6.

## The MediaERP contract (Phase 1)

```
POST /gateway/lease      X-Gateway-Key → { operations:[{command_id,command_type,payload,priority,...}], lease_ttl }
POST /gateway/ack        { results:[{command_id, ok, confirmed, duration_ms, response}] }
POST /gateway/heartbeat  { gateway_id, healthy, live? } → { urgent:[] }
```

## Layout

```
gateway/
  config.py      pydantic-settings config (GATEWAY_* env)
  erp_client.py  MediaERP gateway API (lease/ack/heartbeat)
  radioboss.py   localhost RadioBOSS client + XML parsing + confirm
  executor.py    op → RadioBOSS, confirmed-insert
  queue.py       local SQLite offline queue (work + acks)
  worker.py      the tick loop
  __main__.py    entrypoint + signal handling
winsw/           Windows service wrapper
```
