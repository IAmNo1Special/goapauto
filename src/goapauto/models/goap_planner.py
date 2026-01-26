from __future__ import annotations

import heapq
import logging
import os
import sys
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from goapauto.models.action_provider import ActionProvider, StaticActionProvider
from goapauto.models.actions import Action, Actions
from goapauto.models.goal import Goal
from goapauto.models.node import Node
from goapauto.models.worldstate import WorldState

# Set up console for Windows to support Unicode
if os.name == "nt":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="ignore")


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
Plan = List[str]


class PlanResult(NamedTuple):
    """Result of a planning operation."""

    plan: Optional[Plan]
    message: str


StateKey = int  # Hash of a WorldState
HeuristicFn = Callable[[WorldState, Union[Goal, Dict[str, Any]]], float]


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
    """

    def __init__(
        self,
        actions_list: Optional[
            List[Tuple[str, Dict[str, Any], Dict[str, Any], float]]
        ] = None,
        providers: Optional[List[ActionProvider]] = None,
        max_iterations: int = 1000,
        heuristic_fn: Optional[HeuristicFn] = None,
    ) -> None:
        """Initialize the planner with optional actions, providers, and config.

        Args:
            actions_list: Optional list of action tuples (name, preconditions, effects, cost)
            providers: Optional list of ActionProvider instances
            max_iterations: Maximum number of iterations for the search algorithm
            heuristic_fn: Optional default heuristic function
        """
        self.providers = providers or []
        if actions_list:
            static_actions = Actions()
            static_actions.add_actions(actions_list)
            self.providers.append(StaticActionProvider(static_actions))

        self.max_iterations = max_iterations
        self.stats = PlanStats()
        self.heuristic_fn = heuristic_fn

        # Hook system for middleware
        self.hooks: Dict[str, List[Callable[..., Any]]] = {
            "on_node_expanded": [],
            "on_plan_found": [],
            "on_search_failed": [],
        }

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a specific planner event.

        Args:
            event: One of 'on_node_expanded', 'on_plan_found', 'on_search_failed'
            callback: The function to call when the event occurs
        """
        if event in self.hooks:
            self.hooks[event].append(callback)
        else:
            raise ValueError(f"Unknown event hook: {event}")

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
        print("\n" + "=" * 50)
        print("PLANNING STATISTICS")
        print("=" * 50)
        print(f"- Nodes expanded: {stats.nodes_expanded}")
        print(f"- Nodes visited: {stats.nodes_visited}")
        print(f"- Plan length: {stats.plan_length}")
        print(f"- Total cost: {stats.total_cost:.2f}")
        print(f"- Execution time: {stats.execution_time:.4f} seconds")
        print("=" * 50 + "\n")

    def generate_plan(
        self,
        world_state: Union[Dict[str, Any], WorldState],
        goal: Union[Dict[str, Any], Goal],
        max_depth: Optional[int] = None,
        heuristic_fn: Optional[HeuristicFn] = None,
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

            plan = self._find_plan(world_state, goal, max_depth, h_fn)
            return self._finalize_plan_generation(plan, start_time)

        except Exception as e:
            logger.exception("Error during planning")
            return PlanResult(plan=None, message=f"❌ Error during planning: {str(e)}")

    async def async_generate_plan(
        self,
        world_state: Union[Dict[str, Any], WorldState],
        goal: Union[Dict[str, Any], Goal],
        max_depth: Optional[int] = None,
        heuristic_fn: Optional[HeuristicFn] = None,
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

            plan = await self._async_find_plan(world_state, goal, max_depth, h_fn)
            return self._finalize_plan_generation(plan, start_time)

        except Exception as e:
            logger.exception("Error during async planning")
            return PlanResult(plan=None, message=f"❌ Error during planning: {str(e)}")

    def _print_header(self, goal: Goal) -> None:
        """Print planning header information."""
        print("\n" + "=" * 50)
        print("GOAL-ORIENTED ACTION PLANNING")
        print("=" * 50)
        name = getattr(goal, "name", str(goal))
        target = getattr(goal, "target_state", goal)
        print(f"\nGOAL: {name}")
        print(f"TARGET STATE: {target}\n")

    def _validate_and_convert(
        self, world_state: Any, goal: Any, max_depth: Optional[int]
    ) -> Tuple[WorldState, Goal]:
        """Validate inputs and convert to proper types."""
        if not isinstance(world_state, (dict, WorldState)):
            raise TypeError(
                f"world_state must be a dict or WorldState, got {type(world_state)}"
            )
        if not isinstance(goal, (dict, Goal)):
            raise TypeError(f"goal must be a dict or Goal, got {type(goal)}")
        if max_depth is not None and max_depth <= 0:
            raise ValueError(f"max_depth must be positive, got {max_depth}")

        if isinstance(world_state, dict):
            world_state = WorldState(**world_state)
        if isinstance(goal, dict):
            goal = Goal(target_state=goal)

        return world_state, goal

    def _finalize_plan_generation(
        self, plan: Optional[Plan], start_time: float
    ) -> PlanResult:
        """Finalize stats and print result message."""
        import time

        self.stats.plan_length = len(plan) if plan else 0
        self.stats.execution_time = time.time() - start_time

        # Note: total_cost calculation needs accurate action lookup
        # This might be tricky with dynamic providers, but we'll try
        # The 'total_cost' variable was unused, so it's removed.
        if plan:
            # For stats, we'll just sum the costs if available
            # (Requires actions to be findable by name, which might not always work)
            pass

        safe_print("\n" + "=" * 50)
        safe_print("PLAN GENERATION COMPLETE")
        safe_print("=" * 50)

        if plan:
            message = f"[SUCCESS] Found plan with {len(plan)} actions"
            safe_print(f"\n{message}")
            safe_print("\nPLAN STEPS:")
            for i, action_name in enumerate(plan, 1):
                safe_print(f"  {i}. {action_name}")
            self._display_statistics()
            self._trigger_hook("on_plan_found", plan=plan, stats=self.stats)
            return PlanResult(plan=plan, message=message)

        message = "❌ No valid plan found to achieve the goal."
        print(f"\n{message}")
        self._display_statistics()
        self._trigger_hook("on_search_failed", stats=self.stats)
        return PlanResult(plan=None, message=message)

    def _get_all_available_actions(self, state: WorldState) -> List[Action]:
        """Query all providers for available actions."""
        all_actions = []
        for provider in self.providers:
            try:
                all_actions.extend(provider.provide_actions(state))
            except Exception as e:
                logger.error("Error providing actions from %s: %s", provider, e)
        return all_actions

    def _find_plan(
        self,
        world_state: WorldState,
        goal: Goal,
        max_depth: Optional[int],
        heuristic_fn: Optional[HeuristicFn],
    ) -> Optional[Plan]:
        """Internal method to find a plan using A* search."""
        logger.info("Planning to achieve goal: %s", goal)

        start_node = Node(world_state, None, goal, heuristic_fn=heuristic_fn)
        frontier = []
        heapq.heappush(frontier, (start_node.f_score, id(start_node), start_node))

        g_scores: Dict[StateKey, float] = {hash(world_state): 0}
        iteration = 0

        while frontier and (max_depth is None or iteration < self.max_iterations):
            iteration += 1
            self.stats.nodes_visited += 1
            _, _, current_node = heapq.heappop(frontier)

            if goal.is_satisfied(current_node.state):
                return self._reconstruct_plan(current_node)

            current_state_key = hash(current_node.state)
            if current_node.g_score > g_scores.get(current_state_key, float("inf")):
                continue

            # Phase 2: Use ActionProviders
            self._trigger_hook("on_node_expanded", node=current_node)
            for action in self._get_all_available_actions(current_node.state):
                if not action.is_applicable(current_node.state):
                    continue

                self.stats.nodes_expanded += 1
                new_state = action.apply(current_node.state)
                new_state_key = hash(new_state)
                tentative_g_score = current_node.g_score + action.cost

                if tentative_g_score >= g_scores.get(new_state_key, float("inf")):
                    continue

                g_scores[new_state_key] = tentative_g_score
                new_node = Node(
                    new_state, current_node, goal, action, heuristic_fn=heuristic_fn
                )
                new_node.g_score = tentative_g_score
                heapq.heappush(frontier, (new_node.f_score, id(new_node), new_node))

        return None

    async def _async_find_plan(
        self,
        world_state: WorldState,
        goal: Goal,
        max_depth: Optional[int],
        heuristic_fn: Optional[HeuristicFn],
    ) -> Optional[Plan]:
        """Asynchronously find a plan using A* search."""
        logger.info("Async planning to achieve goal: %s", goal)

        start_node = Node(world_state, None, goal, heuristic_fn=heuristic_fn)
        frontier = []
        heapq.heappush(frontier, (start_node.f_score, id(start_node), start_node))

        g_scores: Dict[StateKey, float] = {hash(world_state): 0}
        iteration = 0

        while frontier and (max_depth is None or iteration < self.max_iterations):
            iteration += 1
            self.stats.nodes_visited += 1
            _, _, current_node = heapq.heappop(frontier)

            if goal.is_satisfied(current_node.state):
                return self._reconstruct_plan(current_node)

            current_state_key = hash(current_node.state)
            if current_node.g_score > g_scores.get(current_state_key, float("inf")):
                continue

            self._trigger_hook("on_node_expanded", node=current_node)
            for action in self._get_all_available_actions(current_node.state):
                if not action.is_applicable(current_node.state):
                    continue

                self.stats.nodes_expanded += 1
                new_state = await action.async_apply(current_node.state)
                new_state_key = hash(new_state)
                tentative_g_score = current_node.g_score + action.cost

                if tentative_g_score >= g_scores.get(new_state_key, float("inf")):
                    continue

                g_scores[new_state_key] = tentative_g_score
                new_node = Node(
                    new_state, current_node, goal, action, heuristic_fn=heuristic_fn
                )
                new_node.g_score = tentative_g_score
                heapq.heappush(frontier, (new_node.f_score, id(new_node), new_node))

        return None

    def _reconstruct_plan(self, node: Node) -> Plan:
        """Reconstruct the plan from the goal node back to the start."""
        plan = []
        current = node

        while current.parent is not None and current.action is not None:
            plan.insert(0, current.action.name)
            current = current.parent

        return plan
