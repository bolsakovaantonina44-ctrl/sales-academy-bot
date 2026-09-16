import unittest

from academy.safety_guards import (
    _TERMINAL_MARKERS,
    _unsupported_numbers,
    _unsupported_proper_names,
    contact_request_exists,
    ground_reply,
)


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

    def test_goal_does_not_count_as_manager_contact_request(self):
        session = {
            'fields': {'goal': 'Получить личные контакты, мессенджер и номер телефона'},
            'history': [{'role': 'user', 'content': 'Мы поставляем керамогранит.'}],
        }
        self.assertFalse(contact_request_exists(session, 'Работаем напрямую с заводами.'))
        reply = 'А зачем вам мои личные контакты? Личный номер я не передаю.'
        self.assertEqual(ground_reply(session, 'Работаем напрямую с заводами.', reply),
                         'Что конкретно вы предлагаете?')

    def test_contact_refusal_is_allowed_after_actual_request(self):
        session = {'history': []}
        manager = 'Оставьте, пожалуйста, ваш номер телефона для оперативной связи.'
        reply = 'Личный номер я незнакомым поставщикам не передаю.'
        self.assertTrue(contact_request_exists(session, manager))
        self.assertEqual(ground_reply(session, manager, reply), reply)

    def test_price_objection_is_removed_before_price_context(self):
        session = {'history': [{'role': 'user', 'content': 'Мы поставляем керамогранит.'}]}
        reply = 'Предложение должно быть конкурентным по цене. Что именно вы предлагаете? Дорого.'
        self.assertEqual(ground_reply(session, 'Работаем напрямую с заводами.', reply),
                         'Что именно вы предлагаете?')

    def test_duplicate_client_sentence_is_removed(self):
        session = {'history': []}
        manager = 'Можно ваш номер телефона?'
        reply = 'Личный номер я не передаю. Личный номер я не передаю.'
        self.assertEqual(ground_reply(session, manager, reply), 'Личный номер я не передаю.')


if __name__ == '__main__':
    unittest.main()
