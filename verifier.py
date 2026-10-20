import logging

import aiohttp

from config import VERIFY_REQUIRE_ADDRESS

logger = logging.getLogger(__name__)

API_URL = "https://www.1secmail.com/api/v1/"

# Mailbox domains served by the 1secmail API.
SUPPORTED_DOMAINS = frozenset({
    "1secmail.com", "1secmail.org", "1secmail.net",
    "esiix.com", "wwjmp.com", "xojxe.com", "yoggm.com", "kzccv.com",
    "qiott.com", "wuuvo.com", "icznn.com", "vjuum.com", "laafd.com", "txcct.com",
})

# Result codes returned by check_inbox
OK = "ok"
NO_MAIL = "no_mail"
UNSUPPORTED = "unsupported"
API_ERROR = "api_error"


class InboxVerifier:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "Mozilla/5.0 (compatible; TaskBot/1.0)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _api(self, **params):
        session = await self._get_session()
        async with session.get(API_URL, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"1secmail HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def check_inbox(self, recovery_email: str, gmail_address: str) -> tuple[bool, str]:
        """Look in the *recovery* mailbox for a mail sent by Google (and, when
        VERIFY_REQUIRE_ADDRESS is on, one that mentions the new Gmail address).

        Returns (verified, code) where code is one of OK / NO_MAIL /
        UNSUPPORTED / API_ERROR."""
        try:
            if "@" not in (recovery_email or "") or "@" not in (gmail_address or ""):
                return False, UNSUPPORTED

            login, domain = recovery_email.lower().split("@", 1)
            if domain not in SUPPORTED_DOMAINS:
                return False, UNSUPPORTED

            gmail_login = gmail_address.lower().split("@", 1)[0]
            messages = await self._api(action="getMessages", login=login, domain=domain)
            if not isinstance(messages, list) or not messages:
                return False, NO_MAIL

            # newest first, cap the work per click
            for msg in sorted(messages, key=lambda m: m.get("id", 0), reverse=True)[:10]:
                sender = str(msg.get("from", "")).lower()
                if "google" not in sender:
                    continue
                if not VERIFY_REQUIRE_ADDRESS:
                    logger.info("✅ Google mail found for %s", gmail_address)
                    return True, OK

                full = await self._api(action="readMessage", login=login, domain=domain, id=msg.get("id"))
                haystack = " ".join(
                    str(full.get(k, "")) for k in ("subject", "body", "textBody", "htmlBody")
                ).lower()
                if gmail_login in haystack:
                    logger.info("✅ Verification mail matched for %s", gmail_address)
                    return True, OK

            return False, NO_MAIL
        except Exception as e:  # noqa: BLE001
            logger.error("Error checking inbox for %s: %s", recovery_email, e)
            return False, API_ERROR


inbox_verifier = InboxVerifier()
