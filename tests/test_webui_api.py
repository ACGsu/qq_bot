import json
import unittest
from webui_support import setup_case, login, wait_task

class ApiTests(unittest.TestCase):
    def setUp(self): setup_case(self); login(self)

    def payload(self):
        config=self.client.get("/api/config").json()
        return {"version":config["version"],"enabled_plugins":config["enabled_plugins"],"plugin_settings":{"rps":{"ban_seconds":"120"}}}

    def test_config_roundtrip_and_save_never_calls_docker(self):
        payload=self.payload()
        before=self.root.joinpath("plugin_config.json").read_bytes()
        response=self.client.post("/api/config/validate",json=payload,headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.root.joinpath("plugin_config.json").read_bytes(),before)
        response=self.client.put("/api/config",json=payload,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["plugin_settings"]["rps"]["ban_seconds"],"120")
        self.assertEqual(self.fake.calls,[])
        response=self.client.put("/api/config",json=payload,headers=self.headers)
        self.assertEqual(response.status_code,409)

    def test_apply_recreates_and_verifies(self):
        response=self.client.post("/api/config/apply",json=self.payload(),headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        task=wait_task(self.app.state.docker)
        self.assertEqual(task["state"],"succeeded",task)
        args=[c[6:] for c in self.fake.calls]
        self.assertTrue(any("--force-recreate" in a and "--no-build" in a for a in args))
        self.assertTrue(any(a[0]=="exec" for a in args))
        self.assertFalse(any("restart" in a or "--build" in a for a in args))
        self.assertIn("已验证",self.client.get("/api/config").json()["application"])

    def test_apply_failure_leaves_saved_config(self):
        self.fake.mismatch=True
        self.client.post("/api/config/apply",json=self.payload(),headers=self.headers)
        task=wait_task(self.app.state.docker)
        self.assertEqual(task["state"],"failed")
        self.assertIn("摘要不匹配",task["output"])
        self.assertEqual(self.client.get("/api/config").json()["plugin_settings"]["rps"]["ban_seconds"],"120")
        self.assertIsNone(self.app.state.config.applied_version)

    def test_corruption_and_invalid_input_do_not_echo_secrets(self):
        response=self.client.put("/api/config",json={"version":"x","enabled_plugins":[],"plugin_settings":{"summary":{"deepseek_api_key": {"secret":"hidden"}}}},headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertNotIn("hidden",response.text)
        self.root.joinpath("plugin_config.json").write_text("{secret:broken",encoding="utf-8")
        response=self.client.get("/api/config")
        self.assertEqual(response.status_code,422)
        self.assertNotIn("secret:broken",response.text)

    def test_preview_invalid_and_no_command_injection(self):
        response=self.client.post("/api/emoji/preview",json={"text":"🍬㊗️"},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()["emoji"]),2)
        response=self.client.post("/api/tasks",json={"action":"stop; docker down -v"},headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.fake.calls,[])

    def test_logs_redacted_and_page_survives_stop(self):
        self.assertNotIn("TOP-SECRET",self.client.get("/api/logs").text)
        self.client.post("/api/tasks",json={"action":"stop"},headers=self.headers)
        wait_task(self.app.state.docker)
        self.assertEqual(self.client.get("/").status_code,200)
        self.assertEqual(self.client.get("/api/session").status_code,200)
        self.assertEqual([a[6:] for a in self.fake.calls if a[6]=="stop"],[["stop","qq-bot"]])

    def test_docker_unavailable_keeps_page_config_and_validation_available(self):
        self.app.state.docker.runner = lambda *args: (1, "daemon not running")
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertEqual(self.client.post("/api/config/validate", json=self.payload(), headers=self.headers).status_code, 200)
        self.assertEqual(self.fake.calls, [])

    def test_static_modules_are_served_as_javascript(self):
        for path in ("/static/app.js", "/static/duration.mjs"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("javascript", response.headers["content-type"])
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("./duration.mjs", self.client.get("/static/app.js").text)

    def test_general_emoji_preview_save_reload_and_invalid_cluster_is_atomic(self):
        response = self.client.post("/api/emoji/preview", json={"text": "😀👍❤️🔥🎉😀"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        ids = ["128512", "128077", "10084", "128293", "127881"]
        self.assertEqual([item["id"] for item in response.json()["emoji"]], ids)
        payload = self.payload()
        payload.update(emoji_text="😀👍❤️🔥🎉", retained_emoji_ids=[])
        response = self.client.put("/api/config", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = self.client.get("/api/config").json()
        stored = json.loads(self.root.joinpath("plugin_config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["plugin_settings"]["auto_emoji"]["emoji_ids"], ",".join(ids))
        self.assertEqual([item["id"] for item in value["emoji"]], ids)
        self.assertTrue(all(not item["legacy"] for item in value["emoji"]))
        before = self.root.joinpath("plugin_config.json").read_bytes()
        payload.update(version=value["version"], emoji_text="😀👍🏽")
        response = self.client.put("/api/config", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.root.joinpath("plugin_config.json").read_bytes(), before)
        self.assertEqual(self.fake.calls, [])

    def test_background_is_available_before_login_without_exposing_assets_directory(self):
        self.client.cookies.clear()
        response = self.client.get("/static/background.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertTrue(response.content.startswith(b"\xff\xd8"))
        self.assertLess(len(response.content), 250_000)
        self.assertEqual(self.client.get("/assets/webui_backpic.jpg").status_code, 404)
        self.assertEqual(self.client.get("/api/config").status_code, 401)
