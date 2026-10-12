"""Manual CRM calibration for CENTARO supervisor reports.

Supervisors can attach real CRM facts to a completed, supervisor-assigned series. The
report keeps three concepts separate:
- Training Conversion: result in the simulation;
- CRM Conversion: actual target outcomes / actual comparable contacts;
- Calibration coefficient: CRM Conversion / Training Conversion.

This deliberately starts without a CRM connector. A future Bitrix connector can write
the same crm_fact payload automatically without changing report semantics.
"""
import json
from academy.domain import normalize_command

import telebot

from academy import call_series
from academy import centaro_performance_runtime as performance
from academy import centaro_role_reports_runtime as role_reports
from academy import mobile_learning_runtime as mobile
from academy import series_ux_runtime as series_ux


_INSTALLED = False
_ORIGINAL_HANDLE = None
_ORIGINAL_MOBILE_CALLBACK = None
_ORIGINAL_HOME_MARKUP = None
_ORIGINAL_SUPERVISOR_REPORT = None


def _is_supervisor(user_id):
    return series_ux._is_supervisor(user_id)


def _load_series(store, series_id):
    with store.db() as db:
        row = db.execute("SELECT payload FROM call_series WHERE id=?", (int(series_id),)).fetchone()
    return json.loads(row["payload"]) if row else None


def _save_series(store, series):
    with store.db() as db:
        db.execute(
            "UPDATE call_series SET payload=? WHERE id=?",
            (json.dumps(series, ensure_ascii=False), int(series["id"])),
        )


def _recent_calibratable(store, supervisor_id, employee_id=None, limit=12):
    params = [int(supervisor_id)]
    where = "assigned_by=? AND status='completed'"
    if employee_id is not None:
        where += " AND user_id=?"
        params.append(int(employee_id))
    with store.db() as db:
        rows = db.execute(
            f"SELECT payload FROM call_series WHERE {where} ORDER BY id DESC LIMIT ?",
            tuple(params + [int(limit)]),
        ).fetchall()
    result = []
    for row in rows:
        value = json.loads(row["payload"])
        if value.get("conversion_goal") and value.get("flow_type"):
            result.append(value)
    return result


def _employees_for_calibration(store, supervisor_id):
    seen = set()
    result = []
    for series in _recent_calibratable(store, supervisor_id, limit=50):
        user_id = int(series["user_id"])
        if user_id in seen:
            continue
        seen.add(user_id)
        result.append((user_id, series_ux._latest_name(store, user_id)))
    return result


def _fmt_pct(value):
    return "н/д" if value is None else f"{float(value) * 100:.1f}%"


def _goal_label(series):
    return role_reports._goal_label(series)


def _flow_label(series):
    try:
        from academy import centaro_rop_runtime as rop
        return rop._flow_label(series)
    except Exception:
        return str(series.get("flow_type") or "не указан")


def _sample_label(real_contacts):
    count = int(real_contacts or 0)
    if count < 30:
        return "предварительная CRM-калибровка"
    if count < 100:
        return "рабочая CRM-калибровка"
    return "устойчивая CRM-калибровка"


def build_crm_fact(training_conversion, real_contacts, real_outcomes, revenue=None):
    real_contacts = int(real_contacts)
    real_outcomes = int(real_outcomes)
    if real_contacts < 10:
        raise ValueError("Для CRM-калибровки нужно минимум 10 сопоставимых контактов.")
    if not 0 <= real_outcomes <= real_contacts:
        raise ValueError("Число целевых результатов должно быть от 0 до числа контактов.")
    if training_conversion is None:
        raise ValueError("В серии нет проверенной Training Conversion.")

    training = float(training_conversion)
    crm_conversion = real_outcomes / real_contacts
    coefficient = crm_conversion / training if training > 0 else None
    fact = {
        "real_contacts": real_contacts,
        "real_outcomes": real_outcomes,
        "crm_conversion": crm_conversion,
        "training_conversion": training,
        "calibration_coefficient": coefficient,
        "confidence": _sample_label(real_contacts),
    }
    if revenue is not None:
        revenue = float(revenue)
        if revenue < 0:
            raise ValueError("Выручка не может быть отрицательной.")
        fact["actual_revenue"] = revenue
    return fact


