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
    return 'Опишите свою ситуацию: что продаёте, кому, что происходит и цель разговора.\n\nИли явно выберите один из готовых учебных примеров:\n' + '\n'.join(k + '. ' + v['title'] + ' — ' + v['product'] for k,v in TEMPLATES.items()) + '\nВсе готовые ситуации вымышленные; реальные условия компании не заданы.'

