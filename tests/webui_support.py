import hashlib
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient
from webui.app import create_app
from webui.settings import Settings

PASSWORD="test-only-password-123456"
ORIGIN="http://127.0.0.1:8765"

class FakeDocker:
    def __init__(self):
        self.calls=[]
        self.container="old-container"
        self.fail=False
        self.mismatch=False
        self.entered=threading.Event()
        self.proceed=None

    def __call__(self,argv,cwd,timeout):
        assert argv[:2] == ["docker","compose"]
        self.calls.append(argv)
        args=argv[6:]
        if args[0] in {"up","stop"}:
            self.entered.set()
            if self.proceed: self.proceed.wait(3)
            if self.fail: return 1,"access_token=TOP-SECRET"
            if args[0]=="up": self.container="new-container"
            return 0,"done"
        if args[:1]==["exec"]:
            if self.mismatch:return 0,"wrong-digest"
            return 0,hashlib.sha256((cwd/"plugin_config.json").read_bytes()).hexdigest()
        if "--format" in args:return 0,json.dumps([{"Name":"my-bot","State":"running","Status":"Up","ID":self.container}])
        if args[0]=="ps":return 0,self.container
        if args[0]=="logs":return 0,"ordinary log\nDEEPSEEK_API_KEY=TOP-SECRET\n"
        raise AssertionError(f"Unexpected mocked command: {args}")

def setup_case(case):
    tmp=TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    case.root=Path(tmp.name)
    case.root.joinpath("plugin_config.json").write_text(json.dumps({"enabled_plugins":["rps","summary"],"plugin_settings":{"summary":{"deepseek_api_key":"TOP-SECRET"}}}),encoding="utf-8")
    case.fake=FakeDocker()
    case.app=create_app(Settings(password=PASSWORD,root=case.root),runner=case.fake)
    case.client=TestClient(case.app,base_url=ORIGIN)
    case.addCleanup(case.client.close)

def login(case):
    response=case.client.post("/api/login",json={"password":PASSWORD},headers={"Origin":ORIGIN})
    case.assertEqual(response.status_code,200,response.text)
    case.headers={"Origin":ORIGIN,"X-CSRF-Token":response.json()["csrf"]}

def wait_task(service):
    limit=time.monotonic()+5
    while time.monotonic()<limit:
        tasks=service.list_tasks()
        if tasks and tasks[0]["state"] in {"failed","succeeded"} and not service.guard.locked(): return tasks[0]
        time.sleep(.01)
    raise AssertionError("Mock task timed out")
