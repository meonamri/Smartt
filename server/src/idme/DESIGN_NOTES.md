# IDME module — design rationale (READ BEFORE "optimizing")

This module automates a **live Malaysian government portal** (`idme.moe.gov.my`
→ `moeispel.moe.gov.my` / MOEIS) via Playwright + Firefox. Several things in
`login_engine.py` and `form_filler.py` look redundant, old-fashioned, or
needlessly defensive. **They are not.** Each was discovered the hard way against
the real portal (there is no staging server), and removing it silently breaks
the automation — often without an error, by submitting nothing or reading an
empty table. If you are tempted to simplify one of these, re-read this file and
re-test against the live portal first.

> Why this lives in code (not just CLAUDE.md): the branch `CLAUDE.md` testing
> banner is removed on merge, and the auto-memory is per-developer-machine.
> This file ships with the repo so the reasoning survives both.

---

## `login_engine.py`

### 1. Firefox with HTTP/2 disabled — REQUIRED, not legacy cruft
`network.http.http2.enabled` / `spdy.*` set to `False`. Malaysian government
portals stall or reset connections over HTTP/2. With HTTP/2 on, navigations hang
or fail intermittently. Do **not** "modernize" by removing these prefs, and do
**not** switch to Chromium (this was tuned for Firefox's networking stack).

### 2. `dom.security.https_only_mode = True` — REQUIRED
The portal hardcodes `http://moeispel.moe.gov.my/...` URLs, but **port 80 is
closed** — only HTTPS is served. Without HTTPS-Only mode the SSO hop to an
`http://` URL dies with `NS_ERROR_CONNECTION_REFUSED`. This pref makes Firefox
auto-upgrade every `http` navigation to `https` (the automated equivalent of a
human re-typing the "s").

### 3. `domcontentloaded`, never `networkidle`
Every wait/navigation uses `wait_until='domcontentloaded'`. The MOEIS pages
**never reach `networkidle`** — the dashboard perpetually re-polls (some of it
failing) so the network is never quiet. Switching any of these back to
`networkidle` (the Playwright "best practice") will time out.

### 4. Attendance page opens on a FRESH page in the same context
After SSO, the `moeispel` home document never settles; calling `goto()` to
navigate *that* document away raises `NS_ERROR_FAILURE`. So we open the
attendance URL on a **new page in the same (cookie-sharing) context** and
reassign `self.page` to it. Don't collapse this back into a single-page
navigation.

### 5. jQuery `$.ajaxPrefilter` rewriting `http://moeispel` → `https://` BEFORE send
Injected on the attendance page. The portal's own AJAX targets `http://` URLs.
With HTTPS-Only mode each one takes a cross-scheme **307 redirect** to `https`,
and Firefox **strips the `X-CSRF-TOKEN` header across that redirect** → the
portal returns **HTTP 419 (Page Expired)** and an **empty student table**. The
prefilter rewrites the URL to `https` *before* the request is sent, so no
redirect happens and the CSRF header survives.
- This only works because the portal uses **jQuery `$.ajax`**. A native `fetch`
  would bypass `$.ajaxPrefilter` — verified that the relevant endpoints
  (`kemaskiniKehadiranHarian`, etc.) are jQuery AJAX, so the prefilter covers
  them. If the portal ever migrates to `fetch`, this approach must change.

### 6. Triggering the class-select `change` event to load rows
The student rows load via AJAX on the class `<select>`'s `change` event, which
does **not** fire for the already-pre-selected class. We trigger it explicitly,
otherwise the table is empty.

### 7. Fail fast when a MOEIS page load returns an HTTP error
STEP 4 checks the status `goto()` returns for the SSO hop (5xx → fail) and the
attendance page (any 4xx/5xx → fail) and raises `MOEISUnavailableError`
("MOEIS unavailable: <host> returned HTTP <code> on <step> (Imperva error N,
incident …)"). Why: when Imperva cannot reach the MOEIS origin it serves its own
503 "Connection Failed, Error 20" page **on the moeispel URL**, so the
`'moeispel' in url` check passes and the old code waited ~35 s more, then failed
with a misleading `Timeout … input.case-hadir` (2026-11-10: every class, all
day). 5xx stays transient for the retry classifier (`returned HTTP 5\d\d`); 4xx
(e.g. a geo-block 403) is not retried. A 4xx on the SSO hop alone is left to the
attendance-page check, and a `goto()` that raises or returns `None` is still
tolerated (§4). Don't drop these checks to "simplify" — they are what makes a
portal outage show up as an outage.

---

## `form_filler.py`

### 1. "Mark absent" = UNCHECK
Students are **checked = present** by default. Marking absent means *unchecking*
the `input.case-hadir` checkbox, which is what reveals the category/reason
dropdowns.

### 2. Reason dropdown = `select.selectsebab` (NOT positional index)
An attendance row contains **three** `<select>`s: two `kategori[]`
(`select.selectkategori`, one is a Select2 accessibility duplicate) and — only
**after a category is chosen** — one lazily-appended `sebabcuti[]`
(`select.selectsebab`). The reason MUST be set on `select.selectsebab`. The
original code used `querySelectorAll('select')[1]`, which is the *second
category* select — so the reason was silently never set. Always target by class
and **re-query/poll** for `select.selectsebab` after changing the category;
never assume an index.

### 3. Category/reason are matched by DISPLAY TEXT
We pick options by visible text (e.g. `PONTENG`, `MALAS KE SEKOLAH`). It happens
that the portal option `value` *equals* our sebab code (e.g. `value="N0040027"`
in `moeis_codes.py`), so value-based matching would also work — but text
matching is what's validated. `moeis_codes.py` was verified text-for-text
against the live dropdowns.

### 4. Submit clicks fire via jQuery `.trigger('click')`, NOT `page.click()`
In `_submit_form` the Kemaskini / `.simpan` / `.simpansah` clicks are dispatched
through `page.evaluate(... jQuery(sel).trigger('click') ...)`. **Do NOT replace
these with Playwright `page.click()` / `locator.click()`.** The portal shows a
`.loadover` overlay that intercepts pointer events, so Playwright's actionability
checks hang for the full timeout (observed: a 30s hang on `#kemaskiniKehadiran`)
and the submit never happens. Triggering the bound jQuery handler directly
bypasses the overlay and is the only reliable method here.

### 5. The submit buttons are `.simpan` / `.simpansah` — NOT "Simpan & Sahkan"
Clicking **Kemaskini** (`#kemaskiniKehadiran`) only builds a client-side
SweetAlert confirmation modal — it does **not** submit. That modal's buttons:
- `.batal`  → cancel
- `.simpan` → `kemaskini('simpan')` → saves a **DRAFT** (status *MENUNGGU
  PENGESAHAN*, re-editable)
- `.simpansah` (green, "Sahkan") → `kemaskini('simpansah')` → **CONFIRMS**
  (status *TELAH DISAHKAN*, hard to reverse — production default)

The literal text **"Simpan & Sahkan" only ever appears as a DISABLED button**
(when `layaksahkan` is false). Targeting it (as the original code did) clicks a
disabled no-op and reports success while submitting nothing. `_submit_form`
takes `confirm: bool` to choose `.simpansah` (default, production) vs `.simpan`
(reversible draft, used for testing).

### 6. Status read-back is best-effort, and waits for the EXPECTED string
After submitting, the portal updates the status badge asynchronously (sometimes
only on reload). `_submit_form` waits for the *specific expected* status string
(`TELAH DISAHKAN` for confirm, `MENUNGGU PENGESAHAN` for draft) so a
just-confirmed day isn't misread as the previous `MENUNGGU`. Even so, the
read-back can lag — treat it as advisory. The **submit action itself** (the
`kemaskiniKehadiranHarian` POST) is the real outcome and is unaffected.

### 7. Dismiss stray SweetAlert + ~10s modal poll before submitting
A leftover single-OK SweetAlert can block the Kemaskini handler from rendering
its modal, and the modal renders a beat after the trigger (more so right after a
table reload). So `_submit_form` dismisses any open SweetAlert first and polls
~10s for `.simpan`/`.simpansah` to appear. Don't shorten the poll or drop the
dismissal.

### 8. A CONFIRMED day (TELAH DISAHKAN) LOCKS the form
Re-submitting a confirmed day is a no-op (no AJAX fires). Any end-to-end test of
the draft→confirm transition needs the day reset in the portal first.

---

## Live test harness (gitignored, this dev machine only)
`server/diag_idme_reasons.py` exercises the above against the live portal:
`--validate-mark` (reason targeting, no submit), `--audit-submit` (read-only
button/handler/endpoint audit), `--do-submit` (real draft→confirm submit).
See the branch `CLAUDE.md` for usage. Do not commit its output (student PII).
