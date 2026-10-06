"""Host-side fixed Compose operations. No shell, user commands, volumes or down."""
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from filelock import FileLock, Timeout
from plugin_control import load_plugin_payload_strict
from .security import redact

COMMANDS = {
    "start": ["up", "-d", "--no-build", "--no-deps", "qq-bot"],
    "stop": ["stop", "qq-bot"],
    "apply": ["up", "-d", "--force-recreate", "--no-build", "--no-deps", "qq-bot"],
    "build": ["up", "-d", "--build", "--force-recreate", "--no-deps", "qq-bot"],
}
VERIFY = "import hashlib,os,pathlib; p=pathlib.Path(os.environ['BOT_PLUGIN_CONFIG']); print(hashlib.sha256(p.read_bytes()).hexdigest())"

class Busy(ValueError):
    pass

class DockerFailure(ValueError):
    pass


def run_bounded(argv, cwd, timeout):
    """Drain pipes continuously with bounded memory; discard oversized log lines."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, shell=False, **options)
    except OSError:
        raise DockerFailure("Docker 不可用，请检查 Docker Desktop 和 Compose 安装") from None
    lines = deque(maxlen=160)
    def drain():
        pending = b""
        dropping = False
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            for piece in chunk.splitlines(keepends=True):
                if not dropping:
                    pending += piece
                    if len(pending) > 2048:
                        pending = b""
                        dropping = True
                if piece.endswith((b"\n", b"\r")):
                    lines.append("[过长日志行已省略]" if dropping else pending.decode("utf-8", errors="replace"))
                    pending = b""
                    dropping = False
        if pending and not dropping:
            lines.append(pending.decode("utf-8", errors="replace"))
        proc.stdout.close()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=5)
        raise DockerFailure("Docker 操作超时；命令已终止，容器状态需重新检查，不保证自动回滚") from None
    reader.join(timeout=5)
    return code, "".join(lines)


class DockerService:
    def __init__(self, config, runner=run_bounded):
        self.config = config
        self.runner = runner
        self.guard = threading.Lock()
        self.operation_lock = FileLock(str(config.root / ".webui-operation.lock"), timeout=0, thread_local=False)
        self.tasks = {}
        self.task_lock = threading.Lock()
        self.last_application = "应用状态未知"
        self.known_secrets = set()
        self.redaction_lock = threading.Lock()
        self.applied_container = None

    def clean(self, text):
        with self.redaction_lock:
            self.known_secrets.update(self.config.secrets())
            return redact(text, self.known_secrets)[-24000:]

    def run(self, args, timeout=20):
        argv = ["docker", "compose", "--project-directory", str(self.config.root),
                "--file", str(self.config.root / "docker-compose.yml"), *args]
        code, output = self.runner(argv, self.config.root, timeout)
        if code:
            raise DockerFailure("Docker 命令失败（启动需要已有镜像，可先构建）：\n" + self.clean(output))
        return output

    def acquire(self):
        if not self.guard.acquire(blocking=False):
            raise Busy("已有保存或容器操作正在执行，请等待完成")
        try:
            self.operation_lock.acquire()
        except BaseException as exc:
            self.guard.release()
            if isinstance(exc, Timeout):
                raise Busy("另一个管理进程正在操作，请稍后重试") from None
            raise

    def release(self):
        self.operation_lock.release()
        self.guard.release()

    @contextmanager
    def exclusive(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def status(self):
        try:
            output = self.run(["ps", "--all", "--format", "json", "qq-bot"])
            try:
                parsed = json.loads(output) if output.strip() else []
                rows = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in output.splitlines() if line.strip()]
            if self.applied_container:
                current_ids = [r.get("ID", "") for r in rows if r.get("State") == "running"]
                _, current_version = load_plugin_payload_strict(self.config.path)
                if self.applied_container not in current_ids or current_version != self.config.applied_version:
                    self.config.applied_version = None
                    self.applied_container = None
                    self.last_application = "容器或配置已变化，应用状态未知／待应用"
            states = [{"name": r.get("Name", "qq-bot"), "state": r.get("State", "unknown"),
                       "status": self.clean(str(r.get("Status", "unknown")))} for r in rows]
            return {"available": True, "containers": states, "application": self.last_application,
                    "notice": "容器运行不代表 QQ 已连接"}
        except (DockerFailure, ValueError):
            self.config.applied_version = None
            self.applied_container = None
            self.last_application = "Docker 或配置不可验证，应用状态未知"
            return {"available": False, "containers": [], "application": self.last_application,
                    "notice": "Docker 不可用或状态读取失败；不代表 QQ 在线状态"}

    def logs(self):
        # Do not fetch logs if a broken config prevents safe secret enumeration.
        load_plugin_payload_strict(self.config.path)
        return self.clean(self.run(["logs", "--no-color", "--tail", "200", "qq-bot"]))

    def update(self, task_id, **values):
        with self.task_lock:
            self.tasks[task_id].update(values)

    def list_tasks(self):
        with self.task_lock:
            return [dict(t) for t in reversed(list(self.tasks.values()))]

    def submit(self, action, edit=None):
        if action not in COMMANDS:
            raise ValueError("不支持的容器操作")
        self.acquire()
        try:
            if edit is not None:
                with self.redaction_lock:
                    self.known_secrets.update(self.config.secrets())
                self.config.save(edit)
            task_id = uuid.uuid4().hex
            task = {"id": task_id, "action": action, "state": "queued", "phase": "等待执行", "output": "",
                    "created": time.time()}
            with self.task_lock:
                if len(self.tasks) >= 20:
                    self.tasks.pop(next(iter(self.tasks)))
                self.tasks[task_id] = task
            worker = threading.Thread(target=self.work, args=(task_id, action), daemon=True)
            worker.start()
            return dict(task)
        except Exception:
            self.release()
            raise

    def work(self, task_id, action):
        try:
            with self.config.lock:
                expected = None
                if action == "start":
                    _, version = load_plugin_payload_strict(self.config.path)
                    if version == "missing":
                        raise DockerFailure("请先保存配置再启动，避免缺失挂载文件")
                if action in {"apply", "build"}:
                    _, expected = load_plugin_payload_strict(self.config.path)
                    if expected == "missing":
                        raise DockerFailure("请先保存配置再应用或构建，避免缺失挂载文件")
                self.update(task_id, state="running", phase="构建并启动" if action == "build" else "执行容器操作")
                if action in {"apply", "build"}:
                    self.last_application = "应用中"
                before = self.run(["ps", "--all", "-q", "qq-bot"]).strip() if expected else None
                output = self.run(COMMANDS[action], timeout=900 if action == "build" else 120)
                self.update(task_id, output=self.clean(output))
                if expected:
                    self.update(task_id, phase="验证新容器配置摘要")
                    after = self.run(["ps", "-q", "qq-bot"]).strip()
                    if not after or after == before:
                        raise DockerFailure("未检测到新运行容器，不能确认配置已应用")
                    actual = self.run(["exec", "-T", "qq-bot", "python", "-c", VERIFY]).strip()
                    _, current = load_plugin_payload_strict(self.config.path)
                    if actual != expected or current != expected:
                        raise DockerFailure("容器配置摘要不匹配或保存期间文件已更改，请重新应用")
                    self.config.applied_version = expected
                    self.applied_container = after
                    self.last_application = "已验证新容器配置；QQ 连接与插件行为需另行验收"
                elif action == "stop":
                    self.config.applied_version = None
                    self.applied_container = None
                    self.last_application = "容器已请求停止，应用状态未知"
                else:
                    self.config.applied_version = None
                    self.applied_container = None
                    self.last_application = "已请求启动，配置应用状态未知"
                self.update(task_id, state="succeeded", phase="完成")
        except Exception as exc:
            self.config.applied_version = None
            self.applied_container = None
            self.last_application = "应用失败／状态未知" if action in {"apply", "build"} else "操作失败／状态未知"
            message = str(exc) if isinstance(exc, (ValueError, Timeout)) else "管理操作失败，请检查本地服务和 Docker 状态"
            self.update(task_id, state="failed", phase="失败", output=self.clean(message))
        finally:
            self.release()
