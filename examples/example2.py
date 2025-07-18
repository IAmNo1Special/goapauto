#!/usr/bin/env python3
"""
Morning Routine Planner

This script demonstrates using the GOAP (Goal-Oriented Action Planning) system
for planning an optimal morning routine based on different constraints and goals.
"""

import logging
from typing import Dict, Any, List, Tuple
from datetime import datetime, time

from models.goap_planner import Planner
from models.goal import Goal

# Configure logging - only show WARNING and above
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_initial_state() -> Dict[str, Any]:
    """Create and return the initial morning state."""
    current_time = datetime.now().time()
    return {
        'time': current_time,
        'in_bed': True,
        'awake': False,
        'dressed': False,
        'showered': False,
        'teeth_brushed': False,
        'breakfast_eaten': False,
        'lunch_packed': False,
        'coffee_made': False,
        'email_checked': False,
        'workout_done': False,
        'lunch_prepared': False,
        'work_bag_packed': False,
        'left_house': False,
        'late_for_work': False,
        'energy': 50,  # 0-100 scale
        'stress': 30   # 0-100 scale
    }

def get_actions_list() -> List[Tuple[str, Dict[str, Any], Dict[str, Any], float]]:
    """Define the list of available morning routine actions."""
    return [
        # Basic wake up actions
        (
            'wake_up',
            {'in_bed': True, 'awake': False},
            {'in_bed': False, 'awake': True, 'energy': -10},
            1.0
        ),
        
        # Hygiene actions
        (
            'shower',
            {'awake': True, 'showered': False},
            {'showered': True, 'energy': 10, 'stress': -15},
            15.0  # Takes 15 minutes
        ),
        
        (
            'brush_teeth',
            {'awake': True, 'teeth_brushed': False},
            {'teeth_brushed': True},
            2.0   # Takes 2 minutes
        ),
        
        # Dressing
        (
            'get_dressed',
            {'awake': True, 'dressed': False},
            {'dressed': True},
            5.0   # Takes 5 minutes
        ),
        
        # Food and drink
        (
            'make_coffee',
            {'awake': True, 'coffee_made': False},
            {'coffee_made': True, 'energy': 20},
            5.0
        ),
        
        (
            'eat_breakfast',
            {'awake': True, 'breakfast_eaten': False},
            {'breakfast_eaten': True, 'energy': 15, 'stress': -10},
            15.0
        ),
        
        (
            'pack_lunch',
            {'awake': True, 'lunch_packed': False},
            {'lunch_packed': True, 'stress': -5},
            10.0
        ),
        
        # Work preparation
        (
            'check_email',
            {'awake': True, 'email_checked': False},
            {'email_checked': True, 'stress': lambda state: 10 if state['time'] > time(9, 0) else -5},
            10.0
        ),
        
        (
            'pack_work_bag',
            {'awake': True, 'work_bag_packed': False},
            {'work_bag_packed': True},
            5.0
        ),
        
        # Exercise
        (
            'workout',
            {'awake': True, 'workout_done': False, 'energy': (lambda x: x > 30)},
            {'workout_done': True, 'energy': -20, 'stress': -20},
            30.0
        ),
        
        # Final steps
        (
            'leave_house',
            {
                'dressed': True,
                'teeth_brushed': True,
                'work_bag_packed': True,
                'left_house': False,
                'time': (lambda t: t < time(8, 30))  # Must leave before 8:30
            },
            {'left_house': True, 'stress': -10},
            2.0
        )
    ]

def main() -> int:
    """Main function to demonstrate morning routine planning."""
    try:
        # Initialize state and actions
        initial_state = get_initial_state()
        actions_list = get_actions_list()
        
        # Define the goal - be awake
        goal = Goal(
            name="Awoke and showered",
            priority=1,
            target_state={
                'awake': True,
                'showered': True,
            }
        )
        
        # Create and configure the planner
        planner = Planner(actions_list, max_iterations=1000)
        
        # Generate the plan
        plan_result = planner.generate_plan(initial_state, goal)
        
    except Exception as e:
        logger.exception("An error occurred during planning")
        return 1

if __name__ == "__main__":
    main()
