"""Role-aware, mobile-first UX for call-series setup.

This layer preserves the fixed-series assessment engine while making setup readable:
- employees configure their own practice in plain language;
- supervisors get a direct employee picker;
- both can add custom situations without technical pipe-delimited syntax;
- technical defaults remain in the underlying call_series engine.
"""
import copy
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import telebot

from academy import call_series
from academy import mobile_learning_runtime as mobile
from academy.access import AKENSO, SUPERVISOR, ensure_access_schema, get_role
from academy.domain import normalize_command
from academy.store import Store


_INSTALLED = False
_ORIGINAL_HANDLE = None
_ORIGINAL_MOBILE_CALLBACK = None
_ORIGINAL_HOME_MARKUP = None

UX_STEPS = ["count", "product", "customer", "goals", "objections", "difficulty", "custom", "confirm"]


def _db_path():
    return os.getenv("DB_PATH", "./data/academy.sqlite3")


def _admins():
    result = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            result.add(int(value))
        except ValueError:
            pass
    return result


def _is_supervisor(user_id):
    return int(user_id) in _admins() or get_role(_db_path(), int(user_id)) == SUPERVISOR


def _dispatch_text(chat_id, text):
    handler = mobile._BOT_ON_TEXT_HANDLER
    if handler is None:
        return False
    handler(
        SimpleNamespace(
            chat=SimpleNamespace(id=int(chat_id)),
            text=text,
            message_id=int(time.time() * 1000),
        )
    )
    return True


