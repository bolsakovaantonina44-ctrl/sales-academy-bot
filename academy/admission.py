"""Aggregate three knowledge-test results into a transparent admission decision."""
from .learning import MODULES, progress_snapshot

MIN_ADMISSION_SCORE = 7.0

PRACTICE_CASES = {
    "first_contact": "Первый контакт: кратко назвать причину обращения и задать вопрос об объекте или текущей задаче.",
    "price": "Возражение «Дорого»: выяснить критерий сравнения и аргументировать через подтверждённую потребность без автоматической скидки.",
    "supplier": "Возражение «Уже работаем с другим»: выяснить, что устраивает в текущем поставщике и при каком условии рассмотрят альтернативу.",
    "send_info": "Возражение «Пришлите информацию»: уточнить, что именно нужно, и согласовать дату и повод следующего контакта.",
    "lpr": "Выход на ЛПР: объяснить секретарю или коллеге цель звонка и получить корректный следующий контакт.",
    "project": "Проектный подбор: выяснить объект, формат, поверхность, объём, сроки, документы и ограничения до предложения.",
    "rules": "Безопасное коммерческое обещание: корректно отработать запрос на цену, скидку, наличие или срок без выдуманных условий.",
}

MODULE_RECOMMENDATIONS = {
    "product": "Повторить продуктовый модуль и потренироваться подбирать решение только после уточнения объекта, объёма, сроков и ограничений.",
    "sales": "Повторить техники продаж: открытые вопросы, аргумент через подтверждённую потребность, работа с возражением и конкретный следующий шаг.",
    "regulations": "Повторить регламенты: не обещать непроверенное, фиксировать факты в карточке и согласовывать цену, скидку и срок до обещания.",
}


def assess(path, user_id):
    modules = progress_snapshot(path, user_id)
    completed = [item for item in modules if item["latest_assessment"]]
    scores = {
        item["id"]: item["latest_assessment"]["score"]
        for item in completed
    }
    missing = [item for item in modules if item["id"] not in scores]
    if missing:
        return {
            "complete": False,
            "scores": scores,
            "missing": [item["title"] for item in missing],
            "grade": None,
            "decision": "Аттестация ещё не завершена.",
            "employee_recommendations": ["Пройти оставшиеся модули и тест по каждому из них."],
            "supervisor_recommendations": ["Не назначать итоговый допуск до результатов всех трёх модулей."],
            "practice_cases": [],
        }

    grade = round(sum(scores.values()) / len(scores) / 10, 1)
    weak = [module_id for module_id, score in scores.items() if score < 80]
    if grade >= 8.0:
        decision = "Допуск к самостоятельной работе"
        employee = ["Поддерживайте уровень на реальных кейсах и проходите тренировки по возражениям."]
        supervisor = [
            "Дайте сотруднику первые реальные задачи с выборочной проверкой карточек и договорённостей.",
            "Отметьте сильный результат конкретно: за знание продукта, технику или соблюдение регламента — без общих оценок личности.",
        ]
    elif grade >= MIN_ADMISSION_SCORE:
        decision = "Условный допуск: первые сделки с сопровождением"
        employee = [MODULE_RECOMMENDATIONS[module_id] for module_id in weak] or [
            "Закрепите материал в тренировке с руководителем перед самостоятельными переговорами."
        ]
        supervisor = [
            "Дайте 1–2 контролируемых кейса и разберите их по фактам, а не по общему впечатлению.",
            "Сформулируйте один следующий навык для отработки и отметьте прогресс после повторной попытки.",
        ]
    else:
        decision = "Допуск пока не рекомендован"
        employee = [MODULE_RECOMMENDATIONS[module_id] for module_id in weak] or [
            "Повторите модули и пересдайте аттестацию."
        ]
        supervisor = [
            "Не использовать наказание за результат теста: назначить короткое повторное обучение и тренировку по слабым темам.",
            "Поставить понятную дату пересдачи и дать обратную связь по одному навыку за раз.",
        ]

    if grade < MIN_ADMISSION_SCORE:
        practice_cases = ["project", "first_contact", "price", "supplier", "send_info", "rules"]
    elif grade < 8.0:
        practice_cases = ["first_contact", "price", "send_info", "lpr"]
    else:
        practice_cases = ["supplier", "lpr", "project"]

    return {
        "complete": True,
        "scores": scores,
        "missing": [],
        "grade": grade,
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
    if not result["complete"]:
        lines += ["", "Осталось пройти: " + ", ".join(result["missing"])]
    else:
        lines += [
            "",
            f"Бал допуска: {result['grade']:.1f}/10",
            f"Решение: {result['decision']}.",
        ]
    lines += ["", "Рекомендации:"]
    lines += [f"• {item}" for item in result["employee_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Обязательные тренировки:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    return "\n".join(lines)


def supervisor_text(employee_name, result):
    lines = [f"АТТЕСТАЦИЯ СОТРУДНИКА: {employee_name}", ""]
    for module in MODULES:
        score = result["scores"].get(module["id"])
        if score is not None:
            lines.append(f"• {module['title']}: {score}%")
    if not result["complete"]:
        lines += ["", "Итоговый допуск пока не формируется.", "Осталось: " + ", ".join(result["missing"])]
    else:
        lines += ["", f"Бал допуска: {result['grade']:.1f}/10", f"Решение: {result['decision']}."]
    lines += ["", "Рекомендации руководителю:"]
    lines += [f"• {item}" for item in result["supervisor_recommendations"]]
    if result["practice_cases"]:
        lines += ["", "Назначить тренировки:"]
        lines += [f"• {PRACTICE_CASES[item]}" for item in result["practice_cases"]]
    return "\n".join(lines)
