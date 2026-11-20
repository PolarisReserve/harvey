"""Quo (quo.com, formerly OpenPhone): texts to and from people who agreed to them.

Harvey reads the texts that reach one Quo number and sends the replies you
approve from it. It never texts anyone cold: Quo's terms require consent
before the first message to each person, a record of that consent, and a
"Reply STOP" line in the first message, and its fair-use policy forbids
"cold calling and/or messaging". Harvey's side of that (the consent record,
the STOP line, approval for every text) lives in harvey/agents/texter.py.

Polling, not webhooks: Quo delivers webhooks only to a public HTTPS address,
and Harvey runs on your own computer. One list call per heartbeat reads every
new incoming text.

The key (QUO_API_KEY) goes in the Authorization header and nowhere else.
Endpoints follow Quo's API reference, version 2026-03-30 (read 2026-11-19).
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from harvey.integrations.mail_provider import SendResult
from harvey.phones import E164_RE as _E164_RE, normalize_phone

logger = logging.getLogger("harvey.quo")

API_BASE = "https://api.quo.com"
API_VERSION = "2026-03-30"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
PAGE_LIMIT = 50          # Quo's largest page
MAX_PAGES = 10           # at most 500 new texts read in one heartbeat
RATE_RETRIES = 3         # 429s retried before giving up for this cycle
MAX_TEXT_CHARS = 1600    # Quo's limit for one message

# What went wrong, in words Harvey can act on. Each is a QuoError.kind.
AUTH = "auth"
OPTED_OUT = "opted_out"
A2P_NOT_APPROVED = "a2p_not_approved"
TOLL_FREE_UNVERIFIED = "toll_free_unverified"
DAILY_CAP = "daily_cap"
NO_CREDITS = "no_credits"
INTERNATIONAL = "international"
UNREACHABLE = "unreachable"
RATE_LIMITED = "rate_limited"
NEW_CONVERSATION_LIMIT = "new_conversation_limit"
NOT_FOUND = "not_found"
NO_ANSWER = "no_answer"
OTHER = "other"

# Problems that stop every text from going out, not just one: the Sender
# holds the whole channel and says why on Today.
CHANNEL_HOLDS = {AUTH, A2P_NOT_APPROVED, TOLL_FREE_UNVERIFIED, DAILY_CAP, NO_CREDITS,
                 NEW_CONVERSATION_LIMIT}

EXPLAIN = {
    AUTH: "Quo rejected the API key. Create a key in Quo (Workspace Settings > API) "
          "and put it in .env as QUO_API_KEY.",
    OPTED_OUT: "This person texted STOP to your Quo number, so Quo won't text them.",
    A2P_NOT_APPROVED: "Carriers haven't approved this workspace's A2P registration, "
                      "which texting US numbers needs. Finish it in Quo.",
    TOLL_FREE_UNVERIFIED: "This toll-free number isn't verified for texting yet. "
                          "Verify it in Quo, or text from a local number.",
    DAILY_CAP: "The carriers' daily text limit for this number is reached. "
               "Texts go out again tomorrow.",
    NO_CREDITS: "The Quo account is out of credits or its subscription has ended.",
    INTERNATIONAL: "This Quo workspace can't text that country.",
    UNREACHABLE: "Quo says this number can't receive texts.",
    RATE_LIMITED: "Quo's API limit is reached. Harvey tries again next cycle.",
    NEW_CONVERSATION_LIMIT: "Quo's limit on starting new conversations is reached.",
    NOT_FOUND: "Quo doesn't have that.",
    NO_ANSWER: "Quo didn't answer. Harvey tries again next cycle.",
    OTHER: "Quo refused the request.",
}



class QuoError(RuntimeError):
    """A Quo request failed. ``kind`` is one of the constants above."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(EXPLAIN.get(kind, EXPLAIN[OTHER]) + (f" ({detail})" if detail else ""))


