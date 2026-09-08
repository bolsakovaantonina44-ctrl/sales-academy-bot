import copy
import unittest
from unittest.mock import Mock
from academy.ai import AI
from academy.domain import attach_evidence, EVAL_MODEL_SCHEMA, EvaluationError, RUBRIC_VERSION, check_evaluation, render_report
import test_core as core
evaluation = core.evaluation


class EvaluationTests(unittest.TestCase):
    setUp = core.CoreTests.setUp
    tearDown = core.CoreTests.tearDown
    event = core.CoreTests.event
    send = core.CoreTests.send
    start = core.CoreTests.start
    talk = core.CoreTests.talk
    def test_quotes_are_copied_from_history_without_model_rephrasing(self):
        s=self.talk();d=evaluation(s)
        for field in ('skills','strengths','mistakes','recommendations'):
            for item in d[field]:
                for ref in item['evidence']:ref.pop('quote',None)
        result=attach_evidence(d,s['history'])
        check_evaluation(result,s)
        self.assertEqual(result['skills'][0]['evidence'][0]['quote'],s['history'][1]['content'])
        props=EVAL_MODEL_SCHEMA['properties']['skills']['items']['properties']['evidence']['items']['properties']
        self.assertNotIn('quote',props)

    def test_client_quote_cannot_be_presented_as_manager_quote(self):
        s = self.talk(); d = evaluation(s)
        d['strengths'][0]['evidence'] = [dict(message_id=1, speaker='manager', quote=s['history'][0]['content'])]
        with self.assertRaisesRegex(EvaluationError, 'speaker mismatch'):
            check_evaluation(d, s)

    def test_client_only_strength_or_score_is_rejected(self):
        s = self.talk()
        for field in ('strengths', 'skills', 'recommendations'):
            d = evaluation(s)
            d[field][0]['evidence'] = [dict(message_id=1, speaker='client', quote=s['history'][0]['content'])]
            with self.assertRaisesRegex(EvaluationError, 'without manager evidence'):
                check_evaluation(d, s)

    def test_proposed_followup_gets_credit_without_becoming_agreement(self):
        s = self.talk(); d = evaluation(s)
        d.update(next_step='Менеджер предложил связаться завтра; клиент не согласовал время.', next_step_status='proposed')
        check_evaluation(d, s)
        self.assertIn('предложен, но не согласован полностью', render_report(d, s))
        d['outcome'] = 2
        with self.assertRaisesRegex(EvaluationError, 'Outcome without agreed step'):
            check_evaluation(d, s)

    def test_recommendation_must_include_observable_check(self):
        s = self.talk(); d = evaluation(s); d['recommendations'][0]['success_check'] = ''
        with self.assertRaisesRegex(EvaluationError, 'Incomplete coaching task'):
            check_evaluation(d, s)

    def test_report_gives_manager_task_and_success_criterion(self):
        s = self.talk(); report = render_report(evaluation(s), s)
        for label in ('ПЛАН ДЛЯ РУКОВОДИТЕЛЯ', 'Задание сотруднику:', 'Как руководителю проверить:', 'Менеджер, реплика'):
            self.assertIn(label, report)

    def test_invalid_report_repaired_once_with_explicit_speakers(self):
        s = self.talk(); good = evaluation(s); bad = copy.deepcopy(good)
        bad['skills'][0]['evidence'][0]['speaker'] = 'client'
        ai = AI(None, 'fake', 'fake'); ai.request = Mock(side_effect=[bad, good, dict(passed=True, issues=[])])
        self.assertEqual(ai.evaluate(s), good)
        self.assertEqual(ai.request.call_count, 3)
        payload = ai.request.call_args_list[1].args[2]
        self.assertEqual([m['speaker'] for m in payload['history']], ['client', 'manager', 'client'])
        self.assertNotIn('role', payload['history'][0])
        self.assertIn('validation_feedback', payload)

    def test_semantic_attribution_review_rejects_wrong_narrative(self):
        s = self.talk(); d = evaluation(s)
        d['strengths'][0]['text'] = 'Менеджер ответил о потребности клиента'
        ai = AI(None, 'fake', 'fake')
        ai.request = Mock(side_effect=[d, dict(passed=False, issues=['strengths: действие клиента']),
                                      evaluation(s), dict(passed=True, issues=[])])
        self.assertEqual(ai.evaluate(s)['strengths'][0]['text'], 'Вопрос')
        self.assertEqual(ai.request.call_count, 4)
        repaired_payload = ai.request.call_args_list[2].args[2]
        self.assertEqual(repaired_payload['rejected_report']['strengths'][0]['text'], d['strengths'][0]['text'])
        self.assertEqual(repaired_payload['review_feedback'], ['strengths: действие клиента'])

    def test_invalid_report_never_published_after_retry_limit(self):
        s = self.talk(); ai = AI(None, 'fake', 'fake')
        ai.request = Mock(side_effect=EvaluationError('Incomplete model response'))
        with self.assertRaises(EvaluationError): ai.evaluate(s)
        self.assertEqual(ai.request.call_count, 2)

    def test_duplicate_finish_returns_saved_report_without_regeneration(self):
        self.talk(); done = self.send('/finish')
        self.ai.evaluate = Mock(side_effect=AssertionError('Must not evaluate again'))
        again = self.send('/finish')
        self.assertEqual(again['report'], done['report'])
        self.assertEqual(self.store.attempts(10), 1)

    def test_failed_finish_can_be_retried_by_finish_button(self):
        before = self.talk(); self.ai.fail = True; event = self.event('/finish')
        self.engine.handle(event)
        self.assertEqual(self.store.current(10)['report_status'], 'technical_partial')
        self.ai.fail = False; done = self.send('/recheck')
        self.assertEqual(done['phase'], 'completed')
        self.assertEqual(done['history'], before['history'])
        self.assertEqual(self.store.attempts(10), 1)
        self.assertIsNone(self.store.failed(10))

    def test_recheck_old_report_keeps_history_and_usage(self):
        self.talk(); old = self.send('/finish')
        old['versions']['rubric'] = 'skills-8-v1'
        old['report'] = 'Старый отчёт'
        self.store.commit(self.event('test setup'), old, [])
        done = self.send('Обновить разбор')
        self.assertEqual(done['versions']['rubric'], RUBRIC_VERSION)
        self.assertEqual(done['history'], old['history'])
        self.assertEqual(done['id'], old['id'])
        self.assertEqual(self.store.attempts(10), 1)


if __name__ == '__main__': unittest.main()
