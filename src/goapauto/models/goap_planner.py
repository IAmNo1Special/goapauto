from __future__ import annotations

import heapq
import io
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import (
    Any,
    NamedTuple,
    TypeVar,
)

from goapauto.models.action_provider import ActionProvider, StaticActionProvider
from goapauto.models.actions import Action, Actions
from goapauto.models.goal import Goal
from goapauto.models.node import Node
from goapauto.models.worldstate import WorldState

# Set up console for Windows to support Unicode
if os.name == "nt":
    try:
        # Mutate the existing stream instead of rebinding sys.stdout so
        # pytest capture and other frameworks keep a valid reference.
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation, ValueError):
        # stdout may be a StringIO, already closed, or not reconfigure-able.
        pass


def safe_print(*args, **kwargs):
    """Safely print text that might contain Unicode characters."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # Fallback for environments that can't handle certain Unicode chars
        cleaned = [str(arg).encode("ascii", "replace").decode("ascii") for arg in args]
        print(*cleaned, **{k: v for k, v in kwargs.items() if k != "end"})


logger = logging.getLogger(__name__)
T = TypeVar("T", bound="Planner")

# Type aliases for better readability
Plan = list[str]


@dataclass
class ScheduleStep:
    """A single step in a temporal schedule."""

    action: str
    start_time: float
    end_time: float
    cost: float


@dataclass
class Schedule:
    """Temporal schedule for a plan with action start/end times."""

    steps: list[ScheduleStep]
    makespan: float
    total_cost: float

    def to_list(self) -> list[dict]:
        """Convert to list of dicts for serialization."""
        return [
            {
                "action": step.action,
                "start_time": step.start_time,
                "end_time": step.end_time,
                "duration": step.end_time - step.start_time,
                "cost": step.cost,
            }
            for step in self.steps
        ]


class PlanExecutionError(ValueError):
    """Exception raised when plan execution fails (e.g. precondition not met)."""

    pass


class PlanResult(NamedTuple):
    """Result of a planning operation."""

    plan: Plan | None
    message: str
    schedule: Schedule | None = None
    makespan: float | None = None
    total_cost: float = 0.0


StateKey = int  # Hash of a WorldState
HeuristicFn = Callable[[WorldState, Goal | dict[str, Any]], float]


@dataclass
class PlanStats:
    """Statistics about the planning process."""

    nodes_expanded: int = 0
    nodes_visited: int = 0
    plan_length: int = 0
    total_cost: float = 0.0
    execution_time: float = 0.0


class Planner:
    """Goal-Oriented Action Planner (GOAP) implementation using A* search.

    This class implements a planning system that finds a sequence of actions to
    achieve a goal state from an initial state, using A* search with a heuristic.

    Attributes:
        providers: List of ActionProvider instances
        max_iterations: Maximum number of iterations before giving up
        stats: Statistics about the last planning operation
        verbose: Whether to print progress to stdout
    """

    def __init__(
        self,
        actions_list: list[tuple[str, dict[str, Any], dict[str, Any], float]]
        | None = None,
        providers: list[ActionProvider] | None = None,
        max_iterations: int = 1000,
        heuristic_fn: HeuristicFn | None = None,
        verbose: bool = True,
        logger: logging.Logger | None = None,
        cost_weights: dict[str, float] | list[float] | None = None,
    ) -> None:
        """Initialize the planner with optional actions, providers, and config.

        Args:
            actions_list: Optional list of action tuples (name, preconditions, effects, cost)
            providers: Optional list of ActionProvider instances
            max_iterations: Maximum number of iterations for the search algorithm
            heuristic_fn: Optional default heuristic function
            verbose: Whether to print progress messages to stdout (default True)
            logger: Optional custom logger instance (uses module logger if None)
            cost_weights: Optional weights for multi-dimensional costs.
                If dict: keys match action cost dict keys (e.g., {"time": 1.0, "energy": 0.5})
                If list: weights correspond to cost list indices
                If None: actions must use scalar costs
        """
        self.providers = providers or []
        if actions_list:
            static_actions = Actions()
            static_actions.add_actions(actions_list)
            self.providers.append(StaticActionProvider(static_actions))

        self.max_iterations = max_iterations
        self.stats = PlanStats()
        self.heuristic_fn = heuristic_fn
        self.verbose = verbose
        self._logger = logger or logger
        self.cost_weights = cost_weights

        # Hook system for middleware
        self.hooks: dict[str, list[Callable[..., Any]]] = {
            "on_node_expanded": [],
            "on_plan_found": [],
            "on_search_failed": [],
            "on_action_start": [],
            "on_action_complete": [],
            "on_action_failed": [],
            "on_execution_complete": [],
            "on_execution_failed": [],
        }

        self.execution_handlers: dict[str, Callable[..., Any]] = {}

        # Search graph tracking (for visualization/debugging)
        self._search_graph_nodes: dict[int, dict[str, Any]] = {}
        self._search_graph_edges: list[dict[str, Any]] = []
        self._search_graph_max_depth: int = 0

    def _get_scalar_cost(self, action: Action) -> float:
        """Compute scalar cost from action's potentially multi-dimensional cost.

        If cost_weights is set, computes weighted sum. Otherwise returns scalar cost.
        """
        cost = action.cost
        if self.cost_weights is None:
            # Scalar cost - return as-is (convert to float)
            if isinstance(cost, (int, float)):
                return float(cost)
            raise ValueError(
                "Action has multi-dimensional cost but no cost_weights provided"
            )

        if isinstance(cost, (int, float)):
            # Scalar cost with weights - just return it (weights not applicable)
            return float(cost)

        if isinstance(cost, dict) and isinstance(self.cost_weights, dict):
            # Both are dicts - compute weighted sum
            total = 0.0
            for key, value in cost.items():
                weight = self.cost_weights.get(key, 0.0)
                total += float(value) * weight
            return total

        if isinstance(cost, list) and isinstance(self.cost_weights, list):
            # Both are lists - compute dot product
            if len(cost) != len(self.cost_weights):
                raise ValueError(
                    "Cost list and cost_weights list must have same length"
                )
            return sum(
                float(c) * w for c, w in zip(cost, self.cost_weights, strict=True)
            )

        raise ValueError("Incompatible cost and cost_weights types")

    def _log(self, level: int, msg: str, *args, **kwargs) -> None:
        """Log a message if verbose, always log to logger."""
        if self._logger:
            self._logger.log(level, msg, *args, **kwargs)
        if self.verbose:
            safe_print(msg, *args, **kwargs)

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a specific planner event.

        Args:
            event: One of 'on_node_expanded', 'on_plan_found', 'on_search_failed',
                'on_action_start', 'on_action_complete', 'on_action_failed',
                'on_execution_complete', 'on_execution_failed'
            callback: The function to call when the event occurs
        """
        if event in self.hooks:
            self.hooks[event].append(callback)
        else:
            raise ValueError(f"Unknown event hook: {event}")

    def register_execution_handler(
        self, action_name: str, handler: Callable[..., Any]
    ) -> None:
        """Register a custom execution handler for an action.

        Args:
            action_name: The name of the action to handle
            handler: Callable that takes (current_state, action) and returns updated WorldState
        """
        if not isinstance(action_name, str) or not action_name.strip():
            raise ValueError("Action name must be a non-empty string")
        if not callable(handler):
            raise TypeError("Handler must be callable")
        self.execution_handlers[action_name] = handler

    def _get_action_by_name(
        self, action_name: str, state: WorldState, goal: Goal | None = None
    ) -> Action | None:
        """Look up an action by name across all registered ActionProviders for a given state."""
        for provider in self.providers:
            actions = provider.provide_actions(state, goal)
            for action in actions:
                if action.name == action_name:
                    return action
        return None

    def execute_plan(
        self,
        initial_state: WorldState,
        plan: Plan | list[Action] | PlanResult,
        goal: Goal | None = None,
    ) -> WorldState:
        """Execute a sequence of actions on the initial state and return the final state.

        Args:
            initial_state: Starting WorldState
            plan: Sequence of action names, Action objects, or a PlanResult
            goal: Optional Goal context for dynamic action providers

        Returns:
            WorldState after executing all plan steps

        Raises:
            TypeError: If initial_state/plan has an invalid type or an async handler is used
            ValueError: If PlanResult.plan is None
            KeyError: If an action name is not found in registered providers
            PlanExecutionError: If an action's preconditions are not met
        """
        if not isinstance(initial_state, WorldState):
            raise TypeError("initial_state must be a WorldState instance")

        steps: list[str | Action] = []
        if hasattr(plan, "plan") and hasattr(plan, "message"):
            p_plan = plan.plan
            if p_plan is None:
                raise ValueError("PlanResult contains no valid plan to execute.")
            steps = list(p_plan)
        elif isinstance(plan, list):
            steps = list(plan)
        elif isinstance(plan, tuple):
            steps = list(plan)
        else:
            raise TypeError(
                "plan must be a list of action names/Action objects or a PlanResult"
            )

        current_state = initial_state.copy(deep=True)

        for step in steps:
            action: Action | None = None
            if isinstance(step, Action):
                action = step
            elif isinstance(step, str):
                action = self._get_action_by_name(step, current_state, goal)
                if action is None:
                    raise KeyError(
                        f"Action '{step}' not found in registered providers."
                    )
            else:
                raise TypeError(
                    f"Plan step must be an Action or action name string, got {type(step)}"
                )

            if not action.is_applicable(current_state):
                self._trigger_hook(
                    "on_action_failed", action=action, state=current_state
                )
                self._trigger_hook(
                    "on_execution_failed", action=action, state=current_state
                )
                raise PlanExecutionError(
                    f"Action '{action.name}' is not applicable to current state."
                )

            self._trigger_hook("on_action_start", action=action, state=current_state)

            try:
                if action.name in self.execution_handlers:
                    handler = self.execution_handlers[action.name]
                    import inspect

                    if inspect.iscoroutinefunction(handler):
                        raise TypeError(
                            f"Async execution handler for action '{action.name}' cannot be used in "
                            "synchronous execute_plan. Use async_execute_plan instead."
                        )
                    current_state = handler(current_state, action)
                else:
                    current_state = action.apply(current_state)
            except Exception as e:
                if not isinstance(e, (TypeError, PlanExecutionError)):
                    self._trigger_hook(
                        "on_action_failed", action=action, state=current_state
                    )
                    self._trigger_hook(
                        "on_execution_failed", action=action, state=current_state
                    )
                raise

            self._trigger_hook("on_action_complete", action=action, state=current_state)

        self._trigger_hook("on_execution_complete", state=current_state)
        return current_state

    async def async_execute_plan(
        self,
        initial_state: WorldState,
        plan: Plan | list[Action] | PlanResult,
        goal: Goal | None = None,
    ) -> WorldState:
        """Asynchronously execute a sequence of actions on the initial state.

        Args:
            initial_state: Starting WorldState
            plan: Sequence of action names, Action objects, or a PlanResult
            goal: Optional Goal context for dynamic action providers

        Returns:
            WorldState after executing all plan steps

        Raises:
            TypeError: If initial_state/plan has an invalid type
            ValueError: If PlanResult.plan is None
            KeyError: If an action name is not found in registered providers
            PlanExecutionError: If an action's preconditions are not met
        """
        if not isinstance(initial_state, WorldState):
            raise TypeError("initial_state must be a WorldState instance")

        steps: list[str | Action] = []
        if hasattr(plan, "plan") and hasattr(plan, "message"):
            p_plan = plan.plan
            if p_plan is None:
                raise ValueError("PlanResult contains no valid plan to execute.")
            steps = list(p_plan)
        elif isinstance(plan, list):
            steps = list(plan)
        elif isinstance(plan, tuple):
            steps = list(plan)
        else:
            raise TypeError(
                "plan must be a list of action names/Action objects or a PlanResult"
            )

        current_state = initial_state.copy(deep=True)

        for step in steps:
            action: Action | None = None
            if isinstance(step, Action):
                action = step
            elif isinstance(step, str):
                action = self._get_action_by_name(step, current_state, goal)
                if action is None:
                    raise KeyError(
                        f"Action '{step}' not found in registered providers."
                    )
            else:
                raise TypeError(
                    f"Plan step must be an Action or action name string, got {type(step)}"
                )

            if not action.is_applicable(current_state):
                self._trigger_hook(
                    "on_action_failed", action=action, state=current_state
                )
                self._trigger_hook(
                    "on_execution_failed", action=action, state=current_state
                )
                raise PlanExecutionError(
                    f"Action '{action.name}' is not applicable to current state."
                )

            self._trigger_hook("on_action_start", action=action, state=current_state)

            try:
                if action.name in self.execution_handlers:
                    handler = self.execution_handlers[action.name]
                    import inspect

                    if inspect.iscoroutinefunction(handler):
                        current_state = await handler(current_state, action)
                    else:
                        current_state = handler(current_state, action)
                else:
                    current_state = await action.async_apply(current_state)
            except Exception as e:
                if not isinstance(e, PlanExecutionError):
                    self._trigger_hook(
                        "on_action_failed", action=action, state=current_state
                    )
                    self._trigger_hook(
                        "on_execution_failed", action=action, state=current_state
                    )
                raise

            self._trigger_hook("on_action_complete", action=action, state=current_state)

        self._trigger_hook("on_execution_complete", state=current_state)
        return current_state

    def _trigger_hook(self, event: str, *args, **kwargs) -> None:
        """Trigger all registered callbacks for an event."""
        for callback in self.hooks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error("Error in hook %s: %s", event, e)

    def _display_statistics(self) -> None:
        """Display planning statistics in a consistent format."""
        if not hasattr(self, "stats") or not self.stats:
            return

        stats = self.stats
        self._log(logging.INFO, "\n" + "=" * 50)
        self._log(logging.INFO, "PLANNING STATISTICS")
        self._log(logging.INFO, "=" * 50)
        self._log(logging.INFO, f"- Nodes expanded: {stats.nodes_expanded}")
        self._log(logging.INFO, f"- Nodes visited: {stats.nodes_visited}")
        self._log(logging.INFO, f"- Plan length: {stats.plan_length}")
        self._log(logging.INFO, f"- Total cost: {stats.total_cost:.2f}")
        self._log(logging.INFO, f"- Execution time: {stats.execution_time:.4f} seconds")
        self._log(logging.INFO, "=" * 50 + "\n")

    def generate_plan(
        self,
        world_state: dict[str, Any] | WorldState,
        goal: dict[str, Any] | Goal,
        max_depth: int | None = None,
        heuristic_fn: HeuristicFn | None = None,
    ) -> PlanResult:
        """Generate a plan to achieve the given goal.

        Args:
            world_state: The current state of the world
            goal: The goal to achieve
            max_depth: Optional maximum depth for the search
            heuristic_fn: Optional custom heuristic function for this plan

        Returns:
            A tuple of (plan, message)
        """
        import time

        self._print_header(goal)
        start_time = time.time()
        self.stats = PlanStats()
        h_fn = heuristic_fn or self.heuristic_fn

        try:
            world_state, goal = self._validate_and_convert(world_state, goal, max_depth)

            if goal.is_satisfied(world_state):
                self.stats.execution_time = time.time() - start_time
                return PlanResult(plan=[], message="✅ Goal is already satisfied!")

            plan, schedule = self._find_plan(world_state, goal, max_depth, h_fn)
            return self._finalize_plan_generation(plan, schedule, start_time)

        except Exception as e:
            logger.exception("Error during planning")
            return PlanResult(plan=None, message=f"❌ Error during planning: {str(e)}")

    def continue_plan(
        self,
        world_state: dict[str, Any] | WorldState,
        goal: dict[str, Any] | Goal,
        executed_actions: list[str],
        max_depth: int | None = None,
        heuristic_fn: HeuristicFn | None = None,
    ) -> PlanResult:
        """Continue planning from a checkpoint after executing some actions.

        This allows incremental replanning by reusing the already-executed actions
        and finding the remainder of the plan from the current state.

        Args:
            world_state: The current state of the world (after executing actions)
            goal: The goal to achieve
            executed_actions: List of action names that have already been executed
            max_depth: Optional maximum depth for the search
            heuristic_fn: Optional custom heuristic function for this plan

        Returns:
            PlanResult with the remaining plan steps
        """
        import time

        self._print_header(goal)
        start_time = time.time()
        self.stats = PlanStats()
        h_fn = heuristic_fn or self.heuristic_fn

        try:
            world_state, goal = self._validate_and_convert(world_state, goal, max_depth)

            if goal.is_satisfied(world_state):
                self.stats.execution_time = time.time() - start_time
                return PlanResult(plan=[], message="✅ Goal is already satisfied!")

            # Find plan from current state
            plan, schedule = self._find_plan(world_state, goal, max_depth, h_fn)

            if not plan:
                return self._finalize_plan_generation([], None, start_time)

            # Filter out already executed actions from the beginning of the plan
            remaining_plan = []
            executed_set = set(executed_actions)
            skip_count = 0

            for action_name in plan:
                if action_name in executed_set and skip_count < len(executed_actions):
                    skip_count += 1
                    continue
                remaining_plan.append(action_name)

            if not remaining_plan:
                self.stats.execution_time = time.time() - start_time
                return PlanResult(
                    plan=[],
                    message="✅ All executed actions complete the plan!",
                    schedule=None,
                )

            # Skip already-executed actions from the beginning of the plan
            if hasattr(self, "_reconstruct_plan"):
                # We could rebuild schedule for remaining actions, but for simplicity
                # just return the remaining plan without schedule
                pass

            self.stats.plan_length = len(remaining_plan)
            self.stats.execution_time = time.time() - start_time

            self._log(logging.INFO, "\n" + "=" * 50)
            self._log(logging.INFO, "CONTINUED PLAN GENERATION COMPLETE")
            self._log(logging.INFO, "=" * 50)

            self._log(
                logging.INFO,
                f"\n[SUCCESS] Found remaining plan with {len(remaining_plan)} actions",
            )
            self._log(logging.INFO, "\nPLAN STEPS:")
            for i, action_name in enumerate(remaining_plan, 1):
                self._log(logging.INFO, f"  {i}. {action_name}")
            self._display_statistics()
            self._trigger_hook("on_plan_found", plan=remaining_plan, stats=self.stats)

            return PlanResult(
                plan=remaining_plan,
                message=f"[SUCCESS] Found remaining plan with {len(remaining_plan)} actions",
            )

        except Exception as e:
            logger.exception("Error during continued planning")
            return PlanResult(
                plan=None, message=f"❌ Error during continued planning: {str(e)}"
            )

    async def async_generate_plan(
        self,
        world_state: dict[str, Any] | WorldState,
        goal: dict[str, Any] | Goal,
        max_depth: int | None = None,
        heuristic_fn: HeuristicFn | None = None,
    ) -> PlanResult:
        """Asynchronously generate a plan."""
        import time

        self._print_header(goal)
        start_time = time.time()
        self.stats = PlanStats()
        h_fn = heuristic_fn or self.heuristic_fn

        try:
            world_state, goal = self._validate_and_convert(world_state, goal, max_depth)

            if goal.is_satisfied(world_state):
                self.stats.execution_time = time.time() - start_time
                return PlanResult(plan=[], message="✅ Goal is already satisfied!")

            plan, schedule = await self._async_find_plan(
                world_state, goal, max_depth, h_fn
            )
            return self._finalize_plan_generation(plan, schedule, start_time)

        except Exception as e:
            logger.exception("Error during async planning")
            return PlanResult(plan=None, message=f"❌ Error during planning: {str(e)}")

    def _print_header(self, goal: Goal | dict[str, Any]) -> None:
        """Print planning header information."""
        self._log(logging.INFO, "\n" + "=" * 50)
        self._log(logging.INFO, "GOAL-ORIENTED ACTION PLANNING")
        self._log(logging.INFO, "=" * 50)
        name = getattr(goal, "name", str(goal))
        target = getattr(goal, "target_state", goal)
        self._log(logging.INFO, f"\nGOAL: {name}")
        self._log(logging.INFO, f"TARGET STATE: {target}\n")

    def _validate_and_convert(
        self, world_state: Any, goal: Any, max_depth: int | None
    ) -> tuple[WorldState, Goal]:
        """Validate inputs and convert to proper types."""
        if not isinstance(world_state, (dict, WorldState)):
            raise TypeError(
                f"world_state must be a dict or WorldState, got {type(world_state)}"
            )
        if not isinstance(goal, (dict, Goal, WorldState)):
            raise TypeError(
                f"goal must be a dict, Goal, or WorldState, got {type(goal)}"
            )
        if max_depth is not None and max_depth <= 0:
            raise ValueError(f"max_depth must be positive, got {max_depth}")

        if isinstance(world_state, dict):
            world_state = WorldState(**world_state)
        if isinstance(goal, dict):
            goal = Goal(target_state=goal)
        elif isinstance(goal, WorldState):
            goal = Goal(target_state=goal.get_state())

        return world_state, goal

    def _finalize_plan_generation(
        self, plan: Plan | None, schedule: Schedule | None, start_time: float
    ) -> PlanResult:
        """Finalize stats and print result message."""
        import time

        self.stats.plan_length = len(plan) if plan else 0
        self.stats.execution_time = time.time() - start_time

        self._log(logging.INFO, "\n" + "=" * 50)
        self._log(logging.INFO, "PLAN GENERATION COMPLETE")
        self._log(logging.INFO, "=" * 50)

        if plan:
            message = f"[SUCCESS] Found plan with {len(plan)} actions"
            self._log(logging.INFO, f"\n{message}")
            self._log(logging.INFO, "\nPLAN STEPS:")
            for i, action_name in enumerate(plan, 1):
                self._log(logging.INFO, f"  {i}. {action_name}")
            if self.verbose:
                self._display_statistics()
            self._trigger_hook("on_plan_found", plan=plan, stats=self.stats)
            return PlanResult(
                plan=plan,
                message=message,
                schedule=schedule,
                total_cost=schedule.total_cost if schedule else self.stats.total_cost,
            )

        message = "❌ No valid plan found to achieve the goal."
        self._log(logging.INFO, f"\n{message}")
        self._display_statistics()
        self._trigger_hook("on_search_failed", stats=self.stats)
        return PlanResult(plan=None, message=message)

    def _get_all_available_actions(
        self, state: WorldState, goal: Goal | None = None
    ) -> list[Action]:
        """Query all providers for available actions."""
        all_actions = []
        for provider in self.providers:
            try:
                all_actions.extend(provider.provide_actions(state, goal))
            except Exception as e:
                logger.error("Error providing actions from %s: %s", provider, e)
        return all_actions

    def _find_plan(
        self,
        world_state: WorldState,
        goal: Goal,
        max_depth: int | None,
        heuristic_fn: HeuristicFn | None,
    ) -> tuple[Plan, Schedule | None]:
        """Internal method to find a plan using A* search."""
        logger.info("Planning to achieve goal: %s", goal)

        # Clear previous search graph
        self._search_graph_nodes = {}
        self._search_graph_edges = []
        self._search_graph_max_depth = 0

        start_node = Node(world_state, None, goal, heuristic_fn=heuristic_fn)
        start_id = id(start_node)
        self._search_graph_nodes[start_id] = {
            "id": start_id,
            "state": start_node.state.get_state(),
            "g": start_node.g_score,
            "h": start_node.h_score,
            "f": start_node.f_score,
            "parent": None,
            "action": None,
        }
        frontier: list[tuple[float, int, Node]] = []
        heapq.heappush(frontier, (start_node.f_score, id(start_node), start_node))

        g_scores: dict[StateKey, float] = {hash(world_state): 0}
        iteration = 0

        while frontier and iteration < self.max_iterations:
            iteration += 1
            self.stats.nodes_visited += 1
            _, _, current_node = heapq.heappop(frontier)

            if goal.is_satisfied(current_node.state):
                plan, schedule = self._reconstruct_plan(current_node)
                return plan, schedule

            current_state_key = hash(current_node.state)
            if current_node.g_score > g_scores.get(current_state_key, float("inf")):
                continue

            # Phase 2: Use ActionProviders
            self._trigger_hook("on_node_expanded", node=current_node)
            for action in self._get_all_available_actions(current_node.state, goal):
                if not action.is_applicable(current_node.state):
                    continue

                self.stats.nodes_expanded += 1
                new_state = action.apply(current_node.state)
                new_state_key = hash(new_state)
                action_cost = self._get_scalar_cost(action)
                tentative_g_score = current_node.g_score + action_cost

                if tentative_g_score >= g_scores.get(new_state_key, float("inf")):
                    continue

                new_node = Node(
                    new_state, current_node, goal, action, heuristic_fn=heuristic_fn
                )
                new_node.g_score = tentative_g_score

                # Respect the max_depth limit for nodes added to the frontier
                if max_depth is not None and new_node.depth() > max_depth:
                    continue

                g_scores[new_state_key] = tentative_g_score
                heapq.heappush(frontier, (new_node.f_score, id(new_node), new_node))

                # Track in search graph
                new_id = id(new_node)
                self._search_graph_max_depth = max(
                    self._search_graph_max_depth, new_node.depth()
                )
                self._search_graph_nodes[new_id] = {
                    "id": new_id,
                    "state": new_node.state.get_state(),
                    "g": new_node.g_score,
                    "h": new_node.h_score,
                    "f": new_node.f_score,
                    "parent": id(current_node),
                    "action": action.name,
                }
                self._search_graph_edges.append(
                    {
                        "from": id(current_node),
                        "to": new_id,
                        "action": action.name,
                        "cost": action_cost,
                    }
                )

        return [], None

    async def _async_find_plan(
        self,
        world_state: WorldState,
        goal: Goal,
        max_depth: int | None,
        heuristic_fn: HeuristicFn | None,
    ) -> tuple[Plan, Schedule | None]:
        """Asynchronously find a plan using A* search."""
        logger.info("Async planning to achieve goal: %s", goal)

        # Clear previous search graph
        self._search_graph_nodes = {}
        self._search_graph_edges = []
        self._search_graph_max_depth = 0

        start_node = Node(world_state, None, goal, heuristic_fn=heuristic_fn)
        start_id = id(start_node)
        self._search_graph_nodes[start_id] = {
            "id": start_id,
            "state": start_node.state.get_state(),
            "g": start_node.g_score,
            "h": start_node.h_score,
            "f": start_node.f_score,
            "parent": None,
            "action": None,
        }
        frontier: list[tuple[float, int, Node]] = []
        heapq.heappush(frontier, (start_node.f_score, id(start_node), start_node))

        g_scores: dict[StateKey, float] = {hash(world_state): 0}
        iteration = 0

        while frontier and iteration < self.max_iterations:
            iteration += 1
            self.stats.nodes_visited += 1
            _, _, current_node = heapq.heappop(frontier)

            if goal.is_satisfied(current_node.state):
                plan, schedule = self._reconstruct_plan(current_node)
                return plan, schedule

            current_state_key = hash(current_node.state)
            if current_node.g_score > g_scores.get(current_state_key, float("inf")):
                continue

            self._trigger_hook("on_node_expanded", node=current_node)
            for action in self._get_all_available_actions(current_node.state, goal):
                if not action.is_applicable(current_node.state):
                    continue

                self.stats.nodes_expanded += 1
                new_state = await action.async_apply(current_node.state)
                new_state_key = hash(new_state)
                action_cost = self._get_scalar_cost(action)
                tentative_g_score = current_node.g_score + action_cost

                if tentative_g_score >= g_scores.get(new_state_key, float("inf")):
                    continue

                new_node = Node(
                    new_state, current_node, goal, action, heuristic_fn=heuristic_fn
                )
                new_node.g_score = tentative_g_score

                # Respect the max_depth limit for nodes added to the frontier
                if max_depth is not None and new_node.depth() > max_depth:
                    continue

                g_scores[new_state_key] = tentative_g_score
                heapq.heappush(frontier, (new_node.f_score, id(new_node), new_node))

                # Track in search graph
                new_id = id(new_node)
                self._search_graph_max_depth = max(
                    self._search_graph_max_depth, new_node.depth()
                )
                self._search_graph_nodes[new_id] = {
                    "id": new_id,
                    "state": new_node.state.get_state(),
                    "g": new_node.g_score,
                    "h": new_node.h_score,
                    "f": new_node.f_score,
                    "parent": id(current_node),
                    "action": action.name,
                }
                self._search_graph_edges.append(
                    {
                        "from": id(current_node),
                        "to": new_id,
                        "action": action.name,
                        "cost": action_cost,
                    }
                )

        return [], None

    def _reconstruct_plan(self, node: Node) -> tuple[Plan, Schedule | None]:
        """Reconstruct the plan and schedule from the goal node back to the start."""
        plan: Plan = []
        schedule_steps: list[tuple[str, float, float]] = []
        total_cost = 0.0
        current = node

        while current.parent is not None and current.action is not None:
            plan.insert(0, current.action.name)
            action_cost = self._get_scalar_cost(current.action)
            total_cost += action_cost

            # Collect (name, duration, cost) in execution order (start -> end)
            if current.action.duration is not None:
                schedule_steps.insert(
                    0,
                    (current.action.name, current.action.duration, action_cost),
                )

            current = current.parent

        self.stats.total_cost = total_cost

        # Create schedule if any actions have duration
        schedule = None
        if schedule_steps:
            steps: list[ScheduleStep] = []
            current_time = 0.0
            for name, duration, action_cost in schedule_steps:
                steps.append(
                    ScheduleStep(
                        action=name,
                        start_time=current_time,
                        end_time=current_time + duration,
                        cost=action_cost,
                    )
                )
                current_time += duration
            makespan = max(step.end_time for step in steps)
            schedule = Schedule(steps=steps, makespan=makespan, total_cost=total_cost)

        return plan, schedule

    def get_search_graph(self) -> dict[str, Any]:
        """Return the search graph from the last planning operation.

        Returns:
            Dictionary containing nodes and edges of the search graph:
            {
                "nodes": {node_id: {id, state, g, h, f, parent, action}},
                "edges": [{"from", "to", "action", "cost"}],
                "metadata": {expanded_count, visited_count, max_depth_reached}
            }
        """
        return {
            "nodes": self._search_graph_nodes,
            "edges": self._search_graph_edges,
            "metadata": {
                "expanded_count": self.stats.nodes_expanded,
                "visited_count": self.stats.nodes_visited,
                "max_depth_reached": self._search_graph_max_depth,
            },
        }
