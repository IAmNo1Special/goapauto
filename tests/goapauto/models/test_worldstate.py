import pytest

from goapauto.models.actions import (
    Decrement,
    Effect,
    Increment,
    Set,
    Unset,
)
from goapauto.models.worldstate import _UNKNOWN, WorldState


class _DoubleEffect(Effect):
    def __call__(self, current_value):
        return current_value * 2


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

    def test_diff_type_error(self):
        """Test diff raises for non-WorldState."""
        ws = WorldState(a=1)
        with pytest.raises(TypeError, match="expected WorldState"):
            ws.diff({"a": 1})  # type: ignore

    def test_mapping_interface(self):
        """Test dict-like interface methods."""
        ws = WorldState(a=1, b=2)

        assert "a" in ws
        assert "c" not in ws
        assert set(iter(ws)) == {"a", "b"}
        assert set(ws.keys()) == {"a", "b"}
        assert set(ws.values()) == {1, 2}
        assert set(ws.items()) == {("a", 1), ("b", 2)}
        assert len(ws) == 2
        assert bool(ws) is True
        assert bool(WorldState()) is False

    def test_get_with_default(self):
        """Test get() with and without default."""
        ws = WorldState(a=1)

        assert ws.get("a") == 1
        assert ws.get("missing") is _UNKNOWN
        assert ws.get("missing", 42) == 42
        # Explicitly-set None/False values are distinguishable
        ws2 = WorldState(none_val=None, false_val=False)
        assert ws2.get("none_val") is None
        assert ws2.get("false_val") is False

    def test_is_known(self):
        """Test is_known distinguishes set vs unset attributes."""
        ws = WorldState(a=None, b=False)
        assert ws.is_known("a")
        assert ws.is_known("b")
        assert not ws.is_known("c")

    def test_apply_effects_via_update(self):
        """Test _apply_effect handles Set, Increment, Decrement, Effect, plain values."""
        ws = WorldState(a=1, b=2, c=3)

        ws.update({"a": Set(10)})
        assert ws.a == 10

        ws.update({"b": Increment(5)})
        assert ws.b == 7

        ws.update({"c": Decrement(2)})
        assert ws.c == 1

        # Plain value
        ws.update({"d": "plain"})
        assert ws.d == "plain"

    def test_apply_effect_creates_missing_attr(self):
        """Test Increment/Decrement on missing attributes starts from 0."""
        ws = WorldState()
        ws.update({"count": Increment(3)})
        assert ws.count == 3

        ws.update({"count": Decrement(1)})
        assert ws.count == 2

    class _DoubleEffect(Effect):
        def __call__(self, current_value):
            return current_value * 2

    class _DoubleEffect(Effect):
        def __call__(self, current_value):
            return current_value * 2

    def test_apply_custom_effect(self):
        """Test generic Effect callable is applied to current value."""
        ws = WorldState(a=5)
        ws.update({"a": _DoubleEffect()})
        assert ws.a == 10

        # Missing attribute treated as 0
        ws2 = WorldState()
        ws2.update({"b": _DoubleEffect()})
        assert ws2.b == 0

    def test_apply_unset_effect(self):
        """Test Unset removes the attribute."""
        ws = WorldState(a=1)
        ws.update({"a": Unset()})
        assert not hasattr(ws, "a")
        assert "a" not in ws
        assert ws.get("a", "MISSING") == "MISSING"

        # Unset on a missing attribute is a no-op
        ws2 = WorldState()
        ws2.update({"missing": Unset()})
        assert "missing" not in ws2

    def test_update_with_kwargs(self):
        """Test update passes kwargs through effect application."""
        ws = WorldState()
        ws.update({}, a=Set(1), b=Increment(2))
        assert ws.a == 1
        assert ws.b == 2

    def test_clear(self):
        """Test clear empties the state."""
        ws = WorldState(a=1, b=2)
        ws.clear()
        assert len(ws) == 0

    def test_to_dict_and_from_dict(self):
        """Test conversion helpers."""
        ws = WorldState(a=1, b="x")
        assert ws.to_dict() == {"a": 1, "b": "x"}
        assert ws.get_state() == {"a": 1, "b": "x"}

        restored = WorldState.from_dict({"a": 1, "b": "x"})
        assert restored == ws
