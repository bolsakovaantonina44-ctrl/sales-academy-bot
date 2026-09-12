import unittest

from academy.pacing import FOCUS_OBJECTIONS, prepare_card


class GenericSupplierObjectionTests(unittest.TestCase):
    def test_supplier_focus_is_product_agnostic(self):
        self.assertEqual(FOCUS_OBJECTIONS['supplier']['label'], 'Уже работаем с другим')
        self.assertNotIn('Уже есть поставщик', FOCUS_OBJECTIONS['supplier']['text'])

    def test_loyalty_barrier_uses_generic_provider_wording(self):
        card = {
            'barriers': [
                {'id': 'loyalty', 'text': 'legacy', 'resolved_when': 'legacy'},
            ],
            'opening': 'Здравствуйте',
        }
        prepared = prepare_card(card, 'easy')
        self.assertEqual(prepared['barriers'][0]['text'], 'Мы уже работаем с другим поставщиком или исполнителем.')


if __name__ == '__main__':
    unittest.main()
