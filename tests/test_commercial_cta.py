import json
import os
import tempfile
import unittest
from pathlib import Path

from academy.store import Store


class CommercialCtaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'academy.sqlite3')
        self.old = {k: os.environ.get(k) for k in ('FREE_TRAININGS', 'ADMIN_IDS', 'COMMERCIAL_CONTACT')}
        os.environ['FREE_TRAININGS'] = '3'
        os.environ['ADMIN_IDS'] = ''
        os.environ['COMMERCIAL_CONTACT'] = '@owner_test'

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _seed(self, user_id, phases):
        store = Store(self.path)
        with store.db() as db:
            for phase in phases:
                payload = {'phase': phase}
                db.execute(
                    'INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,1)',
                    (user_id, json.dumps(payload)),
                )
        return store

    def test_backfill_queues_once_after_three_completed(self):
        self._seed(101, ['completed', 'completed', 'completed'])
        store = Store(self.path)
        pending = store.outgoing(101)
        self.assertEqual(len(pending), 1)
        self.assertIn('@owner_test', pending[0]['body'])
        Store(self.path)
        self.assertEqual(len(store.outgoing(101)), 1)

    def test_incomplete_sessions_do_not_trigger_backfill(self):
        self._seed(202, ['completed', 'completed', 'active'])
        store = Store(self.path)
        self.assertEqual(store.outgoing(202), [])


if __name__ == '__main__':
    unittest.main()
