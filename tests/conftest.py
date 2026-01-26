from typing import Any

import pytest

from goapauto.models.actions import Actions, Increment
from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState


@pytest.fixture
def empty_state() -> WorldState:
    """Fixture providing an empty WorldState."""
    return WorldState()


@pytest.fixture
def populated_state() -> WorldState:
    """Fixture providing a WorldState with some initial data."""
    return WorldState(wood=10, stone=5, has_tool=False)


@pytest.fixture
def simple_goal() -> Goal:
    """Fixture providing a simple goal."""
    return Goal(target_state={"has_tool": True}, priority=1, name="Get Tool")


@pytest.fixture
def sensor_mock(mocker):
    """Fixture providing a mock Sensor."""
    # We define the class locally or mock the interface directly
    mock = mocker.Mock()
    mock.sense.return_value = {"sensed_attr": True}
    return mock


@pytest.fixture
def simple_actions() -> Actions:
    """Fixture providing a standard set of actions."""
    actions = Actions()
    actions.add_actions(
        [
            ("get_tool", {"has_tool": False}, {"has_tool": True}, 1.0),
            ("gather_wood", {"has_tool": True}, {"wood": Increment(amount=5)}, 2.0),
        ]
    )
    return actions
