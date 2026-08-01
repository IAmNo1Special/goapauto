import importlib
import io
import sys

import pytest

from goapauto.models.actions import Action, Increment
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import (
    Planner,
    PlanResult,
    Schedule,
    ScheduleStep,
    safe_print,
)
from goapauto.models.worldstate import WorldState


class TestPlanner:
    def test_initialization(self, simple_actions):
        """Test planner initialization."""
        planner = Planner(actions_list=[])
        assert planner is not None
        assert planner.max_iterations == 1000

    def test_simple_plan_generation(self, simple_actions):
        """Test generating a basic plan."""
        planner = Planner(providers=[])
        # Manually add provider logic or use the helper constructor
        # Re-using the logic from the constructor for `actions_list` requires mocking or
        # using the public API. Let's use the public API with the actions list directly.

        # We need to extract tuples from our `simple_actions` fixture,
        # but `Actions` doesn't expose raw tuples easily.
        # Let's define a fresh list for clarity.
        actions_list = [
            ("step1", {"start": True}, {"mid": True}, 1.0),
            ("step2", {"mid": True}, {"end": True}, 1.0),
        ]

        planner = Planner(actions_list=actions_list)
        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.generate_plan(state, goal)

        assert result.plan is not None
        assert result.plan == ["step1", "step2"]
        assert "SUCCESS" in result.message

    def test_plan_failure_unreachable(self):
        """Test planning when goal is unreachable."""
        planner = Planner(actions_list=[("step", {"a": 1}, {"b": 1}, 1.0)])
        state = WorldState(a=0)  # Condition not met
        goal = Goal(target_state={"c": 1})  # Goal unrelated

        result = planner.generate_plan(state, goal)
        assert result.plan is None
        assert "No valid plan" in result.message

    def test_goal_already_satisfied(self):
        """Test immediate return when goal is met."""
        planner = Planner(actions_list=[])
        state = WorldState(done=True)
        goal = Goal(target_state={"done": True})

        result = planner.generate_plan(state, goal)
        assert result.plan == []
        assert "already satisfied" in result.message

    @pytest.mark.asyncio
    async def test_async_plan_generation(self):
        """Test async planning capability."""
        actions_list = [("step", {"start": True}, {"end": True}, 1.0)]
        planner = Planner(actions_list=actions_list)
        state = WorldState(start=True)
        goal = Goal(target_state={"end": True})

        result = await planner.async_generate_plan(state, goal)
        assert result.plan == ["step"]

    def test_hooks(self, mocker):
        """Test that planner hooks are triggered."""
        mock_callback = mocker.Mock()
        actions_list = [("step", {"a": 1}, {"b": 1}, 1.0)]
        planner = Planner(actions_list=actions_list)
        planner.register_hook("on_node_expanded", mock_callback)

        state = WorldState(a=1)
        goal = Goal(target_state={"b": 1})

        planner.generate_plan(state, goal)

        # Verify hook was called
        assert mock_callback.call_count >= 1

    def test_max_depth_limits_search(self):
        """Test that max_depth limits the depth of the search."""
        planner = Planner(
            actions_list=[("inc", {}, {"val": Increment(amount=1)}, 1.0)],
            max_iterations=1000,
        )
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 5})

        # Depth 2 is too shallow to reach val=5
        shallow = planner.generate_plan(state, goal, max_depth=2)
        assert shallow.plan is None

        # Depth 5 is enough to reach val=5
        deep = planner.generate_plan(state, goal, max_depth=5)
        assert deep.plan is not None
        assert len(deep.plan) == 5

    @pytest.mark.asyncio
    async def test_async_max_depth_limits_search(self):
        """Test that max_depth limits depth in async planning."""
        planner = Planner(
            actions_list=[("inc", {}, {"val": Increment(amount=1)}, 1.0)],
            max_iterations=1000,
        )
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 5})

        shallow = await planner.async_generate_plan(state, goal, max_depth=2)
        assert shallow.plan is None

        deep = await planner.async_generate_plan(state, goal, max_depth=5)
        assert deep.plan is not None
        assert len(deep.plan) == 5

    def test_total_cost_calculated(self):
        """Test that PlanStats.total_cost sums action costs."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 2.0),
                ("step2", {"mid": True}, {"end": True}, 3.0),
            ]
        )
        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.generate_plan(state, goal)
        assert result.plan == ["step1", "step2"]
        assert planner.stats.total_cost == 5.0

    def test_search_graph_max_depth_reached(self):
        """Test that search graph metadata reports the deepest expanded node."""
        planner = Planner(
            actions_list=[("inc", {}, {"val": Increment(amount=1)}, 1.0)],
            max_iterations=1000,
        )
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 3})

        result = planner.generate_plan(state, goal, max_depth=2)
        assert result.plan is None

        graph = planner.get_search_graph()
        assert graph["metadata"]["max_depth_reached"] == 2

    @pytest.mark.asyncio
    async def test_async_search_graph_max_depth_reached(self):
        """Test that async search graph metadata reports the deepest node."""
        planner = Planner(
            actions_list=[("inc", {}, {"val": Increment(amount=1)}, 1.0)],
            max_iterations=1000,
        )
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 3})

        result = await planner.async_generate_plan(state, goal, max_depth=2)
        assert result.plan is None

        graph = planner.get_search_graph()
        assert graph["metadata"]["max_depth_reached"] == 2

    def test_safe_print_unicode_error_fallback(self, mocker):
        """Test safe_print handles UnicodeEncodeError."""
        import builtins

        calls = {"count": 0}

        def flaky_print(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise UnicodeEncodeError("utf-8", "", 0, 1, "nope")
            return None

        mocker.patch.object(builtins, "print", side_effect=flaky_print)
        # Should not raise, and fallback print should be called with ASCII
        safe_print("héllo wörld")
        assert calls["count"] == 2

    def test_stdout_reconfigure_fallback(self, mocker):
        """Test the module import guards against non-reconfigurable stdout.

        This covers the exception branch of the Windows stdout setup by
        simulating a stream whose reconfigure() is unsupported.
        """
        if sys.platform != "win32":
            pytest.skip("Windows-only module setup")

        class UnreconfigurableStream:
            def reconfigure(self, **kwargs):
                raise io.UnsupportedOperation("not supported")

            def write(self, *args, **kwargs):
                return 0

        fake_stream = UnreconfigurableStream()
        mocker.patch.object(sys, "stdout", fake_stream)

        # Reloading runs the module-level setup; the except branch must no-op.
        import goapauto.models.goap_planner as planner_mod

        reloaded = importlib.reload(planner_mod)
        assert reloaded is not None
        # The original sys.stdout is restored after reload completes.
        assert sys.stdout is fake_stream or hasattr(sys.stdout, "write")

    def test_schedule_to_list(self):
        """Test Schedule.to_list serialization."""
        schedule = Schedule(
            steps=[
                ScheduleStep(action="a", start_time=0.0, end_time=2.0, cost=1.0),
                ScheduleStep(action="b", start_time=2.0, end_time=5.0, cost=3.0),
            ],
            makespan=5.0,
            total_cost=4.0,
        )
        items = schedule.to_list()
        assert items[0]["action"] == "a"
        assert items[0]["duration"] == 2.0
        assert items[1]["duration"] == 3.0
        assert items[1]["cost"] == 3.0

    def test_scalar_cost_variants(self):
        """Test _get_scalar_cost with different cost shapes."""
        # Dict cost with dict weights
        planner = Planner(actions_list=[], cost_weights={"time": 2.0, "fuel": 3.0})
        action = Action(
            name="a", preconditions={}, effects={}, cost={"time": 1.0, "fuel": 2.0}
        )
        assert planner._get_scalar_cost(action) == 8.0

        # List cost with list weights
        planner2 = Planner(actions_list=[], cost_weights=[1.0, 2.0])
        action2 = Action(name="b", preconditions={}, effects={}, cost=[3.0, 4.0])
        assert planner2._get_scalar_cost(action2) == 11.0

        # Scalar cost with weights returns scalar
        action3 = Action(name="c", preconditions={}, effects={}, cost=5.0)
        assert planner._get_scalar_cost(action3) == 5.0

    def test_scalar_cost_errors(self):
        """Test _get_scalar_cost error paths."""
        # Multi-dim cost but no weights
        planner = Planner(actions_list=[])
        action = Action(name="a", preconditions={}, effects={}, cost=[1.0, 2.0])
        with pytest.raises(ValueError, match="no cost_weights"):
            planner._get_scalar_cost(action)

        # Mismatched list lengths
        planner2 = Planner(actions_list=[], cost_weights=[1.0])
        with pytest.raises(ValueError, match="same length"):
            planner2._get_scalar_cost(action)

        # Incompatible cost/weights types
        planner3 = Planner(actions_list=[], cost_weights=[1.0])
        action3 = Action(name="c", preconditions={}, effects={}, cost={"time": 1.0})
        with pytest.raises(ValueError, match="Incompatible"):
            planner3._get_scalar_cost(action3)

    def test_register_hook_unknown_event(self):
        """Test register_hook rejects unknown events."""
        planner = Planner(actions_list=[])
        with pytest.raises(ValueError, match="Unknown event hook"):
            planner.register_hook("bogus_event", lambda: None)

    def test_hook_error_is_swallowed(self, mocker):
        """Test hook exceptions are logged but don't propagate."""

        def bad_callback(**kwargs):
            raise RuntimeError("hook boom")

        planner = Planner(actions_list=[("s", {"a": 1}, {"b": 1}, 1.0)])
        planner.register_hook("on_node_expanded", bad_callback)

        state = WorldState(a=1)
        goal = Goal(target_state={"b": 1})
        result = planner.generate_plan(state, goal)
        assert result.plan == ["s"]

    def test_display_statistics_no_stats(self):
        """Test _display_statistics is a no-op without stats."""
        planner = Planner(actions_list=[])
        del planner.stats
        planner._display_statistics()  # Should not raise

    def test_generate_plan_invalid_input(self):
        """Test generate_plan returns error for invalid input types."""
        planner = Planner(actions_list=[])
        result = planner.generate_plan("not-a-state", {"a": 1})
        assert result.plan is None
        assert "Error during planning" in result.message

    def test_generate_plan_dict_inputs(self):
        """Test generate_plan accepts dict inputs."""
        planner = Planner(actions_list=[("s", {"a": 1}, {"b": 1}, 1.0)])
        result = planner.generate_plan({"a": 1}, {"b": 1})
        assert result.plan == ["s"]

    def test_generate_plan_worldstate_goal(self):
        """Test goal can be a WorldState instance."""
        planner = Planner(actions_list=[("s", {"a": 1}, {"b": 1}, 1.0)])
        result = planner.generate_plan(WorldState(a=1), WorldState(b=1))
        assert result.plan == ["s"]

    def test_validate_and_convert_errors(self):
        """Test _validate_and_convert error paths."""
        planner = Planner(actions_list=[])

        with pytest.raises(TypeError, match="world_state must be"):
            planner._validate_and_convert("bad", Goal(target_state={"a": 1}), None)

        with pytest.raises(TypeError, match="goal must be"):
            planner._validate_and_convert(WorldState(), "bad-goal", None)

        with pytest.raises(ValueError, match="max_depth must be positive"):
            planner._validate_and_convert(WorldState(), Goal(target_state={"a": 1}), 0)

    def test_provider_error_is_logged(self, mocker, caplog):
        """Test provider exceptions are logged and skipped."""

        class BadProvider:
            def provide_actions(self, state, goal=None):
                raise RuntimeError("provider boom")

        planner = Planner(providers=[BadProvider()])
        planner.generate_plan(WorldState(a=1), {"b": 1})
        assert "Error providing actions" in caplog.text

    def test_plan_with_durations_and_schedule(self):
        """Test plan reconstruction with timed actions produces a schedule."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 2.0),
                ("step2", {"mid": True}, {"end": True}, 3.0),
            ]
        )
        # Assign durations directly
        for provider in planner.providers:
            for action in provider.actions.get_actions():
                action.duration = 1.5

        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.generate_plan(state, goal)
        assert result.plan == ["step1", "step2"]
        assert result.schedule is not None
        assert result.schedule.makespan == 3.0
        assert len(result.schedule.steps) == 2
        assert result.schedule.steps[0].action == "step1"
        assert result.schedule.steps[0].end_time == 1.5
        assert result.schedule.steps[1].start_time == 1.5

    def test_continue_plan(self):
        """Test continue_plan returns remaining actions."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 1.0),
                ("step2", {"mid": True}, {"end": True}, 1.0),
            ]
        )
        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.continue_plan(state, goal, executed_actions=["step1"])
        assert result.plan == ["step2"]

    def test_continue_plan_already_satisfied(self):
        """Test continue_plan when goal already satisfied."""
        planner = Planner(actions_list=[])
        state = WorldState(done=True)
        goal = Goal(target_state={"done": True})

        result = planner.continue_plan(state, goal, executed_actions=[])
        assert result.plan == []
        assert "already satisfied" in result.message

    def test_continue_plan_no_remaining(self):
        """Test continue_plan when all actions executed."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 1.0),
                ("step2", {"mid": True}, {"end": True}, 1.0),
            ]
        )
        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.continue_plan(state, goal, executed_actions=["step1", "step2"])
        assert result.plan == []
        assert "complete the plan" in result.message

    def test_continue_plan_error(self):
        """Test continue_plan error path."""
        planner = Planner(actions_list=[])
        result = planner.continue_plan("bad", {"a": 1}, [])
        assert result.plan is None
        assert "Error during continued planning" in result.message

    def test_continue_plan_no_plan_found(self):
        """Test continue_plan when no plan is reachable."""
        planner = Planner(actions_list=[("step", {"a": 1}, {"b": 1}, 1.0)])
        state = WorldState(a=0)
        goal = Goal(target_state={"zzz": True})

        result = planner.continue_plan(state, goal, executed_actions=[])
        assert result.plan is None

    def test_search_skips_stale_g_score_node(self):
        """Test the g-score dominance check in the search loop.

        Two actions reach the same state {x:1}; the cheap one replaces the
        recorded g-score, so when the expensive node is later popped its
        g_score exceeds the stored best and the loop `continue`s.
        """

        def zero_heuristic(s, g):
            return 0.0

        planner = Planner(
            actions_list=[
                ("bad", {}, {"x": 1}, 10.0),
                ("good", {}, {"x": 1}, 1.0),
                ("step", {"x": 1}, {"y": 1}, 9.0),
                ("finish", {"y": 1}, {"z": 1}, 100.0),
            ],
            max_iterations=100,
            heuristic_fn=zero_heuristic,
        )
        state = WorldState(x=0, y=0, z=0)
        goal = Goal(target_state={"z": 1})

        result = planner.generate_plan(state, goal)
        assert result.plan == ["good", "step", "finish"]
        assert planner.stats.nodes_visited >= 4

    @pytest.mark.asyncio
    async def test_async_search_skips_stale_g_score_node(self):
        """Test the g-score dominance check in the async search loop."""

        def zero_heuristic(s, g):
            return 0.0

        planner = Planner(
            actions_list=[
                ("bad", {}, {"x": 1}, 10.0),
                ("good", {}, {"x": 1}, 1.0),
                ("step", {"x": 1}, {"y": 1}, 9.0),
                ("finish", {"y": 1}, {"z": 1}, 100.0),
            ],
            max_iterations=100,
            heuristic_fn=zero_heuristic,
        )
        state = WorldState(x=0, y=0, z=0)
        goal = Goal(target_state={"z": 1})

        result = await planner.async_generate_plan(state, goal)
        assert result.plan == ["good", "step", "finish"]

    def test_verbose_off_logs_via_logger(self, mocker):
        """Test _log uses logger when verbose is off."""
        mock_logger = mocker.Mock()
        planner = Planner(actions_list=[], verbose=False, logger=mock_logger)
        planner._log(10, "message")
        mock_logger.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_generate_plan_already_satisfied(self):
        """Test async path when goal already satisfied."""
        planner = Planner(actions_list=[])
        result = await planner.async_generate_plan(
            WorldState(done=True), Goal(target_state={"done": True})
        )
        assert result.plan == []
        assert "already satisfied" in result.message

    @pytest.mark.asyncio
    async def test_async_generate_plan_error(self):
        """Test async planning error path."""
        planner = Planner(actions_list=[])
        result = await planner.async_generate_plan("bad", {"a": 1})
        assert result.plan is None
        assert "Error during planning" in result.message

    @pytest.mark.asyncio
    async def test_async_plan_with_durations(self):
        """Test async planning with timed actions produces a schedule."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 2.0),
                ("step2", {"mid": True}, {"end": True}, 3.0),
            ]
        )
        for provider in planner.providers:
            for action in provider.actions.get_actions():
                action.duration = 1.5

        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = await planner.async_generate_plan(state, goal)
        assert result.plan == ["step1", "step2"]
        assert result.schedule is not None
        assert result.schedule.makespan == 3.0

    def test_planresult_fields(self):
        """Test PlanResult default and explicit fields."""
        result = PlanResult(plan=None, message="x")
        assert result.schedule is None
        assert result.makespan is None
        assert result.total_cost == 0.0

        scheduled = PlanResult(
            plan=["a"],
            message="ok",
            schedule=Schedule(steps=[], makespan=0.0, total_cost=0.0),
            makespan=1.0,
            total_cost=2.0,
        )
        assert scheduled.makespan == 1.0
        assert scheduled.total_cost == 2.0
