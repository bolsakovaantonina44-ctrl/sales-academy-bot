"""Snapshot company data separately from simulation and evaluation prompts."""
import json
import os
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHODOLOGY = {
    'version': 'owner-methodology-v1',
    'principles': [
        'Контакт и уместный small talk без затягивания. Имя собеседника, понятная цель звонка.',
        'Следующий вопрос следует из ответа клиента. Выяснить задачу, сроки, критерии, ограничения и полномочия.',
        'В проектных продажах: объект, сроки, смета, предпочтения; не навязывать эти вопросы другим отраслям.',
        'Аргумент связан с выясненной потребностью. Зацепка для продолжения ценна и без немедленной заявки.',
        'Директор/собственник: коротко и по значимой для бизнеса задаче. Закупщик: условия, сроки, надёжность, документы.',
        'Секретарь: краткая пересказываемая причина разговора с конкретным адресатом и релевантной выгодой без выдуманного эксклюзива.',
        'Дизайнер: замысел, спецификация, образцы, подходящие аналоги. Категорийный менеджер: ассортимент и экономика категории.',
        'Краткость, слушание, адаптация к роли. Не требовать заученных формулировок.',
        'Инициатива следующего контакта отличается от согласованной договорённости.',
        'По тексту и транскрипту не оценивать голосовую энергию, интонацию или темп: акустических данных нет.',
    ],
    'scripts': [],
}


def profile():
    path = os.getenv('COMPANY_PROFILE_PATH')
    value = json.loads(Path(path).read_text()) if path else {}
    return {'company': value.get('company', 'Индивидуальная учебная ситуация'),
            'product_knowledge': deepcopy(value.get('product_knowledge', {})),
            'company_rules': deepcopy(value.get('company_rules', {})),
            'sales_methodology': deepcopy(value.get('sales_methodology', METHODOLOGY)),
            'scenarios': deepcopy(value.get('scenarios', {})),
            'offer': value.get('offer', 'Демо завершено. Для следующего этапа подготовьте описание компании, продукта и задач команды — это основа настройки корпоративного тренажёра.')}


def scenario_catalog():
    configured = profile()['scenarios']
    if configured:
        return configured
    return json.loads((ROOT / 'profiles' / 'demo_scenarios.json').read_text())
