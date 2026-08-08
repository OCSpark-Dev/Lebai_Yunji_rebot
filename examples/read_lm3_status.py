"""Read-only LM3 controller connectivity check.

This script intentionally does not call start_sys(), motion, IO, gripper, or
power APIs. Use the robot's actual address discovered on site.
"""

from __future__ import annotations

import argparse
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read LM3 system, robot-state, and emergency-stop information."
    )
    parser.add_argument("--ip", required=True, help="Actual LM3 controller IP address")
    parser.add_argument(
        "--simulator",
        action="store_true",
        help="Connect using the SDK simulator mode instead of a real controller",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from pylebai import Robot
    except ModuleNotFoundError:
        print(
            "pylebai is not installed. Run: python -m pip install pylebai",
            file=sys.stderr,
        )
        return 2

    try:
        robot = Robot(args.ip, args.simulator)
        print(f"system_info={robot.get_system_info()}")
        print(f"robot_state={robot.get_robot_state()}")
        print(f"estop_reason={robot.get_estop_reason()}")
    except Exception as exc:  # The SDK exposes several native exception types.
        print(f"LM3 read-only check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
