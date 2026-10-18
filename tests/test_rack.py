"""Tests for rack.py: the physical layout of the pumps."""

import pytest

import rack


def test_natural_key():
    names = ["JP 10", "JP 2", "jp 1", "JP 1b", ""]
    assert sorted(names, key=rack.natural_key) == ["", "jp 1", "JP 1b", "JP 2", "JP 10"]


def test_default_layout_is_one_stack_in_the_old_order():
    layout = rack.default_layout(["d3", "d1", "d2", "d1", None])
    assert layout["slots_per_stack"] == 3
    assert layout["stacks"] == [{"name": "Stack A", "slots": ["d3", "d1", "d2"]}]


def test_default_layout_with_no_pumps_yet():
    layout = rack.default_layout([])
    assert len(layout["stacks"]) == 1 and layout["stacks"][0]["slots"] == [None] * 4


def test_many_pumps_split_into_stacks_of_at_most_twelve():
    layout = rack.default_layout([f"d{i}" for i in range(15)])
    assert [len(s["slots"]) for s in layout["stacks"]] == [12, 12]
    assert rack.placed(layout) == [f"d{i}" for i in range(15)]


def test_normalize_repairs_bad_data():
    layout = rack.normalize(
        {"slots_per_stack": 2, "stacks": [{"slots": ["a", "a", "b"]}, "junk", {"name": "X", "slots": None}]}
    )
    assert layout["name"] == rack.DEFAULT_NAME
    assert layout["stacks"] == [
        {"name": "Stack A", "slots": ["a", None]},
        {"name": "X", "slots": [None, None]},
    ]
    assert rack.normalize(None)["stacks"] == [{"name": "Stack A", "slots": [None]}]


def test_place_from_tray_into_empty_slot_and_onto_a_pump():
    layout = rack.default_layout(["a", "b"])
    layout = rack.resize(layout, 2, 2)
    layout = rack.place(layout, "c", 1, 0)
    assert layout["stacks"][1]["slots"] == ["c", None]
    layout = rack.place(layout, "d", 0, 0)  # from the tray onto "a": "a" goes to the tray
    assert layout["stacks"][0]["slots"] == ["d", "b"]
    assert rack.unplaced(layout, ["a", "b", "c", "d"]) == ["a"]


def test_moving_onto_a_pump_swaps_them():
    layout = rack.resize(rack.default_layout(["a", "b"]), 2, 2)
    layout = rack.place(layout, "a", 1, 1)
    layout = rack.place(layout, "b", 1, 1)  # b moves onto a: a takes b's old place
    assert layout["stacks"] == [
        {"name": "Stack A", "slots": [None, "a"]},
        {"name": "Stack B", "slots": [None, "b"]},
    ]


def test_place_rejects_a_slot_that_does_not_exist():
    with pytest.raises(IndexError):
        rack.place(rack.default_layout(["a"]), "a", 3, 0)


def test_remove_and_order():
    layout = rack.remove(rack.default_layout(["a", "b", "c"]), "b")
    assert rack.order(layout, ["c", "b", "a"]) == ["a", "c", "b"]
    assert rack.find(layout, "b") is None and rack.find(layout, "c") == (0, 2)


def test_resize_keeps_names_and_sends_overflow_to_the_tray():
    layout = rack.default_layout(["a", "b", "c", "d"])
    layout["stacks"][0]["name"] = "Left"
    smaller = rack.resize(layout, 1, 3)
    assert smaller["stacks"] == [{"name": "Left", "slots": ["a", "b", "c"]}]
    assert rack.unplaced(smaller, ["a", "b", "c", "d"]) == ["d"]
    bigger = rack.resize(smaller, 3, 5)
    assert [s["name"] for s in bigger["stacks"]] == ["Left", "Stack B", "Stack C"]
    assert rack.resize(bigger, 99, 99)["slots_per_stack"] == rack.MAX_SLOTS


def test_auto_arrange_fills_stacks_in_name_order():
    names = {f"d{i}": f"JP {i}" for i in range(1, 12)}
    layout = rack.resize(rack.default_layout([]), 2, 5)
    layout = rack.auto_arrange(layout, list(reversed(names)), names)
    assert layout["stacks"][0]["slots"] == ["d1", "d2", "d3", "d4", "d5"]
    assert layout["stacks"][1]["slots"] == ["d6", "d7", "d8", "d9", "d10"]
    assert rack.unplaced(layout, list(names)) == ["d11"]
