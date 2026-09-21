import io
import sys
import time

import pytest

from goapauto.models.action_provider import ActionProvider
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

    def test_windows_console_setup_reconfigures_stdout(self, mocker):
        """The Windows console setup reconfigures stdout for UTF-8 output."""
        import goapauto.models.goap_planner as planner_mod

        mocker.patch.object(planner_mod.os, "name", "nt")
        fake_stream = mocker.Mock()
        mocker.patch.object(sys, "stdout", fake_stream)
        planner_mod._configure_windows_console()
        fake_stream.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="ignore"
        )

    def test_windows_console_setup_tolerates_bad_stdout(self, mocker):
        """The Windows console setup never raises on a broken stdout stream."""
        import goapauto.models.goap_planner as planner_mod

        mocker.patch.object(planner_mod.os, "name", "nt")
        fake_stream = mocker.Mock()
        fake_stream.reconfigure.side_effect = io.UnsupportedOperation("not supported")
        mocker.patch.object(sys, "stdout", fake_stream)
        planner_mod._configure_windows_console()  # must not raise

    def test_windows_console_setup_noop_off_windows(self, mocker):
        """The Windows console setup is a no-op on other platforms."""
        import goapauto.models.goap_planner as planner_mod

        mocker.patch.object(planner_mod.os, "name", "posix")
        fake_stream = mocker.Mock()
        mocker.patch.object(sys, "stdout", fake_stream)
        planner_mod._configure_windows_console()
        fake_stream.reconfigure.assert_not_called()

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

    def test_hook_error_propagates(self, mocker):
        """Test hook exceptions propagate instead of being swallowed."""

        def bad_callback(**kwargs):
            raise RuntimeError("hook boom")

        planner = Planner(actions_list=[("s", {"a": 1}, {"b": 1}, 1.0)])
        planner.register_hook("on_node_expanded", bad_callback)

        state = WorldState(a=1)
        goal = Goal(target_state={"b": 1})
        with pytest.raises(RuntimeError, match="hook boom"):
            planner.generate_plan(state, goal)

    def test_display_statistics_no_stats(self):
        """Test _display_statistics is a no-op without stats."""
        planner = Planner(actions_list=[])
        del planner.stats
        planner._display_statistics()  # Should not raise

    def test_generate_plan_invalid_input(self):
        """Test generate_plan raises for invalid input types."""
        planner = Planner(actions_list=[])
        with pytest.raises(TypeError, match="world_state must be a dict"):
            planner.generate_plan("not-a-state", {"a": 1})

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

    def test_provider_error_propagates(self, mocker, caplog):
        """Test provider exceptions propagate instead of being skipped."""

        class BadProvider:
            def provide_actions(self, state, goal=None):
                raise RuntimeError("provider boom")

        planner = Planner(providers=[BadProvider()])
        with pytest.raises(RuntimeError, match="provider boom"):
            planner.generate_plan(WorldState(a=1), {"b": 1})

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
        """Test continue_plan error path raises."""
        planner = Planner(actions_list=[])
        with pytest.raises(TypeError, match="world_state must be a dict"):
            planner.continue_plan("bad", {"a": 1}, [])

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
        """Test async planning error path raises."""
        planner = Planner(actions_list=[])
        with pytest.raises(TypeError, match="world_state must be a dict"):
            await planner.async_generate_plan("bad", {"a": 1})

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


