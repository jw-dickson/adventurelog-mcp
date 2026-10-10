"""Integration tests write to (and delete from) the AdventureLog account behind ADVENTURELOG_API_KEY.

They must only run against a disposable instance/account, so they also require
ADVENTURELOG_IT_DISPOSABLE=1 as an explicit acknowledgement.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ADVENTURELOG_IT_DISPOSABLE") == "1":
        return
    skip = pytest.mark.skip(reason="set ADVENTURELOG_IT_DISPOSABLE=1 (disposable AdventureLog only)")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)
