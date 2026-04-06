#!/usr/bin/env python3
"""Rename 'Retro Movies - XXXXs' collections to just 'XXXXs'."""

import json
import re
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def emby_get(config, endpoint, params=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def emby_post(config, endpoint, json_body=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.post(url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp


def main():
    config = load_config()
    prefix = config["collection_prefix"]

    print(f"Connecting to Emby at {config['emby_url']}...")

    collections = emby_get(config, "/Items", params={
        "IncludeItemTypes": "BoxSet",
        "Recursive": "true",
    })

    pattern = re.compile(rf"^{re.escape(prefix)}\s*-\s*(\d{{4}}s)$")

    users = emby_get(config, "/Users")
    user_id = users[0]["Id"]

    renamed = 0
    for item in collections.get("Items", []):
        match = pattern.match(item["Name"])
        if not match:
            continue

        old_name = item["Name"]
        new_name = match.group(1)

        print(f"  Renaming: {old_name} -> {new_name}")
        full_item = emby_get(config, f"/Users/{user_id}/Items/{item['Id']}")
        full_item["Name"] = new_name
        emby_post(config, f"/Items/{item['Id']}", json_body=full_item)
        renamed += 1

    print(f"\nRenamed {renamed} collections.")


if __name__ == "__main__":
    main()
