import unittest

from academy.scenarios import menu


class OnboardingCopyTests(unittest.TestCase):
    def test_menu_explains_free_access_and_saved_results(self):
        text = menu()
        self.assertIn('3 бесплатные тренировки', text)
        self.assertIn('Мои тренировки', text)
        self.assertIn('любым продуктом или услугой', text)


if __name__ == '__main__':
    unittest.main()
