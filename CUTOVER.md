# Studio Gateway — Production Cutover Runbook (Phase 5)

Moving a station from **direct** delivery (MediaERP calls RadioBOSS over the
network) to **gateway** delivery (a studio-resident agent leases the work and
applies it over localhost), then locking RadioBOSS to localhost.

Do this **one station at a time**. Every step has a **✔ verify** and a
**↩ rollback**. Nothing is irreversible until Part E (the firewall), and even
that reverses by re-opening the port.

---

## 0. Concepts (30-second recap)

- **direct** — today's behaviour: MediaERP → (network) → RadioBOSS. Requires
  RadioBOSS reachable from the server; RadioBOSS is exposed on the network.
- **gateway** — MediaERP only **enqueues** commands (`playout_operations`). The
  gateway **pulls** them (`/gateway/lease`), applies them to RadioBOSS on
  `127.0.0.1`, **confirms** in the playlist, and **acks** back. Only outbound
  HTTPS leaves the studio; RadioBOSS never needs to be reachable from outside.

The retry/confirm/DEAD/as-run brain stays server-side — the gateway is "hands".

---

## 1. Pre-flight checklist

**Studio PC**
- [ ] Windows, RadioBOSS installed and running.
- [ ] RadioBOSS **Remote Control API enabled** (Settings → Remote Control API), note its **host/port/password**.
- [ ] Python 3.11+ installed.
- [ ] Outbound HTTPS to the MediaERP host allowed.
- [ ] The creative media folder exists (where the gateway will place synced files).

**MediaERP (server)**
- [ ] The station exists and currently works in **direct** mode.
- [ ] You can set the station's `delivery_mode` + `gateway_key` (admin UI or DB).
- [ ] Governance policy decided (auto-cut, USB block, allowed folders, app allowlist, switch command).

**Have ready**
- [ ] A strong random **gateway key** (e.g. `openssl rand -hex 24`).
- [ ] A harmless **test creative** file on the studio disk for the smoke test.

---

## 2. Part A — Install & configure the gateway (station stays DIRECT)

Nothing changes for listeners in this part; you're just installing the agent.
The studio PC does **not** need Python — the gateway ships as a single `.exe`
and the installer collects the config in a wizard (see `packaging/README.md`).

1. On the **server**, put the key on the station (leave mode `direct` for now):
   ```php
   // php artisan tinker  (or the stations admin UI)
   \Industry\MediaCore\Domain\Entities\Station::whereKey(<ID>)
       ->update(['gateway_key' => '<the strong key>']);   // delivery_mode stays 'direct'
   ```
2. On the **studio PC**, run the installer (as Administrator) and fill in the
   fields — MediaERP URL, the gateway key, gateway id, RadioBOSS URL + password,
   media/schedule folders:
   - **GUI:** run `MediaCoreStudioGateway-Setup.exe` (Inno Setup build), **or**
   - **PowerShell:** `powershell -ExecutionPolicy Bypass -File packaging\install.ps1`

   The installer writes a locked-down `.env` from your answers, installs the
   **Windows service**, and starts it. (No hand-editing of `.env`.)

**✔ Verify** — the service is running:
```powershell
Get-Service mediacore-studio-gateway     # -> Running
Get-Content "C:\Program Files\MediaCore Studio Gateway\gateway-service.out.log" -Tail 20
```
The log should show `manifest synced: N approved asset(s)` and no auth errors
(401/403). On the server, the station's **`gateway_seen_at`** updates and the
Multi-Station Control board shows it (source `gateway`). Because the station is
still **direct**, `lease` returns 0 ops — that's correct.

> To test the raw loop before packaging (dev machine only), you can still run
> from source: `py -m pip install -e .` then `py -m gateway`.

**↩ Rollback** — stop the service (`.\gateway-service.exe stop`). Zero effect on
playout (station is direct).

---

## 3. Part B — Cut over to gateway mode

This is the flip. From here, MediaERP enqueues instead of calling RadioBOSS.

