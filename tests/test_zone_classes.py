"""zone counts only fuel boxes, so a model that also detects robots does not count robots."""
import pathlib
import sys

import pytest

pytest.importorskip("cv2")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vision"))
from counters.zone import fuel_classes  # noqa: E402


def test_three_class_model_keeps_only_fuel():
    assert fuel_classes({0: "fuel", 1: "robot_blue", 2: "robot_red"}, {}) == [0]


def test_fuel_found_by_name_not_position():
    assert fuel_classes({0: "robot", 1: "Fuel"}, {}) == [1]


def test_single_class_model_not_named_fuel_keeps_everything():
    assert fuel_classes({0: "ball"}, {}) is None


def test_config_classes_override():
    assert fuel_classes({0: "fuel", 1: "robot"}, {"classes": [1]}) == [1]
