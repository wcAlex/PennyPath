import json
import os

DEFAULT_PATH = "data/user_prefs.json"


def load_prefs(path: str = DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        return {"name": "", "goals": []}
    with open(path) as f:
        return json.load(f)


def save_prefs(prefs: dict, path: str = DEFAULT_PATH) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(prefs, f, indent=2)
