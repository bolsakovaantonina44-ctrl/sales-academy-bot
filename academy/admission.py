"""Combine knowledge tests with a mandatory designated practical trainer result."""
import json
import sqlite3
from datetime import datetime, timezone

from .domain import upgrade_session
from .learning import MODULES, progress_snapshot
from .reporting import total_score

THEORY_WEIGHT = 0.40
PRACTICE_WEIGHT = 0.60
MIN_FINAL_SCORE = 75
INDEPENDENT_SCORE = 85
MIN_PRACTICE_SCORE = 70
COMPANY_CHAT_URL = "https://t.me/+F-D9K-WkUEwxNjli"

PRACTICE_CASES = {
    "first_contact": "Первый контакт: кратко назвать причину обращения и получить рабочую зацепку.",
    "price": "Возражение «Дорого»: выяснить критерий сравнения и не давать скидку автоматически.",
    "supplier": "«Уже работаем с другим»: понять, что ценят и при каком условии рассмотрят альтернативу.",
    "send_info": "«Пришлите информацию»: уточнить содержание и согласовать дату/условие следующего контакта.",
    "lpr": "Выход на ЛПР: корректно пройти секретаря и получить содержательный следующий контакт.",
    "project": "Проектный звонок: объект, стадия, зона, продукт, объём, сроки, решение и следующий шаг.",
    "rules": "Безопасные обещания: не придумывать цену, скидку, наличие, документы и срок.",
}

MODULE_RECOMMENDATIONS = {
    "product": "Повторить продукт: зоны, форматы, марки, калибр/тон, подбор и ограничения обещаний.",
    "sales": "Повторить продажи: открытые вопросы, роль собеседника, слушание, аргументация, возражения и следующий шаг.",
    "regulations": "Повторить регламенты: CRM, задачи, комментарии, скорость ответа и подтверждение коммерческих условий.",
}