def _calibration_report(series):
    fact = series.get("crm_fact") or {}
    if not fact:
        return None
    coefficient = fact.get("calibration_coefficient")
    lines = [
        "CENTARO · CRM-КАЛИБРОВКА",
        f"Тип потока: {_flow_label(series)}",
        f"Цель: {_goal_label(series)}",
        f"CRM-выборка: {fact.get('real_contacts', 0)} сопоставимых контактов",
        f"Фактических целевых результатов: {fact.get('real_outcomes', 0)}",
        f"Training Conversion: {_fmt_pct(fact.get('training_conversion'))}",
        f"CRM Conversion: {_fmt_pct(fact.get('crm_conversion'))}",
        f"Надёжность: {fact.get('confidence', 'н/д')}",
    ]
    if coefficient is not None:
        lines.append(f"Коэффициент калибровки: {coefficient:.2f}")
        if coefficient < 0.85:
            lines.append("Учебная модель пока переоценивает фактическую конверсию — коммерческий forecast нужно понижать.")
        elif coefficient > 1.15:
            lines.append("Фактическая конверсия выше учебной — в реальной среде менеджер конвертирует лучше симуляции.")
        else:
            lines.append("Учебная и фактическая конверсия близки — модель хорошо совпадает с CRM-фактом.")
    if fact.get("actual_revenue") is not None:
        actual = float(fact["actual_revenue"])
        lines.append(f"Фактическая выручка периода: {int(round(actual)):,} ₽".replace(",", " "))
        projected = (series.get("business_projection") or {}).get("projected_revenue")
        if projected:
            lines.append(f"Факт к прежнему модельному прогнозу: {actual / float(projected) * 100:.0f}%.")
    return "\n".join(lines)


def _employee_picker(bot, chat_id):
    if not _is_supervisor(chat_id):
        bot.send_message(chat_id, "CRM-калибровка доступна руководителю.")
        return
    store = series_ux.Store(series_ux._db_path())
    employees = _employees_for_calibration(store, chat_id)
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for user_id, name in employees:
        markup.add(telebot.types.InlineKeyboardButton(name[:48], callback_data=f"academyv2:crm:employee:{user_id}"))
    markup.add(telebot.types.InlineKeyboardButton("← В Академию", callback_data="academyv2:home"))
    text = (
        "CENTARO · CRM-ФАКТ\n\n"
        "Выберите сотрудника. CENTARO сопоставит завершённую учебную серию с реальной конверсией "
        "по такому же типу потока и той же бизнес-цели."
    )
    if not employees:
        text += "\n\nПока нет завершённых назначенных серий, которые можно откалибровать."
    bot.send_message(chat_id, text, reply_markup=markup)


def _series_picker(bot, chat_id, employee_id):
    store = series_ux.Store(series_ux._db_path())
    rows = _recent_calibratable(store, chat_id, employee_id=employee_id, limit=8)
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for series in rows:
        count = int(series.get("config", {}).get("count") or 0)
        prefix = "✅ " if series.get("crm_fact") else ""
        label = f"{prefix}#{series['id']} · {count} · {_flow_label(series)} · {_goal_label(series)}"
        markup.add(telebot.types.InlineKeyboardButton(label[:64], callback_data=f"academyv2:crm:series:{series['id']}"))
    markup.add(telebot.types.InlineKeyboardButton("← Сотрудники", callback_data="academyv2:crm"))
    bot.send_message(
        chat_id,
        "Выберите завершённую серию. Вносите CRM-факт только за сопоставимый период: тот же тип потока и та же цель конверсии.",
        reply_markup=markup,
    )


def _start_fact(bot, chat_id, series_id):
    store = series_ux.Store(series_ux._db_path())
    series = _load_series(store, series_id)
    if not series or int(series.get("assigned_by") or 0) != int(chat_id) or series.get("status") != "completed":
        bot.send_message(chat_id, "Эта серия недоступна для CRM-калибровки.")
        return
    name = series_ux._latest_name(store, series["user_id"])
    series["crm_fact_setup"] = {"actor_id": int(chat_id), "step": "contacts", "data": {}}
    _save_series(store, series)
    bot.send_message(
        chat_id,
        f"CENTARO · CRM-ФАКТ\n\nСотрудник: {name}\nСерия #{series['id']}\nТип потока: {_flow_label(series)}\n"
        f"Цель: {_goal_label(series)}\n\n1/3. Сколько реальных сопоставимых целевых контактов было у менеджера за выбранный период?\n"
        "Например: 120. Не смешивайте холодные, входящие и действующие контакты.",
    )


def _find_pending(store, actor_id):
    with store.db() as db:
        rows = db.execute("SELECT payload FROM call_series WHERE status='completed' ORDER BY id DESC LIMIT 100").fetchall()
    for row in rows:
        series = json.loads(row["payload"])
        setup = series.get("crm_fact_setup") or {}
        if int(setup.get("actor_id") or 0) == int(actor_id) and setup.get("step"):
            return series
    return None


def _ack(store, event, session, series, text):
    _save_series(store, series)
    store.commit(event, session, [text])


def _training_conversion(store, series):
    projection = series.get("business_projection") or {}
    if projection.get("training_conversion") is not None:
        return float(projection["training_conversion"])
    results = store.series_results(series["id"])
    return performance.series_metrics(series, results).get("training_conversion")


