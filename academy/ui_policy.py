"""Role- and context-aware Telegram UI policy for Academy navigation.

Pure functions only: no Telegram or database dependencies. The transport layer renders
these rows as reply keyboards while keeping learning, training and management contexts
isolated from one another.
"""

PUBLIC = 'public'
AKENSO = 'akenso'
SUPERVISOR = 'supervisor'

CONTEXT_HOME = 'home'
CONTEXT_LEARNING = 'learning'
CONTEXT_ASSESSMENT = 'assessment'
CONTEXT_TRAINING = 'training'
CONTEXT_RESULT = 'result'
CONTEXT_MANAGEMENT = 'management'


def academy_home_rows(role):
    """Minimal Academy home. Technical/admin actions never appear here."""
    if role == PUBLIC:
        return [['Новая тренировка'], ['Мои тренировки']]
    rows = [
        ['Продолжить обучение'],
        ['Тренировка', 'Аттестация'],
        ['Мой прогресс'],
    ]
    if role == SUPERVISOR:
        rows.append(['Команда'])
    return rows


def learning_rows(role):
    """Learning screens must not leak training-result or admin controls."""
    if role == PUBLIC:
        return []
    return [
        ['Продолжить обучение'],
        ['Мой прогресс'],
        ['К Академии'],
    ]


def assessment_rows(role):
    if role == PUBLIC:
        return []
    return [
        ['Мой прогресс'],
        ['К Академии'],
    ]


def training_rows(role, phase):
    """Only actions relevant to the current training phase are visible."""
    if phase == 'active':
        return [['Завершить тренировку']]
    if phase == 'closed':
        return []
    if phase == 'completed':
        rows = [
            ['Посмотреть разбор', 'Скачать результат'],
            ['Новая тренировка', 'Мои тренировки'],
        ]
        if role in {AKENSO, SUPERVISOR}:
            rows.append(['К Академии'])
        return rows
    if phase == 'ready':
        return [['Начать тренировку'], ['Новая тренировка']]
    return [['Мои тренировки']]


def management_rows(role, is_admin=False):
    """Supervisor functions and technical administration are separate surfaces.

    Employee session browsing stays hidden until a dedicated supervisor-safe view is
    implemented. This avoids accidentally exposing another employee's raw sessions.
    """
    if role != SUPERVISOR and not is_admin:
        return []
    rows = []
    if role == SUPERVISOR:
        rows.extend([
            ['Прогресс команды'],
            ['К Академии'],
        ])
    if is_admin:
        rows.append(['Управление доступом'])
        rows.append(['Технические сессии'])
    return rows


def rows_for_context(role, context, phase=None, is_admin=False):
    """Single source of truth for reply-keyboard visibility."""
    if context == CONTEXT_LEARNING:
        return learning_rows(role)
    if context == CONTEXT_ASSESSMENT:
        return assessment_rows(role)
    if context == CONTEXT_TRAINING:
        return training_rows(role, phase)
    if context == CONTEXT_RESULT:
        return training_rows(role, 'completed')
    if context == CONTEXT_MANAGEMENT:
        return management_rows(role, is_admin=is_admin)
    return academy_home_rows(role)
