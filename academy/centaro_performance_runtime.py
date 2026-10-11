"""CENTARO performance layer for call series.

Adds the business-facing pieces promised in the commercial demo without weakening the
assessment engine:
- post-series business forecast (average check, monthly target contacts, plan, and—when
  the series goal is not a direct purchase—the company's downstream conversion to sale);
- a saved baseline and before/after comparison for comparable series;
- an automatic supervisor copy for completed series;
- one combined CENTARO report: knowledge, skills, training conversion, business model,
  and a technique navigator.

The business forecast is deliberately labelled as a model based on a training series,
not a guarantee of real sales.
"""
from collections import Counter, defaultdict
import json
import math
import re

import telebot

from academy import call_series
from academy import series_ux_runtime as series_ux
from academy.access import PUBLIC, get_role
from academy.domain import SKILLS, chunks
from academy.learning import progress_snapshot
from academy.reporting import total_score


_INSTALLED = False
_ORIGINAL_HANDLE = None
_ORIGINAL_FINISH = None
_ORIGINAL_REPORT = None
_ORIGINAL_SERIES_HUB = None
_ORIGINAL_WIZARD_PROMPT = None


SKILL_NAV = {
    "contact": (
        "Вход в контакт",
        "Отработать первые 20–30 секунд: имя, понятная причина обращения и разрешение на короткий разговор.",
    ),
    "questions": (
        "Вопросы",
        "Тренировать короткую цепочку вопросов, где каждый следующий вопрос следует из ответа клиента.",
    ),
    "needs": (
        "Выявление потребности",
        "До презентации фиксировать задачу, сроки, критерии выбора и ограничения клиента.",
    ),
    "listening": (
        "Слушание",
        "Отражать ключевую мысль клиента и использовать её в следующем вопросе или аргументе.",
    ),
    "control": (
        "Управление разговором",
        "Тренировать возврат разговора к цели и перевод обсуждения в конкретный следующий шаг.",
    ),
    "arguments": (
        "Аргументация",
        "Связывать каждый аргумент только с уже выявленной задачей клиента, а не читать общую презентацию.",
    ),
    "objections": (
        "Работа с возражениями",
        "Сначала выяснять реальную причину возражения, затем отвечать через потребность и проверять, снято ли сомнение.",
    ),
    "next_step": (
        "Следующий шаг",
        "Фиксировать конкретное действие, ответственного и срок/условие следующего контакта.",
    ),
}

MODULE_LABELS = {
    "product": "Продукт",
    "sales": "Техники продаж",
    "regulations": "Регламенты",
}


def _money(value):
    return f"{int(round(float(value))):,}".replace(",", " ") + " ₽"


def _number(text, *, minimum=1, maximum=None, percent=False):
    value = str(text or "").strip().lower().replace("₽", "").replace("руб.", "").replace("руб", "")
    value = value.replace("%", "").replace(" ", "").replace("\u00a0", "")
    value = value.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("Введите число, например 150000.")
    parsed = float(value)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        if percent:
            raise ValueError(f"Введите процент от {minimum} до {maximum}.")
        raise ValueError("Значение вне допустимого диапазона.")
    return parsed


def _conversion_rows(results):
    rows = [r for r in results if r.get("status") != "pending"]
    verified = [
        r
        for r in rows
        if r.get("assessment_status") == "verified"
        and (r.get("session", {}).get("report_data") or {}).get("simulation_valid")
    ]
    verified_ids = {id(r) for r in verified}
    conversion = [
        r
        for r in rows
        if r.get("outcome_status") == "verified"
        and (id(r) in verified_ids or r.get("assessment_status") == "no_speech")
    ]
    return rows, verified, conversion


