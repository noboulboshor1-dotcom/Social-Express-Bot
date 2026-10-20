import logging
import aiohttp
from config import VERIFY_REQUIRE_ADDRESS

logger = logging.getLogger(__name__)

API_URL = "https://api.mail.tm"

OK = "ok"
NO_MAIL = "no_mail"
UNSUPPORTED = "unsupported"
API_ERROR = "api_error"

class InboxVerifier:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            conn = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(
                connector=conn,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (compatible; TaskBot/2.0)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_inbox(self, recovery_data: str, gmail_address: str) -> tuple[bool, str]:
        try:
            # পুরানো ডাটাবেজ ফরম্যাট হলে সরাসরি ম্যানুয়াল সাবমিটে পাঠিয়ে দেবে
            if "|" not in (recovery_data or ""):
                return False, UNSUPPORTED

            recovery_email, recovery_pass = recovery_data.split("|", 1)
            gmail_login = (gmail_address or "").lower().split("@")[0]
            session = await self._get_session()

            # 1. Login to get token
            auth_payload = {"address": recovery_email, "password": recovery_pass}
            async with session.post(f"{API_URL}/token", json=auth_payload) as resp:
                if resp.status != 200:
                    logger.error("Mail.tm login failed: HTTP %s", resp.status)
                    return False, API_ERROR
                token_data = await resp.json()
                token = token_data.get("token")

            if not token:
                return False, API_ERROR

            # 2. Get messages
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(f"{API_URL}/messages", headers=headers) as resp:
                if resp.status != 200:
                    return False, API_ERROR
                msg_data = await resp.json()
                messages = msg_data.get("hydra:member", [])

            if not messages:
                return False, NO_MAIL

            # 3. Check for Google mail
            for msg in messages:
                sender = str(msg.get("from", {}).get("address", "")).lower()
                if "google" not in sender:
                    continue
                if not VERIFY_REQUIRE_ADDRESS:
                    logger.info("✅ Google mail found for %s", gmail_address)
                    return True, OK

                msg_id = msg.get("id")
                async with session.get(f"{API_URL}/messages/{msg_id}", headers=headers) as msg_resp:
                    if msg_resp.status != 200:
                        continue
                    full = await msg_resp.json()
                    haystack = " ".join(
                        str(full.get(k, "")) for k in ("subject", "intro", "text", "html")
                    ).lower()
                    
                    if gmail_login in haystack:
                        logger.info("✅ Verification mail matched for %s", gmail_address)
                        return True, OK

            return False, NO_MAIL
        except Exception as e:
            logger.error("Error checking inbox for %s: %s", recovery_data, e)
            return False, API_ERROR

inbox_verifier = InboxVerifier()
