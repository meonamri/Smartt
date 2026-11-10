"""
IDME Login Engine for Smartt

6-step Playwright automation for logging into the Malaysian Ministry
of Education's IDME portal (idme.moe.gov.my) and navigating to the
MOEIS attendance page.

!!! The networking workarounds here are load-bearing against the LIVE portal,
    which hardcodes http:// but serves only https (port 80 closed) and never
    goes network-idle. HTTP/2-disabled, `https_only_mode`, `domcontentloaded`
    (never `networkidle`), the fresh-page SSO hop, and the jQuery
    `$.ajaxPrefilter` CSRF fix are NOT optional. Before changing any of them,
    READ `DESIGN_NOTES.md` in this package and re-test — they fail silently
    (empty table / HTTP 419 / NS_ERROR_*) otherwise.

Ported from: idme-attendance-automation/automation/engine.py
Key features preserved:
  - Firefox with HTTP/2 disabled (gov portals stall/reset over HTTP/2)
  - 6-step login flow (IC → checkbox → password → submit → navigate → CSRF)
  - CSRF token extraction
  - Cookie extraction
  - Error detection with Malaysian language support

Simplified:
  - No database dependency
  - Returns page object for form filling (doesn't close browser)
  - Credentials passed directly (not from .env)
"""

import logging
import asyncio
import re
from typing import Dict, List, Optional, Any
from datetime import datetime, date
from pathlib import Path
from urllib.parse import unquote_plus, urlparse
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError
)


class LoginEngineError(Exception):
    """Base exception for login engine errors."""
    pass


class LoginFailedError(LoginEngineError):
    """Raised when IDME login fails."""
    pass


class NavigationError(LoginEngineError):
    """Raised when portal navigation fails."""
    pass


class MOEISUnavailableError(NavigationError):
    """Raised when a MOEIS page load comes back as an HTTP error (e.g. the
    Imperva 503 "Connection Failed, Error 20" page when Imperva cannot reach
    the MOEIS origin). Fails the login straight away instead of waiting ~35s
    for a student table that can never appear. The message keeps the
    "returned HTTP <code>" wording: 5xx stays transient for the retry
    classifier, 4xx (e.g. a geo-block 403) does not."""
    pass


_IMPERVA_EDET = re.compile(r"edet=(\d+)")
_IMPERVA_INCIDENT = re.compile(r"incident_id=([0-9-]+)")


async def moeis_http_error(response, what: str) -> Optional[str]:
    """Return a 'MOEIS unavailable' message if ``response`` is an HTTP error
    (status >= 400), else None. ``response`` may be None (goto raised or
    committed without a response) — that is not treated as an error here."""
    if response is None or response.status < 400:
        return None
    host = urlparse(response.url).hostname or response.url
    detail = ""
    try:
        body = await response.text()
    except Exception:
        body = ""
    if "_Incapsula_Resource" in body:
        edet = _IMPERVA_EDET.search(body)
        incident = _IMPERVA_INCIDENT.search(body)
        detail = " (Imperva"
        if edet:
            detail += f" error {edet.group(1)}"
            if edet.group(1) == "20":
                detail += ": Imperva could not connect to the MOEIS server"
        if incident:
            detail += f", incident {incident.group(1)}"
        detail += ")"
    return (f"MOEIS unavailable: {host} returned HTTP {response.status} "
            f"on {what}{detail}")


class AlreadyConfirmedError(LoginEngineError):
    """Raised when the requested day is already confirmed (TELAH DISAHKAN) on
    the portal — locked, typically confirmed by hand by the teacher. Nothing was
    written; the orchestrator records it as 'confirmed_external', not a failure.
    The message keeps the "already confirmed" wording the retry classifier
    treats as permanent."""
    pass


class NonSchoolDayError(LoginEngineError):
    """Raised when the portal signals today is a non-school day (weekend / public holiday).

    The portal shows a 'Tarikh semasa tidak tersedia' modal and silently shifts
    the attendance form to the previous working day. We must not submit in this
    state — the date on the form is wrong.
    """
    pass


