"""CENTARO role-specific reporting and supervisor conversion KPI layer.

A supervisor assigning a call series chooses ONE business conversion target before the
series is fixed. The target is translated to the existing evidence-checked call goal,
so conversion is measured from actual bilateral call outcomes rather than from a loose
marketing label.

Employees receive a development report. Supervisors receive the commercial / management
report with knowledge, skills, funnel, forecast, dynamics and the technique navigator.
"""
import json

from academy import call_series
from academy import series_ux_runtime as series_ux
from academy import centaro_performance_runtime as performance
from academy.domain import normalize_command


_INSTALLED = False
_ORIGINAL_WIZARD = None
_ORIGINAL_PROMPT = None
_ORIGINAL_ROWS = None
_ORIGINAL_REPORT = None
_ORIGINAL_COMBINED_REPORT = None


CONVERSION_GOALS = {
    "meeting": {
        "label": "Встреча",
        "engine_goal": "meeting",
        "button": "🤝 Встреча",
        "meaning": "засчитывается только согласованная встреча с конкретным следующим действием",
    },
    "sale": {
        "label": "Продажа / подтверждённый заказ",
        "engine_goal": "purchase",
        "button": "💳 Продажа / заказ",
        "meaning": "засчитывается только подтверждённый клиентом заказ или покупка",
    },
    "deal": {
        "label": "Получение сделки / квалифицированная заявка",
        "engine_goal": "next_step",
        "button": "📌 Сделка / квалифицированная заявка",
        "meaning": "засчитывается конкретная квалифицированная договорённость о продолжении сделки",
    },
    "salon": {
        "label": "Приглашение / визит в салон",
        "engine_goal": "meeting",
        "button": "🏬 Приглашение в салон",
        "meaning": "засчитывается согласие клиента приехать/прийти в салон с конкретным условием или временем",
    },
    "proposal": {
        "label": "КП / счёт с дальнейшим обсуждением",
        "engine_goal": "proposal",
        "button": "📄 КП / счёт",
        "meaning": "засчитывается релевантное предложение, которое клиент согласился предметно обсуждать",
    },
}


def _parse_conversion_goal(text):
    value = normalize_command(str(text or "")).replace("ё", "е")
    if "салон" in value:
        return "salon"
    if "встреч" in value:
        return "meeting"
    if "сделк" in value or "заяв" in value:
        return "deal"
    if "продаж" in value or "заказ" in value or "покуп" in value:
        return "sale"
    if "кп" in value or "счет" in value or "предлож" in value:
        return "proposal"
    aliases = {"1": "meeting", "2": "sale", "3": "deal", "4": "salon", "5": "proposal"}
    return aliases.get(value)


def _goal_label(series):
    selected = series.get("conversion_goal") or {}
    if selected.get("label"):
        return selected["label"]
    goals = list(series.get("config", {}).get("goals") or [])
    if len(goals) == 1 and goals[0] in call_series.GOALS:
        return call_series.GOALS[goals[0]][0]
    if goals:
        return "одна из целей серии"
    return "целевой результат"


def _prompt(draft):
    if draft.get("step") == "conversion_goal":
        return (
            "ЦЕЛЬ КОНВЕРСИИ\n\n"
            "Что именно в этой серии считать конверсией?\n\n"
            "Выберите ОДИН бизнес-результат. CENTARO будет считать процент выполнения именно по нему — "
            "не по качеству разговора и не по любому положительному исходу.\n\n"
            "1. Встреча\n"
            "2. Продажа / подтверждённый заказ\n"
            "3. Получение сделки / квалифицированная заявка\n"
            "4. Приглашение / визит в салон\n"
            "5. КП / счёт с дальнейшим обсуждением"
        )
    value = _ORIGINAL_PROMPT(draft)
    if draft.get("step") == "confirm" and draft.get("mode") == "attestation":
        selected = draft.get("conversion_goal") or {}
        if selected.get("label"):
            value += f"\n\nЦель конверсии: {selected['label']}."
    return value


def _rows(session):
    draft = session.get("series_setup")
    if draft and draft.get("step") == "conversion_goal":
        return [
            [CONVERSION_GOALS["meeting"]["button"]],
            [CONVERSION_GOALS["sale"]["button"]],
            [CONVERSION_GOALS["deal"]["button"]],
            [CONVERSION_GOALS["salon"]["button"]],
            [CONVERSION_GOALS["proposal"]["button"]],
            ["/cancel"],
        ]
    return _ORIGINAL_ROWS(session)


