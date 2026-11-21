# CLAUDE.md — Smartt

Split-architecture RFID attendance tracking for the Datang.my API, with an optional
**IDME module** that submits absences to Malaysia's IDME/MOEIS portal.

See `README.md` for the full architecture and API. This file covers the
operational facts that aren't obvious from the code.

## Agents working from the host clone

- **Where you are:** a clone on the Proxmox host amakma
  (`/root/homelab-workspace/repos/Smartt`, or a worktree of it). Production runs
  on CT 105 `schoolapps` (192.168.1.20) from `/home/meon/Datang-Reader`, as user
  `meon` — **the directory is still `~/Datang-Reader` although the repo is
  Smartt**. GitHub `meonamri/Smartt` `main` is the source of truth; the host clone
  and CT 105 each sync with GitHub, never with each other.
- **Before starting**, check that CT 105 has nothing GitHub lacks (the fetch only
  updates meon's remote refs; nothing else is written):
  `pct exec 105 -- su meon -s /bin/bash -c 'cd "/home/meon/Datang-Reader" && git fetch -q && git status -sb -uno'`
  — it must print exactly one line, `## main...origin/main`. If it says ahead or
  diverged, or lists modified tracked files, stop and ask the person to push first.
  Run git on CT 105 only as `meon` (root git calls leave root-owned files in `.git`).
- **Never:** deploy, build or restart containers or services, run `docker`
  anything, `pct push` or any `pct exec` that writes, edit files on CT 105, or run
  the app/bot locally with real tokens — Telegram allows one poller per bot token,
  so a local run kicks the live bot off with a 409. Tests use mocks/fixtures only.
- **Your job ends at a pull request.** Deploying is the person's step: on CT 105,
  as `meon`, `git pull`, then rebuild the `smartt` container from `server/`
  (`docker compose build smartt` — the Dockerfile installs with `-c constraints.txt`
  — then `docker compose up -d --no-deps smartt`); see `server/IDME_DEPLOY.md`.
- **Never restart the `smartt` container casually: a restart replays the queued
  attendance. It is school-critical (MOEIS cutoffs).**

## Naming: Smartt vs Datang.my

**Smartt** (Smart Attendance Record Tracking Tajuddin) is this school-owned
service: the server, the readers, the IDME module and the `/cards` screen. It
was called "Datang Reader" until 2026-11. **Datang.my** is the vendor attendance
API that Smartt reports taps to; anything named `DATANG_*`, `DatangAPIClient`,
`datang_*` columns or `ATTENDANCE_MODE=datang` refers to Datang.my and keeps
that name.

Renamed in 2026-11, with the old names still honoured:

| Was | Now |
|---|---|
| container / image / compose service `datang-reader` | `smartt` |
| reader unit `datang-reader-gui.service` | `smartt-gui.service` (the installer retires the old unit) |
| `server/datang_reader.py` | `server/smartt.py` (the old file is a shim) |
| `~/.datang_reader_token`, `~/.datang_reader_queue.db`, `~/.datang_reader.log` | `~/.smartt_token`, `~/.smartt_queue.db`, `~/.smartt.log` |
| `~/.datang_stats.json`, `~/.datang_outbox.db` (readers) | `~/.smartt_stats.json`, `~/.smartt_outbox.db` |
| `DATANG_HOST_PORT`, `_CONTAINER_URL`, `_LOG_LEVEL`, `_FULLSCREEN`, `_ENABLE_PULSE`, `_PULSE_FPS`, `_SERIAL_PORT`, `_SERIAL_BAUD`, `_OUTBOX_FILE`, `_REBOOT_AFTER_UNHEALTHY_S` | `SMARTT_*` (old `DATANG_*` names are read when the new one is unset) |

Renamed state files are moved on first start (`state_path()` in
`server/src/config.py` and `client/gui/config.py`); a legacy SQLite file with a
`-wal`/`-shm`/`-journal` sidecar is used in place instead. The GitHub repo is now
`meonamri/Smartt` (renamed from `Datang-Reader` 2026-11-03; GitHub redirects the
old URL). Existing checkouts keep their `~/Datang-Reader` directory names.

## IDME module (`server/src/idme/`)

At a daily cutoff time the module computes `roster − RFID scans = absent
students`, logs into `idme.moe.gov.my` (Playwright/Firefox), fills the MOEIS
attendance form, and submits. This is a **two-session school**: upper forms
(3–6) submit at a morning cutoff and lower forms (1–2) at an afternoon cutoff.
The scheduler runs one timer per session and each fire submits only that
session's forms; a class is routed to a session purely by the **leading form
number in its class string** (`5 UKM` → Form 5 → morning). A class whose form
maps to no session is never submitted — the settings UI flags these (`no
session`) so they don't silently misfire, the same way it flags class-string
misfires. A class whose string has **no leading form number** (e.g. `MENTARI 1`)
can be pinned to a session with `IDME_CLASS_SESSION_OVERRIDE`
(`"MENTARI 1=morning, MENTARI 2=morning"`, matched verbatim); it resolves
through the single `form_of` chokepoint (to that session's lowest form) so every
consumer — cutoff, scan gate, Telegram routing, settings UI — routes it with no
other change. An explicit override wins over any leading number the string also
has. It is **off by default** (`IDME_ENABLED=false`)
and runs inside the same `smartt` container — it is not a separate
service. Datang.my scanning for every student is unchanged; the only added
behaviour is MOEIS submission for *onboarded* classes (those with a configured
teacher). Classes without a teacher are never submitted to MOEIS.

### Operating it

- **Config (env / `.env`):** `IDME_ENABLED`, `IDME_CUTOFF_TIME_MORNING=HH:MM`
  (upper forms 3–6) and `IDME_CUTOFF_TIME_EVENING=HH:MM` (lower forms 1–2) —
  legacy single `IDME_CUTOFF_TIME` is still honoured as the morning fallback;
  `IDME_ENCRYPTION_KEY` (Fernet, generate with `server/gen_fernet_key.py` —
  generate ONCE, never commit; losing it makes stored teacher credentials
  undecryptable), and `IDME_SCHEDULER_CONFIRM`.
- **`IDME_SCHEDULER_CONFIRM` is the safety gate.** Default **false**: the daily
  scheduler saves re-editable **DRAFTS** (MENUNGGU PENGESAHAN) so a human
  confirms each morning. Set **true** only after a supervised period — true
  **auto-confirms LOCKED records** (TELAH DISAHKAN) daily and unattended, which
  is hard to reverse. Manual `/idme/submit` defaults to a draft; pass
  `{"confirm": true}` to confirm.
- **A full-attendance class is still submitted.** Zero absences is not a
  no-op: an unsubmitted day reads on MOEIS as "attendance never taken", not
  "everyone present", so `_submit_class_async` logs in and submits the untouched
  form (every student is default-checked hadir) with an empty absence list.
  Guarded by the roster: `roster_count == 0` means "we know nothing about this
  class" (roster never initialised, or everyone retired by a portal read), never
  "all present" — that returns `failed` (non-retryable) without a login, and
  deliberately **not** `skipped`, which `submit_all_classes` reads as the
  school-wide non-school-day signal and would abandon every remaining class.
  Tests: `server/test_idme_full_attendance.py`.
- **A class is identified by a string that must match in three places** or it
  silently misfires: the roster `Class`, the Datang.my scan `section`, and the
  teacher `class_name`. A student name mismatch = that student marked absent
  every day until an RFID tag is learned for them.
- Identity resolution: name bridges roster-init + first scan + card
  replacement; the **RFID tag is the daily key once learned** (tag-first,
  name-fallback). IC is unavailable on both sides. See
  `server/src/idme/IDENTITY_RESOLUTION_DESIGN.md`.
- **Dropped students (transfer/withdrawal) are retired by "Read portal".**
  `RosterManager.upsert_from_portal` sets `enabled = 0` on any registry student
  the portal no longer lists — a **soft** delete, so the row and its learned tag
  survive and a student the portal lists again is re-enabled by the next read
  (matched by `idpelajar` even while disabled). This is load-bearing, not
  cosmetic: a dropped student left enabled is submitted absent daily and has no
  portal checkbox to mark, and a single such `Student checkbox not found` fails
  the **whole class** (`failed_count > 0` ⇒ `status='failed'` in
  `orchestrator._submit_class_async`) every day until someone re-reads the
  portal. An **empty** portal list retires nobody — a bad read is never
  authority to wipe a class. Tests: `server/test_idme_roster_retire.py`.

### Telegram reason collection (optional, off by default)

An optional Telegram bot lets each class teacher record a **per-student reason**:
at a per-session prompt time the bot DMs the teacher their current absentee list
with inline buttons (curated quick-pick + "More…" → full MOEIS list by category);
the chosen reason is stored in the `absence_reasons` table and
`AbsenceDetector.detect_absences` merges it over the default `N0040027` PONTENG ·
MALAS KE SEKOLAH. Choosing PONTENG explicitly is still an answer.

**Every absentee needs an answer before MOEIS is opened** (the integrity gate in
`_submit_class_async`, unconditional — Telegram on or off, any caller). A class
with unanswered absentees is recorded `blocked` with no login. The bot is the
only writer of reasons, so with the bot off only zero-absentee classes can ever
be submitted.

**When a class is submitted (Telegram on):**

- **Prompt time** — classes with nothing to answer (all present) and no row yet
  are submitted (`submit_ready_classes`).
- **The moment the last absentee is answered** — `handle_teacher_response`
  submits that class at once, before or after the cutoff, for its own date,
  for today or up to `IDME_RESPONSE_WINDOW_DAYS` (7) back. Past dates go
  through `login_engine` target_date, which refuses a day already locked.
  Ready blocked classes are also resumed after a restart.
- **Session cutoff** — now effectively the **reminder**: it sends the blocked
  notice to teachers still owing answers (once per class per day) and submits
  whatever became ready without an answer event (e.g. late scans). It never
  re-touches a class completed today. The settings page shows it as
  "Remind in Xh Ym" (the day name across `IDME_WEEKEND_DAYS`).
- **Unlinked teachers are never submitted.** With `IDME_TELEGRAM_ENABLED=true`, a
  class whose teacher has no `telegram_chat_id` is never submitted by any path
  — prompt time, answer-complete, cutoff, restart resume, the 3-hourly retry,
  or the manual Submit remaining / `POST /idme/submit` (409 `not_linked`).
  `orchestrator._telegram_unlinked()` is the rule; `submit_class` is the
  chokepoint and writes **no row** (status `not_linked`), and the bulk/retry
  paths drop such classes up front. The settings page flags them "Not linked"
  and the daily-run card counts them ("N not linked") instead of reading
  "Submitted". The rule keys on the config switch, not on whether the bot
  started, and the page follows the same switch. With Telegram off it never
  applies: the cutoff submits every (zero-absentee) class as before.

- **Config (env / `.env`):** `IDME_TELEGRAM_ENABLED` (default false),
  `IDME_TELEGRAM_BOT_TOKEN` (from @BotFather), `IDME_TELEGRAM_PASSPHRASE` (shared
  self-link secret, **required** when the bot is enabled), and per-session prompt
  times `IDME_TELEGRAM_PROMPT_TIME_MORNING` (default 10:00) /
  `IDME_TELEGRAM_PROMPT_TIME_EVENING` (default 15:00) — must be *before* that
  session's cutoff. Off and independent of `IDME_SCHEDULER_CONFIRM`; needs
  outbound HTTPS to `api.telegram.org`.

### "Hadir (lupa kad)" present-override + notifications

The reason prompt's per-student keyboard leads with a **✅ Hadir (lupa kad)**
button (before any absence reason): the teacher asserts an absentee is actually
present but forgot their RFID card. Tapping it writes a row to `present_overrides`
(`PresentOverrideStore`); `AbsenceDetector` then **drops** that student from
`detect_absences` (never submitted absent) and **counts them present** in
`get_attendance_summary`, so `roster = present + absent` still holds. A present
override and an absence reason are **mutually exclusive** — setting either clears
the other. The confirmation carries an **undo** button (`u|` action) that clears
the override and restores the reason keyboard.

- **14-day limit / admin alert.** `mark_present` returns the student's distinct
  Hadir days in the trailing `IDME_HADIR_WINDOW_DAYS` (default 14, **rolling**
  look-back — assumption, not fixed cycles). When that exceeds `IDME_HADIR_LIMIT`
  (default 3) the bot DMs `IDME_TELEGRAM_ADMIN_CHAT_ID` (numeric, from
  @userinfobot) with the student name + class. **The override is never blocked** —
  a genuinely-present student must not be marked absent; the limit only triggers a
  notification. An unset admin id or a Telegram hiccup only logs (best-effort).
- **Post-submission teacher DM.** After a class is successfully recorded to IDME,
  the class teacher is DM'd *"Kehadiran Kelas [Class] telah direkodkan ke dalam
  IDME."*. Wired via `IDMEOrchestrator.submission_notifier` (set in
  `api_routes.init_idme_module`, so both the scheduled cutoff and manual
  `/idme/submit` fire it), invoked in the **sync** `submit_class` wrapper *after*
  the async workflow returns — so it can't run on an `OrchestratorError`. Fires
  only when something actually reached the portal (`status=='completed'` and the
  form was submitted or students were already-absent-recorded) — an all-present
  day does submit, so it notifies too; **not** on skips or failures.
  Best-effort/guarded — a Telegram failure never breaks a submission.
- **Non-school days (weekends + holidays):** the scheduled prompt is gated by
  **three layers, cheapest first**, all re-evaluated **live at prompt time**
  (`TelegramPromptScheduler._should_prompt`), because the bot — unlike the cutoff
  scheduler — never submits, so it has no `NonSchoolDayError` backstop of its own:
  1. **Weekday guard** (`IDMEConfig.is_school_day`) skips the weekend with no
     data or portal contact. Weekend days come from `IDME_WEEKEND_DAYS` (weekday
     names or 0–6 indices, e.g. `sat,sun`), defaulting to **Fri/Sat** for this
     school.
  2. **Scan gate** (`orchestrator.enough_scans_today` → `ScanTracker.count_scans_on`):
     skips when fewer than `IDME_MIN_SCANS_FOR_SCHOOL_DAY` (default **5**) distinct
     students scanned today. On a real school day students tap in before any
     cutoff, so a near-zero count means a holiday **or a down reader** (in which
     case roster−scans marks the whole school absent — must NOT prompt/submit).
     Cheap DB read, no portal; day-level and school-wide (morning scans ⇒ evening
     is a school day too). **Evaluated live at the moment of action**, never
     frozen — a slow-scan morning or a restart can't wrongly skip or spam.
  3. **Daily portal pre-check** catches public holidays the cheaper gates can't:
     a **single** Playwright login (`orchestrator.check_school_day`, school-wide,
     one login serves both sessions) runs `IDME_TELEGRAM_PRECHECK_LEAD_HOURS`
     (default **1**) before the earliest prompt; its verdict is stored per
     prompt-date and read by both prompts. **A full ~30–60s Firefox login, not
     lightweight** — but the pre-check **skips its own login** when the weekday or
     scan gate already says non-school (storing `None`, never a stale `False`), so
     on holidays it costs nothing. Default lead is **1h** (not 3h) precisely so
     students have already scanned by pre-check time, making the scan gate
     meaningful; a 3h lead would run before students arrive.
  - The **cutoff submission** shares the scan gate: the scheduled fire passes
    `submit_all_classes(..., enforce_scan_gate=True)`, which skips the whole run
    as a non-school day **without any portal login** when scans are below
    threshold (recording skip rows only for in-scope forms). The **manual**
    submit-all path is left ungated (portal `NonSchoolDayError` still backstops a
    deliberate human catch-up).
  - **Failure policy (must stay airtight):** ONLY a *definite* non-school answer
    suppresses — the weekday guard, a below-threshold scan count, or the portal's
    stored `False`. Anything **inconclusive** (portal `None`, a scan-count read
    that errors, or a decision lost to a container restart) falls through to the
    next gate / prompts on a weekday. A flaky portal or DB must never silence a
    real school day. The portal decision is in-memory; a restart loses only that
    day's *portal-holiday* coverage — the weekday and (live) scan gates still hold.
  - **Unverified assumption:** the portal is assumed to report the same
    school-day status at pre-check time as at prompt time (1h later). Confirm
    live against a known holiday before trusting portal holiday coverage; the
    weekend + scan gates do not depend on it. (The probe `check_school_day` has
    unit coverage only for how the scheduler consumes True/False/None — not the
    live portal→verdict mapping. Run it once via `test_idme_login.py` on a real
    holiday.)
  - **Timezone:** all timers use naive container-local `datetime.now()`. Current
    fire times (09:00 pre-check / 10:00 / 15:00) are far from local midnight, so
    the date the guards read is unambiguous whether the container clock is MYT or
    UTC. Keep prompt/pre-check times clear of local midnight if they ever move.
  - On-demand paths (`/kehadiran`, admin "Prompt teacher") are **not** gated —
    explicit human requests work any day.
- **Linking (self-service):** the bot is public (BotFather), so the gate is the
  shared passphrase. A teacher searches the bot, sends `/start`, types
  `IDME_TELEGRAM_PASSPHRASE` (constant-time compared), then taps their class — the
  bot binds their `chat_id` to that class's teacher. Only **unlinked** configured
  classes are offered, so a passphrase-holder can't re-point a colleague's class
  to their own chat; an admin unlinks from `/idme/settings` (read-only Linked /
  Not-linked status + unlink) to free a class for a phone change. A persisted,
  auto-expiring lockout (`telegram_auth_attempts`: 3 tries → ~1h) survives
  restarts so the counter can't be reset by bouncing the container. Teachers are
  routed to a prompt by the same leading-form-number rule as the cutoff scheduler.
  Implementation is `server/src/idme/telegram_bot.py` (requests-based long-polling
  daemon thread — same style as `scheduler.py`, no webhook/asyncio).
- **Re-prompting one class** (a teacher who onboarded *after* the scheduled
  session prompt already fired): the scheduler only fires per session. Two on-
  demand paths cover the straggler — the teacher sends **`/kehadiran`** to pull
  their own current list, or an admin clicks **Prompt teacher** on that class's
  card in `/idme/settings` (`POST /idme/telegram/prompt {class_name}` →
  `bot.prompt_class`). Both **must** run in the live bot instance (the web
  process) — the prompt's button ids live in the bot's in-memory `_entries`, so a
  separate one-off process would send the DM but the teacher's taps would answer
  "session expired". Never start a second poller on the same token (Telegram 409).

### Deploying / turning it on

Rollout is an in-place upgrade of the existing `smartt` container.
Follow **`server/IDME_DEPLOY.md`** — it has the volume/bind-mount gotchas and
the post-deploy gating (observe one real scheduled fire; verify per-class
roster/name alignment) that must clear before flipping `IDME_SCHEDULER_CONFIRM`
to true.

## Local test harness (not part of the Docker app)

The IDME test venv is `server/.venv-idme/` (gitignored; none exists on the host
clone — how agents run the suite there is TBD). `server/test_idme_login.py`
(tracked) does a **real** portal login, so it is not for agents; the offline
diagnostics `diag_idme_*.py` and their PII-bearing dumps are gitignored.
Firefox with HTTP/2 disabled is **required** for the Malaysian gov portals — do
not remove that from `login_engine._initialize_browser`.
