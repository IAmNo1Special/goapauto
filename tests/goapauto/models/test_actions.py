import pytest

from goapauto.models.actions import (
    Action,
    Actions,
    Decrement,
    Equal,
    GreaterThan,
    Increment,
    LessThan,
    NotEqual,
    Set,
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

    @pytest.mark.asyncio
    async def test_async_apply(self):
        """Test async action application."""
        action = Action(
            name="async_chop", preconditions={}, effects={"wood": Increment(amount=1)}
        )
        state = WorldState(wood=0)
        new_state = await action.async_apply(state)
        assert new_state.wood == 1


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