def _latest_name(store, user_id):
    try:
        with store.db() as db:
            row = db.execute(
                "SELECT payload FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (int(user_id),),
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            name = " ".join(str(payload.get("employee", {}).get("name") or "").split())
            if name:
                return name
    except Exception:
        pass
    return f"Telegram ID {int(user_id)}"


def _series_hub(bot, chat_id):
    store = Store(_db_path())
    current = store.open_series(int(chat_id))
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)

    if current:
        count = current["config"]["count"]
        if current["status"] == "active":
            text = (
                f"СЕРИЯ ЗВОНКОВ\n\nСерия №{current['id']} уже идёт: "
                f"{count} звонков. Во время серии нет подсказок и промежуточного разбора."
            )
            markup.add(telebot.types.InlineKeyboardButton('Продолжить серию', callback_data='academyv2:series:resume'))
        else:
            source = "назначена руководителем" if current["mode"] == "attestation" else "подготовлена вами"
            text = (
                f"СЕРИЯ ЗВОНКОВ\n\nУ вас есть серия №{current['id']} на {count} звонков — {source}.\n\n"
                "После старта звонки идут подряд. Подсказок, паузы и разбора между звонками нет. "
                "Итог и конверсия — после завершения серии."
            )
            markup.add(
                telebot.types.InlineKeyboardButton(
                    "▶️ Начать назначенную серию" if current["mode"] == "attestation" else "▶️ Начать серию",
                    callback_data="academyv2:series:begin",
                )
            )
    else:
        text = (
            "СЕРИЯ ЗВОНКОВ\n\n"
            "Это имитация рабочего режима менеджера: 10, 20 или 30 разных разговоров подряд.\n\n"
            "Вы задаёте, что продаёте, кому звоните, какие типовые и собственные ситуации должны "
            "встречаться. Перед стартом состав фиксируется и перемешивается. Во время серии нет "
            "подсказок и промежуточного разбора — итог, качество и учебная конверсия появляются в конце."
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                "🎯 Настроить мою серию",
                callback_data="academyv2:series:self",
            )
        )
        if _is_supervisor(chat_id):
            markup.add(
                telebot.types.InlineKeyboardButton(
                    "👥 Назначить серию сотруднику",
                    callback_data="academyv2:series:assign",
                )
            )

    markup.add(
        telebot.types.InlineKeyboardButton(
            "📋 Мои серии",
            callback_data="academyv2:series:list",
        )
    )
    markup.add(
        telebot.types.InlineKeyboardButton(
            "← В Академию",
            callback_data="academyv2:home",
        )
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def _employee_picker(bot, chat_id):
    if not _is_supervisor(chat_id):
        bot.send_message(chat_id, "Назначать серию сотруднику может только руководитель.")
        return

    ensure_access_schema(_db_path())
    store = Store(_db_path())
    with store.db() as db:
        rows = db.execute(
            "SELECT user_id, role FROM user_access WHERE role IN (?, ?) AND user_id<>? ORDER BY user_id",
            (AKENSO, SUPERVISOR, int(chat_id)),
        ).fetchall()

    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        user_id = int(row["user_id"])
        name = _latest_name(store, user_id)
        markup.add(
            telebot.types.InlineKeyboardButton(
                name[:48],
                callback_data=f"academyv2:series:assign_user:{user_id}",
            )
        )
    markup.add(
        telebot.types.InlineKeyboardButton(
            "← К серии звонков",
            callback_data="academyv2:series",
        )
    )
    if not rows:
        bot.send_message(
            chat_id,
            "В корпоративной команде пока нет сотрудников, которым можно назначить серию.",
            reply_markup=markup,
        )
        return
    bot.send_message(
        chat_id,
        "КОМУ НАЗНАЧИТЬ СЕРИЮ?\n\nВыберите сотрудника. Дальше вы сами зададите объём, продукт, клиента, "
        "ситуации, цели и сложность. Сотрудник не сможет изменить условия после назначения.",
        reply_markup=markup,
    )


def _patched_home_markup(user_id):
    markup = _ORIGINAL_HOME_MARKUP(user_id)
    if _is_supervisor(user_id):
        markup.add(
            telebot.types.InlineKeyboardButton(
                "📞 Назначить серию сотруднику",
                callback_data="academyv2:series:assign",
            )
        )
    return markup


def _patched_mobile_callback(bot, call):
    data = str(getattr(call, "data", "") or "")
    if not getattr(call, "message", None) or call.message.chat.id <= 0:
        return _ORIGINAL_MOBILE_CALLBACK(bot, call)

    chat_id = call.message.chat.id
    if data == 'academyv2:series:resume':
        _dispatch_text(chat_id, '/series_resume')
        bot.answer_callback_query(call.id)
        return True
    if data == "academyv2:series":
        _series_hub(bot, chat_id)
        bot.answer_callback_query(call.id)
        return True
    if data == "academyv2:series:self":
        if not _dispatch_text(chat_id, "/series"):
            bot.send_message(chat_id, "Не удалось открыть настройку. Попробуйте ещё раз.")
        bot.answer_callback_query(call.id)
        return True
    if data == "academyv2:series:assign":
        _employee_picker(bot, chat_id)
        bot.answer_callback_query(call.id)
        return True
    if data.startswith("academyv2:series:assign_user:"):
        try:
            target = int(data.rsplit(":", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить сотрудника.", show_alert=True)
            return True
        if not _dispatch_text(chat_id, f"/series_assign_to {target}"):
            bot.send_message(chat_id, "Не удалось открыть настройку. Попробуйте ещё раз.")
        bot.answer_callback_query(call.id)
        return True
    if data == "academyv2:series:begin":
        if not _dispatch_text(chat_id, "/series_begin"):
            bot.send_message(chat_id, "Не удалось запустить серию. Попробуйте ещё раз.")
        bot.answer_callback_query(call.id)
        return True
    if data == "academyv2:series:list":
        if not _dispatch_text(chat_id, "/series_list"):
            bot.send_message(chat_id, "Не удалось открыть список серий. Попробуйте ещё раз.")
        bot.answer_callback_query(call.id)
        return True

    return _ORIGINAL_MOBILE_CALLBACK(bot, call)


def _goal_lines():
    return "\n".join(f"{i + 1}. {value[0]}" for i, value in enumerate(call_series.GOALS.values()))


def _objection_lines():
    return "\n".join(f"{i + 1}. {value[0]}" for i, value in enumerate(call_series.OBJECTIONS.values()))


def _wizard_prompt(draft):
    step = draft["step"]
    data = draft["data"]
    practice = draft["mode"] == "practice"

    if draft.get("custom_stage") == "overcome":
        return (
            "В этой ситуации менеджер в принципе может договориться о следующем шаге?\n\n"
            "Если есть объективное ограничение и договориться сейчас невозможно — выберите «Нет». "
            "Тогда корректный отказ не будет считаться провалом продажи."
        )
    if draft.get("custom_stage") == "frequency":
        return (
            "Как часто включать эту ситуацию в серию?\n\n"
            "«1 раз» — единичный кейс, «Иногда» — обычная частота, «Часто» — повышенная частота."
        )

    prompts = {
        "target": (
            "Кому назначить серию? Укажите Telegram ID сотрудника с корпоративным доступом.\n"
            "Через панель руководителя сотрудника можно выбрать по имени."
        ),
        "count": (
            "Сколько звонков будет в серии?\n\n"
            + ("Вы проходите их сами." if practice else "Их подряд пройдёт выбранный сотрудник.")
            + " После старта подсказок и промежуточного разбора нет."
        ),
        "product": (
            "Что вы продаёте?\n\nНапишите продукт или услугу обычными словами. "
            "Например: «керамогранит для коммерческих объектов»."
            if practice
            else
            "Что продаёт сотрудник?\n\nНапишите продукт или услугу обычными словами. "
            "Например: «керамогранит для коммерческих объектов»."
        ),
        "customer": (
            "Кому вы обычно продаёте?\n\nУкажите роль клиента и, если важно, тип компании. "
            "Например: «руководитель отдела закупок строительной компании»."
            if practice
            else
            "Кому будет звонить сотрудник?\n\nУкажите роль клиента и, если важно, тип компании. "
            "Например: «руководитель отдела закупок строительной компании»."
        ),
        "goals": (
            "Какой результат звонка считать успешным?\n\n"
            "Можно выбрать одну или несколько целей — номера через запятую, например: 1, 2.\n\n"
            + _goal_lines() + "\n✏️ Своя цель — написать свою цель и критерий успеха"
        ),
        "custom_goal_label": "Напишите собственную цель звонка (5–300 символов). Например: договориться о бесплатном тестировании.",
        "custom_goal_criterion": "Как понять, что цель достигнута? Укажите наблюдаемое согласие клиента и следующий шаг (5–300 символов).",
        "objections": (
            "Какие типовые ситуации должны встречаться?\n\n"
            "Введите номера через запятую, например: 1, 4, 8. Если хотите все — нажмите «Все типовые».\n"
            "Если какое-то возражение должно встречаться заметно чаще, можно указать вес: 1:3, 4:1. "
            "Это необязательно.\n\n"
            + _objection_lines()
        ),
        "interested_percent": (
            "Сколько клиентов сделать изначально заинтересованными, без обязательного возражения? "
            "Выберите процент."
        ),
        "difficulty": (
            "До какой сложности довести серию?\n\n"
            "Лёгкая — больше открытых клиентов. Средняя — смешанный режим. "
            "Высокая — серия постепенно доходит до сложных клиентов."
        ),
        "custom": (
            "Есть свои ситуации или фразы клиентов?\n\n"
            "Напишите одну обычным сообщением. Например:\n"
            "• «В прошлый раз вы сорвали сроки»\n"
            "• «Мы уже работаем с другим поставщиком»\n"
            "• «С вами работать не хочу»\n\n"
            "После каждой ситуации я отдельно спрошу только две вещи: можно ли в принципе договориться "
            "и как часто её показывать. Можно добавить несколько ситуаций по одной.\n\n"
            "Если своих ситуаций нет — нажмите «Готово»."
        ),
    }
    if step != "confirm":
        return prompts[step]

    selected = []
    for key, weight in data.get("objections", {}).items():
        if key.startswith("custom_"):
            item = data["custom"][int(key.split("_")[1])]
            label = item["label"]
        else:
            label = call_series.OBJECTIONS[key][0]
        selected.append(f"• {label}" + (f" · чаще" if weight >= 4 else ""))

    lines = [
        "ПРОВЕРЬТЕ СЕРИЮ",
        f"Звонков: {data['count']}",
        f"Продукт: {data['product']}",
        f"Клиент: {data['customer']}",
        "Цели: " + ", ".join((data.get("custom_goal_label", call_series.GOALS[g][0]) if g == "custom_goal" else call_series.GOALS[g][0]) for g in data["goals"]),
        f"Максимальная сложность: {call_series.DIFFICULTIES[data['difficulty']]}",
        "",
        "Ситуации:",
        *(selected or ["• только клиенты без обязательных возражений"]),
        "",
        "Состав будет зафиксирован и перемешан до старта. Разбор и учебная конверсия — только после серии.",
    ]
    if practice:
        lines.append("После подтверждения серия начнётся сразу.")
    else:
        lines.append("После подтверждения сотрудник получит уведомление; изменить условия он не сможет.")
    return "\n".join(lines)


def _wizard_rows(session):
    draft = session.get("series_setup")
    if not draft:
        return None

    if draft.get("custom_stage") == "overcome":
        return [["Да, можно договориться", "Нет, объективно нельзя"], ["/cancel"]]
    if draft.get("custom_stage") == "frequency":
        return [["1 раз", "Иногда", "Часто"], ["/cancel"]]

    rows = {
        "count": [["10", "20", "30"]],
        "goals": [["1", "2", "3"], ["4", "5", "6"], ["7", "✏️ Своя цель"]],
        "objections": [["Все типовые"]],
        "interested_percent": [["10", "20", "30", "50"]],
        "difficulty": [["Лёгкая", "Средняя", "Высокая"]],
        "custom": [["Готово"]],
        "confirm": [[
            "▶️ Начать серию"
            if draft["mode"] == "practice"
            else "✅ Назначить сотруднику"
        ]],
    }.get(draft["step"], [])
    return rows + [["/cancel"]]


def _custom_causes(text):
    return [
        f"Для клиента это реальная ситуация: {text}",
        "Клиент использует эту ситуацию как важный критерий доверия и ждёт, что менеджер сначала уточнит контекст.",
    ]


def _patched_handle(self, event, s):
    text = event["text"].strip()
    cmd = normalize_command(text)
    if cmd.startswith("/series_assign_to "):
        user = event["user_id"]
        active = self.store.open_series(user)
        if active and active["status"] == "active":
            return _ORIGINAL_HANDLE(self, event, s)
        if user not in self.engine.admin_ids and self.engine.access_role(user) != "supervisor":
            self.store.commit(event, s, ["Назначение доступно руководителю."])
            return True
        try:
            target = int(text.split(maxsplit=1)[1])
            if (
                target == user
                or target <= 0
                or self.engine.access_role(target) not in ("akenso", "supervisor")
            ):
                raise ValueError("Выберите другого сотрудника с корпоративным доступом.")
        except (ValueError, IndexError) as exc:
            message = str(exc) if str(exc) else "Не удалось определить сотрудника."
            self.store.commit(event, s, [message])
            return True

        s["series_setup"] = {
            "mode": "attestation",
            "step": "count",
            "target": target,
            "data": {"custom": []},
        }
        self.store.commit(event, s, [_wizard_prompt(s["series_setup"])])
        return True
    return _ORIGINAL_HANDLE(self, event, s)


def _patched_wizard(self, event, s):
    draft = s["series_setup"]
    text = event["text"].strip()
    user = event["user_id"]
    cmd = normalize_command(text)
    data = draft["data"]
    step = draft["step"]

    if cmd in ("/cancel", "отмена"):
        s.pop("series_setup", None)
        self.store.commit(event, s, ["Настройка серии отменена."])
        return True

    try:
        if draft["mode"] == "attestation" and user not in self.engine.admin_ids and self.engine.access_role(user) != "supervisor":
            raise ValueError("Назначение доступно руководителю.")

        if step == "custom" and draft.get("custom_stage"):
            if draft["custom_stage"] == "overcome":
                yes = {"да", "да можно договориться", "можно", "да, можно договориться"}
                no = {"нет", "нельзя", "нет объективно нельзя", "нет, объективно нельзя"}
                if cmd in yes:
                    draft["custom_overcome"] = True
                elif cmd in no:
                    draft["custom_overcome"] = False
                else:
                    raise ValueError("Выберите «Да, можно договориться» или «Нет, объективно нельзя».")
                draft["custom_stage"] = "frequency"
                self.store.commit(event, s, [_wizard_prompt(draft)])
                return True

            if draft["custom_stage"] == "frequency":
                mapping = {
                    "1 раз": 1,
                    "один раз": 1,
                    "редко": 1,
                    "иногда": 2,
                    "несколько раз": 2,
                    "часто": 4,
                }
                weight = mapping.get(cmd)
                if weight is None and cmd.isdigit() and 1 <= int(cmd) <= 5:
                    weight = int(cmd)
                if weight is None:
                    raise ValueError("Выберите «1 раз», «Иногда» или «Часто».")

                phrase = draft.pop("custom_pending")
                overcome = bool(draft.pop("custom_overcome"))
                draft.pop("custom_stage", None)
                if len(data["custom"]) >= 14:
                    raise ValueError("Добавлено 14 своих ситуаций. Нажмите «Готово».")

                label = phrase if len(phrase) <= 70 else phrase[:67] + "..."
                item = {
                    "label": label,
                    "objection": phrase,
                    "causes": _custom_causes(phrase),
                    "overcome": overcome,
                }
                candidate = copy.deepcopy(data)
                candidate["custom"].append(item)
                candidate.setdefault("objections", {})
                candidate["objections"][f"custom_{len(data['custom'])}"] = weight
                call_series.config(candidate)
                data.clear()
                data.update(candidate)
                self.store.commit(
                    event,
                    s,
                    [
                        f"✅ Ситуация добавлена: «{label}».\n\n"
                        "Можно написать следующую ситуацию или нажать «Готово»."
                    ],
                )
                return True

        if step == "target":
            target = int(text)
            if target == user or target <= 0 or self.engine.access_role(target) not in ("akenso", "supervisor"):
                raise ValueError("Укажите ID другого сотрудника с корпоративным доступом.")
            draft["target"] = target
            draft["step"] = "count"

        elif step == "count":
            count = int(text)
            if count not in (10, 20, 30):
                raise ValueError("Выберите 10, 20 или 30.")
            data["count"] = count
            draft["step"] = "product"

        elif step in ("product", "customer"):
            if not 2 <= len(text) <= 300:
                raise ValueError("Введите от 2 до 300 символов.")
            data[step] = text
            draft["step"] = "customer" if step == "product" else "goals"

        elif step == "goals":
            if cmd in ("✏️ своя цель", "своя цель", "8"):
                data["goals"] = ["custom_goal"]
                draft["step"] = "custom_goal_label"
                self.store.commit(event, s, [_wizard_prompt(draft)])
                return True
            ids = [int(value.strip()) for value in text.split(",")]
            if not ids or any(not 1 <= value <= len(call_series.GOALS) for value in ids):
                raise ValueError("Выберите номера целей из списка через запятую.")
            data["goals"] = list(dict.fromkeys(list(call_series.GOALS)[value - 1] for value in ids))
            draft["step"] = "custom_goal_label" if "custom_goal" in data["goals"] else "objections"

        elif step == "custom_goal_label":
            if not 5 <= len(text) <= 300:
                raise ValueError("Введите цель длиной 5–300 символов.")
            data["custom_goal_label"] = text
            draft["step"] = "custom_goal_criterion"

        elif step == "custom_goal_criterion":
            if not 5 <= len(text) <= 300:
                raise ValueError("Введите критерий успеха длиной 5–300 символов.")
            data["custom_goal_criterion"] = text
            draft["step"] = "objections"

        elif step == "objections":
            if cmd in ("все типовые", "все возражения", "все"):
                data["objections"] = {key: 1 for key in call_series.OBJECTIONS}
            else:
                weights = {}
                for token in text.split(","):
                    pair = token.strip().split(":")
                    index = int(pair[0])
                    weight = int(pair[1]) if len(pair) == 2 else 1
                    if not 1 <= index <= len(call_series.OBJECTIONS) or not 1 <= weight <= 100 or len(pair) > 2:
                        raise ValueError("Укажите номера из списка. Например: 1, 4, 8. Для повышенной частоты: 1:3.")
                    weights[list(call_series.OBJECTIONS)[index - 1]] = weight
                if not weights:
                    raise ValueError("Выберите хотя бы одну типовую ситуацию.")
                data["objections"] = weights
            draft["step"] = "difficulty"

        elif step == "interested_percent":
            # Compatibility with drafts created before this UX layer.
            percent = int(text)
            if not 5 <= percent <= 80:
                raise ValueError("Введите процент 5–80.")
            data["interested_percent"] = percent
            draft["step"] = "difficulty"

        elif step == "difficulty":
            value = {
                "легкая": "easy",
                "лёгкая": "easy",
                "средняя": "medium",
                "высокая": "hard",
                "сложная": "hard",
                "1": "easy",
                "2": "medium",
                "3": "hard",
            }.get(cmd)
            if not value:
                raise ValueError("Выберите лёгкая, средняя или высокая.")
            data["difficulty"] = value
            draft["step"] = "custom"

        elif step == "custom":
            if cmd in ("готово", "без своих", "нет"):
                draft["step"] = "confirm"
            else:
                if not 4 <= len(text) <= 200:
                    raise ValueError("Опишите ситуацию одной фразой от 4 до 200 символов.")
                draft["custom_pending"] = text
                draft["custom_stage"] = "overcome"
                self.store.commit(event, s, [_wizard_prompt(draft)])
                return True

        elif step == "confirm":
            allowed = (
                {"начать серию", "подготовить серию", "▶️ начать серию"}
                if draft["mode"] == "practice"
                else {"назначить сотруднику", "назначить серию", "✅ назначить сотруднику"}
            )
            if cmd not in allowed:
                raise ValueError("Подтвердите кнопкой или /cancel — отменить настройку.")

            cfg, calls = call_series.build_manifest(data)
            target = draft.get("target", user)
            if not self.engine.has_unlimited_training(target) and self.store.paid_remaining(target) < cfg["count"]:
                raise ValueError(f"Для серии нужно {cfg['count']} оплаченных попыток или корпоративный доступ.")

            value = self.create(
                target,
                cfg,
                calls,
                draft["mode"],
                user if draft["mode"] == "attestation" else None,
            )
            s.pop("series_setup", None)

            if draft["mode"] == "practice" and s.get("employee", {}).get("name"):
                value.update(status="active", started_at=self.store.now())
                started = call_series.new_call(value, s["employee"])
                started["versions"].update(model=self.ai.model, evaluator=self.ai.eval_model)
                self.store.commit(event, started, call_series.intro(value, started), series=value)
                return True

            if draft["mode"] == "attestation":
                with self.store.db() as db:
                    db.execute(
                        "INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)",
                        (
                            int(target),
                            int(target),
                            f"📞 Руководитель назначил вам серию №{value['id']} на {cfg['count']} звонков.\n\n"
                            "Условия уже зафиксированы. Откройте Академию → «Серия звонков» и нажмите "
                            "«Начать назначенную серию». Разбор будет только после завершения всей серии.",
                        ),
                    )
                self.store.commit(
                    event,
                    s,
                    [
                        f"✅ Серия №{value['id']} назначена сотруднику.\n\n"
                        f"Объём: {cfg['count']} звонков. Сотруднику отправлено уведомление. "
                        "Условия зафиксированы и недоступны для изменения."
                    ],
                )
                return True

            self.store.commit(
                event,
                s,
                [
                    f"Серия №{value['id']} подготовлена: {cfg['count']} звонков. "
                    "Сначала задайте имя через /name, затем откройте «Серия звонков» и нажмите «Начать серию»."
                ],
            )
            return True

        else:
            raise ValueError("Не удалось определить шаг настройки. Нажмите /cancel и начните заново.")

        self.store.commit(event, s, [_wizard_prompt(draft)])

    except (ValueError, sqlite3.IntegrityError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "У сотрудника уже есть открытая серия."
        if any(fragment in message for fragment in ("invalid literal", "not enough values", "too many values")):
            message = "Ответ не распознан. Используйте подсказку в вопросе."
        self.store.commit(event, s, ["Проверьте ответ: " + message])
    return True


def install():
    global _INSTALLED, _ORIGINAL_HANDLE, _ORIGINAL_MOBILE_CALLBACK, _ORIGINAL_HOME_MARKUP
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_HANDLE = call_series.SeriesEngine.handle
    _ORIGINAL_MOBILE_CALLBACK = mobile._handle_callback
    _ORIGINAL_HOME_MARKUP = mobile._home_markup

    # Keep technical compatibility, but the normal wizard no longer asks the user
    # for an "interested percent": call_series.config retains the tested 20% default.
    call_series.STEPS = list(UX_STEPS)
    call_series.wizard_prompt = _wizard_prompt
    call_series.wizard_rows = _wizard_rows
    call_series.SeriesEngine.handle = _patched_handle
    call_series.SeriesEngine.wizard = _patched_wizard

    mobile._handle_callback = _patched_mobile_callback
    mobile._home_markup = _patched_home_markup