def _persist_goal(store, target, before_id, selected):
    current = store.open_series(int(target))
    if not current or current.get("id") == before_id:
        return
    current["conversion_goal"] = selected
    with store.db() as db:
        db.execute(
            "UPDATE call_series SET payload=? WHERE id=? AND user_id=?",
            (json.dumps(current, ensure_ascii=False), int(current["id"]), int(target)),
        )
        # The original assignment notice has already been queued but normally has not yet
        # been sent. Add the KPI so the employee knows what the series is measuring.
        suffix = f"\n\nЦель серии: {selected['label']}."
        db.execute(
            "UPDATE outbox SET body=body||? WHERE user_id=? AND sent=0 AND body LIKE ?",
            (suffix, int(target), f"%серия №{int(current['id'])}%"),
        )
        db.execute(
            "INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)",
            (
                int(current.get("assigned_by") or 0),
                int(current.get("assigned_by") or 0),
                f"Цель конверсии серии №{current['id']}: {selected['label']}.",
            ),
        )


def _wizard(self, event, s):
    draft = s.get("series_setup")
    if not draft or draft.get("mode") != "attestation":
        return _ORIGINAL_WIZARD(self, event, s)

    step = draft.get("step")
    text = str(event.get("text") or "").strip()
    cmd = normalize_command(text)
    if cmd in ("/cancel", "отмена"):
        return _ORIGINAL_WIZARD(self, event, s)

    if step == "customer":
        if not 2 <= len(text) <= 300:
            self.store.commit(event, s, ["Проверьте ответ: введите от 2 до 300 символов."])
            return True
        draft["data"]["customer"] = text
        draft["step"] = "conversion_goal"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "conversion_goal":
        code = _parse_conversion_goal(text)
        if not code:
            self.store.commit(event, s, ["Проверьте ответ: выберите одну цель конверсии кнопкой ниже.\n\n" + _prompt(draft)])
            return True
        value = dict(CONVERSION_GOALS[code])
        value["code"] = code
        draft["conversion_goal"] = value
        # A supervisor-set series deliberately measures one fixed conversion event.
        draft["data"]["goals"] = [value["engine_goal"]]
        draft["step"] = "objections"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "confirm":
        selected = dict(draft.get("conversion_goal") or {})
        target = int(draft.get("target") or event["user_id"])
        before = self.store.open_series(target)
        before_id = before.get("id") if before else None
        result = _ORIGINAL_WIZARD(self, event, s)
        if selected:
            _persist_goal(self.store, target, before_id, selected)
        return result

    return _ORIGINAL_WIZARD(self, event, s)


def _skill_lines(metrics, limit=2):
    values = sorted(metrics.get("skill_normalized", {}).items(), key=lambda item: item[1])
    result = []
    for key, score in values[:limit]:
        label, action = performance.SKILL_NAV.get(key, (key, "Повторить целевую тренировку."))
        result.append((label, score, action))
    return result


def employee_summary(series, results):
    metrics = performance.series_metrics(series, results)
    conversion = metrics.get("training_conversion")
    lines = [
        "CENTARO · ВАШ РЕЗУЛЬТАТ",
        f"Серия: {series.get('config', {}).get('count', metrics['calls_saved'])} звонков",
        f"Цель конверсии: {_goal_label(series)}",
        "",
        "РЕЗУЛЬТАТ",
        "Навыки: " + (f"{metrics['skill_score']:.0f}/100" if metrics.get("skill_score") is not None else "недостаточно проверенных оценок"),
        "Учебная конверсия: " + (
            f"{metrics['goal_achieved']}/{metrics['calls_verified']} ({conversion:.0%})"
            if conversion is not None else "недостаточно данных"
        ),
        f"Качественных звонков 60+/100: {metrics['quality_calls']}/{metrics['calls_verified']}.",
    ]
    outcomes = metrics.get("outcomes") or {}
    visible = []
    for key in ("purchase", "meeting", "proposal", "next_step", "samples", "referral", "refusal", "no_agreement"):
        if outcomes.get(key):
            visible.append(f"{call_series.OUTCOMES[key]} — {outcomes[key]}")
    if visible:
        lines += ["", "ВОРОНКА", "; ".join(visible) + "."]

    gaps = _skill_lines(metrics)
    lines += ["", "ЧТО ТРЕНИРОВАТЬ ДАЛЬШЕ"]
    if gaps:
        for index, (label, score, action) in enumerate(gaps, 1):
            lines.append(f"{index}. {label} ({score:.0f}% от максимума): {action}")
    else:
        lines.append("1. Закрепить результат на более сложной серии.")
    lines += [
        "",
        "После 2–4 целевых тренировок повторите сопоставимую серию — CENTARO покажет изменение балла и учебной конверсии.",
    ]
    return "\n".join(lines)