def series_metrics(series, results):
    rows, verified, conversion_rows = _conversion_rows(results)
    achieved = sum(bool(r.get("goal_achieved")) for r in conversion_rows)
    conversion = achieved / len(conversion_rows) if conversion_rows else None
    scores = []
    skill_values = defaultdict(list)
    maxima = {key: maximum for key, _, maximum in SKILLS}
    for result in verified:
        data = result.get("session", {}).get("report_data") or {}
        score = total_score(data)
        if score is not None:
            scores.append(score)
        for item in data.get("skills", []):
            if item.get("id") in maxima and item.get("score") is not None:
                skill_values[item["id"]].append(item["score"])

    skill_average = sum(scores) / len(scores) if scores else None
    normalized = {}
    for key, values in skill_values.items():
        normalized[key] = (sum(values) / len(values)) / maxima[key] * 100

    outcomes = Counter(r.get("outcome", "unavailable") for r in conversion_rows)
    quality_calls = sum((total_score(r.get("session", {}).get("report_data")) or -1) >= 60 for r in verified)
    return {
        "calls_saved": len(rows),
        "calls_verified": len(conversion_rows),
        "goal_achieved": achieved,
        "training_conversion": conversion,
        "skill_score": skill_average,
        "quality_calls": quality_calls,
        "outcomes": dict(outcomes),
        "skill_normalized": normalized,
    }


def _knowledge(path, user_id):
    snapshot = progress_snapshot(path, user_id)
    scored = []
    rows = []
    for module in snapshot:
        latest = module.get("latest_assessment")
        if latest:
            score = int(latest["score"])
            scored.append(score)
            rows.append((module["id"], module["title"], score, bool(latest["passed"])))
        else:
            rows.append((module["id"], module["title"], None, False))
    return {
        "score": round(sum(scored) / len(scored)) if scored else None,
        "assessed": len(scored),
        "total": len(snapshot),
        "rows": rows,
    }


def _same_scenario(a, b):
    ca, cb = a.get("config", {}), b.get("config", {})
    return (
        str(ca.get("product", "")).strip().lower() == str(cb.get("product", "")).strip().lower()
        and str(ca.get("customer", "")).strip().lower() == str(cb.get("customer", "")).strip().lower()
        and set(ca.get("goals") or []) == set(cb.get("goals") or [])
    )


def _previous_projection(store, series):
    for candidate in store.recent_series(series["user_id"]):
        if candidate.get("id") == series.get("id"):
            continue
        if candidate.get("status") != "completed" or not candidate.get("business_projection"):
            continue
        if _same_scenario(series, candidate):
            return candidate
    return None


def _needs_downstream(series):
    goals = list(series.get("config", {}).get("goals") or [])
    return not goals or set(goals) != {"purchase"}


def build_projection(series, results, data):
    metrics = series_metrics(series, results)
    conversion = metrics["training_conversion"]
    if conversion is None:
        raise ValueError("Недостаточно проверенных звонков для расчёта конверсии.")

    downstream = float(data.get("downstream_percent", 100.0)) / 100.0
    monthly_contacts = float(data["monthly_contacts"])
    average_check = float(data["average_check"])
    monthly_plan = float(data["monthly_plan"])
    expected_sales = monthly_contacts * conversion * downstream
    revenue = expected_sales * average_check
    plan_percent = revenue / monthly_plan * 100 if monthly_plan else None
    projection = {
        "average_check": average_check,
        "monthly_contacts": monthly_contacts,
        "monthly_plan": monthly_plan,
        "downstream_percent": downstream * 100,
        "training_conversion": conversion,
        "expected_sales": expected_sales,
        "projected_revenue": revenue,
        "plan_percent": plan_percent,
        "skill_score": metrics["skill_score"],
        "calls_verified": metrics["calls_verified"],
        "goal_achieved": metrics["goal_achieved"],
    }
    return projection, metrics


def _navigator(metrics, knowledge):
    items = []
    knowledge_gaps = [(score if score is not None else -1, title) for _, title, score, passed in knowledge["rows"] if not passed]
    knowledge_gaps.sort(key=lambda value: value[0])
    if knowledge_gaps:
        labels = ", ".join(title for _, title in knowledge_gaps[:2])
        items.append(f"Знания: закрыть и повторно аттестовать блоки «{labels}».")

    weakest = sorted(metrics.get("skill_normalized", {}).items(), key=lambda item: item[1])
    for key, value in weakest[:2]:
        label, action = SKILL_NAV[key]
        items.append(f"{label} ({value:.0f}% от максимума): {action}")
    if not items:
        items.append("Закрепить результат на новой серии с более сложными клиентами и другими причинами возражений.")
    items.append("После 2–4 целевых тренировок повторить сопоставимую серию и сравнить конверсию и прогноз выполнения плана.")
    return items[:4]