1. Keep the gateway **running** (Part A).
2. On the server, flip the station:
   ```php
   \Industry\MediaCore\Domain\Entities\Station::whereKey(<ID>)
       ->update(['delivery_mode' => 'gateway']);
   ```

**✔ Verify with a smoke test** (a real dispatch through the gateway):
1. Dispatch a **test spot** for this station (a small/silent creative, air time now).
2. On the **Playout Operations** screen you should see the op go
   `QUEUED → SUCCEEDED` with **ack `ACKED`** and a duration in ms.
3. In RadioBOSS, the creative appears in the playlist (the gateway inserted +
   confirmed it).
4. After it plays, the **As-Run report** shows it `AIRED` (reconciliation).
5. Pull it back with the spot's **Pull** action → it's removed from the playlist.

If all five pass, gateway delivery is live for this station.

**↩ Rollback (instant)** — set `delivery_mode = 'direct'`. MediaERP immediately
resumes calling RadioBOSS itself. *(Works because you have NOT firewalled yet —
RadioBOSS is still reachable from the server.)* Queued gateway ops can be left;
they expire, or delete them from Playout Operations.

---

## 4. Part C — Set the governance policy

Set the real policy (served to the gateway via `/gateway/manifest`). On the
server `.env`:
```ini
MEDIACORE_GATEWAY_AUTO_CUT=true
MEDIACORE_GATEWAY_BLOCK_USB=true
MEDIACORE_GATEWAY_APP_ALLOWLIST=RadioBOSS.exe,mediacore-studio-gateway.exe
MEDIACORE_GATEWAY_ALLOWED_FOLDERS=D:\MediaCore\Media,D:\MediaCore\Music
MEDIACORE_GATEWAY_SWITCH_CMD=play 1
```
Then `php artisan config:clear`.

> ⚠️ **Order matters.** Only turn `AUTO_CUT=true` on **after** the manifest is
> populated with this station's approved assets (checksums) — otherwise the
> reconciler will treat real content as unapproved and cut it. Confirm the
> manifest lists your live library before enabling auto-cut.

**✔ Verify** — the gateway log shows the new policy after its next manifest sync
(≤ 5 min, or restart the gateway). USB block / folder ACLs apply if the gateway
runs as an admin service (Part F).

**↩ Rollback** — set `AUTO_CUT=false` / `BLOCK_USB=false`, `config:clear`.

---

## 5. Part D — Harden the service

The installer (Part A) already registered the service (WinSW, auto-start,
restart-on-failure). Now make it tamper-resistant so the operator can't disable
it:

- Run the service under a **dedicated low-privilege account** (not the logged-in
  operator).
- Remove the operator's rights to **stop the service** and to **edit the install
  folder** (`icacls` / service SDDL).
- For durable **USB / app / folder** lockdown, back it with **Group Policy /
  AppLocker / WDAC** — the gateway assists and reports, GPO enforces.

**✔ Verify** — `Get-Service mediacore-studio-gateway` is `Running`; reboot the PC
and confirm it comes back up on its own; the operator account **cannot** stop it.

**↩ Rollback** — `.\gateway-service.exe stop` (admin) then set the station to `direct`.

---

## 6. Part E — Lock RadioBOSS to localhost (point of no return)

Now RadioBOSS no longer needs to be reachable from anywhere but the studio PC.

**Preferred:** in RadioBOSS, bind the Remote Control API to **`127.0.0.1`** only
(Settings → Remote Control API → listen address).

**Or** firewall the port (example for 8181):
```powershell
New-NetFirewallRule -DisplayName "Block RadioBOSS API inbound" -Direction Inbound `
  -Protocol TCP -LocalPort 8181 -RemoteAddress (Get-Content) -Action Block
# simplest: block the port for all remote addresses; localhost is unaffected
New-NetFirewallRule -DisplayName "RadioBOSS API localhost only" -Direction Inbound `
  -Protocol TCP -LocalPort 8181 -Action Block -RemoteAddress Any
```
(Loopback `127.0.0.1` traffic bypasses the firewall, so the gateway still works.)

