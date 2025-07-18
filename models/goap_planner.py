from __future__ import annotations
import sys
import os
from typing import Any, Dict, List, Optional, Tuple, Set, Union, Type, TypeVar, Callable
import logging
import heapq
from dataclasses import dataclass, field

# Set up console for Windows to support Unicode
if os.name == 'nt':
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')

def safe_print(*args, **kwargs):
    """Safely print text that might contain Unicode characters."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # Fallback for environments that can't handle certain Unicode chars
        cleaned = [str(arg).encode('ascii', 'replace').decode('ascii') for arg in args]
        print(*cleaned, **{k: v for k, v in kwargs.items() if k != 'end'})
from collections import defaultdict, deque

from models.node import Node
from models.goal import Goal
from models.worldstate import WorldState
from models.actions import Actions, Action

logger = logging.getLogger(__name__)
T = TypeVar('T', bound='Planner')

# Type aliases for better readability
Plan = List[str]
PlanResult = Tuple[Optional[Plan], str]
StateKey = int  # Hash of a WorldState

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
    achieve a goal state from an initial state, using A* search with a heuristic
    based on the number of unsatisfied goal conditions.
    
    Attributes:
        actions: Collection of available actions
        max_iterations: Maximum number of iterations before giving up
        stats: Statistics about the last planning operation
    """
    
    def __init__(
        self,
        actions_list: Optional[List[Tuple[str, Dict[str, Any], Dict[str, Any], float]]] = None,
        max_iterations: int = 1000
    ) -> None:
        """Initialize the planner with optional actions and configuration.
        
        Args:
            actions_list: Optional list of action tuples (name, preconditions, effects, cost)
            max_iterations: Maximum number of iterations for the search algorithm
        """
        self.actions = Actions()
        if actions_list:
            self.actions.add_actions(actions_list)
            
        self.max_iterations = max_iterations
        self.stats = PlanStats()
    

    
    def _display_statistics(self) -> None:
        """Display planning statistics in a consistent format."""
        if not hasattr(self, 'stats') or not self.stats:
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
        max_depth: Optional[int] = None
    ) -> PlanResult:
        """Generate a plan to achieve the given goal from the current world state.
        
        Args:
            world_state: The current state of the world (dict or WorldState)
            goal: The goal to achieve (dict or Goal object)
            max_depth: Optional maximum depth for the search
            
        Returns:
            A tuple of (plan, message) where:
            - plan: List of action names, or None if no plan found
            - message: Status message describing the result
            
        Raises:
            TypeError: If world_state or goal are of invalid types
            ValueError: If max_depth is not positive
        """
        import time

        # Display goal information
        print("\n" + "=" * 50)
        print("GOAL-ORIENTED ACTION PLANNING")
        print("=" * 50)
        print(f"\nGOAL: {goal.name}")
        print(f"TARGET STATE: {goal.target_state}\n")
        
        start_time = time.time()
        
        # Reset stats
        self.stats = PlanStats()
        
        # Input validation
        if not isinstance(world_state, (dict, WorldState)):
            raise TypeError(f"world_state must be a dict or WorldState, got {type(world_state)}")
            
        if not isinstance(goal, (dict, Goal)):
            raise TypeError(f"goal must be a dict or Goal, got {type(goal)}")
            
        if max_depth is not None and max_depth <= 0:
            raise ValueError(f"max_depth must be positive, got {max_depth}")
        
        # Convert inputs to proper types if needed
        if isinstance(world_state, dict):
            world_state = WorldState(world_state)
            
        if isinstance(goal, dict):
            goal = Goal(goal)
        
        # Check if goal is already satisfied
        if goal.is_satisfied(world_state):
            self.stats.execution_time = time.time() - start_time
            return [], "✅ Goal is already satisfied!"
        
        try:
            # Get the plan using A* search
            plan = self._find_plan(world_state, goal, max_depth)
            
            # Update stats
            self.stats.plan_length = len(plan) if plan else 0
            self.stats.total_cost = sum(
                self.actions.get_action(name).cost 
                for name in plan or []
            )
            self.stats.execution_time = time.time() - start_time
            

            safe_print("\n" + "=" * 50)
            safe_print("PLAN GENERATION COMPLETE")
            safe_print("=" * 50)
            
            if plan:
                message = f"[SUCCESS] Found plan with {len(plan)} actions (cost: {self.stats.total_cost:.1f})"
                safe_print(f"\n{message}")
                safe_print("\nPLAN STEPS:")
                for i, action_name in enumerate(plan, 1):
                    safe_print(f"  {i}. {action_name}")
                
                self._display_statistics()
                return plan, message
            
            message = "❌ No valid plan found to achieve the goal."
            print(f"\n{message}")
            self._display_statistics()
            return None, message
            
        except Exception as e:
            logger.exception("Error during planning")
            return None, f"❌ Error during planning: {str(e)}"
    
    def _find_plan(
        self,
        world_state: WorldState,
        goal: Goal,
        max_depth: Optional[int]
    ) -> Optional[Plan]:
        """Internal method to find a plan using A* search.
        
        Args:
            world_state: The current state of the world
            goal: The goal to achieve
            max_depth: Maximum search depth (None for no limit)
            
        Returns:
            List of action names representing the plan, or None if no plan found
        """
        logger.info('Planning to achieve goal: %s', goal)
        logger.debug('Current world state: %s', world_state)
        
        # Check if goal is already satisfied
        if goal.is_satisfied(world_state):
            logger.info('Goal already satisfied')
            return []
        
        # Initialize data structures
        start_node = Node(world_state, None, goal)
        frontier = []  # Priority queue
        heapq.heappush(frontier, (start_node.f_score, id(start_node), start_node))
        
        # Track visited states and their best known costs
        g_scores: Dict[StateKey, float] = {hash(world_state): 0}
        came_from: Dict[StateKey, Node] = {}
        
        iteration = 0
        
        while frontier and (max_depth is None or iteration < self.max_iterations):
            iteration += 1
            self.stats.nodes_visited += 1
            
            # Get the node with the lowest f_score
            _, _, current_node = heapq.heappop(frontier)
            
            # Check if we've reached the goal
            if goal.is_satisfied(current_node.state):
                return self._reconstruct_plan(current_node)
            
            # Skip if we've already found a better path to this state
            current_state_key = hash(current_node.state)
            if current_node.g_score > g_scores.get(current_state_key, float('inf')):
                continue
            
            # Expand the node by trying all applicable actions
            for action in self.actions.get_actions():
                if not action.is_applicable(current_node.state):
                    continue
                
                self.stats.nodes_expanded += 1
                
                # Apply the action to get the new state
                new_state = action.apply(current_node.state.copy())
                new_state_key = hash(new_state)
                
                # Calculate tentative g_score
                tentative_g_score = current_node.g_score + action.cost
                
                # Check if we've found a better path to this state
                if tentative_g_score >= g_scores.get(new_state_key, float('inf')):
                    continue
                
                # This is the best path to this state so far
                came_from[new_state_key] = current_node
                g_scores[new_state_key] = tentative_g_score
                
                # Create a new node
                new_node = Node(new_state, current_node, goal, action)
                new_node.g_score = tentative_g_score
                
                # Add to frontier
                heapq.heappush(
                    frontier,
                    (new_node.f_score, id(new_node), new_node)
                )
        
        logger.warning(
            'No plan found after %d iterations (max: %d)',
            iteration,
            self.max_iterations
        )
        
        return None
    
    def _reconstruct_plan(self, node: Node) -> Plan:
        """Reconstruct the plan from the goal node back to the start.
        
        Args:
            node: The goal node
            
        Returns:
            List of action names in execution order
        """
        plan = []
        current = node
        
        while current.parent is not None and current.action is not None:
            plan.insert(0, current.action.name)
            current = current.parent
        
        return plan
