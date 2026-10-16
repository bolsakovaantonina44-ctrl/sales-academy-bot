"""ROP-grade management layer for CENTARO call-series assessments.

Adds management context without changing the underlying evidence-based scoring engine:
- lead-flow type (cold, warm, inbound, existing, reactivation);
- company target conversion benchmark for the selected KPI;
- sample-size / statistical confidence labels for 10/20/30-call series;
- comparable-series matching that includes flow type;
- critical-skill red flags for supervisors;
- clearer Training Conversion vs Business Conversion language;
- typical/median check wording for business forecasting.
"""
import json
import math

from academy import call_series
from academy import centaro_performance_runtime as performance
from academy import centaro_role_reports_runtime as role_reports
from academy import series_ux_runtime as series_ux
from academy.domain import normalize_command


_INSTALLED = False
_ORIGINAL_WIZARD = None
_ORIGINAL_PROMPT = None
_ORIGINAL_ROWS = None
_ORIGINAL_EMPLOYEE_REPORT = None
_ORIGINAL_SUPERVISOR_REPORT = None
_ORIGINAL_FORECAST_PROMPT = None
_ORIGINAL_SAME_SCENARIO = None


FLOW_TYPES = {
    "cold": {
        "label": "Холодный контакт",
        "button": "❄️ Холодный контакт",
        "opening": "Добрый день. Слушаю вас.",
        "chat_opening": "Здравствуйте. По какому вопросу?",
        "context": "Первичный холодный контакт: клиент не ожидал обращения и пока не проявлял интерес.",
    },
    "warm": {
        "label": "Тёплый контакт",
        "button": "🌤 Тёплый контакт",
        "opening": "Добрый день. Да, мне передали, что вы можете быть полезны. Слушаю вас.",
        "chat_opening": "Здравствуйте. Да, мне передали ваш контакт. Что вы предлагаете?",
        "context": "Тёплый контакт: у клиента уже есть предварительный интерес или рекомендация.",
    },
    "inbound": {
        "label": "Входящий лид / обратный звонок",
        "button": "📥 Входящий лид",
        "opening": "Здравствуйте. Да, я оставлял заявку. Расскажите, пожалуйста.",
        "chat_opening": "Здравствуйте! Да, я оставлял заявку. Расскажите подробнее.",
        "context": "Входящий лид: клиент ранее проявил интерес и оставил заявку или запрос.",
    },
    "existing": {
        "label": "Действующий клиент",
        "button": "🤝 Действующий клиент",
        "opening": "Добрый день. Да, мы с вами уже работаем. По какому вопросу звоните?",
        "chat_opening": "Добрый день. Да, мы с вами уже работаем. Что хотели?",
        "context": "Действующий клиент: отношения уже существуют, задача — развить или продолжить сотрудничество.",
    },
    "reactivation": {
        "label": "Возврат / реактивация клиента",
        "button": "♻️ Возврат клиента",
        "opening": "Добрый день. Да, помню, мы раньше общались. Что у вас сейчас?",
        "chat_opening": "Здравствуйте. Помню, мы раньше общались. Что у вас нового?",
        "context": "Реактивация: клиент ранее общался или покупал, но сейчас активной сделки нет.",
    },
}

CRITICAL_SKILLS = {
    "meeting": ("needs", "next_step"),
    "sale": ("needs", "objections", "next_step"),
    "deal": ("needs", "objections", "next_step"),
    "salon": ("contact", "needs", "next_step"),
    "proposal": ("needs", "arguments", "next_step"),
}


def _parse_flow(text):
    value = normalize_command(str(text or "")).replace("ё", "е")
    if "холод" in value or value == "1":
        return "cold"
    if "тепл" in value or "тёпл" in value or value == "2":
        return "warm"
    if "вход" in value or "лид" in value or value == "3":
        return "inbound"
    if "действ" in value or "текущ" in value or value == "4":
        return "existing"
    if "возврат" in value or "реактив" in value or value == "5":
        return "reactivation"
    return None


def _parse_percent(text):
    value = str(text or "").strip().lower().replace("%", "").replace(",", ".")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError("Введите число от 1 до 100, например 25.") from exc
    if not 1 <= result <= 100:
        raise ValueError("Введите число от 1 до 100, например 25.")
    return result


def _flow_label(series):
    code = series.get("flow_type")
    return FLOW_TYPES.get(code, {}).get("label", "не указан")