def _strip_employee_business(text):
    marker = "\n\nБИЗНЕС-МОДЕЛЬ CENTARO"
    return text.split(marker, 1)[0] if marker in text else text


def _employee_report(series, results):
    base = _ORIGINAL_REPORT(series, results)
    if series.get("mode") == "attestation":
        base = _strip_employee_business(base)
    summary = employee_summary(series, results)
    return summary + "\n\nПОДРОБНЫЙ РАЗБОР СЕРИИ\n" + base


def _management_conclusion(series, metrics):
    projection = series.get("business_projection") or {}
    plan = projection.get("plan_percent")
    conversion = metrics.get("training_conversion")
    if plan is None:
        return "После ввода коммерческих параметров CENTARO рассчитает модель выполнения плана."
    if plan >= 100:
        return f"Модель показывает потенциал выполнения плана на {plan:.0f}% при сохранении заданного объёма контактов и текущей учебной конверсии."
    gap = max(0.0, 100.0 - plan)
    if plan >= 80:
        return f"Менеджер близок к плановой модели: {plan:.0f}% плана. Дефицит — {gap:.0f} п.п.; приоритет — точечно поднять конверсию по выбранной цели."
    conv = f"{conversion:.0%}" if conversion is not None else "н/д"
    return f"Текущая модель — {plan:.0f}% плана при учебной конверсии {conv}. До плановой модели не хватает {gap:.0f} п.п.; сначала устранить два слабейших навыка и повторить серию."


def supervisor_report(store, series, results):
    base = _ORIGINAL_COMBINED_REPORT(store, series, results)
    metrics = performance.series_metrics(series, results)
    name = series_ux._latest_name(store, series["user_id"])
    base = base.replace(
        "CENTARO · ИТОГ ЭФФЕКТИВНОСТИ",
        "CENTARO · ОТЧЁТ РУКОВОДИТЕЛЮ",
        1,
    )
    lines = base.splitlines()
    insert_at = 2 if len(lines) >= 2 else len(lines)
    lines[insert_at:insert_at] = [
        f"Сотрудник: {name}",
        f"Цель конверсии: {_goal_label(series)}",
    ]
    lines += ["", "УПРАВЛЕНЧЕСКИЙ ВЫВОД", _management_conclusion(series, metrics)]
    gaps = _skill_lines(metrics)
    if gaps:
        lines += ["", "ГДЕ ТЕРЯЕТСЯ РЕЗУЛЬТАТ"]
        for index, (label, score, action) in enumerate(gaps, 1):
            lines.append(f"{index}. {label} — {score:.0f}% от максимума. {action}")
    return "\n".join(lines)


def install():
    global _INSTALLED, _ORIGINAL_WIZARD, _ORIGINAL_PROMPT, _ORIGINAL_ROWS
    global _ORIGINAL_REPORT, _ORIGINAL_COMBINED_REPORT
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_WIZARD = call_series.SeriesEngine.wizard
    _ORIGINAL_PROMPT = series_ux._wizard_prompt
    _ORIGINAL_ROWS = series_ux._wizard_rows
    _ORIGINAL_REPORT = call_series.report
    _ORIGINAL_COMBINED_REPORT = performance.combined_report

    call_series.SeriesEngine.wizard = _wizard
    series_ux._wizard_prompt = _prompt
    series_ux._wizard_rows = _rows
    call_series.wizard_prompt = _prompt
    call_series.wizard_rows = _rows
    call_series.report = _employee_report
    performance.combined_report = supervisor_report
