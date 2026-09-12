import tempfile
import unittest
from pathlib import Path

from academy.access import PUBLIC, AKENSO, SUPERVISOR, get_role, set_role, has_company_access, is_supervisor


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'academy.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_is_default(self):
        self.assertEqual(get_role(self.path, 101), PUBLIC)
        self.assertFalse(has_company_access(self.path, 101))

    def test_akenso_access_persists(self):
        set_role(self.path, 101, AKENSO)
        self.assertEqual(get_role(self.path, 101), AKENSO)
        self.assertTrue(has_company_access(self.path, 101))
        self.assertFalse(is_supervisor(self.path, 101))

    def test_supervisor_access_persists(self):
        set_role(self.path, 101, SUPERVISOR)
        self.assertEqual(get_role(self.path, 101), SUPERVISOR)
        self.assertTrue(has_company_access(self.path, 101))
        self.assertTrue(is_supervisor(self.path, 101))

    def test_public_revokes_private_access(self):
        set_role(self.path, 101, AKENSO)
        set_role(self.path, 101, PUBLIC)
        self.assertEqual(get_role(self.path, 101), PUBLIC)

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            set_role(self.path, 101, 'unknown')


if __name__ == '__main__':
    unittest.main()
