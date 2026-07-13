#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook.
Reads hook context from stdin, checks if the current project directory
has a Kimai mapping. If not, prints an injection message so Claude
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

    dir_name = os.path.basename(project_dir.rstrip("/")).lower()
    mappings = load_mappings()

    if dir_name not in mappings:
        here = os.path.dirname(os.path.abspath(__file__))
        print(
            f"[KIMAI] The current project '{dir_name}' ({project_dir}) has no Kimai "
            f"project mapping yet. Before responding to the user's message, ask them which "
            f"Kimai project + activity to associate with '{dir_name}' — fetch the lists with: "
            f"`python3 {here}/tracker.py --list-projects` and "
            f"`python3 {here}/tracker.py --list-activities`, and present the options. "
            f"Once they choose, save it with: "
            f"`python3 {here}/tracker.py --map {dir_name} <PROJECT_ID> <ACTIVITY_ID>`"
        )


if __name__ == "__main__":
    main()