class IDMELoginEngine:
    """
    Playwright-based automation engine for IDME portal login.

    Returns a logged-in page object ready for form filling.
    Caller is responsible for cleanup via close().
    """

    # IDME URLs
    LOGIN_URL = 'https://idme.moe.gov.my/login'
    ATTENDANCE_URL = 'https://moeispel.moe.gov.my/sahsiah/kehadiran/tabguru'

    def __init__(
        self,
        ic_number: str,
        password: str,
        headless: bool = True,
        debug: bool = False,
        timeout: int = 30000,
        target_date: Optional[str] = None,
    ):
        """
        Initialize login engine.

        Args:
            ic_number: Teacher IC number (12 digits).
            password: Teacher password (plaintext).
            headless: Run browser headless (default: True).
            debug: Save screenshots on errors (default: False).
            timeout: Default timeout in ms (default: 30000).
            target_date: YYYY-MM-DD to open the attendance form for. None or
                today = the portal's default (today). A past date is selected
                via the form's TARIKH field (#tkh_HH), the same way the portal's
                own monthly "sah harian" button does it.
        """
        if target_date is not None:
            datetime.strptime(target_date, '%Y-%m-%d')  # validate early
        self.target_date = target_date
        self.ic_number = ic_number
        self.password = password
        self.headless = headless
        self.debug = debug
        self.timeout = timeout

        # Playwright objects (kept open for form filling)
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Extracted session data
        self.cookies: List[Dict[str, Any]] = []
        self.csrf_token: Optional[str] = None

        self.logger = logging.getLogger(__name__)

    async def _initialize_browser(self):
        """Initialize Playwright with Firefox and Malaysian locale."""
        self.logger.info(f"Initializing browser (headless={self.headless})...")

        self.playwright = await async_playwright().start()

        # Firefox with HTTP/2 disabled — REQUIRED, not legacy cruft. Malaysian
        # gov portals stall / reset connections over HTTP/2; with it on,
        # navigations hang intermittently. Do not remove these prefs or switch
        # to Chromium. See DESIGN_NOTES.md §login_engine.1.
        self.browser = await self.playwright.firefox.launch(
            headless=self.headless,
            firefox_user_prefs={
                'network.http.http2.enabled': False,
                'network.http.spdy.enabled': False,
                'network.http.spdy.enabled.http2': False,
                # MOEIS SSO sometimes redirects to http://moeispel.moe.gov.my/
                # (port 80 is closed → NS_ERROR_CONNECTION_REFUSED). Force
                # Firefox to upgrade every http navigation to https, which is
                # the automated equivalent of manually re-adding the "s".
                'dom.security.https_only_mode': True,
            }
        )

        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) '
                'Gecko/20100101 Firefox/121.0'
            ),
            locale='ms-MY',
            timezone_id='Asia/Kuala_Lumpur',
            ignore_https_errors=True,
            bypass_csp=True,
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ms-MY,ms;q=0.9,en-US;q=0.8,en;q=0.7',
                'DNT': '1',
            }
        )

        self.page = await self.context.new_page()
        self.page.set_default_timeout(self.timeout)
        self.logger.info("Browser initialized (Firefox)")

    async def _take_screenshot(self, name: str):
        """Take debug screenshot."""
        if self.page and self.debug:
            try:
                screenshot_dir = Path("/data/idme/screenshots")
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                filename = screenshot_dir / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
                await self.page.screenshot(path=str(filename), timeout=5000)
                self.logger.info(f"Screenshot: {filename}")
            except Exception as e:
                self.logger.warning(f"Screenshot failed: {e}")

    async def _check_login_errors(self, stage: str):
        """Check for error messages on the login page."""
        error_selectors = [
            '.alert-danger', '.error-message', '.text-danger',
            '[class*="error"]', '[class*="alert"]',
            '#error-message', '.invalid-feedback',
        ]

        for selector in error_selectors:
            try:
                elem = await self.page.query_selector(selector)
                if elem:
                    text = (await elem.inner_text()).strip()
                    if text:
                        self.logger.error(f"Login error ({stage}): {text}")
                        await self._take_screenshot(f"error_{stage}")

                        if 'kata laluan' in text.lower():
                            raise LoginFailedError(f"Wrong password: {text}")
                        elif 'tidak sah' in text.lower() or 'invalid' in text.lower():
                            raise LoginFailedError(f"Invalid IC: {text}")
                        elif 'tidak wujud' in text.lower():
                            raise LoginFailedError(f"Account not found: {text}")
                        else:
                            raise LoginFailedError(f"Login error: {text}")
            except LoginFailedError:
                raise
            except Exception:
                continue

        # Check page content for error keywords
        content = await self.page.content()
        error_keywords = [
            'Kata laluan salah', 'No IC tidak sah', 'Akaun tidak wujud',
            'Login gagal', 'Login failed',
        ]
        for keyword in error_keywords:
            if keyword.lower() in content.lower():
                await self._take_screenshot(f"error_keyword_{stage}")
                raise LoginFailedError(f"Login failed: {keyword}")

    async def step1_login(self):
        """
        Step 1: Multi-step login.

        1a. Fill IC number, click first 'Daftar Masuk'
        1b. Check security checkbox
        1c. Fill password
        1d. Click submit button, wait for navigation
        """
        self.logger.info("STEP 1: Logging in...")

        await self.page.goto(self.LOGIN_URL, wait_until='domcontentloaded')
        await self.page.wait_for_timeout(2000)

        # 1a: Fill IC number
        ic_field = await self.page.wait_for_selector(
            'input[placeholder*="Kad Pengenalan"], #ic, input[name="ic"]',
            timeout=10000
        )
        if not ic_field:
            raise LoginFailedError("IC field not found")

        await ic_field.fill(self.ic_number)
        await self.page.wait_for_timeout(500)

        # Click first "Daftar Masuk" button
        first_button = await self.page.wait_for_selector(
            'button:has-text("Daftar Masuk")', timeout=5000
        )
        if not first_button:
            raise LoginFailedError("First 'Daftar Masuk' button not found")

        await first_button.click()
        await self.page.wait_for_timeout(3000)
        await self._check_login_errors("after_ic")

        # 1b: Check security checkbox
        self.logger.info("Checking security checkbox...")
        checkbox = await self.page.wait_for_selector(
            'input[type="checkbox"]', timeout=10000
        )
        if checkbox:
            if not await checkbox.is_checked():
                await checkbox.click()
                await self.page.wait_for_timeout(1000)

        # 1c: Fill password
        self.logger.info("Filling password...")
        password_field = await self.page.wait_for_selector(
            'input[type="password"], input[placeholder*="Kata Laluan"], #password',
            timeout=10000
        )
        if not password_field:
            await self._take_screenshot("error_no_password")
            raise LoginFailedError("Password field not found")

        await password_field.fill(self.password)
        await self.page.wait_for_timeout(500)

        # 1d: Submit login
        self.logger.info("Submitting login...")
        submit_button = await self.page.wait_for_selector(
            'button:has-text("Daftar Masuk"):not([disabled])', timeout=10000
        )
        if not submit_button:
            await self._take_screenshot("error_button_disabled")
            raise LoginFailedError("Submit button still disabled")

        await submit_button.click()

        # Wait for navigation
        for attempt in range(15):
            await self.page.wait_for_timeout(2000)
            url = self.page.url
            if 'home' in url or ('login' not in url.lower() and 'verification' not in url.lower()):
                self.logger.info(f"Login successful: {url}")
                break
            try:
                await self._check_login_errors(f"attempt_{attempt}")
            except LoginFailedError:
                raise
        else:
            await self._take_screenshot("error_login_timeout")
            raise LoginFailedError("Login timed out after 30 seconds")

    async def step2_verification(self):
        """Step 2: Handle verification page if present."""
        self.logger.info("STEP 2: Checking verification...")
        url = self.page.url
        if 'verify' in url or 'verification' in url:
            self.logger.warning("Verification page detected")
            try:
                await self.page.wait_for_url(
                    lambda u: 'verify' not in u, timeout=30000
                )
            except PlaywrightTimeout:
                self.logger.warning("Verification timeout, continuing...")
        else:
            self.logger.info("No verification required")

    async def step3_navigate_aplikasi(self):
        """Step 3: Navigate to 'Aplikasi' section."""
        self.logger.info("STEP 3: Navigating to Aplikasi...")

        await self.page.wait_for_load_state('domcontentloaded', timeout=10000)
        await self.page.wait_for_timeout(500)

        selectors = [
            'a:has-text("Aplikasi")',
            'a[href*="aplikasi"]',
            'text="Aplikasi"'
        ]

        link = None
        for selector in selectors:
            try:
                link = await self.page.wait_for_selector(selector, timeout=3000)
                if link:
                    break
            except PlaywrightTimeout:
                continue

        if not link:
            self.logger.warning("Aplikasi link not found (may already be on correct page)")
            return

        async with self.page.expect_navigation(wait_until='domcontentloaded', timeout=15000):
            await link.click()

        self.logger.info("Navigated to Aplikasi")

    async def step4_select_pengurusan_murid(self):
        """Step 4: Select 'Pengurusan Murid' and navigate to MOEIS."""
        self.logger.info("STEP 4: Selecting Pengurusan Murid...")

        await self.page.wait_for_load_state('domcontentloaded', timeout=10000)
        await self.page.wait_for_timeout(1000)

        link = await self.page.query_selector('a:has-text("Pengurusan Murid")')
        if not link:
            self.logger.warning("Pengurusan Murid link not found")
            return

        href = await link.get_attribute('href')
        if not href or href == 'javascript: void(0);':
            self.logger.warning("Pengurusan Murid has no valid URL")
            return

        self.logger.info(f"SSO URL: {href[:80]}...")

        # SSO hop: this establishes the MOEIS session cookie in the browser
        # context. The moeispel home page never reaches a settled load state
        # (it perpetually re-polls failing http:// dashboard endpoints), so we
        # only need it to commit. We then open the attendance page on a FRESH
        # page in the same context — navigating away from the stuck home
        # document directly raises NS_ERROR_FAILURE.
        sso_response = None
        try:
            sso_response = await self.page.goto(
                href, wait_until='domcontentloaded', timeout=30000)
        except PlaywrightError as e:
            self.logger.warning(f"SSO landing reported {e}; continuing on fresh page")
        # A 5xx here is the gateway (Imperva) failing to reach MOEIS. Its
        # error page is served on the moeispel URL, so the 'moeispel' check
        # below would pass and we'd wait ~35s more for a table that can never
        # load (2026-11-10: every class failed that way). Fail fast with the
        # real cause. 4xx is left to the attendance-page check below.
        if sso_response is not None and sso_response.status >= 500:
            raise MOEISUnavailableError(
                await moeis_http_error(sso_response, "the SSO hop"))
        await self.page.wait_for_timeout(2000)
        self.logger.info(f"On MOEIS: {self.page.url}")

        if 'moeispel' not in self.page.url:
            # The SSO redirect sometimes lands back on the IDME app picker
            # (idme.moe.gov.my/list_aplikasi) instead of completing through to
            # MOEIS — a transient, per-class bounce. Fail loudly here rather than
            # returning silently: a silent return let login_and_navigate continue
            # through STEP 5/6 and report success, so the orchestrator then burned
            # the 10s fill timeout on a page with no attendance table. Raising
            # makes this a fast 'failed' the run-level retry can recover. NOT a
            # NonSchoolDayError — that is school-wide and would skip every
            # remaining class; an SSO bounce is local to this login.
            raise LoginEngineError(
                f"Not on MOEIS after SSO (landed on {self.page.url}); "
                "cannot open attendance page"
            )

        # Fresh page in the same context (shares the auth cookies set above).
        attendance_page = await self.context.new_page()
        attendance_response = await attendance_page.goto(
            self.ATTENDANCE_URL, wait_until='domcontentloaded', timeout=30000
        )
        # Without the attendance document there is no form, so any HTTP error
        # here is final for this login — don't wait 20s for input.case-hadir.
        error = await moeis_http_error(attendance_response, "the attendance page")
        if error:
            await attendance_page.close()
            raise MOEISUnavailableError(error)

        # The portal hardcodes http:// AJAX URLs. With HTTPS-Only mode each one
        # takes a cross-scheme 307 to https, and the browser strips the
        # X-CSRF-TOKEN header across that redirect -> HTTP 419 (Page Expired)
        # and an empty student table. Rewrite jQuery AJAX URLs to https BEFORE
        # they are sent so the CSRF header survives.
        await attendance_page.evaluate(
            """() => {
                if (window.jQuery) {
                    jQuery.ajaxPrefilter(function(opts) {
                        if (opts.url) {
                            opts.url = opts.url.replace(/^http:\\/\\/moeispel/i,
                                                        'https://moeispel');
                        }
                    });
                }
            }"""
        )

        # Student rows load via AJAX on the class-select 'change' event, which
        # does not fire for the pre-selected class — trigger it explicitly.
        await attendance_page.evaluate(
            """() => {
                if (window.jQuery && jQuery('#txtNamakelas').length) {
                    jQuery('#txtNamakelas').trigger('change');
                }
            }"""
        )
        await attendance_page.wait_for_selector('input.case-hadir', timeout=20000)

        # Detect holiday/weekend: the portal shows a "Tarikh semasa tidak tersedia"
        # modal and silently shifts the form to the previous working day.
        # Detect this before filling so we never submit for the wrong date.
        # The guard must fail SAFE: presence of the heading alone means
        # non-school day, regardless of whether we can extract a message body
        # (an empty/missing <p> must NOT let us fall through and submit).
        holiday = await attendance_page.evaluate("""() => {
            const h = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                .find(el => el.textContent.includes('Tarikh semasa tidak tersedia'));
            if (!h) return {detected: false, message: ''};
            const p = h.parentElement && h.parentElement.querySelector('p');
            const message = (p && p.textContent.trim()) || h.textContent.trim();
            return {detected: true, message};
        }""")
        backfill = bool(self.target_date) and self.target_date != date.today().isoformat()
        if holiday['detected']:
            if not backfill:
                raise NonSchoolDayError(
                    holiday['message'] or 'Tarikh semasa tidak tersedia'
                )
            # The modal is about TODAY's default date; an explicit past date is
            # selected and verified below. Dismiss it so it can't cover the form.
            self.logger.info(
                f"'Tarikh semasa tidak tersedia' for today; continuing to "
                f"explicit target date {self.target_date}")
            await attendance_page.evaluate("""() => {
                document.querySelectorAll('.sweet-alert .confirm, .swal2-confirm')
                    .forEach(b => b.click());
                if (window.swal && typeof window.swal.close === 'function') window.swal.close();
            }""")
            await attendance_page.wait_for_timeout(500)

        if backfill:
            await self._select_target_date(attendance_page)

        # Hand the attendance page to the remaining steps (CSRF, cookies,
        # form filling all operate on self.page). Close the stuck home page so
        # its failing dashboard polling stops churning the network.
        old_page = self.page
        self.page = attendance_page
        try:
            await old_page.close()
        except Exception:
            pass
        self.logger.info(f"On attendance page: {self.page.url}")

    async def _select_target_date(self, page: Page):
        """Switch the attendance form to self.target_date and verify it took.

        Mirrors the portal's own '.sahHarianBtn' handler:
        ``$('#tkh_HH').val(date).change()`` reloads the student table via
        POST ajaxloadkehadiranharian with ``tarikhpilihan=<DD/MM/YYYY>``.
        Fails SAFE: any doubt about which date the form shows raises, so the
        caller never ticks a checkbox on the wrong day. Also refuses a day the
        portal already shows as confirmed (TELAH DISAHKAN, locked).
        """
        want = datetime.strptime(self.target_date, '%Y-%m-%d').strftime('%d/%m/%Y')
        self.logger.info(f"Selecting attendance date {want} (#tkh_HH)")
        if not await page.evaluate(
                "() => !!(window.jQuery && jQuery('#tkh_HH').length)"):
            raise NavigationError("TARIKH field #tkh_HH not found on attendance page")

        def _is_reload_for_date(resp) -> bool:
            if 'ajaxloadkehadiranharian' not in resp.url:
                return False
            body = unquote_plus(resp.request.post_data or '')
            return f'tarikhpilihan={want}' in body

        try:
            async with page.expect_response(_is_reload_for_date, timeout=30000) as info:
                await page.evaluate(
                    "(d) => { jQuery('#tkh_HH').val(d).change(); }", want)
            resp = await info.value
        except PlaywrightTimeout:
            raise NavigationError(
                f"Student table did not reload for {want} after setting #tkh_HH")
        if resp.status != 200:
            raise NavigationError(
                f"Student table reload for {want} returned HTTP {resp.status}")
        # The portal silently substitutes the nearest available day for a
        # non-school day (e.g. asked 30/10, answered tarikhpilihan=2026-10-29,
        # statushari=1). Trust the server's answer, not our request.
        try:
            served = (await resp.json()).get('tarikhpilihan')
        except Exception:
            served = None
        if served != self.target_date:
            raise NavigationError(
                f"Portal served attendance for {served!r}, not requested "
                f"{self.target_date!r} (non-school day or unavailable); refusing to continue")

        # DataTables renders after the AJAX callback; give it a moment, then
        # require rows and re-check the field still holds the requested date
        # (the portal's validator resets it to the last valid date on error).
        await page.wait_for_timeout(1500)
        await page.wait_for_selector('input.case-hadir', timeout=20000)
        shown = await page.evaluate("() => jQuery('#tkh_HH').val()")
        if shown != want:
            raise NavigationError(
                f"Attendance form shows date {shown!r}, expected {want!r}; refusing to continue")

        # The day's own status badge (#statusBadge) — not any table cell, which
        # could belong to another day in the monthly view.
        status = await page.evaluate('''() => {
            const b = document.querySelector('#statusBadge');
            return b ? (b.textContent || '').trim() : '';
        }''')
        self.target_date_status = status
        if status == 'TELAH DISAHKAN':
            raise AlreadyConfirmedError(
                f"Attendance for {want} is already confirmed (TELAH DISAHKAN); "
                "locked days are not edited by automation")
        self.logger.info(f"Attendance form now on {want} (status: {status or 'none'})")

    async def step5_extract_csrf_token(self):
        """Step 5: Extract CSRF token from the page."""
        self.logger.info("STEP 5: Extracting CSRF token...")

        for attempt in range(5):
            token = await self.page.evaluate("""
                () => {
                    let meta = document.querySelector('meta[name="_token"]') ||
                              document.querySelector('meta[name="csrf-token"]');
                    if (meta) return meta.content || meta.getAttribute('content');

                    let input = document.querySelector('input[name="_token"]') ||
                               document.querySelector('input[name="csrf_token"]');
                    if (input) return input.value;

                    return null;
                }
            """)

            if token:
                self.csrf_token = token
                self.logger.info(f"CSRF token found: {token[:20]}...")
                return

            self.logger.info(f"Token not found, retry {attempt + 1}/5")
            await asyncio.sleep(2)

        self.logger.warning("CSRF token not found after retries")

    async def step6_extract_cookies(self):
        """Step 6: Extract cookies from browser context."""
        self.logger.info("STEP 6: Extracting cookies...")

        self.cookies = await self.context.cookies()
        self.logger.info(f"Extracted {len(self.cookies)} cookies")

        # Retry CSRF extraction if not found in step 5
        if not self.csrf_token:
            self.csrf_token = await self.page.evaluate("""
                () => {
                    let meta = document.querySelector('meta[name="_token"]') ||
                              document.querySelector('meta[name="csrf-token"]');
                    if (meta) return meta.content;
                    let input = document.querySelector('input[name="_token"]');
                    return input ? input.value : null;
                }
            """)

    async def login_and_navigate(self) -> Dict[str, Any]:
        """
        Run the complete 6-step login and return session data.

        The page and browser are kept OPEN for form filling.
        Caller must call close() when done.

        Returns:
            {
                'page': Page,          # Playwright page (on attendance form)
                'cookies': [...],
                'csrf_token': 'abc123',
                'success': True,
            }

        Raises:
            LoginEngineError: If login fails.
        """
        start = datetime.now()
        self.logger.info("=" * 50)
        self.logger.info("Starting IDME login automation")
        self.logger.info(f"IC: {self.ic_number}")
        self.logger.info("=" * 50)

        try:
            await self._initialize_browser()
            await self.step1_login()
            await self.step2_verification()
            await self.step3_navigate_aplikasi()
            await self.step4_select_pengurusan_murid()
            await self.step5_extract_csrf_token()
            await self.step6_extract_cookies()

            duration = (datetime.now() - start).total_seconds()
            self.logger.info("=" * 50)
            self.logger.info(f"LOGIN SUCCESSFUL ({duration:.1f}s)")
            self.logger.info(f"Cookies: {len(self.cookies)}")
            self.logger.info(f"CSRF: {'Yes' if self.csrf_token else 'No'}")
            self.logger.info("=" * 50)

            return {
                'page': self.page,
                'context': self.context,
                'cookies': self.cookies,
                'csrf_token': self.csrf_token,
                'success': True,
                'duration': duration,
            }

        except NonSchoolDayError:
            # Normal operating condition (weekend / public holiday), not a login
            # failure. Don't emit a LOGIN FAILED log or error_final screenshot:
            # during the supervised rollout those are the operator's gating
            # signal and a daily false alarm would pollute it.
            duration = (datetime.now() - start).total_seconds()
            self.logger.info(
                f"Non-school day detected after login ({duration:.1f}s) — "
                f"skipping submission"
            )
            await self.close()
            raise

        except AlreadyConfirmedError as e:
            # The day is locked on the portal (confirmed outside automation).
            # Login itself succeeded — no LOGIN FAILED log or error screenshot.
            duration = (datetime.now() - start).total_seconds()
            self.logger.info(f"Day already confirmed on portal ({duration:.1f}s): {e}")
            await self.close()
            raise

        except Exception as e:
            duration = (datetime.now() - start).total_seconds()
            self.logger.error(f"LOGIN FAILED ({duration:.1f}s): {e}")
            await self._take_screenshot("error_final")
            await self.close()
            raise

    async def close(self):
        """Close browser and cleanup resources."""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.logger.debug("Browser closed")
        except Exception as e:
            self.logger.warning(f"Cleanup error: {e}")
