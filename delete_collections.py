#!/usr/bin/env python3
"""Delete specific decade collections from Emby."""

import json
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"

COLLECTIONS_TO_DELETE = [
    "Retro Movies - 2000s",
    "Retro Movies - 2010s",
    "Retro Movies - 2020s",
]


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def emby_get(config, endpoint, params=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def emby_delete(config, endpoint):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.delete(url, headers=headers)
    resp.raise_for_status()
    return resp


def main():
    config = load_config()

    print(f"Connecting to Emby at {config['emby_url']}...")

    collections = emby_get(config, "/Items", params={
        "IncludeItemTypes": "BoxSet",
        "Recursive": "true",
    })

    deleted = 0
    for item in collections.get("Items", []):
        if item["Name"] in COLLECTIONS_TO_DELETE:
            print(f"  Deleting: {item['Name']} (id: {item['Id']})")
            emby_delete(config, f"/Items/{item['Id']}")
            deleted += 1

    print(f"\nDeleted {deleted} collections.")


if __name__ == "__main__":
    main()
