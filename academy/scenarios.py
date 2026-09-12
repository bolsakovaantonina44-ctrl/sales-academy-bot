"""Synthetic fixtures, not verified terms or customers of KWAY."""
from copy import deepcopy
from .domain import validate_card

from .knowledge import scenario_catalog

TEMPLATES = scenario_catalog()


def template(key, difficulty='medium'):
    t = deepcopy(TEMPLATES[key])
    validate_card(t['card'])
    return t


def menu():
    return ('Тестовый доступ: 3 бесплатные тренировки. Разборы сохраняются в «Мои тренировки».\n\n'
            'Опишите свою рабочую ситуацию одним сообщением:\n'
            '• что вы продаёте;\n'
            '• кому продаёте — роль клиента и тип компании;\n'
            '• что сейчас происходит;\n'
            '• какого результата хотите достичь в разговоре.\n\n'
            'Тренажёр работает с любым продуктом или услугой и построит ситуацию по вашему описанию.')
