#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook.
Reads hook context from stdin, checks if the current project directory
has a Clockify mapping. If not, prints an injection message so Claude
knows to ask the user before proceeding.
"""

import json
import os
import sys

MAPPINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_mappings.json")


def load_mappings():
    if os.path.exists(MAPPINGS_PATH):
        with open(MAPPINGS_PATH) as f:
            return json.load(f)
    return {}


def main():
    try:
        context = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    project_dir = context.get("session", {}).get("projectDir", "")
    if not project_dir:
        sys.exit(0)

    dir_name = os.path.basename(project_dir.rstrip("/"))
    mappings = load_mappings()

    if dir_name not in mappings:
        print(
            f"[CLOCKIFY] The current project '{dir_name}' ({project_dir}) has no Clockify "
            f"project mapping yet. Before responding to the user's message, ask them which "
            f"Clockify project to associate with '{dir_name}' — fetch the list with: "
            f"`python3 {os.path.dirname(os.path.abspath(__file__))}/tracker.py --list-projects` "
            f"and present the options. Once they choose, save it with: "
            f"`python3 {os.path.dirname(os.path.abspath(__file__))}/tracker.py --map {dir_name} <PROJECT_ID>`"
        )


if __name__ == "__main__":
    main()
