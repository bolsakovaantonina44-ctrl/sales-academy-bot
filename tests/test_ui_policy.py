from academy.ui_policy import (
    PUBLIC, AKENSO, SUPERVISOR,
    CONTEXT_HOME, CONTEXT_LEARNING, CONTEXT_ASSESSMENT,
    CONTEXT_TRAINING, CONTEXT_MANAGEMENT,
    rows_for_context,
)


def flatten(rows):
    return [item for row in rows for item in row]


def test_employee_learning_hides_training_result_buttons():
    buttons = flatten(rows_for_context(AKENSO, CONTEXT_LEARNING))
    assert 'Посмотреть разбор' not in buttons
    assert 'Скачать результат' not in buttons
    assert 'Сессии сотрудников' not in buttons
    assert 'Управление доступом' not in buttons
    assert buttons == ['Продолжить обучение', 'Мой прогресс', 'К Академии']


def test_employee_never_gets_team_or_admin_management():
    assert rows_for_context(AKENSO, CONTEXT_MANAGEMENT) == []
    assert 'Команда' not in flatten(rows_for_context(AKENSO, CONTEXT_HOME))


def test_supervisor_gets_team_entry_but_not_technical_admin_on_home():
    home = flatten(rows_for_context(SUPERVISOR, CONTEXT_HOME))
    assert 'Команда' in home
    assert 'Управление доступом' not in home
    assert 'Технические сессии' not in home


def test_public_user_cannot_see_corporate_learning_actions():
    assert rows_for_context(PUBLIC, CONTEXT_LEARNING) == []
    assert rows_for_context(PUBLIC, CONTEXT_ASSESSMENT) == []
    home = flatten(rows_for_context(PUBLIC, CONTEXT_HOME))
    assert 'Продолжить обучение' not in home
    assert 'Мой прогресс' not in home


def test_training_context_only_shows_current_training_actions():
    active = flatten(rows_for_context(AKENSO, CONTEXT_TRAINING, phase='active'))
    assert active == ['Завершить тренировку']
    completed = flatten(rows_for_context(AKENSO, CONTEXT_TRAINING, phase='completed'))
    assert 'Посмотреть разбор' in completed
    assert 'Скачать результат' in completed
    assert 'Продолжить обучение' not in completed
    assert 'Прогресс команды' not in completed


def test_supervisor_management_is_separate_from_technical_admin():
    supervisor = flatten(rows_for_context(SUPERVISOR, CONTEXT_MANAGEMENT, is_admin=False))
    assert supervisor == ['Прогресс команды', 'К Академии']
    assert 'Сессии сотрудников' not in supervisor
    technical = flatten(rows_for_context(SUPERVISOR, CONTEXT_MANAGEMENT, is_admin=True))
    assert 'Управление доступом' in technical
    assert 'Технические сессии' in technical
