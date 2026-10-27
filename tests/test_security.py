import io
import json
import zipfile

import pytest

from harness.config import Settings,redact
from harness.sandbox import DockerSandbox
from harness.workspace import extract_archive,safe_path,sha


@pytest.mark.parametrize("name",["../secret","/etc/passwd","C:/Windows/file","a\\b",".git/config","a/../../x","a\x00b"])
def test_path_escape_rejected(tmp_path,name):
    with pytest.raises(PermissionError):
        safe_path(tmp_path,name)


def test_symlink_escape_rejected(tmp_path):
    root=tmp_path/"repo"
    root.mkdir()
    try:
        (root/"link").symlink_to(tmp_path,target_is_directory=True)
    except OSError:
        pytest.skip("Windows host lacks symlink creation privilege; Linux CI covers this test")
    with pytest.raises(PermissionError):
        safe_path(root,"link/secret")


def test_zip_slip_rejected(tmp_path):
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w") as z:
        z.writestr("repo/../../escape","danger")
    with pytest.raises(PermissionError):
        extract_archive(data.getvalue(),tmp_path/"dest")


def test_archive_expansion_bound(tmp_path):
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w",compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("repo/large","a"*2_000_000)
    with pytest.raises(ValueError,match="expansion"):
        extract_archive(data.getvalue(),tmp_path/"dest",1)


def test_test_tampering_blocked(ready):
    _,ws=ready
    with pytest.raises(PermissionError):
        ws.write("tests/test_calc.py","pass\n",sha((ws.repo/"tests/test_calc.py").read_bytes()))
    (ws.repo/"tests/test_calc.py").write_text("pass\n")
    with pytest.raises(PermissionError):
        ws.clean_verification()


def test_sandbox_command_confinement(tmp_path):
    settings=Settings(root=tmp_path,image="image@sha256:"+"a"*64,key="SECRET")
    runner=DockerSandbox(settings,"a"*32,1,lambda:None)
    _,command=runner.command(tmp_path,"b"*32,["tests/test_foo.py::test_x"])
    text=" ".join(command)
    for required in ("--network none","--read-only","--cap-drop ALL","--pids-limit 64","no-new-privileges","dst=/source,readonly","size=256m"):
        assert required in text
    assert "SECRET" not in text and "docker.sock" not in text and "--privileged" not in text
    assert "evaluator" not in text
    with pytest.raises(PermissionError):
        runner.command(tmp_path,"b"*32,["--override-ini=x"])


def test_redaction_valid_json(store,spec):
    tid=store.create(spec)
    store.record(tid,"log",{"key":"supersecretapikey123456789","authorization":"Bearer sk-abcdefghijklmnop123456"})
    value=store.rows("events",tid)[-1]["payload"]
    assert "supersecret" not in json.dumps(value)
    assert "abcdefghijklmnop" not in json.dumps(value)


def test_hash_precondition_blocks_stale_write(ready):
    _,ws=ready
    before=(ws.repo/"calc.py").read_bytes()
    with pytest.raises(ValueError,match="hash mismatch"):
        ws.write("calc.py","changed\n","wrong")
    assert (ws.repo/"calc.py").read_bytes()==before


def test_platform_disk_budget_blocks_persisted_artifact(ready):
    _,ws=ready
    ws.total_disk_mb=0
    with pytest.raises(ValueError,match="platform persisted disk budget"):
        ws.artifact("report","no capacity")


def test_model_cannot_shadow_pytest_entrypoint(tmp_path):
    settings=Settings(root=tmp_path)
    runner=DockerSandbox(settings,"a"*32,1,lambda:None)
    _,command=runner.command(tmp_path,"b"*32,["tests"])
    python_index=command.index("/opt/venv/bin/python")
    assert command[python_index+1:python_index+3]==["-I","-c"]
    entrypoint=command[python_index+3]
    assert entrypoint.index("import sys, pytest")<entrypoint.index("sys.path.insert")


def test_nonstandard_trusted_test_selector_is_protected(store,spec):
    from harness.workspace import Workspace
    tid=store.create({**spec,"tests":["checks/verify.py::test_issue"]})
    ws=Workspace(store,tid)
    ws.repo.mkdir(parents=True)
    with pytest.raises(PermissionError):
        ws.write("checks/verify.py","pass\n","ABSENT")
