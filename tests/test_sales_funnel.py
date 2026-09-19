import os
import tempfile
import unittest
from pathlib import Path

from academy import commercial_cta
from academy.store import Store


class SalesFunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'academy.sqlite3')
        self.old_admins = os.environ.get('ADMIN_IDS')
        os.environ['ADMIN_IDS'] = ''
        Store(self.path)

    def tearDown(self):
        if self.old_admins is None:
            os.environ.pop('ADMIN_IDS', None)
        else:
            os.environ['ADMIN_IDS'] = self.old_admins
        self.tmp.cleanup()

    def test_company_questionnaire_persists_and_creates_lead(self):
        state = commercial_cta.start_company(
            self.path, 101, {'username': 'buyer', 'first_name': 'Иван'}
        )
        self.assertEqual(state['question']['key'], 'company_name')

        for value in ['ООО Тест', 'Производство', '16-50']:
            result = commercial_cta.answer_company(self.path, 101, value)

        state = commercial_cta.company_state(self.path, 101)
        self.assertTrue(state['question']['multiple'])
        commercial_cta.toggle_company_option(self.path, 101, 'onboarding')
        commercial_cta.toggle_company_option(self.path, 101, 'knowledge')
        state = commercial_cta.finish_company_multi(self.path, 101)
        self.assertEqual(state['question']['key'], 'knowledge')

        result = None
        for value in ['partial', 'Иван', '@buyer']:
            result = commercial_cta.answer_company(self.path, 101, value)

        self.assertTrue(result['finished'])
        self.assertEqual(result['data']['team_size'], '16-50')
        self.assertEqual(result['data']['goal'], ['onboarding', 'knowledge'])
        self.assertIsNone(commercial_cta.company_state(self.path, 101))

        with Store(self.path).db() as db:
            row = db.execute(
                'SELECT kind,status,payload FROM sales_leads WHERE id=?',
                (result['lead_id'],),
            ).fetchone()
        self.assertEqual(row['kind'], 'company')
        self.assertEqual(row['status'], 'new')
        self.assertIn('ООО Тест', row['payload'])

    def test_company_intro_explains_value_before_form(self):
        text = commercial_cta.company_intro_text()
        self.assertIn('AI-тренировки', text)
        self.assertIn('база знаний', text.lower())
        self.assertIn('аттестация', text.lower())
        self.assertIn('2 минут', text)

    def test_multiselect_requires_at_least_one_goal(self):
        commercial_cta.start_company(self.path, 303)
        commercial_cta.answer_company(self.path, 303, 'ООО Тест')
        commercial_cta.answer_company(self.path, 303, 'Опт')
        commercial_cta.answer_company(self.path, 303, '1-5')
        with self.assertRaises(ValueError):
            commercial_cta.finish_company_multi(self.path, 303)

    def test_individual_interest_records_selected_plan(self):
        lead_id = commercial_cta.record_individual_interest(
            self.path, 202, 'month', {'username': 'seller'}
        )
        with Store(self.path).db() as db:
            row = db.execute(
                'SELECT kind,payload FROM sales_leads WHERE id=?',
                (lead_id,),
            ).fetchone()
        self.assertEqual(row['kind'], 'individual')
        self.assertIn('"plan": "month"', row['payload'])
        self.assertIn(str(commercial_cta.INDIVIDUAL_MONTH_PRICE), row['payload'])

    def test_cta_has_no_company_price(self):
        text = commercial_cta.cta_text()
        self.assertIn('для компании', text)
        self.assertNotIn('29 900', text)
        self.assertNotIn('49 900', text)

    def test_payment_links_are_optional(self):
        old_10 = os.environ.get('PAYMENT_10_URL')
        old_month = os.environ.get('PAYMENT_MONTH_URL')
        try:
            os.environ.pop('PAYMENT_10_URL', None)
            os.environ.pop('PAYMENT_MONTH_URL', None)
            self.assertIsNone(commercial_cta.payment_url('10'))
            self.assertIsNone(commercial_cta.payment_url('month'))
            os.environ['PAYMENT_10_URL'] = 'https://example.test/pay-10'
            self.assertEqual(
                commercial_cta.payment_url('10'),
                'https://example.test/pay-10',
            )
        finally:
            if old_10 is None:
                os.environ.pop('PAYMENT_10_URL', None)
            else:
                os.environ['PAYMENT_10_URL'] = old_10
            if old_month is None:
                os.environ.pop('PAYMENT_MONTH_URL', None)
            else:
                os.environ['PAYMENT_MONTH_URL'] = old_month


if __name__ == '__main__':
    unittest.main()
