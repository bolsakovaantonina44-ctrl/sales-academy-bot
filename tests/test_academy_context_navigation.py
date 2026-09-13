from academy.ui_policy import (
    AKENSO,
    PUBLIC,
    SUPERVISOR,
    CONTEXT_HOME,
    CONTEXT_LEARNING,
    CONTEXT_ASSESSMENT,
    CONTEXT_MANAGEMENT,
    rows_for_context,
)
from bot import keyboard_rows


def flatten(rows):
    return [item for row in rows for item in row]


def test_learning_hides_training_result_actions():
    buttons = flatten(rows_for_context(AKENSO, CONTEXT_LEARNING))
    assert 'Посмотреть разбор' not in buttons
    assert 'Скачать результат' not in buttons
    assert 'Отчёт руководителю' not in buttons
    assert 'Сессии пользователей' not in buttons
    assert 'Доступ сотрудников' not in buttons


def test_assessment_hides_unrelated_actions():
    buttons = flatten(rows_for_context(AKENSO, CONTEXT_ASSESSMENT))
    assert buttons == ['Мой прогресс', 'К Академии']


def test_employee_home_has_no_management_access():
    buttons = flatten(rows_for_context(AKENSO, CONTEXT_HOME))
    assert 'Команда' not in buttons
    assert 'Прогресс команды' not in buttons
    assert 'Технические сессии' not in buttons


def test_supervisor_home_exposes_team_but_not_technical_admin():
    buttons = flatten(rows_for_context(SUPERVISOR, CONTEXT_HOME))
    assert 'Команда' in buttons
    assert 'Технические сессии' not in buttons
    assert 'Управление доступом' not in buttons


def test_supervisor_management_does_not_expose_raw_sessions():
    buttons = flatten(rows_for_context(SUPERVISOR, CONTEXT_MANAGEMENT))
    assert buttons == ['Прогресс команды', 'К Академии']
    assert 'Сессии пользователей' not in buttons
    assert 'Сессии сотрудников' not in buttons


def test_public_user_has_no_company_learning_menu():
    assert rows_for_context(PUBLIC, CONTEXT_LEARNING) == []
    assert rows_for_context(PUBLIC, CONTEXT_ASSESSMENT) == []


def test_training_keyboard_no_longer_leaks_admin_buttons():
    session = {'phase': 'completed', 'report_status': 'ready'}
    buttons = flatten(keyboard_rows(session, admin=True))
    assert 'Посмотреть разбор' in buttons
    assert 'Скачать результат' in buttons
    assert 'Отчёт руководителю' not in buttons
    assert 'Показать скрытый сценарий' not in buttons
    assert 'Сессии пользователей' not in buttons
