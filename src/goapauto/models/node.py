from __future__ import annotations
from typing import Any, Dict, Optional, Type, TypeVar, Union, Tuple, List
import logging
from dataclasses import dataclass, field

from models.worldstate import WorldState
from models.goal import Goal
from models.actions import Action

logger = logging.getLogger(__name__)
T = TypeVar('T', bound='Node')

class Node:
    """Represents a node in the A* search tree for planning.
    
    Each node contains a world state, a reference to its parent node,
    the goal being pursued, and the action that led to this node.
    It also maintains g, h, and f scores for A* pathfinding.
    
    Attributes:
        state: The world state at this node
        parent: The parent node that led to this node (None for root)
        goal: The goal being pursued (either a Goal object or dict)
        action: The action that led to this node (None for root)
        g_score: The cost from start to this node
        h_score: Heuristic estimate from this node to goal
        f_score: Total score (g + h) used for A* priority
    """
    
    def __init__(
        self,
        state: WorldState,
        parent: Optional[Node],
        goal: Union[Goal, Dict[str, Any]],
        action: Optional[Action] = None
    ) -> None:
        """Initialize a new Node in the search tree.
        
        Args:
            state: The world state at this node
            parent: The parent node (None for root)
            goal: The goal being pursued (Goal object or dict)
            action: The action that led to this node (None for root)
            
        Raises:
            TypeError: If state is not a WorldState or goal is invalid
        """
        if not isinstance(state, WorldState):
            raise TypeError(f"state must be a WorldState, got {type(state)}")
            
        self.state = state
        self.parent = parent
        self.goal = goal
        self.action = action
        
        # Calculate g-score (cost from start to current node)
        self.g_score = self._calculate_g_score(parent, action)
        
        # Calculate h-score (heuristic estimate to goal)
        self.h_score = self.heuristic(state, goal)
        
        # Calculate f-score (total score for A*)
        self.f_score = self.g_score + self.h_score
    
    def _calculate_g_score(self, parent: Optional[Node], action: Optional[Action]) -> float:
        """Calculate the g-score for this node.
        
        Args:
            parent: The parent node (None for root)
            action: The action taken to reach this node
            
        Returns:
            float: The calculated g-score
        """
        if parent is None:
            return 0.0
            
        if action is None:
            return parent.g_score
            
        return parent.g_score + (action.cost if hasattr(action, 'cost') else 1.0)
    
    @classmethod
    def heuristic(
        cls,
        state: WorldState,
        goal: Union[Goal, Dict[str, Any]]
    ) -> float:
        """Calculate the heuristic value for a state relative to a goal.
        
        The heuristic estimates the cost from the current state to the goal.
        This implementation counts the number of unsatisfied goal conditions.
        
        Args:
            state: The current world state
            goal: Either a Goal object or a dictionary of goal conditions
            
        Returns:
            float: The heuristic value (number of unmet goal conditions)
            
        Raises:
            TypeError: If goal is not a Goal or dict
        """
        if not isinstance(state, WorldState):
            raise TypeError(f"state must be a WorldState, got {type(state)}")
            
        if hasattr(goal, 'get_unsatisfied_conditions'):  # It's a Goal object
            unsatisfied = goal.get_unsatisfied_conditions(state)
            return float(len(unsatisfied))
            
        if isinstance(goal, dict):  # It's a dictionary of goal conditions
            return float(sum(
                1 for key, value in goal.items()
                if getattr(state, key, None) != value
            ))
            
        raise TypeError(f"goal must be a Goal or dict, got {type(goal)}")
    
    def get_path(self) -> List[Action]:
        """Reconstruct the path from the start node to this node.
        
        Returns:
            List[Action]: The sequence of actions from start to this node
        """
        path = []
        current = self
        
        while current is not None and current.action is not None:
            path.append(current.action)
            current = current.parent
            
        return list(reversed(path))
    
    def get_path_with_states(self) -> List[Tuple[Optional[Action], WorldState]]:
        """Reconstruct the path with both actions and states.
        
        Returns:
            List of tuples containing (action, resulting_state) pairs
        """
        path = []
        current = self
        
        while current is not None:
            path.append((current.action, current.state))
            current = current.parent
            
        path.reverse()
        return path[1:]  # Skip the initial None action
    
    def depth(self) -> int:
        """Get the depth of this node in the search tree.
        
        Returns:
            int: The depth of the node (0 for root)
        """
        depth = 0
        current = self
        
        while current.parent is not None:
            depth += 1
            current = current.parent
            
        return depth
    
    def __lt__(self, other: Any) -> bool:
        """Compare nodes by f-score for priority queue ordering."""
        if not isinstance(other, Node):
            return NotImplemented
        return self.f_score < other.f_score
    
    def __eq__(self, other: Any) -> bool:
        """Check if two nodes have the same state and goal."""
        if not isinstance(other, Node):
            return NotImplemented
            
        return (
            self.state == other.state
            and self.goal == other.goal
        )
    
    def __hash__(self) -> int:
        """Compute a hash value for this node."""
        return hash((
            hash(self.state),
            hash(frozenset(self.goal.target_state.items())) if hasattr(self.goal, 'target_state')
                else hash(frozenset(self.goal.items())),
            hash(self.action) if self.action is not None else 0
        ))
    
    def __str__(self) -> str:
        """Return a string representation of the node."""
        action_name = self.action.name if self.action is not None else 'None'
        return (
            f"{self.__class__.__name__}("
            f"action={action_name}, "
            f"g={self.g_score:.2f}, "
            f"h={self.h_score:.2f}, "
            f"f={self.f_score:.2f}"
            ")"
        )
    
    def __repr__(self) -> str:
        """Return a detailed string representation of the node."""
        action_repr = repr(self.action) if self.action is not None else 'None'
        return (
            f"<{self.__class__.__name__} "
            f"action={action_repr}, "
            f"g={self.g_score:.2f}, "
            f"h={self.h_score:.2f}, "
            f"depth={self.depth()}, "
            f"state={repr(self.state)}"
            ">"
        )