def _sample_label(count):
    count = int(count or 0)
    if count <= 10:
        return "экспресс-диагностика"
    if count <= 20:
        return "рабочая оценка"
    return "расширенная аттестация"


def _sample_note(count):
    count = int(count or 0)
    if count <= 10:
        return "10 звонков дают быстрый ориентир: один звонок меняет конверсию сразу на 10 п.п."
    if count <= 20:
        return "20 звонков дают рабочую оценку для развития и контроля динамики."
    return "30 звонков дают наиболее устойчивую из доступных в CENTARO оценку для аттестации и сравнения во времени."


def _wilson_interval(successes, total, z=1.96):
    total = int(total or 0)
    successes = int(successes or 0)
    if total <= 0:
        return None
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _critical_flags(series, metrics):
    code = (series.get("conversion_goal") or {}).get("code")
    keys = CRITICAL_SKILLS.get(code, ("needs", "objections", "next_step"))
    normalized = metrics.get("skill_normalized") or {}
    flags = []
    for key in keys:
        if key not in normalized:
            continue
        score = float(normalized[key])
        if score < 50:
            label = performance.SKILL_NAV.get(key, (key, ""))[0]
            flags.append((label, score))
    return flags


def _prompt(draft):
    step = draft.get("step")
    if step == "flow_type":
        return (
            "ТИП ПОТОКА\n\n"
            "С каким типом клиентов менеджер работает в этой серии?\n\n"
            "Это важно: холодный звонок нельзя честно сравнивать с входящим лидом или действующим клиентом.\n\n"
            "1. Холодный контакт\n"
            "2. Тёплый контакт\n"
            "3. Входящий лид / обратный звонок\n"
            "4. Действующий клиент\n"
            "5. Возврат / реактивация клиента"
        )
    if step == "target_conversion":
        goal = (draft.get("conversion_goal") or {}).get("label", "выбранной цели")
        return (
            "НОРМАТИВ КОНВЕРСИИ\n\n"
            f"Какой процент конверсии в «{goal}» считается целевым для этого типа клиентов в вашей компании?\n\n"
            "Например: 25 означает, что норматив — 25%.\n"
            "CENTARO сравнит фактический результат серии с этим нормативом."
        )

    value = _ORIGINAL_PROMPT(draft)
    if step == "confirm" and draft.get("mode") == "attestation":
        flow = FLOW_TYPES.get(draft.get("flow_type"), {}).get("label")
        target = draft.get("target_conversion_pct")
        extras = []
        if flow:
            extras.append(f"Тип потока: {flow}.")
        if target is not None:
            extras.append(f"Норматив конверсии: {target:g}%.")
        if extras:
            value += "\n" + "\n".join(extras)
    return value


def _rows(session):
    draft = session.get("series_setup")
    if draft and draft.get("step") == "flow_type":
        return [
            [FLOW_TYPES["cold"]["button"], FLOW_TYPES["warm"]["button"]],
            [FLOW_TYPES["inbound"]["button"]],
            [FLOW_TYPES["existing"]["button"]],
            [FLOW_TYPES["reactivation"]["button"]],
            ["/cancel"],
        ]
    if draft and draft.get("step") == "target_conversion":
        return [["10%", "20%", "30%"], ["40%", "50%"], ["/cancel"]]
    return _ORIGINAL_ROWS(session)


def _apply_flow_to_manifest(series, flow_code):
    flow = FLOW_TYPES.get(flow_code)
    if not flow:
        return
    for call in series.get("manifest") or []:
        card = call.get("card") or {}
        difficulty = call.get("difficulty")
        chat = call.get("channel") == "chat"
        opening = flow["chat_opening"] if chat else flow["opening"]
        if difficulty == "hard":
            opening += " Только коротко, пожалуйста." if chat else " Только давайте коротко, у меня мало времени."
        card["opening"] = opening
        context = str(card.get("company_context") or "").strip()
        if flow["context"] not in context:
            card["company_context"] = flow["context"] + (" " + context if context else "")
        background = card.get("background") or {}
        situation = str(background.get("situation") or "").strip()
        if flow["context"] not in situation:
            background["situation"] = flow["context"] + (" " + situation if situation else "")


