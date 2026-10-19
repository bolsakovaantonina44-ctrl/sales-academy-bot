import unittest

from academy.domain import reduce_plan, session_empty
from academy.pacing import apply_behavior, prepare_card
from academy.scenarios import template
import test_core as core


class ObjectionTimingTests(unittest.TestCase):
    def test_price_objection_waits_for_offer_context(self):
        session = session_empty()
        session['fields']['difficulty'] = 'hard'
        session['card'] = prepare_card(template('1')['card'], 'hard', 'price')

        first = core.plan(intent='role', action='question')
        session['state'] = apply_behavior(
            session, first, reduce_plan(session['state'], first, session['card'])
        )
        self.assertEqual(session['state']['required_objection'], '')

        second = core.plan(intent='other', action='monologue')
        session['_current_manager_text'] = 'Мы поставляем керамогранит напрямую с заводов.'
        session['state'] = apply_behavior(
            session, second, reduce_plan(session['state'], second, session['card'])
        )
        self.assertEqual(session['state']['required_objection'], '')

        third = core.plan(intent='other', action='monologue')
        session['_current_manager_text'] = 'Стоимость зависит от объёма, можем подготовить коммерческое предложение.'
        session['state'] = apply_behavior(
            session, third, reduce_plan(session['state'], third, session['card'])
        )
        self.assertEqual(session['state']['required_objection'], 'Дорого.')


class ClientVoicesObjectionTests(unittest.TestCase):
    def _payload(self, required, focus='b1', suppress=False):
        from academy.ai import AI
        ai = AI.__new__(AI)
        seen = {}

        def fake_request(name, instructions, payload, schema, model=None, max_output_tokens=6000):
            seen.update(name=name, instructions=instructions, payload=payload)
            return {'reply': 'Сейчас не до этого.', 'used_fact_ids': []}

        ai.request = fake_request
        session = session_empty()
        session['fields'].update(difficulty='hard', product='Тренажёр')
        session['card'] = {'role': 'Директор', 'behavior_type': 'busy', 'identity': {'name': 'Анна'},
                           'facts': [], 'unknown': [], 'barriers': [{'id': 'b1', 'text': 'Сейчас сезон, не до обучения.'}]}
        state = {'trust': 2, 'interest': 2, 'last_action': 'question', 'close': 'continue', 'agreement': '',
                 'revealed': [], 'required_objection': required, 'focus_issue_id': focus,
                 'suppress_issue_repeat': suppress}
        AI.reply(ai, session, 'Можем начать с пробы.', state)
        return seen

    def test_required_objection_text_reaches_client_and_must_be_voiced(self):
        seen = self._payload('Сейчас сезон, не до обучения.')
        self.assertEqual(seen['name'], 'client_reply')
        self.assertEqual(seen['payload']['required_objection'], 'Сейчас сезон, не до обучения.')
        self.assertIn('обязательно выскажи именно это сомнение', seen['instructions'])
        self.assertNotIn('программа добавит отдельно', seen['instructions'])

    def test_open_barrier_stays_as_context_when_no_new_objection(self):
        seen = self._payload('')
        self.assertEqual(seen['payload']['required_objection'], '')
        self.assertEqual(seen['payload']['active_barrier'], 'Сейчас сезон, не до обучения.')
        self.assertEqual(self._payload('', suppress=True)['payload']['active_barrier'], '')


if __name__ == '__main__':
    unittest.main()
