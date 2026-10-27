import json
import shutil
import time
from pathlib import Path

import pytest

from harness.config import Settings
from harness.store import Store
from harness.workspace import Workspace, inventory


@pytest.fixture
def spec():
    return {"instance_id":"fixture__repair-1","repo":"fixture/repair","base_commit":"a"*40,
            "problem_statement":"Fix the addition function", "tests":["tests"],"data_revision":"fixture",
            "max_calls":4,"max_tokens":100000,"max_seconds":120,"max_tool_seconds":30}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "var", ("supersecretapikey123456789",))


@pytest.fixture
def ready(store, spec):
    tid = store.create(spec)
    ws = Workspace(store, tid)
    ws.base.mkdir(parents=True)
    (ws.base/"calc.py").write_text("def add(a, b):\n    return a - b\n")
    (ws.base/"tests").mkdir()
    (ws.base/"tests/test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    shutil.copytree(ws.base,ws.repo)
    (ws.home/"baseline.json").write_text(json.dumps(inventory(ws.base)))
    return tid,ws


@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient
    from harness.api import make_app
    settings = Settings(root=store.root,token="x"*40)
    with TestClient(make_app(settings)) as c:
        c.headers["Authorization"] = "Bearer " + settings.token
        yield c
