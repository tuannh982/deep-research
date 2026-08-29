import pytest

import ids


def test_first_id_of_a_type_is_001():
    assert ids.next_id([], "task") == "T-001"
    assert ids.next_id([], "citation") == "C-001"


def test_next_id_continues_from_the_highest():
    assert ids.next_id(["T-001", "T-002"], "task") == "T-003"


def test_next_id_ignores_other_types():
    assert ids.next_id(["F-009", "C-099"], "task") == "T-001"


def test_next_id_does_not_reuse_a_gap():
    assert ids.next_id(["T-001", "T-003"], "task") == "T-004"


def test_next_id_widens_past_three_digits():
    assert ids.next_id(["T-999"], "task") == "T-1000"


def test_next_id_ignores_unparseable_entries():
    assert ids.next_id(["T-001", "T-draft", "notanid"], "task") == "T-002"


def test_next_id_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        ids.next_id([], "widget")
