import unittest

from academy.domain import score_level


class ScoreLevelTests(unittest.TestCase):
    def test_score_level_boundaries(self):
        cases = {
            0: 'требует системной отработки',
            39: 'требует системной отработки',
            40: 'рабочая база',
            59: 'рабочая база',
            60: 'уверенный уровень',
            74: 'уверенный уровень',
            75: 'сильный уровень',
            89: 'сильный уровень',
            90: 'очень сильный уровень',
            100: 'очень сильный уровень',
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                self.assertEqual(score_level(score), expected)


if __name__ == '__main__':
    unittest.main()
