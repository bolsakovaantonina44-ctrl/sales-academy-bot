"""Combine knowledge checks and the AI sales trainer into a work-admission verdict.

Knowledge proves that an employee knows the standards. The practical AI dialogue is
the main evidence that the employee can apply them. A strong theory score never
compensates for weak practice.
"""
import json
import sqlite3

from .domain import upgrade_session
from .learning import MODULES, progress_snapshot
from .reporting import total_score

PASS_THEORY_PERCENT = 80
PRACTICE_INDEPENDENT = 70
PRACTICE_CONDITIONAL = 60
THEORY_WEIGHT = 0.35
PRACTICE_WEIGHT = 0.65

PRACTICE_CASES = {
    "first_contact": "Первый контакт: получить содержательную зацепку и понятный следующий контакт.",
    "price": "Возражение «Дорого»: выяснить критерий сравнения и аргументировать без автоматической скидки.",
    "supplier": "«Уже работаем с другим»: выяснить ценность текущего поставщика и окно для альтернативы.",
    "send_info": "«Пришлите информацию»: уточнить предмет отправки и согласовать повод и срок следующей связи.",
    "lpr": "Выход на ЛПР: корректно использовать секретаря как источник контекста и получить следующий контакт.",
    "project": "Проектное выявление: получить картину объекта, зоны, сроков, критериев и участников решения.",
    "rules": "Коммерческие обещания: не придумывать цену, скидку, наличие, документы и сроки.",
}

MODULE_RECOMMENDATIONS = {
    "product": "Повторить продукт: зоны, форматы, объектное и дизайнерское направления, марки и логику подбора.",
    "sales": "Повторить продажи: открытые вопросы, слушание, аргументацию, возражения и конкретный следующий шаг.",
    "regulations": "Повторить правила: Bitrix24, содержательный комментарий, задача, срок и запрет неподтверждённых обещаний.",
}


def latest_practice(path, user_id):
    """Return the latest valid completed trainer result for this employee."""
    try:
        with sqlite3.connect(str(path), timeout=30) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,payload FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 30",
                (int(user_id),),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return None

    for row in rows:
        try:
            session = upgrade_session(json.loads(row["payload"]))
        except Exception:
            continue
        if session.get("phase") != "completed":
            continue
        data = session.get("report_data")
        score = total_score(data)
        if score is None:
            continue
        return {
            "session_id": int(row["id"]),
            "score": int(score),
            "focus": session.get("training_focus"),
            "difficulty": (session.get("fields") or {}).get("difficulty"),
        }
    return None


