import ast
from pathlib import Path
import unittest


class AdminSessionReportTests(unittest.TestCase):
    def test_admin_viewer_has_direct_supervisor_report_action(self):
        source = (Path(__file__).resolve().parents[1] / 'telemetry_launcher.py').read_text(encoding='utf-8')
        ast.parse(source)
        self.assertIn('Отчёт руководителю {session_id}', source)
        self.assertIn('отчет руководителю\\s+#?\\s*(\\d+)', source)
        self.assertIn('_send_supervisor_report', source)
        self.assertIn('render_pdf(session, "supervisor")', source)

    def test_admin_session_card_uses_human_labels(self):
        source = (Path(__file__).resolve().parents[1] / 'telemetry_launcher.py').read_text(encoding='utf-8')
        self.assertIn('score_level(score)', source)
        self.assertIn('2 — средняя', source)
        self.assertIn('FOCUS_OBJECTIONS.get(focus', source)


if __name__ == '__main__':
    unittest.main()
