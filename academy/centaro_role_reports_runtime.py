"""CENTARO role-specific reporting and supervisor conversion KPI layer.

A supervisor assigning a call series chooses ONE business conversion target before the
series is fixed. Employees receive a development report; supervisors receive the
commercial / management report with knowledge, skills, funnel, forecast, dynamics and
the technique navigator.

The conversion target is evidence checked. Deal and showroom are distinct goals rather
than aliases for any next step or any meeting.
"""
import copy
import json

from academy import call_series
from academy import series_ux_runtime as series_ux
from academy import centaro_performance_runtime as performance
from academy.ai import AI
from academy.domain import B, I, S, EvaluationError, arr, choice, normalize_command, obj, validate_card


_INSTALLED = False
_ORIGINAL_WIZARD = None
_ORIGINAL_PROMPT = None
_ORIGINAL_ROWS = None
_ORIGINAL_REPORT = None
_ORIGINAL_COMBINED_REPORT = None
_ORIGINAL_BUILD_MANIFEST = None
_ORIGINAL_AI_SERIES_OUTCOME = None
_ORIGINAL_FORECAST_PROMPT = None


EXTRA_GOALS = {
    "deal": (
        "Получить сделку",
        "Клиент явно согласился перейти к оформлению сделки: подтвердил заказ, договор, счёт или другой конкретный шаг оформления. Просто интерес, встреча или просьба прислать КП недостаточны.",
    ),
    "showroom": (
        "Пригласить в салон",
        "Клиент явно согласился приехать/прийти в салон или шоурум: согласован визит, время/дата либо однозначное условие визита. Одного приглашения менеджера без согласия клиента недостаточно.",
    ),
}

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
        "label": "Получение сделки",
        "engine_goal": "deal",
        "button": "📌 Получение сделки",
        "meaning": "засчитывается явный переход клиента к оформлению заказа/договора/счёта",
    },
    "salon": {
        "label": "Приглашение / визит в салон",
        "engine_goal": "showroom",
        "button": "🏬 Приглашение в салон",
        "meaning": "засчитывается явное согласие клиента приехать/прийти в салон",
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
    return {"1": "meeting", "2": "sale", "3": "deal", "4": "salon", "5": "proposal"}.get(value)


def _goal_label(series):
    selected = series.get("conversion_goal") or {}
    if selected.get("label"):
        return selected["label"]
    goals = list(series.get("config", {}).get("goals") or [])
    if len(goals) == 1 and goals[0] in call_series.GOALS:
        return call_series.GOALS[goals[0]][0]
    return "целевой результат" if not goals else "одна из целей серии"


def _prompt(draft):
    if draft.get("step") == "conversion_goal":
        return (
            "ЦЕЛЬ КОНВЕРСИИ\n\n"
            "Что именно в этой серии считать конверсией?\n\n"
            "Выберите ОДИН бизнес-результат. CENTARO будет считать процент именно по нему — "
            "не по качеству разговора и не по любому положительному исходу.\n\n"
            "1. Встреча\n"
            "2. Продажа / подтверждённый заказ\n"
            "3. Получение сделки\n"
            "4. Приглашение / визит в салон\n"
            "5. КП / счёт с дальнейшим обсуждением\n\n"
            "Цель фиксируется до старта и одинакова для всех звонков серии."
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
            [CONVERSION_GOALS["meeting"]["button"], CONVERSION_GOALS["sale"]["button"]],
            [CONVERSION_GOALS["deal"]["button"]],
            [CONVERSION_GOALS["salon"]["button"]],
            [CONVERSION_GOALS["proposal"]["button"]],
            ["/cancel"],
        ]
    return _ORIGINAL_ROWS(session)


def _build_manifest(settings, seed=None):
    """Translate new business goals only while building a CARD_SCHEMA-compatible card."""
    goals = list((settings or {}).get("goals") or []) if isinstance(settings, dict) else []
    if not any(goal in EXTRA_GOALS for goal in goals):
        return _ORIGINAL_BUILD_MANIFEST(settings, seed=seed)

    translated = copy.deepcopy(settings)
    translated["goals"] = [
        "next_step" if goal == "deal" else "meeting" if goal == "showroom" else goal
        for goal in goals
    ]
    cfg, calls = _ORIGINAL_BUILD_MANIFEST(translated, seed=seed)
    cfg["goals"] = goals
    for call in calls:
        actual = goals[(int(call["ordinal"]) - 1) % len(goals)]
        if actual not in EXTRA_GOALS:
            continue
        call["goal"] = actual
        call["goal_label"] = call_series.GOALS[actual][0]
        call["card"]["success"] = "meeting"
        condition = call_series.GOALS[actual][1]
        if not call.get("overcome", True):
            condition += " Цель сделки в этом звонке недостижима; корректный отказ не снижает навыки автоматически."
        call["card"]["success_condition"] = condition
        validate_card(call["card"])
    return cfg, calls


def _series_outcome(self, session, goal):
    goals = call_series.GOALS
    schema = obj(
        outcome=choice(*goals, "refusal", "no_agreement"),
        goal_achieved=B,
        reason=S,
        evidence=arr(obj(message_id=I, speaker=choice("manager", "client"))),
    )
    catalogue = "; ".join(f"{key} — {value[0]}" for key, value in goals.items())
    result = self.request(
        "series_outcome",
        """Определи фактический результат учебного звонка по всей истории.
Цель и критерий назначены до звонка. Не засчитывай намерение менеджера вместо подтверждения
клиента. Выбери один наиболее сильный фактически достигнутый результат из каталога либо
refusal/no_agreement. goal_achieved=true только если criterion выполнен. При положительном
результате обязательны ссылки на действие manager И явное подтверждение client.
Для showroom недостаточно самого приглашения — клиент должен согласиться на визит.
Для deal недостаточно интереса, встречи или КП — клиент должен перейти к оформлению сделки.
Если результат неоднозначен, используй no_agreement.""" + "\nКаталог: " + catalogue,
        {
            "goal": goal,
            "criterion": (session.get("series_goal_criterion") or goals[goal][1]),
            "history": [
                {
                    "message_id": index + 1,
                    "speaker": "manager" if message["role"] == "user" else "client",
                    "content": message["content"],
                }
                for index, message in enumerate(session["history"])
            ],
        },
        schema,
        self.eval_model,
        max_output_tokens=1200,
    )
    roles = set()
    for ref in result["evidence"]:
        ident = ref["message_id"]
        if not 1 <= ident <= len(session["history"]):
            raise EvaluationError("Unknown outcome evidence")
        message = session["history"][ident - 1]
        role = "manager" if message["role"] == "user" else "client"
        if role != ref["speaker"]:
            raise EvaluationError("Outcome speaker mismatch")
        ref["quote"] = message["content"]
        roles.add(role)
    if (result["outcome"] in goals or result["goal_achieved"]) and roles != {"manager", "client"}:
        raise EvaluationError("Outcome without bilateral evidence")
    if result["goal_achieved"] and result["outcome"] not in goals:
        raise EvaluationError("Goal achieved without a positive result")
    if session.get("series_call_unreachable") and result["goal_achieved"]:
        raise EvaluationError("Impossible goal reported achieved")
    if not result["reason"].strip():
        raise EvaluationError("Outcome reason missing")
    review = self.request(
        "series_outcome_review",
        """Проверь результат по исходной истории. Убедись, что положительный outcome и
 goal_achieved подтверждены действиями менеджера и явным согласием клиента по criterion.
Для showroom должно быть согласие на визит; для deal — переход к оформлению сделки.
При неоднозначности положительного результата passed=false.""",
        {"criterion": (session.get("series_goal_criterion") or goals[goal][1]), "history": session["history"], "result": result},
        obj(passed=B, issues=arr(S)),
        self.eval_model,
        max_output_tokens=800,
    )
    if not review["passed"] or review["issues"]:
        raise EvaluationError("Series outcome failed independent review")
    return result


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
        suffix = f"\n\nЦель серии: {selected['label']}."
        db.execute(
            "UPDATE outbox SET body=body||? WHERE user_id=? AND sent=0 AND body LIKE ?",
            (suffix, int(target), f"%серия №{int(current['id'])}%"),
        )
        owner = int(current.get("assigned_by") or 0)
        if owner > 0:
            db.execute(
                "INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)",
                (owner, owner, f"Цель конверсии серии №{current['id']}: {selected['label']}."),
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
        selected = dict(CONVERSION_GOALS[code])
        selected["code"] = code
        draft["conversion_goal"] = selected
        draft["data"]["goals"] = [selected["engine_goal"]]
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


def _funnel(metrics):
    values = []
    outcomes = metrics.get("outcomes") or {}
    for key in ("purchase", "deal", "showroom", "meeting", "proposal", "next_step", "samples", "referral", "refusal", "no_agreement"):
        if outcomes.get(key):
            values.append(f"{call_series.OUTCOMES.get(key, key)} — {outcomes[key]}")
    return values


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
        "Конверсия по выбранной цели: " + (
            f"{metrics['goal_achieved']}/{metrics['calls_verified']} ({conversion:.0%})"
            if conversion is not None else "недостаточно данных"
        ),
        f"Качественных звонков 60+/100: {metrics['quality_calls']}/{metrics['calls_verified']}.",
    ]
    funnel = _funnel(metrics)
    if funnel:
        lines += ["", "ВОРОНКА", "; ".join(funnel) + "."]

    gaps = _skill_lines(metrics)
    lines += ["", "CENTARO · НАВИГАТОР ПО ТЕХНИКЕ"]
    if gaps:
        for index, (label, score, action) in enumerate(gaps, 1):
            lines.append(f"{index}. {label} ({score:.0f}% от максимума): {action}")
    else:
        lines.append("1. Закрепить результат на более сложной серии.")
    lines += [
        "",
        "После 2–4 целевых тренировок повторите сопоставимую серию — CENTARO покажет изменение балла и конверсии.",
    ]
    return "\n".join(lines)


def _strip_employee_business(text):
    marker = "\n\nБИЗНЕС-МОДЕЛЬ CENTARO"
    return text.split(marker, 1)[0] if marker in text else text


def _employee_report(series, results):
    base = _ORIGINAL_REPORT(series, results)
    if series.get("mode") == "attestation":
        base = _strip_employee_business(base)
    return employee_summary(series, results) + "\n\nПОДРОБНЫЙ РАЗБОР СЕРИИ\n" + base


FRAGILE_SKILL_SCORE = 60


def _management_conclusion(series, metrics):
    text = _plan_conclusion(series, metrics)
    plan = (series.get("business_projection") or {}).get("plan_percent")
    skill = metrics.get("skill_score")
    if plan is not None and plan >= 80 and skill is not None and skill < FRAGILE_SKILL_SCORE:
        text += (f"\nВнимание: навыки — {skill:.0f}/100. Результат держится не на технике продажи "
                 "и может не повториться; проверьте его следующей сопоставимой серией.")
    return text


def _plan_conclusion(series, metrics):
    projection = series.get("business_projection") or {}
    plan = projection.get("plan_percent")
    conversion = metrics.get("training_conversion")
    if plan is None:
        return "После ввода коммерческих параметров CENTARO рассчитает модель выполнения плана."
    if plan >= 100:
        return f"Модель показывает потенциал выполнения плана на {plan:.0f}% при сохранении заданного объёма контактов и текущей конверсии."
    gap = max(0.0, 100.0 - plan)
    if plan >= 80:
        return f"Менеджер близок к плановой модели: {plan:.0f}% плана. Дефицит — {gap:.0f} п.п.; приоритет — точечно поднять конверсию по выбранной цели."
    conv = f"{conversion:.0%}" if conversion is not None else "н/д"
    return f"Текущая модель — {plan:.0f}% плана при учебной конверсии {conv}. До плановой модели не хватает {gap:.0f} п.п.; сначала устранить два слабейших навыка и повторить серию."


def supervisor_report(store, series, results):
    base = _ORIGINAL_COMBINED_REPORT(store, series, results)
    metrics = performance.series_metrics(series, results)
    name = series_ux._latest_name(store, series["user_id"])
    base = base.replace("CENTARO · ИТОГ ЭФФЕКТИВНОСТИ", "CENTARO · ОТЧЁТ РУКОВОДИТЕЛЮ", 1)
    lines = base.splitlines()
    insert_at = 2 if len(lines) >= 2 else len(lines)
    lines[insert_at:insert_at] = [
        f"Сотрудник: {name}",
        f"Цель конверсии: {_goal_label(series)}",
    ]
    funnel = _funnel(metrics)
    if funnel:
        lines += ["", "ВОРОНКА ПО ИСХОДАМ", "; ".join(funnel) + "."]
    lines += ["", "УПРАВЛЕНЧЕСКИЙ ВЫВОД", _management_conclusion(series, metrics)]
    gaps = _skill_lines(metrics)
    if gaps:
        lines += ["", "ГДЕ ТЕРЯЕТСЯ РЕЗУЛЬТАТ"]
        for index, (label, score, action) in enumerate(gaps, 1):
            lines.append(f"{index}. {label} — {score:.0f}% от максимума. {action}")
    return "\n".join(lines)


def _forecast_prompt(series):
    setup = series.get("projection_setup") or {}
    if setup.get("step") != "downstream_percent":
        return _ORIGINAL_FORECAST_PROMPT(series)
    selected = (series.get("conversion_goal") or {}).get("code")
    examples = {
        "meeting": "из 10 состоявшихся встреч 3 заканчиваются оплатой → 30%",
        "deal": "из 10 полученных сделок 7 доходят до оплаты → 70%",
        "salon": "из 10 визитов в салон 4 заканчиваются оплатой → 40%",
        "proposal": "из 10 согласованных КП/счетов 3 заканчиваются оплатой → 30%",
    }
    return (
        f"Последний вопрос: какой процент результата «{_goal_label(series)}» у вас обычно превращается в оплату?\n\n"
        f"Например: {examples.get(selected, 'из 10 целевых результатов 3 заканчиваются оплатой → 30%')}."
    )


def install():
    global _INSTALLED, _ORIGINAL_WIZARD, _ORIGINAL_PROMPT, _ORIGINAL_ROWS
    global _ORIGINAL_REPORT, _ORIGINAL_COMBINED_REPORT, _ORIGINAL_BUILD_MANIFEST
    global _ORIGINAL_AI_SERIES_OUTCOME, _ORIGINAL_FORECAST_PROMPT
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_WIZARD = call_series.SeriesEngine.wizard
    _ORIGINAL_PROMPT = series_ux._wizard_prompt
    _ORIGINAL_ROWS = series_ux._wizard_rows
    _ORIGINAL_REPORT = call_series.report
    _ORIGINAL_COMBINED_REPORT = performance.combined_report
    _ORIGINAL_BUILD_MANIFEST = call_series.build_manifest
    _ORIGINAL_AI_SERIES_OUTCOME = AI.series_outcome
    _ORIGINAL_FORECAST_PROMPT = performance._prompt

    for key, value in EXTRA_GOALS.items():
        call_series.GOALS.setdefault(key, value)
    call_series.OUTCOMES.setdefault("deal", "Получение сделки")
    call_series.OUTCOMES.setdefault("showroom", "Приглашение в салон")

    call_series.build_manifest = _build_manifest
    AI.series_outcome = _series_outcome
    call_series.SeriesEngine.wizard = _wizard
    series_ux._wizard_prompt = _prompt
    series_ux._wizard_rows = _rows
    call_series.wizard_prompt = _prompt
    call_series.wizard_rows = _rows
    call_series.report = _employee_report
    performance.combined_report = supervisor_report
    performance._prompt = _forecast_prompt
