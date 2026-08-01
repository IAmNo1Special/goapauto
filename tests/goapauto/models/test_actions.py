import pytest

from goapauto.models.actions import (
    Action,
    Actions,
    Decrement,
    Delete,
    Equal,
    GreaterThan,
    Increment,
    LessThan,
    NotEqual,
    Range,
    Set,
    Unset,
)
from goapauto.models.worldstate import WorldState


class TestPredicatesAndEffects:
    def test_predicates(self):
        """Test all predicate types."""
        assert Equal(value=5)(5)
        assert not Equal(value=5)(6)

        assert NotEqual(value=5)(6)
        assert not NotEqual(value=5)(5)

        assert GreaterThan(value=10)(15)
        assert not GreaterThan(value=10)(5)

        assert LessThan(value=10)(5)
        assert not LessThan(value=10)(15)

    def test_effects(self):
        """Test all effect types."""
        assert Set(value=10)(999) == 10
        assert Increment(amount=5)(10) == 15
        assert Decrement(amount=3)(10) == 7

    def test_predicates_positional_args(self):
        """Test predicates accept positional arguments as shown in docs."""
        assert Equal(5)(5)
        assert NotEqual(5)(6)
        assert GreaterThan(10)(15)
        assert LessThan(10)(5)

    def test_effects_positional_args(self):
        """Test effects accept positional arguments as shown in docs."""
        assert Set("kitchen")(None) == "kitchen"
        assert Increment(5)(10) == 15
        assert Decrement(3)(10) == 7
        assert Increment()(10) == 11  # Default amount

    def test_predicate_str_representations(self):
        """Test predicate string representations."""
        assert str(Equal(5)) == "== 5"
        assert str(NotEqual(5)) == "!= 5"
        assert str(GreaterThan(10)) == "> 10"
        assert str(LessThan(10)) == "< 10"
        assert str(Range(0, 10)) == "0 <= x <= 10"

    def test_effect_str_representations(self):
        """Test effect string representations."""
        assert str(Set(5)) == "= 5"
        assert str(Increment(5)) == "+= 5"
        assert str(Decrement(5)) == "-= 5"
        assert str(Unset()) == "unset"

    def test_range_predicate(self):
        """Test Range predicate inclusive bounds and validation."""
        rng = Range(0, 10)
        assert rng(5)
        assert rng(0)
        assert rng(10)
        assert not rng(-1)
        assert not rng(11)

        with pytest.raises(ValueError, match="min_value"):
            Range(10, 5)

    def test_unset_and_delete_alias(self):
        """Test Unset effect and Delete alias."""
        assert Unset()(999) is not None
        assert Delete is Unset
        assert Delete()(999) is not None