def combined_report(store, series, results):
    metrics = series_metrics(series, results)
    knowledge = _knowledge(store.path, series["user_id"])
    projection = series.get("business_projection")
    lines = [
        "CENTARO · ИТОГ ЭФФЕКТИВНОСТИ",
        f"Серия №{series['id']} · {series['config']['count']} звонков",
        "",
        "ДИАГНОСТИКА",
        "Навыки: " + (f"{metrics['skill_score']:.0f}/100" if metrics["skill_score"] is not None else "недостаточно проверенных оценок"),
        "Знания: " + (
            f"{knowledge['score']}/100 · аттестовано {knowledge['assessed']}/{knowledge['total']} блоков"
            if knowledge["score"] is not None
            else "аттестации по базе знаний ещё не пройдены"
        ),
        "Учебная конверсия: " + (
            f"{metrics['goal_achieved']}/{metrics['calls_verified']} ({metrics['training_conversion']:.0%})"
            if metrics["training_conversion"] is not None
            else "недостаточно данных"
        ),
        f"Качественных звонков (60+/100): {metrics['quality_calls']}.",
    ]

    if metrics["outcomes"]:
        visible = []
        for key in ("purchase", "meeting", "proposal", "next_step", "samples", "referral", "refusal", "no_agreement"):
            if metrics["outcomes"].get(key):
                visible.append(f"{call_series.OUTCOMES[key]} — {metrics['outcomes'][key]}")
        if visible:
            lines += ["Воронка серии: " + "; ".join(visible) + "."]

    if projection:
        lines += [
            "",
            "МОДЕЛЬ ВЫПОЛНЕНИЯ ПЛАНА",
            f"Средний чек: {_money(projection['average_check'])}",
            f"Целевых звонков/контактов в месяц: {projection['monthly_contacts']:.0f}",
        ]
        if _needs_downstream(series):
            lines.append(f"Конверсия целевого результата в оплату: {projection['downstream_percent']:.0f}%")
        lines += [
            f"Расчётное число продаж: {projection['expected_sales']:.1f} в месяц",
            f"Модельный потенциал выручки: {_money(projection['projected_revenue'])}",
            f"Личный план: {_money(projection['monthly_plan'])}",
            f"Модельное выполнение плана: {projection['plan_percent']:.0f}%",
            "Это модель по результатам учебной серии и вашим коммерческим параметрам, а не гарантия реальных продаж.",
        ]

        previous = series.get("comparison_projection")
        if previous:
            prev_conv = previous.get("training_conversion")
            current_conv = projection.get("training_conversion")
            lines += ["", "ДИНАМИКА · ТОЧКА А → СЕЙЧАС"]
            if prev_conv is not None and current_conv is not None:
                pp = (current_conv - prev_conv) * 100
                if prev_conv > 0:
                    relative = (current_conv / prev_conv - 1) * 100
                    lines.append(f"Учебная конверсия: {prev_conv:.0%} → {current_conv:.0%} ({pp:+.1f} п.п.; {relative:+.0f}% относительно точки А).")
                else:
                    lines.append(f"Учебная конверсия: 0% → {current_conv:.0%} ({pp:+.1f} п.п.).")
            if previous.get("plan_percent") is not None and projection.get("plan_percent") is not None:
                lines.append(f"Модель выполнения плана: {previous['plan_percent']:.0f}% → {projection['plan_percent']:.0f}% ({projection['plan_percent']-previous['plan_percent']:+.0f} п.п.).")
            if previous.get("skill_score") is not None and projection.get("skill_score") is not None:
                lines.append(f"Навыки: {previous['skill_score']:.0f}/100 → {projection['skill_score']:.0f}/100 ({projection['skill_score']-previous['skill_score']:+.0f}).")
        else:
            lines += ["", "ТОЧКА А СОХРАНЕНА", "Следующая сопоставимая серия покажет изменение навыков, учебной конверсии и модельного выполнения плана."]

    lines += ["", "CENTARO · НАВИГАТОР ПО ТЕХНИКЕ"]
    for index, item in enumerate(_navigator(metrics, knowledge), 1):
        lines.append(f"{index}. {item}")
    return "\n".join(lines)


