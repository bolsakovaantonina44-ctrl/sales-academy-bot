import unittest

from academy.scenarios import menu


class OnboardingCopyTests(unittest.TestCase):
    def test_menu_explains_free_access_and_saved_results(self):
        text = menu()
        self.assertIn('3 бесплатные тренировки', text)
        self.assertIn('Мои тренировки', text)
        self.assertIn('любым продуктом или услугой', text)

    def test_menu_explicitly_explains_what_situation_to_describe(self):
        text = menu()
        self.assertIn('Опишите своими словами ситуацию, которую хотите отработать.', text)
        self.assertIn('Я впервые звоню закупщику строительной компании.', text)
        self.assertIn('получить ТЗ', text)

    def test_company_menu_does_not_show_public_limit(self):
        text = menu(public_access=False)
        self.assertIn('тренировки доступны без публичного лимита', text)
        self.assertNotIn('3 бесплатные тренировки', text)


if __name__ == '__main__':
    unittest.main()
