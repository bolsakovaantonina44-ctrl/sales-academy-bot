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
        session['state'] = apply_behavior(
            session, second, reduce_plan(session['state'], second, session['card'])
        )
        self.assertEqual(session['state']['required_objection'], 'Дорого.')


if __name__ == '__main__':
    unittest.main()