class TestPlanExecution:
    def test_register_execution_handler_validation(self):
        """Test execution handler registration validation."""
        planner = Planner(actions_list=[])

        with pytest.raises(ValueError, match="non-empty string"):
            planner.register_execution_handler("", lambda s, a: s)

        with pytest.raises(TypeError, match="must be callable"):
            planner.register_execution_handler("act", "not-callable")  # type: ignore

    def test_execute_plan_sync_and_custom_handler(self, mocker):
        """Test synchronous plan execution with default effects and custom handler."""
        planner = Planner(
            actions_list=[
                ("step1", {"start": True}, {"mid": True}, 1.0),
                ("step2", {"mid": True}, {"done": True}, 1.0),
            ]
        )

        handler_mock = mocker.Mock(
            side_effect=lambda state, action: state.copy(deep=True)
        )
        planner.register_execution_handler("step2", handler_mock)

        hook_start = mocker.Mock()
        hook_complete = mocker.Mock()
        planner.register_hook("on_action_start", hook_start)
        planner.register_hook("on_action_complete", hook_complete)

        start_state = WorldState(start=True, mid=False, done=False)
        result = planner.generate_plan(start_state, Goal(target_state={"done": True}))

        final_state = planner.execute_plan(start_state, result)

        assert final_state.mid is True
        assert handler_mock.call_count == 1
        assert hook_start.call_count == 2
        assert hook_complete.call_count == 2

    def test_execute_plan_with_action_objects(self):
        """Test execute_plan accepting a list or tuple of Action objects directly."""
        act1 = Action("a1", {"x": 1}, {"x": 2})
        act2 = Action("a2", {"x": 2}, {"x": 3})
        planner = Planner()

        final_state = planner.execute_plan(WorldState(x=1), [act1, act2])
        assert final_state.x == 3

        # Test passing plain tuple
        final_state_tuple = planner.execute_plan(WorldState(x=1), (act1, act2))
        assert final_state_tuple.x == 3

    def test_execute_plan_none_plan_result_error(self):
        """Test execute_plan raises ValueError when passed PlanResult with plan=None."""
        planner = Planner()
        res = PlanResult(plan=None, message="No plan")

        with pytest.raises(ValueError, match="contains no valid plan"):
            planner.execute_plan(WorldState(), res)

    def test_execute_plan_precondition_failure(self, mocker):
        """Test execute_plan raises PlanExecutionError when preconditions are unmet."""
        from goapauto.models.goap_planner import PlanExecutionError

        planner = Planner(actions_list=[("step", {"req": True}, {"out": True}, 1.0)])
        hook_failed = mocker.Mock()
        planner.register_hook("on_action_failed", hook_failed)

        with pytest.raises(PlanExecutionError, match="not applicable"):
            planner.execute_plan(WorldState(req=False), ["step"])

        assert hook_failed.call_count == 1

    def test_execute_plan_async_handler_in_sync_mode_error(self):
        """Test execute_plan raises TypeError when an async handler is provided."""
        planner = Planner(actions_list=[("step", {}, {}, 1.0)])

        async def async_handler(state, action):
            return state

        planner.register_execution_handler("step", async_handler)

        with pytest.raises(TypeError, match="Async execution handler"):
            planner.execute_plan(WorldState(), ["step"])

    def test_execute_plan_type_errors(self):
        """Test type validation for initial_state and plan."""
        planner = Planner()
        with pytest.raises(TypeError, match="must be a WorldState"):
            planner.execute_plan("not-a-state", [])  # type: ignore

        with pytest.raises(TypeError, match="must be a list"):
            planner.execute_plan(WorldState(), "not-a-plan")  # type: ignore

        with pytest.raises(TypeError, match="must be an Action or action name"):
            planner.execute_plan(WorldState(), [123])  # type: ignore

        with pytest.raises(KeyError, match="not found"):
            planner.execute_plan(WorldState(), ["missing_action"])

    @pytest.mark.asyncio
    async def test_async_execute_plan_success_and_failure(self):
        """Test async_execute_plan with async custom handlers and failure paths."""
        from goapauto.models.goap_planner import PlanExecutionError

        planner = Planner(actions_list=[("step", {"start": True}, {"end": True}, 1.0)])

        async def async_handler(state, action):
            new_s = state.copy(deep=True)
            new_s.end = True
            return new_s

        planner.register_execution_handler("step", async_handler)

        initial = WorldState(start=True, end=False)
        final_state = await planner.async_execute_plan(initial, ["step"])
        assert final_state.end is True

        # Test failure in async mode
        with pytest.raises(PlanExecutionError):
            await planner.async_execute_plan(WorldState(start=False), ["step"])

    def test_execute_plan_handler_raises_exception_triggers_hooks(self, mocker):
        """Test execute_plan triggers failure hooks when handler raises unexpected exception."""
        planner = Planner(actions_list=[("step", {}, {}, 1.0)])

        def bad_handler(state, action):
            raise RuntimeError("Custom failure")

        planner.register_execution_handler("step", bad_handler)

        hook_failed = mocker.Mock()
        planner.register_hook("on_action_failed", hook_failed)

        with pytest.raises(RuntimeError, match="Custom failure"):
            planner.execute_plan(WorldState(), ["step"])

        assert hook_failed.call_count == 1

    @pytest.mark.asyncio
    async def test_async_execute_plan_error_coverage(self, mocker):
        """Test async_execute_plan error paths for complete test coverage."""

        planner = Planner(actions_list=[("step1", {}, {}, 1.0)])

        # 1. Non-WorldState initial_state
        with pytest.raises(TypeError, match="must be a WorldState"):
            await planner.async_execute_plan("not-a-state", ["step1"])  # type: ignore

        # 2. PlanResult with plan=None
        res_none = PlanResult(plan=None, message="None plan")
        with pytest.raises(ValueError, match="contains no valid plan"):
            await planner.async_execute_plan(WorldState(), res_none)

        # 3. Invalid plan type
        with pytest.raises(TypeError, match="must be a list"):
            await planner.async_execute_plan(WorldState(), "invalid-type")  # type: ignore

        # 4. Invalid step item type
        with pytest.raises(TypeError, match="must be an Action or action name"):
            await planner.async_execute_plan(WorldState(), [999])  # type: ignore

        # 5. Missing action key
        with pytest.raises(KeyError, match="not found"):
            await planner.async_execute_plan(WorldState(), ["unknown_step"])

        # 6. Action object step & sync handler in async mode
        def sync_handler(state, action):
            new_s = state.copy(deep=True)
            new_s.handled = True
            return new_s

        planner.register_execution_handler("act_obj", sync_handler)
        act = Action("act_obj", {}, {})
        result_state = await planner.async_execute_plan(WorldState(), [act])
        assert result_state.handled is True

        # 7. Handler raising non-PlanExecutionError exception in async mode
        def failing_handler(state, action):
            raise RuntimeError("Async handler exception")

        planner.register_execution_handler("fail_act", failing_handler)
        hook_failed = mocker.Mock()
        planner.register_hook("on_action_failed", hook_failed)

        with pytest.raises(RuntimeError, match="Async handler exception"):
            await planner.async_execute_plan(WorldState(), [Action("fail_act", {}, {})])

        assert hook_failed.call_count == 1

        # 8. Valid PlanResult in async_execute_plan
        res_valid = PlanResult(plan=["step1"], message="Valid")
        valid_state = await planner.async_execute_plan(WorldState(), res_valid)
        assert valid_state is not None

        # 9. Default async_apply without registered handler
        unhandled_act = Action("unhandled", {}, {"done": True})
        unhandled_planner = Planner()
        final_unhandled = await unhandled_planner.async_execute_plan(
            WorldState(), [unhandled_act]
        )
        assert final_unhandled.done is True

        # 10. Plain tuple step in async_execute_plan
        tuple_state = await unhandled_planner.async_execute_plan(
            WorldState(), (unhandled_act,)
        )
        assert tuple_state.done is True


