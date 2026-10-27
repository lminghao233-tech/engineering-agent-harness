"""Git credential helper subprocess. Token is read from parent environment, never argv/disk."""
import os
import sys

if sys.argv[-1:] == ["get"]:
    request=dict(line.rstrip("\n").split("=",1) for line in sys.stdin if "=" in line)
    if request.get("protocol")=="https" and request.get("host")=="github.com":
        token=os.environ.get("HARNESS_GIT_TOKEN", "")
        if token:
            sys.stdout.write("username=x-access-token\npassword="+token+"\n\n")