def _prompt(series):
    setup = series.get("projection_setup") or {}
    step = setup.get("step")
    if step == "average_check":
        return (
            "Чтобы перевести учебную серию в бизнес-цифры, CENTARO задаст несколько коротких вопросов.\n\n"
            "1. Какой у менеджера средний чек в рублях?\nНапример: 150000\n\n"
            "/forecast_skip — пропустить бизнес-прогноз."
        )
    if step == "monthly_contacts":
        return "2. Сколько целевых звонков/контактов этот менеджер обычно делает за месяц?\nНапример: 120"
    if step == "monthly_plan":
        return "3. Какой у менеджера личный план выручки на месяц в рублях?\nНапример: 2000000"
    if step == "downstream_percent":
        return (
            "Последний вопрос нужен, чтобы не выдавать встречу или КП за реальную продажу.\n\n"
            "Какой процент таких успешных целевых результатов у вас обычно превращается в оплату?\n"
            "Например: из 10 встреч/КП 3 заканчиваются сделкой → 30%."
        )
    return ""


def _find_pending_for_actor(store, actor_id):
    with store.db() as db:
        rows = db.execute("SELECT payload FROM call_series WHERE status='completed' ORDER BY id DESC LIMIT 100").fetchall()
    for row in rows:
        series = json.loads(row["payload"])
        setup = series.get("projection_setup") or {}
        if int(setup.get("actor_id") or 0) == int(actor_id) and setup.get("step"):
            return series
    return None


def _ack_projection(store, event, series, replies):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE inbox SET status='done' WHERE id=?", (event["id"],))
        db.execute("UPDATE call_series SET payload=? WHERE id=?", (json.dumps(series, ensure_ascii=False), series["id"]))
        for reply in replies:
            for part in chunks(reply):
                db.execute(
                    "INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)",
                    (event["user_id"], event["chat_id"], part),
                )


def _queue_once(store, recipient, key, body):
    recipient = int(recipient)
    with store.db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS notifications(user_id INTEGER NOT NULL,key TEXT NOT NULL,PRIMARY KEY(user_id,key))"
        )
        inserted = db.execute(
            "INSERT OR IGNORE INTO notifications(user_id,key) VALUES(?,?)",
            (recipient, key),
        ).rowcount
        if inserted:
            for part in chunks(body):
                db.execute(
                    "INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)",
                    (recipient, recipient, part),
                )
    return bool(inserted)


def _supervisors(engine, series):
    recipients = set()
    assigned = series.get("assigned_by")
    if assigned:
        recipients.add(int(assigned))
    try:
        if get_role(engine.store.path, series["user_id"]) != PUBLIC:
            recipients.update(int(value) for value in engine.admin_ids)
    except Exception:
        pass
    recipients.discard(int(series["user_id"]))
    return recipients


def _notify_base_report(engine, series, results):
    recipients = _supervisors(engine, series)
    if not recipients:
        return
    base = _ORIGINAL_REPORT(series, results)
    actor = int((series.get("projection_setup") or {}).get("actor_id") or 0)
    for recipient in recipients:
        text = "CENTARO · СЕРИЯ СОТРУДНИКА ЗАВЕРШЕНА\n\n" + base
        if recipient == actor:
            text += "\n\n" + _prompt(series)
        else:
            text += "\n\nБизнес-прогноз будет добавлен после ввода коммерческих параметров ответственным руководителем."
        _queue_once(engine.store, recipient, f"centaro_series_base_v1:{series['id']}", text)


def _notify_final_report(engine, series, text, actor):
    for recipient in _supervisors(engine, series):
        if int(recipient) == int(actor):
            continue
        _queue_once(engine.store, recipient, f"centaro_series_business_v1:{series['id']}", text)