class TestPlannerBugfixes:
    def test_init_does_not_mutate_providers_list(self):
        """Planner copies the providers list; the caller's list is untouched."""
        from goapauto.models.action_provider import StaticActionProvider
        from goapauto.models.actions import Actions

        providers = [StaticActionProvider(Actions())]
        Planner(
            providers=providers,
            actions_list=[("x", {}, {"y": 1}, 1)],
            verbose=False,
        )
        assert len(providers) == 1

    def test_default_logger_is_module_logger(self):
        """Without a custom logger the planner falls back to the module logger."""
        import logging

        planner = Planner(actions_list=[], verbose=False)
        assert planner._logger is logging.getLogger("goapauto.models.goap_planner")

    def test_custom_logger_is_used(self):
        """A passed-in logger is kept as-is."""
        import logging

        custom = logging.getLogger("test_custom_planner_logger")
        planner = Planner(actions_list=[], verbose=False, logger=custom)
        assert planner._logger is custom

    def _weighted_planner(self):
        return Planner(
            actions_list=[
                ("finish", {}, {"done": True}, {"time": 10.0, "energy": 10.0}),
            ],
            cost_weights={"time": 1.0, "energy": 0.0},
            verbose=False,
        )

    def test_f_score_matches_weighted_g_plus_h(self):
        """Heap f-scores use the planner's weighted g, not Node's fallback sum."""
        planner = self._weighted_planner()
        result = planner.generate_plan(WorldState(), {"done": True})
        assert result.plan == ["finish"]
        for node in planner.get_search_graph()["nodes"].values():
            assert node["f"] == pytest.approx(node["g"] + node["h"])

    async def test_async_f_score_matches_weighted_g_plus_h(self):
        """Same f-score invariant for the async search loop."""
        planner = self._weighted_planner()
        result = await planner.async_generate_plan(WorldState(), {"done": True})
        assert result.plan == ["finish"]
        for node in planner.get_search_graph()["nodes"].values():
            assert node["f"] == pytest.approx(node["g"] + node["h"])

    def _chain_planner(self):
        return Planner(
            actions_list=[
                ("step1", {"x": 0}, {"x": 1}, 1),
                ("step2", {"x": 1}, {"x": 2}, 1),
            ],
            verbose=False,
        )

    def test_continue_plan_strips_matching_prefix(self):
        """Executed leading actions are stripped from the fresh plan."""
        planner = self._chain_planner()
        result = planner.continue_plan(
            WorldState(x=1), {"x": 2}, executed_actions=["step1"]
        )
        assert result.plan == ["step2"]

    def test_continue_plan_ignores_non_prefix_executed(self):
        """Executed actions that are not a prefix must not drop later steps."""
        planner = self._chain_planner()
        result = planner.continue_plan(
            WorldState(x=0), {"x": 2}, executed_actions=["step2"]
        )
        assert result.plan == ["step1", "step2"]

    def test_continue_plan_executed_longer_than_plan(self):
        """An executed list longer than the fresh plan strips at most the plan."""
        planner = self._chain_planner()
        result = planner.continue_plan(
            WorldState(x=0), {"x": 1}, executed_actions=["step1", "step2", "step1"]
        )
        assert result.plan == []

    def test_default_search_is_optimal_with_sub_unit_costs(self):
        """Dijkstra default: the old counting heuristic overestimates here
        (h=1 for one unsatisfied condition, true remaining cost 0.6) and
        would return the suboptimal shortcut."""
        planner = Planner(
            actions_list=[
                ("cheap1", {}, {"a": 1}, 0.3),
                ("cheap2", {"a": 1}, {"b": 1}, 0.3),
                ("pricey", {}, {"b": 1}, 0.7),
            ],
            verbose=False,
        )
        result = planner.generate_plan(WorldState(), {"b": 1})
        assert result.plan == ["cheap1", "cheap2"]

    def test_repeated_searches_are_deterministic(self):
        """Equal-cost heap ties expand in insertion order: same plan every run."""
        planner = Planner(
            actions_list=[
                ("left", {}, {"done": True}, 1.0),
                ("right", {}, {"done": True}, 1.0),
            ],
            verbose=False,
        )
        plans = [
            planner.generate_plan(WorldState(), {"done": True}).plan for _ in range(5)
        ]
        assert all(p == plans[0] for p in plans)
        assert plans[0] == ["left"]


