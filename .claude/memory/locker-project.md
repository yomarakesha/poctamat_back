---
name: locker-project
description: "Reverse-engineering project of user's own 43-cell face-recognition parcel locker (poctamat)"
metadata: 
  node_type: memory
  type: project
  originSessionId: a4f1ef6d-138c-4503-a67e-4a4b57404939
  modified: 2026-08-05T12:11:40.743Z
---

User's own parcel locker (43 cells, face recognition, Russian UI). Legal: device is user's, no real personal data. Full brief lives in repo at `locker_project_brief.md` — read it for details.

Architecture: Allwinner/Android terminal (all logic) `←UART→` lock board (24-ch dumb executor) → solenoids. Two boards: address 1 = cells 1–21, address 2 = cells 22–43.

Entry point: web panel `http://192.168.11.242:8088`, admin password `111111`. Security hole: only `/Login` and `/sendData` check password; everything else (open/shutdown/APK) is unauthenticated.

Key API: `/GetBoxList` (read, safe), `/OpenBox [n]` (logical cell 1–43), `/OpenLockTest [addr,num]` (raw). Lock board protocol: UART 9600 8N1, frame `0x8A <addr> <lock> 0x11 <XOR-of-prev-bytes>`. Utility `lockctl.py` written for direct UART.

**Why:** Ongoing multi-session hardware RE project; state not derivable from repo alone.
**How to apply:** Continue from brief section 8 (next steps). Follow safety rules in brief section 10: nothing irreversible without explicit human "yes", never touch biometrics endpoints, keep DRY_RUN default on, don't hit `/AllOpenBox`/`/AllClearBox`/`/uploadApk`/`/RestartShutdown` or port 14035 unprompted.

**Built (Смысл 1, working):** `server.py` — zero-dependency stdlib proxy (http.server + urllib), because pip/PyPI is blocked by a corporate proxy on this machine (LAN to terminal works fine, external does not — use stdlib, run with `py`, not `python`). Serves `static/index.html` (43-cell grid UI). Routes: `GET /api/health`, `GET /api/cells` (biometrics stripped), `POST /api/open/{n}`. DRY_RUN defaults ON. Run: `py server.py` → http://127.0.0.1:8000. See README.md.

**Gotcha:** terminal AndServer randomly resets ~30% of TCP connections; a `User-Agent` header + up to 5 retries in `_upstream()` makes it reliable (12/12). `Connection: close` header makes it worse — don't add it.

**Live cell state (2026-08-05):** occupied 11,12,17,18,25 (state=1), special 13 (state=2), rest free. Use a free cell for any open test.

**Terminal is a consumer Android TABLET** (the screen unit where cells open) at 192.168.11.242 — it has its own browser and is already on the LAN. So the on-screen UI needs NO adb/APK: just open the proxy URL in the tablet's browser.

**USB/adb dead end:** tablet's Type-C did not enumerate on PC at all (tried Type-C↔Type-C). No adb, no FEL device seen. Likely charge-only cable or Type-C is power-only / no data. Abandoned USB for the display goal — not needed since it's a tablet with a browser.

**Serving to the tablet (set up 2026-08-05):** proxy moved to **PORT 8090** because the user's OTHER project (km-site backend, uvicorn) already holds 127.0.0.1:8000 — do NOT kill that. Proxy runs `HOST=0.0.0.0 PORT=8090 py server.py`. Windows firewall inbound rule "Locker Proxy 8090 (tablet only)" allows TCP 8090 from 192.168.11.242 only. PC LAN IP = **192.168.11.29**. Tablet opens **http://192.168.11.29:8090**. PC must stay awake for the UI to serve.

**Web panel (8088) fully studied (frontend JS reversed):** uni-app pages login/index/BoxState/person/record/addPerson/Authorization/ChangePasswordf/showdata/unpackingsettings. New findings:
- `/sendData` tasktypes decoded: **26 = lock, 27 = unlock**, body `{devicepass, tasktype, data:[boxnum]}` (needs password 111111). So a LOCK command exists — brief was imprecise.
- `/uploadApk` exact format: multipart, field name `file`, plus `formData:{user:"test"}`, unauthenticated, works from PC over LAN.
- New endpoint `/UploadFileToBase64` = face-photo upload (biometrics, don't touch).
- Vendor = **kefaface.com**; showdata page links to cloud `http://locker.kefaface.com/#/weui/isLogin` and downloadable APKs `webapk`/`sfapk` on coding.net. Vendor "webapk" may be a configurable WebView kiosk.
- Panel has NO setting to repoint the on-screen display at a custom URL and no way to exit the kiosk. On-screen UI is native.

**Blocker for on-screen UI:** tablet is locked in its kiosk app (user can't exit), USB/adb dead, so the only route to put our UI on the tablet's own screen is `/uploadApk` (a write) — but no eMMC backup possible (FEL/USB down), so it's bare risk. Recommended interim: external screen/tablet beside it pointing at http://192.168.11.29:8090 (zero risk, works now).

**Chosen end-state (2026-08-05):** Option C — user's UI on the tablet's OWN screen via `/uploadApk` kiosk APK. Face recognition NOT needed (user opens cells via the UI), so vendor screen can be fully covered. If the vendor app's AndServer(8088) dies when backgrounded, fall back to driving the lock board over UART directly (protocol known). Physical board access exists but user doesn't know how — guide them.

**Kiosk APK BUILT (offline):** `poctamat/kiosk-apk/` — minimal fullscreen WebView (`com.poctamat.kiosk`, label "Локер", min21/target34, INTERNET) pointing at `http://192.168.11.29:8090` (TARGET_URL in MainActivity.java; rebuild to change). Built with raw SDK toolchain (build-tools 34.0.0 + android-34 + JDK17, NO Gradle/internet) via `build.sh` → `locker-kiosk.apk` (signed, verified). NOT yet installed/tested on hardware. Note: dl.google.com blocked but SDK already had everything; general internet works only with curl `--noproxy '*'`.

**Vendor webapk:** could NOT download — coding.net and kefaface.com are China-hosted (Tencent Cloud), unreachable from this network even bypassing proxy. Opens only via a China VPN.

**GATING next step — recovery net before any `/uploadApk` write:** bring up FEL + dump eMMC first (USB adb dead; `/uploadApk` is irreversible, no other rollback). FEL forces the top USB into Allwinner device mode (VID 1f3a) even though normal Type-C didn't enumerate. Need a photo of the terminal board to locate the FEL pad + USB — brief warns 220V in top PSU compartment. sunxi-tools not installed yet.

**Not done:** FEL/eMMC dump; APK not flashed/tested; no live `/OpenBox` fired.
