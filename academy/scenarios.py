"""Synthetic fixtures, not verified terms or customers of KWAY."""
from copy import deepcopy
from .domain import validate_card

from .knowledge import scenario_catalog

TEMPLATES = scenario_catalog()


def template(key, difficulty='medium'):
    t = deepcopy(TEMPLATES[key])
    validate_card(t['card'])
    return t


def menu(public_access=True):
    access = ('Тестовый доступ: 3 бесплатные тренировки. Разборы сохраняются в «Мои тренировки».'
              if public_access else
              'Корпоративный доступ: тренировки доступны без публичного лимита. Разборы сохраняются в «Мои тренировки».')
    return (access + '\n\n'
            'Опишите своими словами ситуацию, которую хотите отработать. Например: '
            '“Я впервые звоню закупщику строительной компании. Хочу выяснить, есть ли текущий объект и получить ТЗ”.\n\n'
            'Можно одним сообщением указать, что вы предлагаете, с кем разговариваете, что сейчас происходит '
            'и какого результата хотите достичь.\n\n'
            'Тренажёр работает с любым продуктом или услугой и построит ситуацию по вашему описанию.')