class SlowApplyAction(Action):
    """An action whose apply() sleeps past the planning budget."""

    def apply(self, state):
        time.sleep(0.05)
        return super().apply(state)


class SlowApplyProvider(ActionProvider):
    """Provider serving a single slow action that achieves the goal."""

    def provide_actions(self, state, goal=None):
        return [
            SlowApplyAction(
                name="slow",
                preconditions={},
                effects={"done": True},
                cost=1.0,
            )
        ]


class SlowAsyncApplyAction(Action):
    """An action whose async_apply() sleeps past the planning budget."""

    async def async_apply(self, state):
        import asyncio

        await asyncio.sleep(0.05)
        return await super().async_apply(state)


class SlowAsyncApplyProvider(ActionProvider):
    """Provider serving a single slow async action that achieves the goal."""

    def provide_actions(self, state, goal=None):
        return [
            SlowAsyncApplyAction(
                name="slow_async",
                preconditions={},
                effects={"done": True},
                cost=1.0,
            )
        ]


class TestTimeBudget:
    """Per-call planning time budgets (design 01)."""

    def _deep_chain_planner(self, depth=200):
        actions = [(f"step{i}", {"x": i}, {"x": i + 1}, 1.0) for i in range(depth)]
        return Planner(actions_list=actions, verbose=False, max_iterations=100000)

    def test_default_path_unchanged(self):
        """time_budget=None plans normally; flags stay at defaults."""
        planner = self._deep_chain_planner(depth=5)
        result = planner.generate_plan(WorldState(x=0), {"x": 5})
        assert result.plan == [f"step{i}" for i in range(5)]
        assert planner.stats.budget_exhausted is False
        assert planner.stats.budget_limit is None

    @pytest.mark.parametrize("bad", [0, 0.0, -1.0, float("nan")])
    def test_invalid_budget_raises_value_error(self, bad):
        planner = Planner(actions_list=[], verbose=False)
        with pytest.raises(ValueError):
            planner.generate_plan(WorldState(), {"x": 1}, time_budget=bad)

    @pytest.mark.parametrize("bad", ["fast", True, False, object()])
    def test_non_numeric_budget_raises_type_error(self, bad):
        planner = Planner(actions_list=[], verbose=False)
        with pytest.raises(TypeError):
            planner.generate_plan(WorldState(), {"x": 1}, time_budget=bad)

    def test_validation_runs_before_header(self, capsys):
        """A bad budget must not print the planning banner (fail-loud first)."""
        planner = Planner(
            actions_list=[("a", {}, {"x": 1}, 1.0)],
            verbose=True,
        )
        with pytest.raises(ValueError):
            planner.generate_plan(WorldState(), {"x": 1}, time_budget=-1.0)
        captured = capsys.readouterr()
        assert "GOAL-ORIENTED ACTION PLANNING" not in captured.out

    def test_continue_plan_validation_runs_before_header(self, capsys):
        planner = Planner(actions_list=[("a", {}, {"x": 1}, 1.0)], verbose=True)
        with pytest.raises(TypeError):
            planner.continue_plan(
                WorldState(), {"x": 1}, executed_actions=[], time_budget="fast"
            )
        assert "GOAL-ORIENTED ACTION PLANNING" not in capsys.readouterr().out

    async def test_async_validation_runs_before_header(self, capsys):
        planner = Planner(actions_list=[("a", {}, {"x": 1}, 1.0)], verbose=True)
        with pytest.raises(ValueError):
            await planner.async_generate_plan(
                WorldState(), {"x": 1}, time_budget=float("nan")
            )
        assert "GOAL-ORIENTED ACTION PLANNING" not in capsys.readouterr().out

    def test_exhaustion_reports_explicit_miss(self):
        """Tiny budget on a deep chain: plan None, flag set, limit recorded."""
        planner = self._deep_chain_planner()
        result = planner.generate_plan(WorldState(x=0), {"x": 200}, time_budget=1e-6)
        assert result.plan is None
        assert "budget" in result.message.lower()
        assert planner.stats.budget_exhausted is True
        assert planner.stats.budget_limit == 1e-6

    def test_slow_apply_overrun_still_reports_exhaustion(self):
        """The post-apply deadline check catches a single slow expansion."""
        planner = Planner(providers=[SlowApplyProvider()], verbose=False)
        result = planner.generate_plan(WorldState(), {"done": True}, time_budget=0.01)
        assert result.plan is None
        assert planner.stats.budget_exhausted is True
        assert "budget" in result.message.lower()

    async def test_slow_async_apply_overrun_still_reports_exhaustion(self):
        """The async post-apply deadline check catches a slow coroutine."""
        planner = Planner(providers=[SlowAsyncApplyProvider()], verbose=False)
        result = await planner.async_generate_plan(
            WorldState(), {"done": True}, time_budget=0.01
        )
        assert result.plan is None
        assert planner.stats.budget_exhausted is True
        assert "budget" in result.message.lower()

    def test_max_iterations_wins_when_smaller(self):
        """Iterations exhausted first: old message, flag stays False."""
        planner = Planner(
            actions_list=[
                ("step1", {"x": 0}, {"x": 1}, 1.0),
                ("step2", {"x": 1}, {"x": 2}, 1.0),
            ],
            verbose=False,
            max_iterations=1,
        )
        result = planner.generate_plan(WorldState(x=0), {"x": 2}, time_budget=3600.0)
        assert result.plan is None
        assert "No valid plan" in result.message
        assert planner.stats.budget_exhausted is False

    def test_budget_wins_tie_deterministically(self):
        """Both bounds would fire in one iteration: the deadline is checked first."""
        planner = Planner(
            actions_list=[
                ("step1", {"x": 0}, {"x": 1}, 1.0),
                ("step2", {"x": 1}, {"x": 2}, 1.0),
            ],
            verbose=False,
            max_iterations=1,
        )
        result = planner.generate_plan(WorldState(x=0), {"x": 2}, time_budget=1e-9)
        assert result.plan is None
        assert planner.stats.budget_exhausted is True
        assert "budget" in result.message.lower()

    def test_goal_satisfied_records_limit_without_exhaustion(self):
        planner = Planner(actions_list=[], verbose=False)
        result = planner.generate_plan(WorldState(x=1), {"x": 1}, time_budget=0.05)
        assert result.plan == []
        assert planner.stats.budget_exhausted is False
        assert planner.stats.budget_limit == 0.05

    def test_inf_budget_behaves_as_unlimited(self):
        planner = self._deep_chain_planner(depth=5)
        result = planner.generate_plan(
            WorldState(x=0), {"x": 5}, time_budget=float("inf")
        )
        assert result.plan == [f"step{i}" for i in range(5)]
        assert planner.stats.budget_exhausted is False

    def test_budget_hooks_fire_in_order_with_stats_kwarg(self):
        """on_search_failed fires, then on_budget_exhausted, both with stats= only."""
        planner = self._deep_chain_planner()
        calls = []

        def record(name):
            def cb(**kwargs):
                calls.append((name, kwargs))

            return cb

        planner.register_hook("on_search_failed", record("failed"))
        planner.register_hook("on_budget_exhausted", record("budget"))
        planner.generate_plan(WorldState(x=0), {"x": 200}, time_budget=1e-6)

        assert [name for name, _ in calls] == ["failed", "budget"]
        for _, kwargs in calls:
            assert kwargs == {"stats": planner.stats}
        assert planner.stats.budget_exhausted is True

    def test_budget_hook_registered_by_default_as_noop(self):
        planner = Planner(actions_list=[], verbose=False)
        assert "on_budget_exhausted" in planner.hooks
        assert planner.hooks["on_budget_exhausted"] == []

    def test_unknown_budget_hook_name_raises(self):
        planner = Planner(actions_list=[], verbose=False)
        with pytest.raises(ValueError):
            planner.register_hook("on_budget_exhaust", lambda stats: None)

    def test_continue_plan_honors_budget(self):
        planner = self._deep_chain_planner()
        result = planner.continue_plan(
            WorldState(x=0), {"x": 200}, executed_actions=[], time_budget=1e-6
        )
        assert result.plan is None
        assert "budget" in result.message.lower()
        assert planner.stats.budget_exhausted is True
        assert planner.stats.budget_limit == 1e-6

    async def test_async_generate_plan_honors_budget(self):
        planner = self._deep_chain_planner()
        result = await planner.async_generate_plan(
            WorldState(x=0), {"x": 200}, time_budget=1e-6
        )
        assert result.plan is None
        assert "budget" in result.message.lower()
        assert planner.stats.budget_exhausted is True
        assert planner.stats.budget_limit == 1e-6

    def test_get_action_returns_action_or_none(self):
        planner = Planner(
            actions_list=[("pickup_key", {}, {"has_key": True}, 1.0)], verbose=False
        )
        state = WorldState()
        action = planner.get_action("pickup_key", state)
        assert isinstance(action, Action)
        assert action.name == "pickup_key"
        assert planner.get_action("missing", state) is None

    def test_get_action_goal_keyword_optional(self):
        planner = Planner(
            actions_list=[("pickup_key", {}, {"has_key": True}, 1.0)], verbose=False
        )
        state = WorldState()
        goal = Goal(target_state={"has_key": True})
        assert planner.get_action("pickup_key", state).name == "pickup_key"
        assert planner.get_action("pickup_key", state, goal=goal).name == "pickup_key"
