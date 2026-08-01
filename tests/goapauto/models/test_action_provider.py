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
