import json
import subprocess
import threading
import unittest
from unittest.mock import patch
from webui.docker_service import COMMANDS, Busy, DockerFailure, run_bounded
from webui_support import setup_case, wait_task

class DockerTests(unittest.TestCase):
    def setUp(self): setup_case(self)

    def test_whitelist_has_no_destructive_or_arbitrary_commands(self):
        self.assertEqual(COMMANDS["stop"],["stop","qq-bot"])
        self.assertIn("--no-build",COMMANDS["start"])
        self.assertIn("--force-recreate",COMMANDS["apply"])
        self.assertNotIn("--build",COMMANDS["apply"])
        for cmd in COMMANDS.values():
            self.assertNotIn("down",cmd); self.assertNotIn("-v",cmd)
            self.assertEqual(cmd[-1],"qq-bot")
        with self.assertRaises(ValueError): self.app.state.docker.submit("arbitrary")

    def test_mutual_exclusion_and_bounded_history(self):
        docker=self.app.state.docker
        gate=threading.Event(); self.fake.proceed=gate
        docker.submit("stop"); self.assertTrue(self.fake.entered.wait(2))
        with self.assertRaises(Busy): docker.submit("start")
        with self.assertRaises(Busy):
            with docker.exclusive(): pass
        gate.set(); wait_task(docker); self.fake.proceed=None
        for _ in range(22): docker.submit("stop"); wait_task(docker)
        self.assertEqual(len(docker.tasks),20)

    def test_failed_docker_is_sanitized_and_unlocks(self):
        self.fake.fail=True
        docker=self.app.state.docker
        docker.submit("apply"); task=wait_task(docker)
        self.assertEqual(task["state"],"failed")
        self.assertNotIn("TOP-SECRET",task["output"])
        self.assertFalse(docker.guard.locked())

    def test_same_container_cannot_be_claimed_as_applied(self):
        docker=self.app.state.docker
        self.fake.container="new-container"
        docker.submit("apply"); task=wait_task(docker)
        self.assertEqual(task["state"],"failed")
        self.assertIn("新运行容器",task["output"])

    def test_daemon_unavailable_status_is_not_online(self):
        self.app.state.docker.runner=lambda *args: (1,"daemon not running")
        status=self.app.state.docker.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["containers"],[])

    def test_runner_never_uses_shell_and_drains_bounded_output(self):
        import io
        from unittest.mock import MagicMock
        proc=MagicMock(); proc.stdout=io.BytesIO(b"small\n"+b"x"*5000+b"\n"+b"a\n"*1000); proc.wait.return_value=0
        with patch("subprocess.Popen",return_value=proc) as popen:
            code,output=run_bounded(["mock"],self.root,1)
        self.assertEqual(code,0); self.assertLessEqual(len(output),2048*160)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_timeout_kills_process_tree_and_reports_unknown_state(self):
        import io
        from unittest.mock import MagicMock
        proc=MagicMock(); proc.pid=99999; proc.stdout=io.BytesIO(b"")
        proc.wait.side_effect=[subprocess.TimeoutExpired("mock",1),0]
        with patch("subprocess.Popen",return_value=proc),patch("subprocess.run") as kill,patch("os.killpg",create=True):
            with self.assertRaises(DockerFailure): run_bounded(["mock"],self.root,1)
        proc.kill.assert_called_once()

    def test_external_container_change_invalidates_application_claim(self):
        docker=self.app.state.docker
        docker.submit("apply")
        self.assertEqual(wait_task(docker)["state"],"succeeded")
        self.fake.container="externally-replaced"
        self.assertIn("未知",docker.status()["application"])
        self.assertIsNone(docker.config.applied_version)

    def test_missing_config_start_does_not_create_mount_or_call_docker(self):
        self.root.joinpath("plugin_config.json").unlink()
        docker=self.app.state.docker
        docker.submit("start")
        self.assertEqual(wait_task(docker)["state"],"failed")
        self.assertEqual(self.fake.calls,[])

    def test_lock_file_error_releases_in_process_guard(self):
        docker = self.app.state.docker
        with patch.object(docker.operation_lock, "acquire", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                docker.acquire()
        self.assertFalse(docker.guard.locked())
        with docker.exclusive():
            self.assertTrue(docker.guard.locked())
        self.assertFalse(docker.guard.locked())
