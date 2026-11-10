"""STEP 4 fails fast when MOEIS answers with an HTTP error.

2026-11-10: Imperva could not reach the MOEIS origin and served its 503
"Connection Failed, Error 20" page on the moeispel URL. The old 'moeispel in
url' check passed, so every class waited ~35s more and failed with a misleading
"Timeout waiting for input.case-hadir". These tests drive step4 with fake
Playwright objects — no browser, no network.
"""
import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from src.idme.login_engine import (
    IDMELoginEngine, LoginEngineError, MOEISUnavailableError, NavigationError,
    moeis_http_error,
)
from src.idme.orchestrator import IDMEOrchestrator

SSO_HREF = 'https://moeispel.moe.gov.my?token_idms=abc&t=1&u=x'
IMPERVA_503 = (
    '<html><body><iframe id="main-iframe" src="/_Incapsula_Resource?CWUDNSAI=26'
    '&xinfo=5-1-0%20NNNN&incident_id=1134000790085428146-243886454471443&edet=20'
    '&cinfo=ffffffffe60d&cip=183.171.242.187&mth=GET">Request unsuccessful.'
    '</iframe></body></html>')


class FakeResponse:
    def __init__(self, status, url, body='', text_raises=False):
        self.status, self.url, self._body, self._raise = status, url, body, text_raises

    async def text(self):
        if self._raise:
            raise PlaywrightError('Response body is unavailable')
        return self._body


class FakeLink:
    async def get_attribute(self, name):
        return SSO_HREF


class FakePage:
    def __init__(self, url, goto_result=None, goto_raises=None):
        self.url = url
        self.goto_result, self.goto_raises = goto_result, goto_raises
        self.selector_waits = []
        self.closed = False

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def query_selector(self, sel):
        return FakeLink()

    async def goto(self, url, **k):
        if self.goto_raises:
            raise self.goto_raises
        if self.goto_result is not None:
            self.url = self.goto_result.url
        else:
            self.url = url
        return self.goto_result

    async def evaluate(self, script, *args):
        if 'Tarikh semasa tidak tersedia' in script:
            return {'detected': False, 'message': ''}
        return None

    async def wait_for_selector(self, sel, **k):
        self.selector_waits.append(sel)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, attendance_page):
        self.attendance_page = attendance_page
        self.new_pages = 0

    async def new_page(self):
        self.new_pages += 1
        return self.attendance_page


def make_engine(sso_page, attendance_page):
    eng = IDMELoginEngine(ic_number='000000000000', password='x')
    eng.page = sso_page
    eng.context = FakeContext(attendance_page)
    return eng


def run(coro):
    return asyncio.run(coro)


def att_ok():
    return FakePage('about:blank', goto_result=FakeResponse(
        200, IDMELoginEngine.ATTENDANCE_URL))


# ---- fail fast -----------------------------------------------------------------

def test_sso_hop_503_fails_fast_with_imperva_detail():
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=FakeResponse(
        503, 'https://moeispel.moe.gov.my/?token_idms=abc', IMPERVA_503))
    att = att_ok()
    eng = make_engine(sso, att)
    with pytest.raises(MOEISUnavailableError) as exc:
        run(eng.step4_select_pengurusan_murid())
    msg = str(exc.value)
    assert msg.startswith('MOEIS unavailable: moeispel.moe.gov.my returned HTTP 503 on the SSO hop')
    assert 'Imperva error 20: Imperva could not connect to the MOEIS server' in msg
    assert 'incident 1134000790085428146-243886454471443' in msg
    # Never opened the attendance page, never waited for the table.
    assert eng.context.new_pages == 0 and att.selector_waits == []


def test_attendance_page_503_fails_fast_and_closes_the_page():
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=FakeResponse(
        200, 'https://moeispel.moe.gov.my/?token_idms=abc'))
    att = FakePage('about:blank', goto_result=FakeResponse(
        503, IDMELoginEngine.ATTENDANCE_URL, IMPERVA_503))
    eng = make_engine(sso, att)
    with pytest.raises(MOEISUnavailableError) as exc:
        run(eng.step4_select_pengurusan_murid())
    assert 'returned HTTP 503 on the attendance page' in str(exc.value)
    assert att.selector_waits == [] and att.closed