def iso_utc(when: datetime) -> str:
    """A time as Quo's filters want it: ISO 8601 in UTC with a Z."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text(x) -> str:
    return x if isinstance(x, str) else ""


def _error_kind(status: int, body: dict) -> str:
    words = " ".join(_text(body.get(k)) for k in ("message", "title", "code", "error", "detail"))
    errors = body.get("errors")
    if isinstance(errors, list):
        words += " " + " ".join(_text(e.get("message")) if isinstance(e, dict) else _text(e)
                                for e in errors)
    w = words.lower()
    if "opted out" in w or "opted-out" in w:
        return OPTED_OUT
    if "a2p" in w and ("approv" in w or "registration" in w) and "cap" not in w:
        return A2P_NOT_APPROVED
    if "daily message cap" in w or ("cap" in w and "a2p" in w):
        return DAILY_CAP
    if "tfn" in w or "toll-free" in w or "toll free" in w:
        return TOLL_FREE_UNVERIFIED
    if "international" in w:
        return INTERNATIONAL
    if "unreachable" in w:
        return UNREACHABLE
    if "new conversation" in w:
        return NEW_CONVERSATION_LIMIT
    if status == 402 or "credit" in w or "subscription" in w:
        return NO_CREDITS
    if status in (401, 403):
        return AUTH
    if status == 404:
        return NOT_FOUND
    if status == 429:
        return RATE_LIMITED
    return OTHER


@dataclass(frozen=True)
class InboundText:
    id: str
    from_number: str
    body: str
    created_at: str = ""
    conversation_id: str = ""


class QuoClient:
    """The few Quo calls Harvey makes. Every method raises QuoError on failure."""

    name = "quo"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep):
        self.api_key = (api_key or "").strip()
        self._transport = transport
        self._sleep = sleep

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        if not self.api_key:
            raise QuoError(AUTH, "QUO_API_KEY is not set")
        headers = {"Authorization": self.api_key, "Quo-Api-Version": API_VERSION,
                   "Accept": "application/json"}
        async with httpx.AsyncClient(base_url=API_BASE, timeout=TIMEOUT,
                                     transport=self._transport) as client:
            for attempt in range(RATE_RETRIES + 1):
                try:
                    r = await client.request(method, path, params=params, json=json,
                                             headers=headers)
                except httpx.HTTPError as e:
                    raise QuoError(NO_ANSWER, type(e).__name__) from None
                try:
                    body = r.json() if r.content else {}
                except ValueError:
                    body = {}
                body = body if isinstance(body, dict) else {}
                if r.status_code < 300:
                    return body
                kind = _error_kind(r.status_code, body)
                if kind == RATE_LIMITED and attempt < RATE_RETRIES:
                    # Backoff with jitter, as Quo's rate-limit page asks.
                    await self._sleep(min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.25))
                    continue
                raise QuoError(kind, f"HTTP {r.status_code}")
        raise QuoError(RATE_LIMITED)

    async def phone_numbers(self) -> list[dict]:
        """The workspace's numbers: [{"id": "PN...", "number": "+1..."}]."""
        out = []
        params: dict = {"limit": PAGE_LIMIT}
        for _ in range(MAX_PAGES):
            body = await self._request("GET", "/phone-numbers", params=params)
            for item in body.get("data") or []:
                if not isinstance(item, dict):
                    continue
                number = _text(item.get("phoneNumber") or item.get("number"))
                out.append({"id": _text(item.get("id")), "number": number,
                            "name": _text(item.get("name"))})
            cursor = _text(body.get("nextCursor"))
            if not cursor:
                break
            params = {**params, "after": cursor}
        return out

    async def resolve_number(self, e164: str) -> str:
        """The Quo id ("PN...") of one of the workspace's numbers."""
        want = normalize_phone(e164, "US")
        for n in await self.phone_numbers():
            if n["id"] and normalize_phone(n["number"], "US") == want:
                return n["id"]
        raise QuoError(NOT_FOUND, "channels.sms.from_number isn't one of this "
                                  "workspace's Quo numbers")

    async def send_text(self, phone_number_id: str, to: str, body: str) -> SendResult:
        """Text one person. ``to`` is E.164. One recipient only: more than
        one would make a group message."""
        if not _E164_RE.match(to or ""):
            return SendResult(ok=False, error=f"'{to}' is not a phone number Quo can text")
        if not body or len(body) > MAX_TEXT_CHARS:
            return SendResult(ok=False, error="text is empty or too long for Quo")
        data = await self._request("POST", "/messages", json={
            "from": phone_number_id, "to": [to], "content": body})
        msg = data.get("data") if isinstance(data.get("data"), dict) else data
        return SendResult(ok=True, message_id=_text(msg.get("id")),
                          thread_ref=_text(msg.get("conversationId")))

    async def incoming_messages(self, phone_number_id: str, since: datetime) -> list[InboundText]:
        """Texts that reached the number after ``since``, oldest first."""
        params = {"direction": "incoming", "phoneNumberId": phone_number_id,
                  "createdAt[gt]": iso_utc(since), "limit": PAGE_LIMIT}
        found: list[InboundText] = []
        for _ in range(MAX_PAGES):
            body = await self._request("GET", "/messages", params=params)
            for m in body.get("data") or []:
                if not isinstance(m, dict) or not _text(m.get("id")):
                    continue
                found.append(InboundText(
                    id=_text(m.get("id")),
                    from_number=_text(m.get("from")),
                    body=_text(m.get("text") or m.get("content") or m.get("body")),
                    created_at=_text(m.get("createdAt")),
                    conversation_id=_text(m.get("conversationId")),
                ))
            cursor = _text(body.get("nextCursor"))
            if not cursor:
                break
            params = {**params, "after": cursor}
        found.sort(key=lambda m: m.created_at)
        return found

    async def message_status(self, message_id: str) -> str:
        """queued, sent, delivered, undelivered, failed or received."""
        body = await self._request("GET", f"/messages/{message_id}")
        msg = body.get("data") if isinstance(body.get("data"), dict) else body
        return _text(msg.get("status"))

    async def a2p_status(self) -> str:
        """The workspace's A2P registration status, or "none"."""
        try:
            body = await self._request("GET", "/a2p-registration")
        except QuoError as e:
            # Quo answers "no registration yet" with a 404 whose message
            # names A2P, which reads like "not approved" if taken by its words.
            if e.kind == NOT_FOUND or e.detail == "HTTP 404":
                return "none"
            raise
        reg = body.get("data") if isinstance(body.get("data"), dict) else body
        return _text(reg.get("status")) or "unknown"

    async def test_connection(self, from_number: str) -> tuple[bool, str]:
        """Checks the key and the number. Sends nothing."""
        try:
            numbers = await self.phone_numbers()
        except QuoError as e:
            return False, str(e)
        if not numbers:
            return False, "The key works, but this Quo workspace has no phone numbers."
        if not from_number:
            return False, ("The key works. Set channels.sms.from_number to the number "
                           "Harvey should text from.")
        want = normalize_phone(from_number, "US")
        if not any(normalize_phone(n["number"], "US") == want for n in numbers):
            return False, "channels.sms.from_number isn't one of this workspace's Quo numbers."
        return True, f"Connected. {len(numbers)} number(s) in the workspace."
