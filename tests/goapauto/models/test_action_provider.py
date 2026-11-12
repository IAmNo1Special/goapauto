from goapauto.models.action_provider import (
    ActionProvider,
    StaticActionProvider,
)
from goapauto.models.actions import Action, Actions
from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState


class TestActionProvider:
    def test_static_provider(self):
        """Test StaticActionProvider returns all wrapped actions."""
        actions = Actions()
        actions.add_action("move", {}, {}, 1)

        provider = StaticActionProvider(actions)
        state = WorldState()
        goal = Goal(target_state={"done": True})

        provided = provider.provide_actions(state, goal)
        assert len(provided) == 1
        assert provided[0].name == "move"

    def test_static_provider_no_goal(self):
        """Test StaticActionProvider works without a goal."""
        actions = Actions()
        actions.add_action("idle", {}, {}, 1)

        provider = StaticActionProvider(actions)
        provided = provider.provide_actions(WorldState())
        assert provided[0].name == "idle"

    def test_action_protocol_runtime_check(self):
        """Test ActionProvider is runtime-checkable."""

        class DynamicProvider:
            def provide_actions(self, state, goal=None):
                return [Action(name="dynamic", preconditions={}, effects={})]

        provider = DynamicProvider()
        assert isinstance(provider, ActionProvider)

        provided = provider.provide_actions(WorldState())
        assert provided[0].name == "dynamic"


class TestFunctionActionProvider:
    def test_function_provider_varies_by_state(self):
        """A function provider returns different actions per state."""
        from goapauto.models.action_provider import FunctionActionProvider

        def provide(state, goal=None):
            if state.get("armed", False):
                return [Action(name="fight", preconditions={}, effects={})]
            return [Action(name="arm", preconditions={}, effects={})]

        provider = FunctionActionProvider(provide)
        assert [a.name for a in provider.provide_actions(WorldState())] == [
            "arm"
        ]
        assert [
            a.name
            for a in provider.provide_actions(WorldState(armed=True))
        ] == ["fight"]

    def test_function_provider_varies_by_goal(self):
        """A function provider returns different actions per goal."""
        from goapauto.models.action_provider import FunctionActionProvider

        def provide(state, goal=None):
            target = {}
            if isinstance(goal, Goal):
                target = goal.target_state
            elif isinstance(goal, dict):
                target = goal
            if "door_open" in target:
                return [Action(name="open_door", preconditions={}, effects={})]
            return [Action(name="pickup_key", preconditions={}, effects={})]

        provider = FunctionActionProvider(provide)
        state = WorldState()
        assert [
            a.name
            for a in provider.provide_actions(
                state, Goal(target_state={"door_open": True})
            )
        ] == ["open_door"]
        assert [
            a.name
            for a in provider.provide_actions(
                state, Goal(target_state={"has_key": True})
            )
        ] == ["pickup_key"]
        assert [
            a.name
            for a in provider.provide_actions(
                state, {"door_open": True}
            )
        ] == ["open_door"]

    def test_function_provider_return_shapes(self):
        """Actions, a single Action, and None are all accepted."""
        from goapauto.models.action_provider import FunctionActionProvider

        actions = Actions()
        actions.add_action("from_collection", {}, {}, 1)
        assert [
            a.name
            for a in FunctionActionProvider(
                lambda state, goal=None: actions
            ).provide_actions(WorldState())
        ] == ["from_collection"]
        assert [
            a.name
            for a in FunctionActionProvider(
                lambda state, goal=None: Action(
                    name="single", preconditions={}, effects={}
                )
            ).provide_actions(WorldState())
        ] == ["single"]
        assert (
            FunctionActionProvider(
                lambda state, goal=None: None
            ).provide_actions(WorldState())
            == []
        )

    def test_function_provider_rejects_bad_input(self):
        """Non-callables and non-Action returns fail loudly."""
        import pytest

        from goapauto.models.action_provider import FunctionActionProvider

        with pytest.raises(TypeError):
            FunctionActionProvider("not-callable")
        with pytest.raises(TypeError):
            FunctionActionProvider(
                lambda state, goal=None: ["not-an-action"]
            ).provide_actions(WorldState())
        with pytest.raises(TypeError):
            FunctionActionProvider(
                lambda state, goal=None: 42
            ).provide_actions(WorldState())

    def test_function_provider_is_a_protocol_provider(self):
        """FunctionActionProvider satisfies the ActionProvider protocol."""
        from goapauto.models.action_provider import FunctionActionProvider

        assert issubclass(FunctionActionProvider, ActionProvider)
        provider = FunctionActionProvider(lambda state, goal=None: [])
        assert isinstance(provider, ActionProvider)

    def test_static_provider_ignores_state_and_goal(self):
        """Static provider behaviour is unchanged by the new provider."""
        actions = Actions()
        actions.add_action("idle", {}, {}, 1)
        provider = StaticActionProvider(actions)
        assert [
            a.name
            for a in provider.provide_actions(
                WorldState(armed=True),
                Goal(target_state={"door_open": True}),
            )
        ] == ["idle"]


class TestPlannerWithFunctionActionProvider:
    def test_planner_end_to_end_with_function_provider(self):
        """The planner plans through a state-dependent function provider."""
        from goapauto.models.action_provider import FunctionActionProvider
        from goapauto.models.goap_planner import Planner

        def provide(state, goal=None):
            if getattr(state, "has_key", False):
                return [
                    Action(
                        name="open_door",
                        preconditions={"has_key": True},
                        effects={"door_open": True},
                        cost=1,
                    )
                ]
            return [
                Action(
                    name="pickup_key",
                    preconditions={"key_available": True},
                    effects={"has_key": True},
                    cost=1,
                )
            ]

        planner = Planner(
            providers=[FunctionActionProvider(provide)], verbose=False
        )
        state = WorldState(
            key_available=True, has_key=False, door_open=False
        )
        goal = Goal(target_state={"door_open": True})
        result = planner.generate_plan(state, goal)
        assert result.plan == ["pickup_key", "open_door"]
