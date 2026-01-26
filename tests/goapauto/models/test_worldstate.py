import pytest
from pydantic import ValidationError

from goapauto.models.worldstate import WorldState


class TestWorldState:
    def test_initialization(self):
        """Test strict keyword-only initialization."""
        ws = WorldState(foo="bar", count=1)
        assert ws.foo == "bar"
        assert ws.count == 1

        # Verify strict positional arg failure
        with pytest.raises(TypeError):
            WorldState({"foo": "bar"})  # type: ignore

    def test_attribute_access(self):
        """Test attribute and item access patterns."""
        ws = WorldState(key="value")
        assert ws.key == "value"
        assert ws["key"] == "value"

        ws.key = "new_value"
        assert ws.key == "new_value"
        assert ws["key"] == "new_value"

        ws["key"] = "newer_value"
        assert ws.key == "newer_value"

    def test_update_methods(self):
        """Test update and update_state methods."""
        ws = WorldState(a=1)
        ws.update(WorldState(b=2))
        assert ws.a == 1
        assert ws.b == 2

        ws.update_state({"c": 3})
        assert ws.c == 3

    def test_copy_semantics(self):
        """Test deep copying of state."""
        ws = WorldState(nested_list=[1, 2])
        ws_copy = ws.copy(deep=True)

        ws_copy.nested_list.append(3)
        assert ws.nested_list == [1, 2]
        assert ws_copy.nested_list == [1, 2, 3]

    def test_hashing_and_equality(self):
        """Test equality and hashability."""
        ws1 = WorldState(a=1, b=2)
        ws2 = WorldState(a=1, b=2)
        ws3 = WorldState(a=1, b=3)

        assert ws1 == ws2
        assert ws1 != ws3
        assert hash(ws1) == hash(ws2)
        assert hash(ws1) != hash(ws3)

    def test_diff(self):
        """Test state differencing."""
        ws1 = WorldState(a=1, b=2, c=3)
        ws2 = WorldState(a=1, b=99, d=4)

        diff = ws1.diff(ws2)
        assert diff["b"] == (2, 99)
        assert diff["c"] == (3, None)
        assert diff["d"] == (None, 4)
        assert "a" not in diff