def assess(path, user_id):
    modules = progress_snapshot(path, user_id)
    completed = [item for item in modules if item["latest_assessment"]]
    scores = {item["id"]: item["latest_assessment"]["score"] for item in completed}
    missing_modules = [item for item in modules if item["id"] not in scores]
    weak = [module_id for module_id, score in scores.items() if score < PASS_THEORY_PERCENT]
    practice = latest_practice(path, user_id)

    if missing_modules:
        return {
            "complete": False,
            "theory_complete": False,
            "scores": scores,
            "missing": [item["title"] for item in missing_modules],
            "theory_average": None,
            "practice": practice,
            "grade": None,
            "decision": "Сначала завершите проверку знаний.",
            "employee_recommendations": ["Пройдите оставшиеся проверки знаний, затем практический экзамен в тренажёре."],
            "supervisor_recommendations": ["Итоговый допуск не формировать до завершения теории и практического диалога."],
            "practice_cases": [],
        }

    theory_average = round(sum(scores.values()) / len(scores), 1)
    if weak:
        return {
            "complete": False,
            "theory_complete": True,
            "scores": scores,
            "missing": [],
            "theory_average": theory_average,
            "practice": practice,
            "grade": None,
            "decision": "Проверка знаний пока не пройдена.",
            "employee_recommendations": [MODULE_RECOMMENDATIONS[module_id] for module_id in weak],
            "supervisor_recommendations": ["Назначить повторение слабых модулей и пересдачу. Практика не отменяет обязательный минимум знаний."],
            "practice_cases": ["project", "rules"],
        }

    if practice is None:
        return {
            "complete": False,
            "theory_complete": True,
            "scores": scores,
            "missing": [],
            "theory_average": theory_average,
            "practice": None,
            "grade": None,
            "decision": "Теория пройдена. Нужен практический экзамен в тренажёре.",
            "employee_recommendations": ["Пройдите полноценный диалог с AI-клиентом и завершите тренировку, чтобы система сформировала практическую оценку."],
            "supervisor_recommendations": ["Не выдавать самостоятельный допуск только по тестам. Дождаться проверенного отчёта практического диалога."],
            "practice_cases": ["first_contact", "project", "price", "send_info"],
        }

    practice_score = practice["score"]
    final_score = round(theory_average * THEORY_WEIGHT + practice_score * PRACTICE_WEIGHT, 1)

    if practice_score >= PRACTICE_INDEPENDENT and final_score >= 75:
        decision = "Допуск к самостоятельным переговорам"
        employee = ["Знания и применение навыков подтверждены. Закрепляйте результат на новых ролях и более сложных сценариях."]
        supervisor = [
            "Можно давать самостоятельные переговоры с выборочной проверкой первых реальных сделок и записей в CRM.",
            "Для развития назначать тренировки по новым ролям и возражениям, а не повторять базовый сценарий.",
        ]
        practice_cases = ["supplier", "lpr"]
    elif practice_score >= PRACTICE_CONDITIONAL:
        decision = "Условный допуск: первые переговоры с сопровождением"
        employee = ["Практический результат рабочий, но до устойчивого самостоятельного уровня нужен повторный диалог по слабым навыкам."]
        supervisor = [
            "Дать 1–2 контролируемых кейса и повторную тренировку после обратной связи.",
            "Опирайтесь на конкретные навыки из отчёта тренажёра, а не на общее впечатление о сотруднике.",
        ]
        practice_cases = ["first_contact", "price", "send_info", "lpr"]
    else:
        decision = "Допуск пока не рекомендован: практические навыки не подтверждены"
        employee = ["Повторите практику: теория пройдена, но в разговоре навыки пока не проявлены на рабочем уровне."]
        supervisor = [
            "Не допускать к самостоятельным переговорам до повторной проверенной тренировки.",
            "Назначить 2–3 коротких сценария по выявлению, аргументации и следующему шагу, затем повторить полноценный экзамен.",
        ]
        practice_cases = ["project", "first_contact", "price", "supplier", "send_info", "rules"]

    return {
        "complete": True,
        "theory_complete": True,
        "scores": scores,
        "missing": [],
        "theory_average": theory_average,
        "practice": practice,
        "grade": final_score,
        "decision": decision,
        "employee_recommendations": employee,
        "supervisor_recommendations": supervisor,
        "practice_cases": practice_cases,
    }


def employee_text(result):
    lines = ["ИТОГ АТТЕСТАЦИИ", ""]
    for module in MODULES:
        score = result["scores"].get(module["id"])
        if score is not None:
            lines.append(f"• {module['title']}: {score}%")
    if result.get("theory_average") is not None:
        lines.append(f"• Теория в среднем: {result['theory_average']:.1f}%")
    practice = result.get("practice")
    if practice:
        lines.append(f"• Практический тренажёр: {practice['score']}/100 · сессия #{practice['session_id']}")
    else:
        lines.append("• Практический тренажёр: ещё не зачтён")

    if result.get("missing"):
        lines += ["", "Осталось пройти: " + ", ".join(result["missing"])]
    if result.get("grade") is not None:
        lines += ["", f"Итоговый балл: {result['grade']:.1f}/100"]
    lines += ["", f"Вердикт: {result['decision']}", "", "Что делать дальше:"]
    lines += [f"• {item}" for item in result["employee_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Рекомендуемые тренировки:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    return "\n".join(lines)


def supervisor_text(employee_name, result):
    lines = [f"АТТЕСТАЦИЯ СОТРУДНИКА: {employee_name}", ""]
    for module in MODULES:
        score = result["scores"].get(module["id"])
        if score is not None:
            lines.append(f"• {module['title']}: {score}%")
    if result.get("theory_average") is not None:
        lines.append(f"• Теория: {result['theory_average']:.1f}%")
    practice = result.get("practice")
    if practice:
        lines.append(f"• Практика: {practice['score']}/100 · сессия #{practice['session_id']}")
    else:
        lines.append("• Практика: нет зачтённой сессии")
    if result.get("grade") is not None:
        lines.append(f"• Итог: {result['grade']:.1f}/100 (теория 35% / практика 65%)")
    lines += ["", f"Вердикт: {result['decision']}", "", "Рекомендации руководителю:"]
    lines += [f"• {item}" for item in result["supervisor_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Назначить тренировки:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    return "\n".join(lines)
