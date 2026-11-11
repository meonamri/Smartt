"""
Unit tests: Smartt logs must not carry teacher IC numbers or SSO session tokens.

On 2026-11-10 every failed IDME login wrote the teacher's full IC number and the
start of the SSO URL (token_idms=...) to smartt.log, which was world-readable.
These tests pin the redaction helpers and the 0600 log-file mode.

No network, no portal, no real credentials. Run directly
(`python test_log_redaction.py`) or under pytest.
"""

import logging
import os
import stat
import tempfile
import unittest

from src.idme.login_engine import mask_ic, redact_url

FAKE_IC = '900101015555'


class MaskIcTest(unittest.TestCase):
    def test_keeps_only_last_four(self):
        self.assertEqual(mask_ic(FAKE_IC), '********5555')
        self.assertNotIn('900101', mask_ic(FAKE_IC))

    def test_short_or_empty_is_fully_masked(self):
        self.assertEqual(mask_ic(''), '****')
        self.assertEqual(mask_ic(None), '****')
        self.assertEqual(mask_ic('1234'), '****')


class RedactUrlTest(unittest.TestCase):
    def test_sso_token_removed(self):
        url = 'https://moeispel.moe.gov.my/sso/login?token_idms=abc.DEF-123&x=1#frag'
        out = redact_url(url)
        self.assertEqual(out, 'https://moeispel.moe.gov.my/sso/login?<redacted>')
        self.assertNotIn('token_idms', out)
        self.assertNotIn('abc.DEF', out)

    def test_userinfo_and_port_dropped(self):
        out = redact_url('https://user:pw@idme.moe.gov.my:8443/list_aplikasi')
        self.assertEqual(out, 'https://idme.moe.gov.my/list_aplikasi')

    def test_plain_url_unchanged_apart_from_normalisation(self):
        self.assertEqual(redact_url('https://idme.moe.gov.my/home'),
                         'https://idme.moe.gov.my/home')

    def test_relative_and_empty(self):
        self.assertEqual(redact_url('/sso?token_idms=zzz'), '/sso?<redacted>')
        self.assertEqual(redact_url(''), '')
        self.assertEqual(redact_url(None), 'None')


class LoginLogLinesTest(unittest.TestCase):
    """Every logger/exception line in login_engine that prints a URL or IC
    must go through the helpers."""

    def test_no_raw_url_or_ic_in_log_calls(self):
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, 'src', 'idme', 'login_engine.py')).read()
        for bad in ('{self.ic_number}', '{self.page.url}', '{url}', '{href[:80]}', '{href}'):
            self.assertNotIn(bad, src, f'raw value still formatted into a message: {bad}')


class LogFileModeTest(unittest.TestCase):
    def test_log_file_and_rotations_are_owner_only(self):
        import smartt
        old_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, 'smartt.log')
                h = smartt.PrivateRotatingFileHandler(path, maxBytes=200, backupCount=2)
                log = logging.getLogger('redaction-test')
                log.propagate = False
                log.addHandler(h)
                try:
                    for i in range(20):
                        log.warning('line %d %s', i, 'x' * 40)
                finally:
                    log.removeHandler(h)
                    h.close()
                files = sorted(f for f in os.listdir(d) if f.startswith('smartt.log'))
                self.assertGreaterEqual(len(files), 2, 'rotation did not happen')
                for f in files:
                    mode = stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode)
                    self.assertEqual(mode, 0o600, f'{f} has mode {oct(mode)}')
        finally:
            os.umask(old_umask)


if __name__ == '__main__':
    unittest.main()