def _persist_management_context(store, target, before_id, flow_code, target_pct):
    current = store.open_series(int(target))
    if not current or current.get("id") == before_id:
        return
    current["flow_type"] = flow_code
    current["target_conversion_pct"] = float(target_pct)
    current["assessment_level"] = _sample_label(current.get("config", {}).get("count"))
    _apply_flow_to_manifest(current, flow_code)
    current["manifest_hash"] = call_series.manifest_hash(current["manifest"])
    with store.db() as db:
        db.execute(
            "UPDATE call_series SET payload=? WHERE id=? AND user_id=?",
            (json.dumps(current, ensure_ascii=False), int(current["id"]), int(target)),
        )
        suffix = (
            f"\nТип потока: {_flow_label(current)}."
            f"\nНорматив конверсии: {float(target_pct):g}%."
        )
        db.execute(
            "UPDATE outbox SET body=body||? WHERE user_id=? AND sent=0 AND body LIKE ?",
            (suffix, int(target), f"%серия №{int(current['id'])}%"),
        )


def _wizard(self, event, s):
    draft = s.get("series_setup")
    if not draft or draft.get("mode") != "attestation":
        return _ORIGINAL_WIZARD(self, event, s)

    step = draft.get("step")
    text = str(event.get("text") or "").strip()
    cmd = normalize_command(text)
    if cmd in ("/cancel", "отмена", "к академии"):
        return _ORIGINAL_WIZARD(self, event, s)

    if step == "customer":
        if not 2 <= len(text) <= 300:
            self.store.commit(event, s, ["Проверьте ответ: введите от 2 до 300 символов."])
            return True
        draft["data"]["customer"] = text
        draft["step"] = "flow_type"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "flow_type":
        code = _parse_flow(text)
        if not code:
            self.store.commit(event, s, ["Проверьте ответ: выберите тип потока кнопкой ниже.\n\n" + _prompt(draft)])
            return True
        draft["flow_type"] = code
        draft["step"] = "conversion_goal"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "conversion_goal":
        code = role_reports._parse_conversion_goal(text)
        if not code:
            self.store.commit(event, s, ["Проверьте ответ: выберите одну цель конверсии кнопкой ниже.\n\n" + _prompt(draft)])
            return True
        selected = dict(role_reports.CONVERSION_GOALS[code])
        selected["code"] = code
        draft["conversion_goal"] = selected
        draft["data"]["goals"] = [selected["engine_goal"]]
        draft["step"] = "target_conversion"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "target_conversion":
        try:
            target = _parse_percent(text)
        except ValueError as exc:
            self.store.commit(event, s, ["Проверьте ответ: " + str(exc) + "\n\n" + _prompt(draft)])
            return True
        draft["target_conversion_pct"] = target
        draft["step"] = "objections"
        self.store.commit(event, s, [_prompt(draft)])
        return True

    if step == "confirm":
        target_user = int(draft.get("target") or event["user_id"])
        flow_code = draft.get("flow_type")
        target_pct = draft.get("target_conversion_pct")
        before = self.store.open_series(target_user)
        before_id = before.get("id") if before else None
        result = _ORIGINAL_WIZARD(self, event, s)
        if flow_code and target_pct is not None:
            _persist_management_context(self.store, target_user, before_id, flow_code, target_pct)
        return result

    return _ORIGINAL_WIZARD(self, event, s)


def _benchmark_block(series, metrics):
    target = series.get("target_conversion_pct")
    conversion = metrics.get("training_conversion")
    if target is None or conversion is None:
        return []
    actual = conversion * 100
    gap = actual - float(target)
    status = "выше норматива" if gap > 0.05 else "ниже норматива" if gap < -0.05 else "на уровне норматива"
    return [
        f"Норматив компании: {float(target):g}%.",
        f"Результат серии: {actual:.0f}% — {status} ({gap:+.1f} п.п.).",
    ]


def _employee_report(series, results):
    text = _ORIGINAL_EMPLOYEE_REPORT(series, results)
    metrics = performance.series_metrics(series, results)
    count = int(series.get("config", {}).get("count") or metrics.get("calls_saved") or 0)
    block = [
        "",
        "КОНТЕКСТ ОЦЕНКИ",
        f"Тип потока: {_flow_label(series)}.",
        f"Формат: {count} звонков — {_sample_label(count)}.",
        _sample_note(count),
    ]
    block += _benchmark_block(series, metrics)
    return text + "\n" + "\n".join(block)


