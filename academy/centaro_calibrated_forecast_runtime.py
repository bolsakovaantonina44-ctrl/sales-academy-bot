"""Apply prior CRM calibration to the next comparable CENTARO supervisor forecast."""

from academy import centaro_performance_runtime as performance


_INSTALLED = False
_ORIGINAL_SUPERVISOR_REPORT = None


def build_calibrated_projection(projection, coefficient):
    if not projection or coefficient is None:
        return None
    training = projection.get("training_conversion")
    if training is None:
        return None
    calibrated_conversion = max(0.0, min(1.0, float(training) * float(coefficient)))
    contacts = float(projection.get("monthly_contacts") or 0)
    downstream = float(projection.get("downstream_percent", 100.0)) / 100.0
    average_check = float(projection.get("average_check") or 0)
    monthly_plan = float(projection.get("monthly_plan") or 0)
    expected_sales = contacts * calibrated_conversion * downstream
    revenue = expected_sales * average_check
    plan_percent = revenue / monthly_plan * 100 if monthly_plan else None
    return {
        "calibrated_conversion": calibrated_conversion,
        "expected_sales": expected_sales,
        "projected_revenue": revenue,
        "plan_percent": plan_percent,
        "coefficient": float(coefficient),
    }


def _previous_calibration(store, series):
    with store.db() as db:
        rows = db.execute(
            "SELECT payload FROM call_series WHERE user_id=? AND status='completed' AND id<>? ORDER BY id DESC LIMIT 50",
            (int(series["user_id"]), int(series["id"])),
        ).fetchall()
    import json
    for row in rows:
        candidate = json.loads(row["payload"])
        fact = candidate.get("crm_fact") or {}
        coefficient = fact.get("calibration_coefficient")
        if coefficient is None:
            continue
        if performance._same_scenario(series, candidate):
            return candidate, float(coefficient)
    return None, None


def _money(value):
    return f"{int(round(float(value))):,} ₽".replace(",", " ")


def _patched_supervisor_report(store, series, results):
    text = _ORIGINAL_SUPERVISOR_REPORT(store, series, results)
    projection = series.get("business_projection") or {}
    if not projection:
        return text

    source, coefficient = _previous_calibration(store, series)
    calibrated = build_calibrated_projection(projection, coefficient)
    if not calibrated:
        return text

    fact = (source or {}).get("crm_fact") or {}
    lines = [
        "",
        "CENTARO · CRM-CALIBRATED FORECAST",
        f"Калибровка взята из сопоставимой серии #{source['id']}.",
        f"CRM-выборка источника: {fact.get('real_contacts', 0)} контактов · {fact.get('confidence', 'н/д')}.",
        f"Коэффициент калибровки: {calibrated['coefficient']:.2f}.",
        f"Training Conversion текущей серии: {float(projection['training_conversion']):.1%}.",
        f"Калиброванная конверсия для business forecast: {calibrated['calibrated_conversion']:.1%}.",
        f"Калиброванное число продаж: {calibrated['expected_sales']:.1f} в месяц.",
        f"Калиброванный потенциал выручки: {_money(calibrated['projected_revenue'])}.",
    ]
    if calibrated.get("plan_percent") is not None:
        lines.append(f"Калиброванное выполнение плана: {calibrated['plan_percent']:.0f}%.")
    lines.append(
        "Этот forecast использует прошлый CRM-факт по сопоставимому потоку и цели. Он точнее сырой учебной модели, но остаётся прогнозом, а не гарантией выручки."
    )
    return text + "\n" + "\n".join(lines)


def install():
    global _INSTALLED, _ORIGINAL_SUPERVISOR_REPORT
    if _INSTALLED:
        return
    _INSTALLED = True
    _ORIGINAL_SUPERVISOR_REPORT = performance.combined_report
    performance.combined_report = _patched_supervisor_report