def _ensure_practical_schema(path):
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS practical_exam_state(
                user_id INTEGER PRIMARY KEY,
                baseline_session_id INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL
            )
        """)


def start_practical_exam(path, user_id):
    """Mark the current training-session boundary so historical practice cannot satisfy admission."""
    _ensure_practical_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        try:
            row = db.execute("SELECT COALESCE(MAX(id),0) FROM sessions WHERE user_id=?", (int(user_id),)).fetchone()
            baseline = int(row[0] or 0)
        except sqlite3.Error:
            baseline = 0
        db.execute("""
            INSERT INTO practical_exam_state(user_id,baseline_session_id,started_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              baseline_session_id=excluded.baseline_session_id,
              started_at=excluded.started_at
        """, (int(user_id), baseline, datetime.now(timezone.utc).isoformat()))
    return baseline


def _practical_baseline(path, user_id):
    _ensure_practical_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        row = db.execute(
            "SELECT baseline_session_id FROM practical_exam_state WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    return int(row[0]) if row else None


def _latest_practice(path, user_id):
    """Return latest validated completed training created after practical-exam start."""
    baseline = _practical_baseline(path, user_id)
    if baseline is None:
        return None
    try:
        with sqlite3.connect(str(path), timeout=30) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,payload FROM sessions WHERE user_id=? AND id>? ORDER BY id DESC LIMIT 30",
                (int(user_id), int(baseline)),
            ).fetchall()
    except sqlite3.Error:
        return None

    for row in rows:
        try:
            session = upgrade_session(json.loads(row["payload"]))
        except Exception:
            continue
        if session.get("phase") != "completed":
            continue
        score = total_score(session.get("report_data"))
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
    practice = _latest_practice(path, user_id)

    if missing_modules:
        return {
            "complete": False,
            "passed": False,
            "scores": scores,
            "missing": [item["title"] for item in missing_modules],
            "theory_score": None,
            "practice_score": practice["score"] if practice else None,
            "practice_session_id": practice["session_id"] if practice else None,
            "grade": None,
            "final_score": None,
            "decision": "Аттестация ещё не завершена.",
            "employee_recommendations": ["Пройти оставшиеся проверки знаний. После них обязательна итоговая тренировка."],
            "supervisor_recommendations": ["Не формировать допуск до завершения всех проверок знаний и практического разговора."],
            "practice_cases": [],
        }

    theory_score = round(sum(scores.values()) / len(scores), 1)
    weak = [module_id for module_id, score in scores.items() if score < 80]

    if practice is None:
        started = _practical_baseline(path, user_id) is not None
        recommendation = (
            "Практический экзамен запущен. Проведите полноценный разговор и завершите его до проверенного разбора."
            if started else
            "Запустите «Практический экзамен» из Академии, затем проведите полноценный разговор до проверенного разбора."
        )
        return {
            "complete": False,
            "passed": False,
            "scores": scores,
            "missing": ["Практический экзамен в тренажёре"],
            "theory_score": theory_score,
            "practice_score": None,
            "practice_session_id": None,
            "grade": None,
            "final_score": None,
            "decision": "Теория завершена. Нужен практический экзамен.",
            "employee_recommendations": [recommendation],
            "supervisor_recommendations": ["Не считать сотрудника аттестованным только по тестам. Нужен новый проверенный результат тренажёра."],
            "practice_cases": ["project"],
        }

    practice_score = practice["score"]
    final_score = round(theory_score * THEORY_WEIGHT + practice_score * PRACTICE_WEIGHT, 1)
    grade = round(final_score / 10, 1)
    practice_gate_ok = practice_score >= MIN_PRACTICE_SCORE

    if final_score >= INDEPENDENT_SCORE and practice_gate_ok:
        decision = "Допуск к самостоятельным переговорам"
        passed = True
        employee = ["Поддерживайте уровень на новых сценариях и используйте рекомендации из разбора тренажёра."]
        supervisor = [
            "Можно давать самостоятельные переговоры с выборочным контролем первых реальных кейсов.",
            "Смотрите не только итоговый балл, но и повторяющиеся слабые навыки в отчётах тренажёра.",
        ]
    elif final_score >= MIN_FINAL_SCORE and practice_gate_ok:
        decision = "Условный допуск: первые переговоры с контролем"
        passed = True
        employee = [MODULE_RECOMMENDATIONS[module_id] for module_id in weak] or [
            "Повторите один сложный сценарий тренажёра перед самостоятельной работой."
        ]
        supervisor = [
            "Дайте первые реальные звонки с выборочным контролем и назначьте повтор слабого сценария.",
            "Сравните следующую тренировку с текущей по тем же навыкам.",
        ]
    else:
        decision = "Допуск пока не рекомендован"
        passed = False
        employee = [MODULE_RECOMMENDATIONS[module_id] for module_id in weak]
        if practice_score < MIN_PRACTICE_SCORE:
            employee.append("Практический разговор ниже минимального уровня: повторите практический экзамен после разбора ошибок.")
        if not employee:
            employee.append("Повторите практический экзамен и закрепите слабые навыки из отчёта.")
        supervisor = [
            "Назначьте повторную практику по двум главным слабым навыкам вместо простого повторения тестов.",
            "Допуск пересмотреть после нового проверенного практического разговора.",
        ]

    if practice_score < MIN_PRACTICE_SCORE:
        practice_cases = ["project", "first_contact", "price", "send_info", "rules"]
    elif final_score < INDEPENDENT_SCORE:
        practice_cases = ["first_contact", "price", "send_info", "lpr"]
    else:
        practice_cases = ["supplier", "lpr", "project"]

    return {
        "complete": True,
        "passed": passed,
        "scores": scores,
        "missing": [],
        "theory_score": theory_score,
        "practice_score": practice_score,
        "practice_session_id": practice["session_id"],
        "grade": grade,
        "final_score": final_score,
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
    if result.get("theory_score") is not None:
        lines.append(f"• Теория в целом: {result['theory_score']:.1f}%")
    if result.get("practice_score") is not None:
        lines.append(f"• Практический тренажёр: {result['practice_score']}/100")

    if not result["complete"]:
        lines += ["", "Осталось пройти: " + ", ".join(result["missing"]), f"Решение: {result['decision']}"]
    else:
        lines += [
            "",
            "Формула допуска: теория 40% + практический тренажёр 60%.",
            f"Итог: {result['final_score']:.1f}/100",
            f"Решение: {result['decision']}.",
        ]

    lines += ["", "Рекомендации:"]
    lines += [f"• {item}" for item in result["employee_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Что потренировать дальше:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    if result.get("passed"):
        lines += [
            "",
            "Аттестация пройдена. Присоединяйтесь к рабочему чату компании:",
            COMPANY_CHAT_URL,
        ]
    return "\n".join(lines)


def supervisor_text(employee_name, result):
    lines = [f"АТТЕСТАЦИЯ СОТРУДНИКА: {employee_name}", ""]
    for module in MODULES:
        score = result["scores"].get(module["id"])
        if score is not None:
            lines.append(f"• {module['title']}: {score}%")
    if result.get("theory_score") is not None:
        lines.append(f"• Теория: {result['theory_score']:.1f}%")
    if result.get("practice_score") is not None:
        lines.append(f"• Тренажёр: {result['practice_score']}/100")

    if not result["complete"]:
        lines += ["", "Итоговый допуск пока не формируется.", "Осталось: " + ", ".join(result["missing"])]
    else:
        lines += [
            "",
            "Вес: теория 40% · тренажёр 60%",
            f"Итог: {result['final_score']:.1f}/100",
            f"Решение: {result['decision']}.",
        ]
    lines += ["", "Рекомендации руководителю:"]
    lines += [f"• {item}" for item in result["supervisor_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Назначить тренировки:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    return "\n".join(lines)
