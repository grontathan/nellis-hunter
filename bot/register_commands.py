"""Register (or update) the Discord `/hunt` slash command.

Run once after creating the Discord application, and again whenever you change
the command's options. Global commands can take up to an hour to propagate the
first time; re-registers are near-instant.

    DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... python bot/register_commands.py

App id + bot token come from the Discord dev portal:
  General Information → Application ID
  Bot → Reset Token (the bot token, not the public key)
"""

from __future__ import annotations

import os
import sys

import httpx

# Category dropdown. Discord caps choices at 25; these are the Nellis Taxonomy
# values worth flipping (mix of Level 1 buckets and high-resale Level 2 leaves).
# The Algolia facet filter matches either level, so both kinds work as-is.
CATEGORY_CHOICES = [
    "Electronics",
    "Home Improvement",
    "Tools",
    "Hardware",
    "Lighting",
    "Electrical",
    "Plumbing",
    "Home & Household Essentials",
    "Automotive",
    "Patio & Garden",
    "Outdoors & Sports",
    "Furniture & Appliances",
    "Office & School Supplies",
    "Toys & Games",
    "Computers, Laptops, Tablets & Accessories",
    "Monitors & Monitor Stands",
    "Networking & Drives",
    "Headphones",
    "Speakers",
    "Cameras & Photography Equipment",
]

COMMAND = {
    "name": "hunt",
    "description": "Scan Nellis for flip-worthy lots in a category and post a ranked digest.",
    "options": [
        {
            "type": 3,  # STRING
            "name": "category",
            "description": "Which Nellis category to hunt",
            "required": True,
            "choices": [{"name": c, "value": c} for c in CATEGORY_CHOICES[:25]],
        },
        {
            "type": 3,  # STRING
            "name": "location",
            "description": "Warehouse to search (default: both)",
            "required": False,
            "choices": [
                {"name": "Phoenix", "value": "Phoenix"},
                {"name": "Mesa", "value": "Mesa"},
                {"name": "Both", "value": "Phoenix|Mesa"},
            ],
        },
        {
            "type": 3,  # STRING
            "name": "keywords",
            "description": "Optional free-text filter, e.g. 'dewalt drill'",
            "required": False,
        },
        {
            "type": 4,  # INTEGER
            "name": "closing_within_hours",
            "description": "Only lots closing within N hours (default 48)",
            "required": False,
        },
    ],
}


def main() -> int:
    app_id = os.getenv("DISCORD_APP_ID")
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not (app_id and bot_token):
        print("Set DISCORD_APP_ID and DISCORD_BOT_TOKEN in the environment first.")
        return 1

    url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bot {bot_token}"},
        json=COMMAND,
        timeout=30.0,
    )
    if resp.status_code in (200, 201):
        print(f"Registered /hunt (status {resp.status_code}). "
              "Global commands can take up to ~1h to appear the first time.")
        return 0
    print(f"Discord rejected the registration ({resp.status_code}): {resp.text}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