class TestActionModel:
    def test_action_applicability(self):
        """Test basic precondition checking."""
        action = Action(
            name="test",
            preconditions={"wood": GreaterThan(value=0)},
            effects={"wood": Decrement(amount=1)},
        )

        assert action.is_applicable(WorldState(wood=5))
        assert not action.is_applicable(WorldState(wood=0))
        assert not action.is_applicable(WorldState(stone=5))  # Missing attr

    def test_action_application(self):
        """Test applying effects to state."""
        action = Action(
            name="chop",
            preconditions={},
            effects={
                "wood": Increment(amount=1),
                "stamina": Decrement(amount=10),
                "status": Set(value="tired"),
            },
        )

        state = WorldState(wood=0, stamina=100, status="fresh")
        new_state = action.apply(state)

        assert new_state.wood == 1
        assert new_state.stamina == 90
        assert new_state.status == "tired"

        # Ensure original state is untouched (immutability)
        assert state.wood == 0

    def test_action_application_missing_attr(self):
        """Test applying Increment/Decrement effects to missing attributes."""
        action = Action(
            name="heal",
            preconditions={},
            effects={"health_level": Increment(amount=10)},
            cost=1.0,
        )

        state = WorldState(medicine_current=100)
        new_state = action.apply(state)

        # Missing attribute treated as 0, so Increment creates it
        assert new_state.health_level == 10

    @pytest.mark.asyncio
    async def test_async_apply(self):
        """Test async action application."""
        action = Action(
            name="async_chop", preconditions={}, effects={"wood": Increment(amount=1)}
        )
        state = WorldState(wood=0)
        new_state = await action.async_apply(state)
        assert new_state.wood == 1

    @pytest.mark.asyncio
    async def test_async_apply_coroutine_effect(self):
        """Test async apply with a coroutine function effect."""

        async def async_effect(current):
            return current + 100

        action = Action(name="boost", preconditions={}, effects={"power": async_effect})
        state = WorldState(power=1)
        new_state = await action.async_apply(state)
        assert new_state.power == 101

    @pytest.mark.asyncio
    async def test_async_apply_plain_value(self):
        """Test async apply with a plain (non-callable) effect value."""
        action = Action(name="flag", preconditions={}, effects={"done": True})
        state = WorldState()
        new_state = await action.async_apply(state)
        assert new_state.done is True

    def test_action_str_and_repr(self):
        """Test Action string representations."""
        action = Action(name="test", preconditions={"a": 1}, effects={"b": 2}, cost=3.0)
        assert "Action" in str(action)
        assert "test" in str(action)
        assert repr(action) == str(action)

    def test_action_name_validation(self):
        """Test action name validation."""
        with pytest.raises(ValueError, match="non-empty"):
            Action(name="", preconditions={}, effects={})
        with pytest.raises(ValueError, match="non-empty"):
            Action(name="   ", preconditions={}, effects={})

    def test_action_type_validation(self):
        """Test action type validation."""
        with pytest.raises(TypeError, match="Preconditions"):
            Action(name="a", preconditions=[], effects={})
        with pytest.raises(TypeError, match="Effects"):
            Action(name="a", preconditions={}, effects=[])
        with pytest.raises(TypeError, match="Cost"):
            Action(name="a", preconditions={}, effects={}, cost="high")

    def test_action_cost_validation(self):
        """Test action cost validation."""
        with pytest.raises(ValueError, match="Cost must be positive"):
            Action(name="a", preconditions={}, effects={}, cost=0)
        with pytest.raises(ValueError, match="Cost must be positive"):
            Action(name="a", preconditions={}, effects={}, cost=-5)

        # Dict cost with negative value
        with pytest.raises(ValueError, match="non-negative"):
            Action(name="a", preconditions={}, effects={}, cost={"time": -1.0})
        # Dict cost with non-numeric value
        with pytest.raises(ValueError, match="non-negative"):
            Action(name="a", preconditions={}, effects={}, cost={"time": "x"})

        # List cost with negative value
        with pytest.raises(ValueError, match="non-negative"):
            Action(name="a", preconditions={}, effects={}, cost=[1.0, -2.0])
        with pytest.raises(ValueError, match="non-negative"):
            Action(name="a", preconditions={}, effects={}, cost=[1.0, "x"])

    def test_action_duration_validation(self):
        """Test action duration validation."""
        with pytest.raises(TypeError, match="Duration"):
            Action(name="a", preconditions={}, effects={}, duration="long")
        with pytest.raises(ValueError, match="Duration must be non-negative"):
            Action(name="a", preconditions={}, effects={}, duration=-1.0)

        # Valid durations
        assert (
            Action(name="a", preconditions={}, effects={}, duration=0.0).duration == 0.0
        )
        assert (
            Action(name="a", preconditions={}, effects={}, duration=2.5).duration == 2.5
        )

    def test_is_applicable_callable_and_exception(self):
        """Test callable preconditions and exception handling."""
        # Callable predicate that fails
        action = Action(
            name="conditional",
            preconditions={"wood": lambda v: v > 5},
            effects={},
        )
        assert action.is_applicable(WorldState(wood=10))
        assert not action.is_applicable(WorldState(wood=3))

        # Exception during applicability check returns False
        def broken(value):
            raise RuntimeError("boom")

        action2 = Action(name="broken", preconditions={"x": broken}, effects={})
        assert not action2.is_applicable(WorldState(x=1))

    def test_apply_not_applicable_raises(self):
        """Test apply raises when not applicable."""
        action = Action(name="needs_key", preconditions={"has_key": True}, effects={})
        with pytest.raises(ValueError, match="not applicable"):
            action.apply(WorldState(has_key=False))

    def test_apply_callable_effect_and_error(self):
        """Test apply with callable effects and error propagation."""
        action = Action(
            name="calc",
            preconditions={},
            effects={"val": lambda v: v * 2},
        )
        new_state = action.apply(WorldState(val=5))
        assert new_state.val == 10

        # Plain (non-callable) effect value
        action_plain = Action(
            name="plain",
            preconditions={},
            effects={"flag": True, "count": 7},
        )
        plain_state = action_plain.apply(WorldState())
        assert plain_state.flag is True
        assert plain_state.count == 7

        # Effect that raises should propagate
        def broken_effect(v):
            raise RuntimeError("effect boom")

        action2 = Action(name="bad", preconditions={}, effects={"x": broken_effect})
        with pytest.raises(RuntimeError, match="effect boom"):
            action2.apply(WorldState(x=1))

    @pytest.mark.asyncio
    async def test_async_apply_not_applicable_raises(self):
        """Test async apply raises when not applicable."""
        action = Action(name="needs_key", preconditions={"has_key": True}, effects={})
        with pytest.raises(ValueError, match="not applicable"):
            await action.async_apply(WorldState(has_key=False))

    @pytest.mark.asyncio
    async def test_async_apply_error_propagation(self):
        """Test async apply error propagation."""

        async def broken_effect(v):
            raise RuntimeError("async boom")

        action = Action(name="bad", preconditions={}, effects={"x": broken_effect})
        with pytest.raises(RuntimeError, match="async boom"):
            await action.async_apply(WorldState(x=1))


