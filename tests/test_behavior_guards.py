import unittest
from unittest.mock import Mock

from academy.ai import AI
from academy.domain import reduce_plan, session_empty
from academy.pacing import FOCUS_OBJECTIONS, apply_behavior, prepare_card
from academy.scenarios import template


def plan(**changes):
    value = dict(
        intent='other', issue_updates=[], focus_issue_id='', action='question',
        reveal_ids=[], resolved_ids=[], trust_delta=0, interest_delta=0,
        close='continue', next_step_requested=False, agreement='', reason='test',
    )
    value.update(changes)
    return value


def session(focus='price', difficulty='hard'):
    s = session_empty()
    s['fields'].update(product='Услуга', customer='Директор', goal='Согласовать встречу', difficulty=difficulty)
    s['card'] = prepare_card(template('1')['card'], difficulty, focus)
    return s


class BehaviorGuardTests(unittest.TestCase):
    def test_contextual_objections_wait_until_offer_exists(self):
        for focus in ('price', 'no_need', 'supplier', 'send_info'):
            s = session(focus, 'hard')
            first = plan(intent='role', action='question')
            state = apply_behavior(s, first, reduce_plan(s['state'], first, s['card']))
            self.assertEqual(state['required_objection'], '', focus)

            s['state'] = state
            offer = plan(intent='other', action='monologue')
            state = apply_behavior(s, offer, reduce_plan(s['state'], offer, s['card']))
            self.assertEqual(state['required_objection'], FOCUS_OBJECTIONS[focus]['text'], focus)

    def test_no_time_can_be_immediate(self):
        s = session('no_time', 'hard')
        first = plan(intent='role', action='question')
        state = apply_behavior(s, first, reduce_plan(s['state'], first, s['card']))
        self.assertEqual(state['required_objection'], FOCUS_OBJECTIONS['no_time']['text'])

    def test_same_issue_is_suppressed_after_two_mentions_without_reopen_reason(self):
        s = session('no_need', 'easy')
        issue_id = s['card']['barriers'][0]['id']
        current = s['state']
        for expected_mentions, expected_suppressed in ((1, False), (2, False), (2, True)):
            p = plan(
                intent='objection', action='objection_work', focus_issue_id=issue_id,
                issue_updates=[dict(id=issue_id, status='open', reopen_reason='none')],
            )
            current = reduce_plan(current, p, s['card'])
            self.assertEqual(current['issue_mentions'][issue_id], expected_mentions)
            self.assertEqual(current['suppress_issue_repeat'], expected_suppressed)

    def test_success_is_blocked_before_required_barrier_is_encountered(self):
        s = session('price', 'easy')
        issue_id = s['card']['barriers'][0]['id']
        p = plan(
            intent='next_step', action='next_step', close='success',
            next_step_requested=True, agreement='Встреча завтра', resolved_ids=[issue_id],
        )
        reduced = reduce_plan(s['state'], p, s['card'])
        self.assertEqual(reduced['close'], 'success')
        guarded = apply_behavior(s, p, reduced)
        self.assertEqual(guarded['close'], 'continue')
        self.assertEqual(guarded['agreement'], '')

    def test_suppressed_issue_is_not_sent_to_client_writer(self):
        s = session('no_need', 'easy')
        issue_id = s['card']['barriers'][0]['id']
        state = dict(s['state'])
        state.update(
            revealed=[], focus_issue_id=issue_id, suppress_issue_repeat=True,
            required_objection='', wrap_up=False, ending_reason='', last_action='question',
        )
        ai = AI(None, 'fake', 'fake')
        ai.request = Mock(return_value=dict(reply='Да, слушаю.', used_fact_ids=[]))
        ai.reply(s, 'Продолжу.', state)
        payload = ai.request.call_args.args[2]
        self.assertEqual(payload['active_barrier'], '')


if __name__ == '__main__':
    unittest.main()
