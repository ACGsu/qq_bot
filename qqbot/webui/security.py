import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque

COOKIE = "qqbot_webui_session"

class Security:
    def __init__(self, settings):
        self.settings = settings
        self.password_hash = hashlib.sha256(settings.password.encode()).digest()
        self.sessions = {}
        self.attempts = deque(maxlen=30)
        self.lock = threading.Lock()

    def login(self, password):
        with self.lock:
            now = time.monotonic()
            while self.attempts and now - self.attempts[0] > 60:
                self.attempts.popleft()
            if len(self.attempts) >= 5:
                return None, "rate"
            self.attempts.append(now)
            if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.password_hash):
                return None, "password"
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            if len(self.sessions) >= 20:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + self.settings.session_seconds}
            self.sessions[token] = session
            return (token, session), None

    def get(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session["expires"] <= time.monotonic():
                self.sessions.pop(token, None)
                return None
            return session

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


def redact(text, known=()):
    for value in sorted(set(known), key=len, reverse=True):
        if value:
            text = text.replace(value, "[已脱敏]")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[已脱敏]", text)
    text = re.sub(r"(?i)((?:[\w-]*(?:api[_-]?key|token|secret|password|cookie)[\w-]*)[\"']?\s*[:=]\s*)[^\r\n]+", r"\1[已脱敏]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[已脱敏]@", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已脱敏]", text)
    return text
