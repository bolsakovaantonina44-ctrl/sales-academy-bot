"""CENTARO branding and role-aware home copy.

Keeps the underlying Academy/Trainer mechanics intact while presenting one product
brand to users. Supervisors see management language; employees see a development
workspace. The patch is installed last so buttons added by other CENTARO runtimes
(series assignment, CRM fact, etc.) remain available.
"""
from academy import mobile_learning_runtime as mobile
from academy.access import SUPERVISOR, get_role
from academy.onboarding_progress import progress


_INSTALLED = False
_ORIGINAL_HOME_MARKUP = None
_ORIGINAL_REMOVE_REPLY_KEYBOARD = None


def _is_supervisor(user_id):
    return get_role(mobile._db_path(), int(user_id)) == SUPERVISOR or int(user_id) in mobile._admins()


def _rebrand_markup(markup):
    replacements = {
        "▶️ Продолжить маршрут": "▶️ Мой маршрут",
        "🎭 Тренажёр / практика": "🎭 CENTARO Trainer",
        "📞 Серия звонков": "📞 Серия звонков · CENTARO",
        "📚 База знаний": "📚 CENTARO Academy",
        "📊 Мой прогресс": "📊 Мои результаты",
        "← В Академию": "← В CENTARO",
    }
    for row in getattr(markup, "keyboard", []) or []:
        for button in row:
            text = getattr(button, "text", None)
            if text in replacements:
                button.text = replacements[text]
    return markup


def _home_markup(user_id):
    return _rebrand_markup(_ORIGINAL_HOME_MARKUP(user_id))


def _remove_reply_keyboard(bot, chat_id):
    import telebot
    bot.send_message(chat_id, "Открываю CENTARO…", reply_markup=telebot.types.ReplyKeyboardRemove())


def _send_home(bot, chat_id, remove_keyboard=False):
    if remove_keyboard:
        _remove_reply_keyboard(bot, chat_id)

    p = progress(mobile._db_path(), chat_id)
    completed = int(p["completed"])
    total = int(p["total"])

    if _is_supervisor(chat_id):
        if completed >= total:
            personal_status = "Ваш личный базовый маршрут завершён."
        else:
            personal_status = f"Ваш личный маршрут: пройдено {completed} из {total} уроков."
        text = (
            "CENTARO · ПАНЕЛЬ РУКОВОДИТЕЛЯ\n\n"
            "Управляйте развитием отдела продаж на основе данных: назначайте серии звонков, "
            "задавайте цель и норматив конверсии, оценивайте навыки по 100-балльной системе, "
            "сопоставляйте учебную конверсию с данными CRM и отслеживайте прогноз выполнения плана.\n\n"
            + personal_status
            + "\n\nДля работы с сотрудниками используйте «Команда», «Назначить серию сотруднику» и «CRM-факт»."
        )
    else:
        if completed >= total:
            status = "Базовое обучение завершено. Следующий этап: проверка знаний → практический экзамен → итог аттестации."
        else:
            status = f"Ваш маршрут: пройдено {completed} из {total} уроков."
        text = (
            "CENTARO · ЛИЧНЫЙ КАБИНЕТ\n\n"
            "Здесь вы развиваете продажи в рабочем формате: тренируете разговоры с ИИ-клиентами, "
            "отрабатываете возражения, проходите серии звонков, изучаете базу знаний и подтверждаете навыки.\n\n"
            "CENTARO оценивает качество продаж по 100-балльной системе, показывает учебную конверсию "
            "и формирует персональный навигатор — что именно улучшить в следующей тренировке.\n\n"
            + status
        )

    bot.send_message(chat_id, text, reply_markup=_home_markup(chat_id))


def install():
    global _INSTALLED, _ORIGINAL_HOME_MARKUP, _ORIGINAL_REMOVE_REPLY_KEYBOARD
    if _INSTALLED:
        return
    _INSTALLED = True
    _ORIGINAL_HOME_MARKUP = mobile._home_markup
    _ORIGINAL_REMOVE_REPLY_KEYBOARD = mobile._remove_reply_keyboard
    mobile._home_markup = _home_markup
    mobile._remove_reply_keyboard = _remove_reply_keyboard
    mobile._send_home = _send_home