def _patched_report(series, results):
    base = _ORIGINAL_REPORT(series, results)
    base = base.replace(
        "Учебная конверсия не является прогнозом реальных продаж.",
        "Учебная конверсия показывает результат в симуляции; бизнес-прогноз CENTARO рассчитывается отдельно по коммерческим параметрам компании.",
    )
    if series.get("business_projection"):
        # report() has no Store argument; the complete combined report is sent after
        # the business inputs and remains available through the saved projection.
        projection = series["business_projection"]
        base += (
            "\n\nБИЗНЕС-МОДЕЛЬ CENTARO\n"
            f"Модельный потенциал: {_money(projection['projected_revenue'])} в месяц.\n"
            f"Модельное выполнение плана: {projection['plan_percent']:.0f}%.\n"
            "Подробный отчёт содержит знания, навыки, динамику и навигатор по технике."
        )
    else:
        setup = series.get("projection_setup") or {}
        if setup.get("step") and int(setup.get("actor_id") or 0) == int(series.get("user_id") or 0):
            base += "\n\n" + _prompt(series)
    return base


def _patched_finish(self, event, s, series, reason, interrupted=False):
    final_normal = (
        not interrupted
        and reason not in ("employee_abort", "technical_abort")
        and series.get("index") == len(series.get("manifest") or []) - 1
    )
    if final_normal and not series.get("business_projection") and not series.get("projection_setup"):
        actor = int(series.get("assigned_by") or series["user_id"])
        series["projection_setup"] = {"actor_id": actor, "step": "average_check", "data": {}}

    result = _ORIGINAL_FINISH(self, event, s, series, reason, interrupted)
    if final_normal and series.get("status") == "completed":
        results = self.store.series_results(series["id"])
        _notify_base_report(self.engine, series, results)
    return result


def _patched_handle(self, event, s):
    user = int(event["user_id"])
    text = str(event.get("text") or "").strip()
    pending = _find_pending_for_actor(self.store, user)
    if not pending:
        return _ORIGINAL_HANDLE(self, event, s)

    command = text.lower().strip()
    if command.startswith("/series") or command in {"серия звонков", "/start", "/cancel"}:
        return _ORIGINAL_HANDLE(self, event, s)

    setup = pending["projection_setup"]
    data = setup.setdefault("data", {})
    if command in {"/forecast_skip", "пропустить прогноз", "пропустить"}:
        setup["step"] = None
        pending["projection_skipped"] = True
        _ack_projection(self.store, event, pending, ["Бизнес-прогноз пропущен. Учебный итог серии сохранён."])
        return True

    try:
        if setup["step"] == "average_check":
            data["average_check"] = _number(text, minimum=1)
            setup["step"] = "monthly_contacts"
            _ack_projection(self.store, event, pending, [_prompt(pending)])
            return True
        if setup["step"] == "monthly_contacts":
            data["monthly_contacts"] = _number(text, minimum=1, maximum=100000)
            setup["step"] = "monthly_plan"
            _ack_projection(self.store, event, pending, [_prompt(pending)])
            return True
        if setup["step"] == "monthly_plan":
            data["monthly_plan"] = _number(text, minimum=1)
            if _needs_downstream(pending):
                setup["step"] = "downstream_percent"
                _ack_projection(self.store, event, pending, [_prompt(pending)])
                return True
            data["downstream_percent"] = 100.0
        elif setup["step"] == "downstream_percent":
            data["downstream_percent"] = _number(text, minimum=0, maximum=100, percent=True)
        else:
            return _ORIGINAL_HANDLE(self, event, s)

        results = self.store.series_results(pending["id"])
        projection, _ = build_projection(pending, results, data)
        previous = _previous_projection(self.store, pending)
        pending["business_projection"] = projection
        pending["comparison_projection"] = previous.get("business_projection") if previous else None
        pending["comparison_series_id"] = previous.get("id") if previous else None
        pending["projection_setup"] = None
        text_report = combined_report(self.store, pending, results)
        _ack_projection(self.store, event, pending, [text_report])
        _notify_final_report(self.engine, pending, text_report, user)
        return True
    except ValueError as exc:
        _ack_projection(self.store, event, pending, ["Проверьте ответ: " + str(exc) + "\n\n" + _prompt(pending)])
        return True