def _patched_handle(self, event, s):
    user = int(event["user_id"])
    active = self.store.open_series(user)
    if ((active and active["status"] == "active") or s.get("series_setup")
            or normalize_command(event.get('text') or '') in ('посмотреть итог серии', 'скачать отчет серии')):
        return _ORIGINAL_HANDLE(self, event, s)
    text = str(event.get("text") or "").strip()
    pending = _find_pending(self.store, user)
    if not pending:
        return _ORIGINAL_HANDLE(self, event, s)
    if text.lower() in {"/cancel", "отмена"}:
        pending["crm_fact_setup"] = None
        _ack(self.store, event, s, pending, "CRM-калибровка отменена.")
        return True

    setup = pending["crm_fact_setup"]
    data = setup.setdefault("data", {})
    try:
        if setup["step"] == "contacts":
            value = int(text.replace(" ", ""))
            if value < 10:
                raise ValueError("Введите не меньше 10 сопоставимых контактов.")
            data["real_contacts"] = value
            setup["step"] = "outcomes"
            _ack(
                self.store, event, s, pending,
                f"2/3. Сколько из этих {value} контактов реально завершились результатом «{_goal_label(pending)}»?\nНапример: 28.",
            )
            return True
        if setup["step"] == "outcomes":
            value = int(text.replace(" ", ""))
            if value < 0 or value > int(data["real_contacts"]):
                raise ValueError("Число результатов не может быть больше числа контактов.")
            data["real_outcomes"] = value
            setup["step"] = "revenue"
            _ack(
                self.store, event, s, pending,
                "3/3. Какая фактическая выручка этого менеджера за тот же период?\n\n"
                "Введите сумму в рублях, например 1850000. Если выручка сейчас не нужна — напишите «пропустить».",
            )
            return True
        if setup["step"] == "revenue":
            revenue = None
            if text.lower() not in {"пропустить", "/skip", "нет"}:
                revenue = float(text.replace(" ", "").replace(",", "."))
            fact = build_crm_fact(
                _training_conversion(self.store, pending),
                data["real_contacts"],
                data["real_outcomes"],
                revenue,
            )
            pending["crm_fact"] = fact
            pending["crm_fact_setup"] = None
            _ack(self.store, event, s, pending, _calibration_report(pending))
            return True
    except (ValueError, TypeError) as exc:
        _ack(self.store, event, s, pending, "Проверьте ответ: " + str(exc))
        return True

    return _ORIGINAL_HANDLE(self, event, s)


def _patched_mobile_callback(bot, call):
    data = str(getattr(call, "data", "") or "")
    if not getattr(call, "message", None):
        return _ORIGINAL_MOBILE_CALLBACK(bot, call)
    chat_id = int(call.message.chat.id)
    if data == "academyv2:crm":
        _employee_picker(bot, chat_id)
        bot.answer_callback_query(call.id)
        return True
    if data.startswith("academyv2:crm:employee:"):
        try:
            employee_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить сотрудника.", show_alert=True)
            return True
        _series_picker(bot, chat_id, employee_id)
        bot.answer_callback_query(call.id)
        return True
    if data.startswith("academyv2:crm:series:"):
        try:
            series_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить серию.", show_alert=True)
            return True
        _start_fact(bot, chat_id, series_id)
        bot.answer_callback_query(call.id)
        return True
    return _ORIGINAL_MOBILE_CALLBACK(bot, call)


def _patched_home_markup(user_id):
    markup = _ORIGINAL_HOME_MARKUP(user_id)
    if _is_supervisor(user_id):
        markup.add(telebot.types.InlineKeyboardButton("📊 Внести CRM-факт", callback_data="academyv2:crm"))
    return markup


def _patched_supervisor_report(store, series, results):
    text = _ORIGINAL_SUPERVISOR_REPORT(store, series, results)
    calibration = _calibration_report(series)
    if calibration:
        text += "\n\n" + calibration
    elif series.get("mode") == "attestation":
        text += (
            "\n\nCRM-КАЛИБРОВКА\n"
            "CRM-факт ещё не внесён. После накопления реальных сопоставимых контактов руководитель может "
            "сопоставить Training Conversion с фактической CRM Conversion."
        )
    return text


def install():
    global _INSTALLED, _ORIGINAL_HANDLE, _ORIGINAL_MOBILE_CALLBACK, _ORIGINAL_HOME_MARKUP
    global _ORIGINAL_SUPERVISOR_REPORT
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_HANDLE = call_series.SeriesEngine.handle
    _ORIGINAL_MOBILE_CALLBACK = mobile._handle_callback
    _ORIGINAL_HOME_MARKUP = mobile._home_markup
    _ORIGINAL_SUPERVISOR_REPORT = performance.combined_report

    call_series.SeriesEngine.handle = _patched_handle
    mobile._handle_callback = _patched_mobile_callback
    mobile._home_markup = _patched_home_markup
    performance.combined_report = _patched_supervisor_report
