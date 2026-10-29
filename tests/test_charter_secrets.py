import tempfile
from pathlib import Path
import unittest

from charter.secrets import read_credentials


class CredentialTests(unittest.TestCase):
    def test_unknown_entries_remain_strict_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENROUTER_API_KEY="dummy"\nOTHER_SETTING=value\n')
            with self.assertRaises(ValueError):
                read_credentials(path)
            self.assertEqual(read_credentials(path, ignore_unknown=True),
                             {'OPENROUTER_API_KEY': 'dummy'})

    def test_ignore_unknown_does_not_accept_malformed_known_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENROUTER_API_KEY="unterminated\n')
            with self.assertRaises(ValueError):
                read_credentials(path, ignore_unknown=True)
