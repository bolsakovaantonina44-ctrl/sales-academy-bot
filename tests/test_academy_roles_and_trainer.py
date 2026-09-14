import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from academy.access import AKENSO, SUPERVISOR, set_role
from academy import mobile_learning_runtime as runtime


class FakeBot:
    def __init__(self):
        self.send_message = Mock()


class AcademyRoleAndTrainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "academy.sqlite3")
        self.env = patch.dict(os.environ, {"DB_PATH": self.path, "ADMIN_IDS": "999"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old_handler = runtime._BOT_ON_TEXT_HANDLER
        self.addCleanup(setattr, runtime, "_BOT_ON_TEXT_HANDLER", self.old_handler)

    @staticmethod
    def _callbacks(markup):
        payload = markup.to_dict()["inline_keyboard"]
        return [button["callback_data"] for row in payload for button in row]

    def test_employee_home_does_not_show_team_management(self):
        set_role(self.path, 101, AKENSO)
        callbacks = self._callbacks(runtime._home_markup(101))
        self.assertIn("academyv2:training", callbacks)
        self.assertIn("academyv2:continue", callbacks)
        self.assertNotIn("team:list", callbacks)

    def test_supervisor_home_has_team_without_access_admin_screen(self):
        set_role(self.path, 202, SUPERVISOR)
        callbacks = self._callbacks(runtime._home_markup(202))
        self.assertIn("team:list", callbacks)
        self.assertNotIn("acc:list", callbacks)

    def test_regular_training_opens_existing_trainer_directly(self):
        bot = FakeBot()
        handler = Mock()
        runtime._BOT_ON_TEXT_HANDLER = handler
        runtime._open_training(bot, 101, exam=False)
        handler.assert_called_once()
        fake_message = handler.call_args.args[0]
        self.assertEqual(fake_message.chat.id, 101)
        self.assertEqual(fake_message.text, "Тренировка")

    def test_exam_requires_theory_and_then_marks_fresh_boundary(self):
        bot = FakeBot()
        handler = Mock()
        runtime._BOT_ON_TEXT_HANDLER = handler
        with patch.object(runtime, "_knowledge_complete", return_value=False), \
             patch.object(runtime.admission, "start_practical_exam") as start:
            runtime._open_training(bot, 101, exam=True)
            start.assert_not_called()
            handler.assert_not_called()

        bot.send_message.reset_mock()
        with patch.object(runtime, "_knowledge_complete", return_value=True), \
             patch.object(runtime.admission, "start_practical_exam") as start:
            runtime._open_training(bot, 101, exam=True)
            start.assert_called_once_with(self.path, 101)
            handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