**✔ Verify**
- From **another machine**, `curl http://<studio-ip>:8181/?...` now **fails** (good — no external reach).
- The gateway keeps dispatching/heartbeating normally (it uses localhost).
- MediaERP's old direct probe to RadioBOSS now fails — which is why the board
  correctly relies on the **gateway heartbeat** for this station (it no longer
  probes gateway stations).

**↩ Rollback** — remove the firewall rule / re-bind RadioBOSS to all interfaces,
then set the station to `direct`.

---

## 7. Part F — Monitor & sign-off (first 24h)

- [ ] **Playout Operations** — no growing `DEAD` count; acks healthy.
- [ ] **As-Run report** — spots reconciling to `AIRED` on schedule.
- [ ] **Compliance** — violations sane (no false auto-cuts); resolve/tune.
- [ ] Board shows the station **online via gateway** continuously.
- [ ] A `PLAYOUT_DEAD` or `PLAYOUT_VIOLATION` alert reaches the team.

Sign off the station, then repeat Parts A–F for the **next** station.

---

## Rollback cheat-sheet

| Situation | Action |
|---|---|
| Anything wrong, pre-firewall | Station → `direct` (MediaERP resumes direct calls) |
| Anything wrong, post-firewall | Re-open the RadioBOSS port, then station → `direct` |
| Gateway misbehaving | Stop the service; station → `direct` |
| Bad auto-cut | `MEDIACORE_GATEWAY_AUTO_CUT=false` + `config:clear` |
| Stuck queued ops | Delete them on the Playout Operations screen |

The single lever is **`delivery_mode`** — flipping it back to `direct` is the
universal escape hatch (just keep RadioBOSS reachable from the server if you're
still in the rollback window).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Gateway 401/403 on lease | wrong/absent `gateway_key`, or station not in `gateway` mode | match the key; set `delivery_mode=gateway` |
| Ops stay `QUEUED`, never leased | gateway not running / wrong `ERP_BASE_URL` | start the service; check the URL + firewall (outbound) |
| Op `SUCCEEDED` but not confirmed | RadioBOSS returned OK but track absent | check the creative file exists on disk (Part 3 file sync); check `insert_position` |
| Everything flags as `UNAPPROVED_CONTENT` | manifest empty / checksums missing | populate approved assets + checksums before `AUTO_CUT` |
| Board shows station offline | no heartbeat | gateway service down, or RadioBOSS API unreachable from the gateway |
| Files not airing (missing) | creative not synced to studio disk | check `/gateway/files` + `MEDIA_FOLDER` + checksums |

---

## Appendix — env reference

**Gateway (studio `.env`, `GATEWAY_` prefix):** `ERP_BASE_URL`, `GATEWAY_KEY`,
`GATEWAY_ID`, `RADIOBOSS_BASE_URL`, `RADIOBOSS_PASSWORD`, `RADIOBOSS_TIMEOUT`,
`INSERT_POSITION`, `MEDIA_FOLDER`, `SCHEDULE_FOLDER`, `DB_PATH`, `LOG_LEVEL`,
`POLL_INTERVAL`, `HEARTBEAT_INTERVAL`, `LEASE_LIMIT`.

**MediaERP (server `.env`):** `MEDIACORE_GATEWAY_AUTO_CUT`,
`MEDIACORE_GATEWAY_BLOCK_USB`, `MEDIACORE_GATEWAY_ALLOWED_FOLDERS`,
`MEDIACORE_GATEWAY_APP_ALLOWLIST`, `MEDIACORE_GATEWAY_SWITCH_CMD`,
`MEDIACORE_MEDIA_SOURCE_PATH`, `MEDIACORE_PLAYOUT_PROCESSING_TTL`.

**Station fields:** `delivery_mode` (`direct`|`gateway`), `gateway_key`,
`gateway_seen_at`.

**Gateway API (machine-to-machine, `X-Gateway-Key`):** `POST gateway/lease`,
`POST gateway/ack`, `POST gateway/heartbeat`, `GET gateway/files/{asset}`,
`GET gateway/schedule`, `GET gateway/manifest`, `POST gateway/violations`.
