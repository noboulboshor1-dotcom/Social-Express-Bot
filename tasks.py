import base64
import binascii
import hashlib
import hmac
import re
import secrets
import string
import struct
import time

DEFAULT_TASK_REWARD = 10.0

# Domains the 1secmail API serves; recovery mailboxes are generated on this domain.
RECOVERY_DOMAIN = "1secmail.com"

_FIRST_NAMES = ["sidney", "alex", "david", "michael", "robert", "john", "william", "james"]


class TaskManager:
    @staticmethod
    def generate_random_gmail() -> dict:
        """Generate a fallback task (used when admin stock is empty).
        Only DB-safe keys are returned so it can be inserted as-is."""
        name = secrets.choice(_FIRST_NAMES)
        digits = "".join(secrets.choice(string.digits) for _ in range(4))
        chars = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        email = f"{name}{digits}{chars}@gmail.com"

        pw_chars = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$"),
        ] + [secrets.choice(string.ascii_letters + string.digits) for _ in range(8)]
        secrets.SystemRandom().shuffle(pw_chars)
        password = "".join(pw_chars)

        recovery = f"rec_{chars}{secrets.token_hex(2)}@{RECOVERY_DOMAIN}"
        return {
            "email": email,
            "password": password,
            "recovery": recovery,
            "reward": DEFAULT_TASK_REWARD,
        }

    @staticmethod
    def display_names(task: dict) -> tuple[str, str]:
        local = task["email"].split("@")[0]
        letters = re.match(r"[A-Za-z]+", local)
        first = task.get("first_name") or (letters.group(0).capitalize() if letters else local.capitalize())
        last = task.get("last_name") or "❌"
        return first, last

    # --- TOTP (RFC 6238, stdlib only) -----------------------------------
    @staticmethod
    def normalize_secret(raw: str) -> str | None:
        secret = re.sub(r"[\s\-]", "", raw or "").upper().rstrip("=")
        return secret if re.fullmatch(r"[A-Z2-7]{16,64}", secret) else None

    @classmethod
    def generate_totp(cls, raw_secret: str, for_time: float | None = None) -> tuple[str, int] | None:
        """Return (6-digit code, seconds until it rotates) or None for an invalid key."""
        secret = cls.normalize_secret(raw_secret)
        if not secret:
            return None
        try:
            key = base64.b32decode(secret + "=" * (-len(secret) % 8))
        except (binascii.Error, ValueError):
            return None
        now = time.time() if for_time is None else for_time
        counter = int(now // 30)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(value % 1_000_000).zfill(6), 30 - int(now % 30)


task_manager = TaskManager()