def test_attendance_page_403_fails_fast_but_is_not_retried():
    # e.g. a geo-block if traffic ever leaves Malaysia through the wrong exit node
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=FakeResponse(
        200, 'https://moeispel.moe.gov.my/'))
    att = FakePage('about:blank', goto_result=FakeResponse(
        403, IDMELoginEngine.ATTENDANCE_URL, '<html>Forbidden</html>'))
    eng = make_engine(sso, att)
    with pytest.raises(MOEISUnavailableError) as exc:
        run(eng.step4_select_pengurusan_murid())
    assert str(exc.value) == ('MOEIS unavailable: moeispel.moe.gov.my returned '
                              'HTTP 403 on the attendance page')
    assert not IDMEOrchestrator.is_transient_failure(str(exc.value))


def test_sso_hop_4xx_defers_to_the_attendance_page_check():
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=FakeResponse(
        404, 'https://moeispel.moe.gov.my/'))
    eng = make_engine(sso, att_ok())
    run(eng.step4_select_pengurusan_murid())   # unchanged: 4xx on the hop is not fatal
    assert eng.page is eng.context.attendance_page


def test_error_is_a_navigation_and_login_engine_error():
    # login_and_navigate / the orchestrator handle LoginEngineError generically.
    assert issubclass(MOEISUnavailableError, NavigationError)
    assert issubclass(MOEISUnavailableError, LoginEngineError)


@pytest.mark.parametrize('error,expected', [
    ('MOEIS unavailable: moeispel.moe.gov.my returned HTTP 503 on the SSO hop '
     '(Imperva error 20: Imperva could not connect to the MOEIS server, incident 1-2)', True),
    ('Submission failed: MOEIS unavailable: moeispel.moe.gov.my returned HTTP 502 '
     'on the attendance page', True),
    ('MOEIS unavailable: moeispel.moe.gov.my returned HTTP 403 on the attendance page', False),
])
def test_retry_classification(error, expected):
    assert IDMEOrchestrator.is_transient_failure(error) is expected


# ---- unchanged healthy paths ---------------------------------------------------

def test_healthy_path_switches_to_attendance_page():
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=FakeResponse(
        200, 'https://moeispel.moe.gov.my/?token_idms=abc'))
    eng = make_engine(sso, att_ok())
    run(eng.step4_select_pengurusan_murid())
    assert eng.page is eng.context.attendance_page
    assert eng.page.selector_waits == ['input.case-hadir']
    assert sso.closed


def test_sso_goto_raising_still_continues_on_fresh_page():
    # DESIGN_NOTES §4: the moeispel home document may never settle / may raise.
    sso = FakePage('https://moeispel.moe.gov.my/?token_idms=abc',
                   goto_raises=PlaywrightError('NS_ERROR_FAILURE'))
    eng = make_engine(sso, att_ok())
    run(eng.step4_select_pengurusan_murid())
    assert eng.page is eng.context.attendance_page


def test_none_responses_are_not_errors():
    sso = FakePage('https://idme.moe.gov.my/list_aplikasi', goto_result=None)
    sso.goto_result = None
    att = FakePage('about:blank', goto_result=None)
    eng = make_engine(sso, att)
    sso.url = 'https://moeispel.moe.gov.my/'
    run(eng.step4_select_pengurusan_murid())
    assert eng.page is att


def test_unreadable_body_still_reports_status():
    resp = FakeResponse(503, 'https://moeispel.moe.gov.my/', text_raises=True)
    assert run(moeis_http_error(resp, 'the SSO hop')) == (
        'MOEIS unavailable: moeispel.moe.gov.my returned HTTP 503 on the SSO hop')
    assert run(moeis_http_error(FakeResponse(200, 'https://x/'), 'x')) is None
    assert run(moeis_http_error(None, 'x')) is None
