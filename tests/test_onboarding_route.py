from academy.onboarding import MODULE_ORDER, ONBOARDING
from academy.onboarding_progress import ordered_lessons, next_after


def test_entry_route_is_compact_and_ordered():
    assert MODULE_ORDER == ("product", "sales", "regulations")
    assert [len(ONBOARDING[key]["lessons"]) for key in MODULE_ORDER] == [6, 6, 2]
    assert len(ordered_lessons()) == 14
    total_minutes = sum(item["minutes"] for _, _, item in ordered_lessons())
    assert 45 <= total_minutes <= 80


def test_every_lesson_has_mobile_checkpoint():
    ids = set()
    for _, _, item in ordered_lessons():
        assert item["id"] not in ids
        ids.add(item["id"])
        assert 2 <= item["minutes"] <= 6
        assert item["title"].strip()
        assert item["body"].strip()
        assert item["checkpoint"].strip()


def test_next_position_crosses_module_boundaries():
    after_product = next_after("product", 5)
    assert after_product[0] == "sales"
    assert after_product[1] == 0

    after_sales = next_after("sales", 5)
    assert after_sales[0] == "regulations"
    assert after_sales[1] == 0

    assert next_after("regulations", 1) is None
