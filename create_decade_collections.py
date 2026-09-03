#!/usr/bin/env python3
"""Create decade-based movie collections on an Emby server."""

import json
import sys
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


def emby_post(config, endpoint, params=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.post(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp


def get_movies_for_decade(config, decade_start):
    """Fetch all movie IDs for a given decade (e.g. 1930-1939)."""
    years = ",".join(str(y) for y in range(decade_start, decade_start + 10))
    items = emby_get(config, "/Items", params={
        "IncludeItemTypes": "Movie",
        "Years": years,
        "Recursive": "true",
        "Fields": "ProductionYear",
        "Limit": "10000",
    })
    return items.get("Items", [])


def find_existing_collection(config, name):
    """Check if a collection with this name already exists."""
    results = emby_get(config, "/Items", params={
        "IncludeItemTypes": "BoxSet",
        "Recursive": "true",
        "SearchTerm": name,
    })
    for item in results.get("Items", []):
        if item["Name"] == name:
            return item["Id"]
    return None


def create_collection(config, name, item_ids):
    """Create a new collection with the given movies."""
    resp = emby_post(config, "/Collections", params={
        "Name": name,
        "Ids": ",".join(item_ids),
    })
    data = resp.json()
    return data.get("Id")


def add_to_collection(config, collection_id, item_ids):
    """Add items to an existing collection."""
    emby_post(config, f"/Collections/{collection_id}/Items", params={
        "Ids": ",".join(item_ids),
    })


def decade_label(decade_start):
    """Return the short label for a decade, e.g. 1930 -> '30s'."""
    return f"{decade_start}s"


def main():
    config = load_config()
    prefix = config["collection_prefix"]

    print(f"Connecting to Emby at {config['emby_url']}...")

    # Verify connectivity
    try:
        emby_get(config, "/System/Info")
    except requests.RequestException as e:
        print(f"Error connecting to Emby: {e}")
        sys.exit(1)

    print("Connected.\n")

    for decade_start in range(1900, 2000, 10):
        label = decade_label(decade_start)
        collection_name = label

        movies = get_movies_for_decade(config, decade_start)
        if not movies:
            print(f"{collection_name}: no movies found, skipping.")
            continue

        movie_ids = [m["Id"] for m in movies]
        print(f"{collection_name}: found {len(movies)} movies.")

        existing_id = find_existing_collection(config, collection_name)
        if existing_id:
            print(f"  Collection already exists (id: {existing_id}), adding movies...")
            add_to_collection(config, existing_id, movie_ids)
        else:
            print(f"  Creating collection...")
            create_collection(config, collection_name, movie_ids)

        print(f"  Done.")

    print("\nAll collections processed.")


if __name__ == "__main__":
    main()
