import base64
import binascii
import hashlib
import hmac
import re
import secrets
import string
import struct
import time
import aiohttp

DEFAULT_TASK_REWARD = 10.0

_FIRST_NAMES = ["sidney", "alex", "david", "michael", "robert", "john", "william", "james"]

class TaskManager:
    @staticmethod
    async def generate_random_gmail() -> dict:
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

        # --- Mail.tm Integration ---
        recovery_pass = secrets.token_urlsafe(12)
        # 1secmail পুরোপুরি বাদ দিয়ে Mail.tm এর একটি ডিফল্ট ডোমেইন ব্যাকআপ হিসেবে রাখা হলো
        recovery_email = f"rec_{chars}@mail.tm" 
        
        try:
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=conn) as session:
                # 1. Fetch available domain from Mail.tm
                async with session.get("https://api.mail.tm/domains") as resp:
                    if resp.status == 200:
                        domains_data = await resp.json()
                        domain = domains_data["hydra:member"][0]["domain"]
                        recovery_email = f"rec_{chars}@{domain}"
                
                # 2. Create Mail.tm Account
                payload = {"address": recovery_email, "password": recovery_pass}
                async with session.post("https://api.mail.tm/accounts", json=payload) as resp:
                    pass # Account successfully registered
        except Exception as e:
            print(f"Mail.tm account creation error: {e}")

        # ইমেইল এবং পাসওয়ার্ড | চিহ্ন দিয়ে সেভ করা হলো
        recovery = f"{recovery_email}|{recovery_pass}"

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

    @staticmethod
    def normalize_secret(raw: str) -> str | None:
        secret = re.sub(r"[\s\-]", "", raw or "").upper().rstrip("=")
        return secret if re.fullmatch(r"[A-Z2-7]{16,64}", secret) else None

    @classmethod
    def generate_totp(cls, raw_secret: str, for_time: float | None = None) -> tuple[str, int] | None:
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