class TestActionsCollection:
    def test_add_and_retrieve(self):
        """Test Actions container methods."""
        actions = Actions()
        actions.add_action("move", {}, {}, 1)

        assert "move" in actions
        assert actions.get_action("move") is not None
        assert len(actions) == 1

        with pytest.raises(ValueError):
            actions.add_action("move", {}, {}, 1)  # Duplicate name

    def test_filter_actions(self):
        """Test fitlering applicable actions."""
        actions = Actions()
        actions.add_action("valid", {"a": 1}, {}, 1)
        actions.add_action("invalid", {"a": 99}, {}, 1)

        state = WorldState(a=1)
        applicable = actions.filter_actions(state)

        assert len(applicable) == 1
        assert applicable[0].name == "valid"

    def test_add_actions_mixed(self):
        """Test adding multiple actions with mixed formats (tuples and Action objects)."""
        actions = Actions()

        # 1. Add via tuple
        # 2. Add via Action object
        mixed_definitions = [
            ("tuple_action", {"a": 1}, {"b": 1}, 1.0),
            Action(
                name="object_action", preconditions={"c": 1}, effects={"d": 1}, cost=2.0
            ),
        ]

        actions.add_actions(mixed_definitions)

        assert "tuple_action" in actions
        assert "object_action" in actions
        assert len(actions) == 2
        assert actions.get_action("object_action").cost == 2.0

        # Test duplicate name error with object
        with pytest.raises(ValueError, match="already exists"):
            actions.add_actions(
                [Action(name="tuple_action", preconditions={}, effects={})]
            )

        # Test invalid format error
        with pytest.raises(ValueError, match="must be an Action object or a 4-tuple"):
            actions.add_actions([{"invalid": "format"}])

    def test_add_actions_type_error(self):
        """Test add_actions rejects non-list/tuple."""
        actions = Actions()
        with pytest.raises(TypeError, match="list or tuple"):
            actions.add_actions("not-a-list")  # type: ignore

    def test_add_action_validation(self):
        """Test add_action name and error-path coverage."""
        actions = Actions()

        with pytest.raises(ValueError, match="non-empty"):
            actions.add_action("", {}, {}, 1)
        with pytest.raises(ValueError, match="non-empty"):
            actions.add_action("   ", {}, {}, 1)

        # Invalid action definition raises and propagates
        with pytest.raises(ValueError, match="Cost must be positive"):
            actions.add_action("bad", {}, {}, cost=0)

    def test_get_action_type_error(self):
        """Test get_action rejects non-string names."""
        actions = Actions()
        with pytest.raises(TypeError, match="must be a string"):
            actions.get_action(123)  # type: ignore

    def test_get_actions_and_clear(self):
        """Test get_actions returns a copy and clear empties."""
        actions = Actions()
        actions.add_action("a", {}, {}, 1)

        snapshot = actions.get_actions()
        assert len(snapshot) == 1
        # Modifying the returned list doesn't affect the collection
        snapshot.clear()
        assert len(actions) == 1

        actions.clear_actions()
        assert len(actions) == 0

    def test_iteration_and_contains(self):
        """Test Actions iteration and containment."""
        actions = Actions()
        actions.add_action("one", {}, {}, 1)
        actions.add_action("two", {}, {}, 1)

        names = [a.name for a in actions]
        assert names == ["one", "two"]
        assert "one" in actions
        assert "missing" not in actions

    def test_str_and_repr(self):
        """Test Actions string representations."""
        actions = Actions()
        actions.add_action("a", {}, {}, 1)

        assert "2" in str(actions) or "1" in str(actions)
        assert "Actions" in repr(actions)
