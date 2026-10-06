import time
import unittest
from webui.security import COOKIE, redact
from webui_support import setup_case, login, PASSWORD, ORIGIN

class SecurityTests(unittest.TestCase):
    def setUp(self): setup_case(self)

    def test_all_private_routes_require_authentication(self):
        for url in ["config","status","logs","tasks","session"]:
            self.assertEqual(self.client.get("/api/"+url).status_code,401)
        self.assertEqual(self.fake.calls,[])

    def test_source_and_host_validation_including_login(self):
        for headers in [{"Host":"evil.example"}, {"Origin":"http://evil.example"},
                        {"Sec-Fetch-Site":"cross-site"}]:
            self.assertEqual(self.client.get("/",headers=headers).status_code,403)
        for origin in [None,"null","http://evil.example"]:
            response=self.client.post("/api/login",json={"password":PASSWORD},headers={"Origin":origin} if origin else {})
            self.assertEqual(response.status_code,403)

    def test_csrf_cookie_logout_and_expiry(self):
        login(self)
        response=self.client.post("/api/logout",json={},headers={"Origin":ORIGIN})
        self.assertEqual(response.status_code,403)
        token=self.client.cookies.get(COOKIE)
        session=self.app.state.auth.get(token)
        session["expires"]=time.monotonic()-1
        self.assertEqual(self.client.get("/api/config").status_code,401)
        login(self)
        response=self.client.post("/api/logout",json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get("/api/config").status_code,401)

    def test_login_throttle_and_cookie_flags(self):
        response=self.client.post("/api/login",json={"password":PASSWORD},headers={"Origin":ORIGIN})
        cookie=response.headers["set-cookie"].lower()
        self.assertIn("httponly",cookie); self.assertIn("samesite=strict",cookie); self.assertIn("max-age=3600",cookie)
        for _ in range(4): self.client.post("/api/login",json={"password":"wrong"},headers={"Origin":ORIGIN})
        self.assertEqual(self.client.post("/api/login",json={"password":PASSWORD},headers={"Origin":ORIGIN}).status_code,429)

    def test_body_limit_security_headers_and_no_get_mutations(self):
        login(self)
        response=self.client.get("/")
        self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertIn("frame-ancestors 'none'",response.headers["content-security-policy"])
        response=self.client.post("/api/emoji/preview",content='x'*33000,headers=self.headers)
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.client.get("/api/config/apply").status_code,405)
        self.assertEqual(self.fake.calls,[])

    def test_redaction(self):
        text="exact-secret Authorization: Bearer abcdef\nCookie: cookie-data\nhttps://user:pass@example.org\nsk-1234\n"
        result=redact(text,["exact-secret"])
        for private in ["exact-secret","abcdef","cookie-data","user:pass","sk-1234"]: self.assertNotIn(private,result)
