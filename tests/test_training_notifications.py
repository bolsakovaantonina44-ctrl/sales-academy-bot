import json
import tempfile
import unittest
from pathlib import Path

from academy.access import AKENSO, PUBLIC, set_role
from academy.domain import session_empty
from academy.store import Store
from bot import queue_training_reports


class TrainingNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'academy.sqlite3')
        self.store = Store(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _completed(self, user_id):
        session = session_empty()
        session['phase'] = 'completed'
        session['employee'] = {'id': user_id, 'name': 'Ирина Менеджер'}
        session['fields'].update(
            product='Керамогранит', customer='Закупщик', goal='Назначить встречу'
        )
        session['report_data'] = {'simulation_valid': False, 'technical_partial': True}
        with self.store.db() as db:
            session['id'] = db.execute(
                'INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,1)',
                (user_id, json.dumps(session, ensure_ascii=False)),
            ).lastrowid
            db.execute(
                'UPDATE sessions SET payload=? WHERE id=?',
                (json.dumps(session, ensure_ascii=False), session['id']),
            )
        return session

    def test_corporate_completion_queues_notice_and_supervisor_pdf_once(self):
        set_role(self.path, 10, AKENSO)
        session = self._completed(10)

        self.assertEqual(queue_training_reports(self.store, 10, session, [999]), 1)
        bodies = [item['body'] for item in self.store.outgoing(999)]
        self.assertEqual(len(bodies), 2)
        self.assertIn('Ирина Менеджер', bodies[0])
        self.assertIn('оценка требует проверки', bodies[0])
        self.assertEqual(bodies[1], f'__academy_pdf__:{session["id"]}:supervisor')

        self.assertEqual(queue_training_reports(self.store, 10, session, [999]), 0)
        self.assertEqual(len(self.store.outgoing(999)), 2)

    def test_public_completion_does_not_notify_admin(self):
        set_role(self.path, 10, PUBLIC)
        session = self._completed(10)
        self.assertEqual(queue_training_reports(self.store, 10, session, [999]), 0)
        self.assertEqual(self.store.outgoing(999), [])

    def test_employee_does_not_receive_own_supervisor_copy(self):
        set_role(self.path, 999, AKENSO)
        session = self._completed(999)
        self.assertEqual(queue_training_reports(self.store, 999, session, [999]), 0)
        self.assertEqual(self.store.outgoing(999), [])


if __name__ == '__main__':
    unittest.main()
