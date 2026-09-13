from academy.onboarding import MODULE_ORDER, ONBOARDING, next_position, route, total_lessons, total_minutes


def test_entry_route_is_compact_and_ordered():
    assert MODULE_ORDER == ("product", "sales", "regulations")
    assert [len(ONBOARDING[key]["lessons"]) for key in MODULE_ORDER] == [4, 5, 2]
    assert total_lessons() == 11
    assert 30 <= total_minutes() <= 60


def test_every_lesson_has_mobile_checkpoint():
    ids = set()
    for item in route():
        assert item["id"] not in ids
        ids.add(item["id"])
        assert 2 <= item["minutes"] <= 5
        assert item["title"].strip()
        assert item["body"].strip()
        assert item["checkpoint"].strip()


def test_next_position_crosses_module_boundaries():
    after_product = next_position("product", 3)
    assert after_product["module_id"] == "sales"
    assert after_product["index"] == 0

    after_sales = next_position("sales", 4)
    assert after_sales["module_id"] == "regulations"
    assert after_sales["index"] == 0

    assert next_position("regulations", 1) is None
