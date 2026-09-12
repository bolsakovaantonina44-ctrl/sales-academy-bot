import unittest

from academy.safety_guards import _unsupported_numbers, _unsupported_proper_names, _TERMINAL_MARKERS


class SafetyGuardTests(unittest.TestCase):
    def test_rejects_invented_percentage(self):
        source = 'Менеджер сказал, что продукт увеличит продажи.'
        reply = 'На чём основана оценка роста продаж на 50%?'
        self.assertEqual(_unsupported_numbers(reply, source), {'50%'})

    def test_allows_number_already_said_by_manager(self):
        source = 'Менеджер сказал: рост продаж на 50%.'
        reply = 'На чём основана оценка роста продаж на 50%?'
        self.assertEqual(_unsupported_numbers(reply, source), set())

    def test_rejects_invented_person_name(self):
        source = 'Клиент: компания, которая продаёт мерч.'
        reply = 'Да, этим занимается Марина. Сейчас соединю.'
        self.assertIn('Марина', _unsupported_proper_names(reply, source))

    def test_allows_grounded_person_name(self):
        source = 'Контактное лицо Марина, руководитель отдела продаж.'
        reply = 'Да, этим занимается Марина. Сейчас соединю.'
        self.assertEqual(_unsupported_proper_names(reply, source), [])

    def test_terminal_marker_covers_live_case(self):
        text = 'Тогда на этом закончим разговор.'.lower()
        self.assertTrue(any(marker in text for marker in _TERMINAL_MARKERS))


if __name__ == '__main__':
    unittest.main()