def _supervisor_report(store, series, results):
    text = _ORIGINAL_SUPERVISOR_REPORT(store, series, results)
    metrics = performance.series_metrics(series, results)
    count = int(series.get("config", {}).get("count") or metrics.get("calls_saved") or 0)
    interval = _wilson_interval(metrics.get("goal_achieved"), metrics.get("calls_verified"))
    block = [
        "",
        "КОНТЕКСТ И НАДЁЖНОСТЬ ОЦЕНКИ",
        f"Тип потока: {_flow_label(series)}.",
        f"Выборка: {count} звонков — {_sample_label(count)}.",
        _sample_note(count),
    ]
    block += _benchmark_block(series, metrics)
    if interval:
        block.append(
            f"Статистический ориентир для учебной конверсии (95%): {interval[0]*100:.0f}–{interval[1]*100:.0f}%. "
            "Не использовать одну серию как единственное основание для кадрового решения."
        )

    flags = _critical_flags(series, metrics)
    if flags:
        block += ["", "КРИТИЧЕСКИЕ КОМПЕТЕНЦИИ"]
        for label, score in flags:
            block.append(f"⚠️ {label}: {score:.0f}% от максимума — ниже контрольного порога 50%.")
        block.append("Общий балл сам по себе не перекрывает критический провал по ключевой компетенции.")

    projection = series.get("business_projection") or {}
    if projection:
        downstream = projection.get("downstream_percent")
        block += ["", "ДВЕ КОНВЕРСИИ"]
        tc = metrics.get("training_conversion")
        if tc is not None:
            block.append(f"Учебная конверсия: {tc:.0%} — результат в учебной симуляции по выбранной цели.")
        if downstream is not None:
            block.append(f"Конверсия в оплату: {float(downstream):.0f}% — переход выбранного результата в фактическую оплату по данным компании.")

    text = text.replace("Средний чек:", "Типичный чек для модели:")
    text = text.replace("Учебная конверсия:", "Учебная конверсия:")
    text = text.replace("Конверсия целевого результата в оплату:", "Конверсия в оплату (цель → оплата):")
    return text + "\n" + "\n".join(block)


def _forecast_prompt(series):
    setup = series.get("projection_setup") or {}
    if setup.get("step") == "average_check":
        return (
            "Чтобы перевести учебную серию в бизнес-цифры, CENTARO задаст несколько коротких вопросов.\n\n"
            "1. Какой у менеджера типичный чек сделки в рублях?\n"
            "Если чеки сильно различаются, укажите медианный (типичный), а не самый крупный.\n"
            "Например: 150000\n\n"
            "/forecast_skip — пропустить бизнес-прогноз."
        )
    return _ORIGINAL_FORECAST_PROMPT(series)


def _same_scenario(a, b):
    if not _ORIGINAL_SAME_SCENARIO(a, b):
        return False
    if (a.get("flow_type") or "") != (b.get("flow_type") or ""):
        return False
    ag = (a.get("conversion_goal") or {}).get("code")
    bg = (b.get("conversion_goal") or {}).get("code")
    return ag == bg


def install():
    global _INSTALLED, _ORIGINAL_WIZARD, _ORIGINAL_PROMPT, _ORIGINAL_ROWS
    global _ORIGINAL_EMPLOYEE_REPORT, _ORIGINAL_SUPERVISOR_REPORT
    global _ORIGINAL_FORECAST_PROMPT, _ORIGINAL_SAME_SCENARIO
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_WIZARD = call_series.SeriesEngine.wizard
    _ORIGINAL_PROMPT = series_ux._wizard_prompt
    _ORIGINAL_ROWS = series_ux._wizard_rows
    _ORIGINAL_EMPLOYEE_REPORT = call_series.report
    _ORIGINAL_SUPERVISOR_REPORT = performance.combined_report
    _ORIGINAL_FORECAST_PROMPT = performance._prompt
    _ORIGINAL_SAME_SCENARIO = performance._same_scenario

    call_series.SeriesEngine.wizard = _wizard
    series_ux._wizard_prompt = _prompt
    series_ux._wizard_rows = _rows
    call_series.wizard_prompt = _prompt
    call_series.wizard_rows = _rows
    call_series.report = _employee_report
    performance.combined_report = _supervisor_report
    performance._prompt = _forecast_prompt
    performance._same_scenario = _same_scenario