def _branded_series_hub(bot, chat_id):
    store = series_ux.Store(series_ux._db_path())
    current = store.open_series(int(chat_id))
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)

    if current:
        count = current["config"]["count"]
        if current["status"] == "active":
            text = (
                f"CENTARO · СЕРИЯ ЗВОНКОВ\n\nСерия №{current['id']} уже идёт: {count} звонков. "
                "Во время серии нет подсказок и промежуточного разбора."
            )
        else:
            source = "назначена руководителем" if current["mode"] == "attestation" else "подготовлена вами"
            text = (
                f"CENTARO · СЕРИЯ ЗВОНКОВ\n\nУ вас есть серия №{current['id']} на {count} звонков — {source}.\n\n"
                "После старта звонки идут подряд. Подсказок, паузы и разбора между звонками нет. "
                "В конце CENTARO покажет оценку навыков, учебную конверсию и воронку результатов."
            )
            markup.add(
                telebot.types.InlineKeyboardButton(
                    "▶️ Начать назначенную серию" if current["mode"] == "attestation" else "▶️ Начать серию",
                    callback_data="academyv2:series:begin",
                )
            )
    else:
        text = (
            "CENTARO · СЕРИЯ ЗВОНКОВ\n\n"
            "Имитация рабочего режима менеджера: 10, 20 или 30 разных разговоров подряд.\n\n"
            "Вы задаёте продукт, клиента, цели, типовые и собственные ситуации. Перед стартом состав "
            "фиксируется и перемешивается. Во время серии нет подсказок и промежуточного разбора.\n\n"
            "После серии CENTARO покажет:\n"
            "• оценку навыков по 100-балльной системе;\n"
            "• отработку возражений и воронку исходов;\n"
            "• учебную конверсию;\n"
            "• навигатор по технике;\n"
            "• при вводе бизнес-параметров — модель выполнения плана и динамику к предыдущей серии."
        )
        markup.add(telebot.types.InlineKeyboardButton("🎯 Настроить мою серию", callback_data="academyv2:series:self"))
        if series_ux._is_supervisor(chat_id):
            markup.add(telebot.types.InlineKeyboardButton("👥 Назначить серию сотруднику", callback_data="academyv2:series:assign"))

    markup.add(telebot.types.InlineKeyboardButton("📋 Мои серии", callback_data="academyv2:series:list"))
    markup.add(telebot.types.InlineKeyboardButton("← В Академию", callback_data="academyv2:home"))
    bot.send_message(chat_id, text, reply_markup=markup)


def _branded_wizard_prompt(draft):
    value = _ORIGINAL_WIZARD_PROMPT(draft)
    if draft.get("step") == "confirm":
        value += (
            "\n\nПосле серии CENTARO рассчитает учебную конверсию и воронку. "
            "Для бизнес-прогноза система отдельно запросит средний чек, месячный объём целевых контактов и план."
        )
    return value


def install():
    global _INSTALLED, _ORIGINAL_HANDLE, _ORIGINAL_FINISH, _ORIGINAL_REPORT
    global _ORIGINAL_SERIES_HUB, _ORIGINAL_WIZARD_PROMPT
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_HANDLE = call_series.SeriesEngine.handle
    _ORIGINAL_FINISH = call_series.SeriesEngine.finish
    _ORIGINAL_REPORT = call_series.report
    _ORIGINAL_SERIES_HUB = series_ux._series_hub
    _ORIGINAL_WIZARD_PROMPT = series_ux._wizard_prompt

    call_series.SeriesEngine.handle = _patched_handle
    call_series.SeriesEngine.finish = _patched_finish
    call_series.report = _patched_report
    series_ux._series_hub = _branded_series_hub
    series_ux._wizard_prompt = _branded_wizard_prompt
