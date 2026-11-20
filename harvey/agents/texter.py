"""Texter: texts to and from warm contacts through Quo.

Every heartbeat (with channels.sms.enabled and QUO_API_KEY set) it:

  1. READS new texts that reached your Quo number. A text from someone
     Harvey knows goes through the Handler exactly like an email reply:
     opt-outs, intent, stop-on-reply (their company's cold email stops),
     hand-offs. Texting first is consent to be texted back, so it's
     recorded as such. A text from a number Harvey can't match to one
     person is never answered: it shows on Today for you.
  2. SENDS the texts you approved. A text never goes on its own: every
     one waits in the Outbox, whatever channels.email.require_approval
     says. Then the last checks (gate.pre_text_check): consent granted,
     not opted out or listed, plain text, at most one link and yours,
     inside the recipient's daytime hours, under max_daily_texts. The
     first text to each number ends with "Reply STOP to unsubscribe.",
     as Quo's terms require.

A STOP (or any opt-out) by text revokes texting consent, cancels the texts
still queued for them, and opts them out of email too: one person saying
stop. Harvey never texts anyone cold. Quo's fair-use policy forbids it,
and so does this module: nothing here can create a consent from a phone
number Harvey merely found.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from harvey.brain import Brain, BrainUnavailable
from harvey.config import (
    EnvConfig, HarveyConfig, compliance_of, live_do_not_contact, operator_timezone,
)
from harvey.gate import STOP_LINE, pre_text_check
from harvey.integrations.quo import (
    AUTH, CHANNEL_HOLDS, NO_ANSWER, OPTED_OUT, RATE_LIMITED, QuoClient, QuoError,
)
from harvey.models.conversation import Message
from harvey.phones import normalize_phone
from harvey.state import StateManager

logger = logging.getLogger("harvey.texter")

KILL_SWITCH_KEY = "sending_paused"
READ_CURSOR_KEY = "quo:read_cursor"
NUMBER_ID_KEY = "quo:number_id:"
HOLD_KEY = "quo:hold"
DELIVERY_CHECKED_KEY = "quo:delivery_checked:"
# Re-read this far back each time; dedup drops what was already handled, and
# a text Quo stamped a moment late isn't skipped.
READ_OVERLAP = timedelta(minutes=5)
# A channel-wide problem (A2P not approved, no credits) is retried this often.
HOLD_RETRY = timedelta(hours=1)
MAX_TEXTS_PER_CYCLE = 5
SEND_JITTER_SECONDS = (1.0, 3.0)
# Texts go out only in these local hours of the recipient's day.
TEXT_HOURS = (8, 21)
# Check delivery once, this long after a text went out, for this long.
DELIVERY_CHECK_AFTER = timedelta(minutes=5)
DELIVERY_CHECK_FOR = timedelta(days=1)
# The keywords carriers treat as an opt-out from texts on their own.
CARRIER_STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit",
                      "revoke", "optout", "opt out", "opt-out"}
# Problems that won't fix themselves between cycles: not reading texts then
# says so on Today, but doesn't hold email (it would hold it forever).
LASTING_READ_PROBLEMS = {AUTH}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def is_carrier_stop(text: str) -> bool:
    return " ".join((text or "").lower().replace(".", " ").replace("!", " ").split()) \
        in CARRIER_STOP_WORDS


class Texter:
    def __init__(self, brain: Brain, state: StateManager, config: HarveyConfig,
                 env: EnvConfig, handler=None, client: QuoClient | None = None,
                 sleep=asyncio.sleep):
        self.brain = brain
        self.state = state
        self.config = config
        self.client = client or QuoClient(getattr(env, "quo_api_key", ""))
        self.handler = handler
        self._sleep = sleep
        self.last_read_problem = ""

    @property
    def sms(self):
        from harvey.config import SmsChannelConfig
        return getattr(getattr(self.config, "channels", None), "sms", None) or SmsChannelConfig()

    @property
    def enabled(self) -> bool:
        return bool(self.sms.enabled and self.client.is_configured() and self.sms.from_number)

    @property
    def country(self) -> str:
        return (self.sms.default_country
                or compliance_of(self.config).precleared.prospect_country or "CA")

    async def _number_id(self) -> str:
        """The Quo id of channels.sms.from_number, looked up once."""
        number = normalize_phone(self.sms.from_number, self.country)
        key = NUMBER_ID_KEY + number
        cached = await self.state.get_setting(key)
        if cached:
            return cached
        found = await self.client.resolve_number(number)
        await self.state.set_setting(key, found)
        return found

    # ── Reading ──

    async def read(self) -> bool:
        """Read and handle new texts. True when every one was handled, or
        when the problem is one only you can fix (a rejected key)."""
        if not self.sms.enabled:
            return True
        if not self.enabled:
            self.last_read_problem = ("Texting is on, but QUO_API_KEY or "
                                      "channels.sms.from_number is missing.")
            logger.warning(f"Texter: {self.last_read_problem}")
            return True
        if self.handler is None:
            raise RuntimeError("the Texter needs the Handler to read texts")
        self.last_read_problem = ""
        try:
            number_id = await self._number_id()
            now = _utcnow()
            since = _parse(await self.state.get_setting(READ_CURSOR_KEY))
            if since is None:
                # First read: texts from before texting was switched on are
                # yours to answer, not Harvey's.
                await self.state.set_setting(READ_CURSOR_KEY, now.isoformat())
                logger.info("Texter: reading texts from now on.")
                return True
            texts = await self.client.incoming_messages(number_id, since - READ_OVERLAP)
        except QuoError as e:
            self.last_read_problem = f"Quo: {e}"
            logger.error(f"Texter: couldn't read texts: {e}")
            return e.kind in LASTING_READ_PROBLEMS

        self.handler.skills = self.brain.load_skills_for_agent("handler")
        newest = since
        for text in texts:
            key = "quo:msg:" + text.id
            if await self.state.is_reply_processed(key):
                continue
            try:
                await self._handle_text(text)
            except BrainUnavailable as e:
                logger.error(f"Texter: stopping; texts are left for later ({e}).")
                await self._save_cursor(newest)
                return False
            except Exception as e:
                logger.error(f"Texter: a text couldn't be handled: {e}")
                self.last_read_problem = f"a text couldn't be handled ({e})"
            await self.state.mark_reply_processed(key)
            newest = max(newest, _parse(text.created_at) or newest)
        await self._save_cursor(newest)
        return not self.last_read_problem

    async def _save_cursor(self, when: datetime):
        await self.state.set_setting(READ_CURSOR_KEY, when.isoformat())

    async def _handle_text(self, text):
        phone = normalize_phone(text.from_number, self.country)
        body = (text.body or "").strip()
        people = await self.state.find_prospects_by_phone(phone, self.country) if phone else []
        if len(people) != 1:
            # Not one known person: never answered by Harvey. Kept for you.
            await self.state.log_action(
                action_type="text_unknown", agent="texter",
                details={"phone": phone or text.from_number, "preview": body[:200],
                         "matches": len(people)})
            logger.info("Texter: a text from a number Harvey can't match to one person. "
                        "Left for you on Today.")
            return
        prospect = people[0]
        stop = is_carrier_stop(body)
        if not stop and prospect.status != "opted_out":
            # They texted first: answering them by text is what they asked for.
            await self.state.grant_sms_consent(phone, prospect.id, "inbound_text",
                                               evidence=body[:200])
        if body:
            await self.handler._process_reply(
                phone, body, reply_uuid="",
                reply_meta={"phone": phone, "message_id": text.id,
                            "thread_ref": text.conversation_id},
                prospect=prospect, channel="sms")
        after = await self.state.get_prospect(prospect.id)
        if stop and after is not None and after.status != "opted_out":
            # A carrier keyword is an opt-out whatever the model made of it.
            await self.state.update_prospect_status(prospect.id, "opted_out")
            await self.state.cancel_pending_outbox_for_prospect(prospect.id, reason="opted out")
            await self.state.log_action(
                action_type="opt_out", agent="texter",
                details={"prospect_id": prospect.id, "rule": "carrier_keyword",
                         "why": f'They texted "{body[:40]}", a carrier opt-out keyword.',
                         "message": body[:1000]})
            after = await self.state.get_prospect(prospect.id)
        if stop or (after is not None and after.status == "opted_out"):
            await self.state.revoke_sms_consent(prospect_id=prospect.id, reason="they opted out")
            await self.state.revoke_sms_consent(phone=phone, reason="they opted out")
            logger.info("Texter: they opted out by text. No more texts or email to them.")

    # ── Sending ──

    async def send(self):
        """Send due texts you approved."""
        if not self.enabled:
            return
        if await self.state.get_setting(KILL_SWITCH_KEY):
            logger.info("Texter: sending is paused. Texts wait.")
            return
        hold = await self._current_hold()
        if hold:
            logger.info(f"Texter: texts are held ({hold}).")
            return
        try:
            number_id = await self._number_id()
        except QuoError as e:
            await self._hold(str(e))
            return
        await self._check_delivery()

        left = self.sms.max_daily_texts - await self.state.count_outbox_sent_today(channel="sms")
        if left <= 0:
            logger.info("Texter: today's text limit is reached.")
            return
        now = _utcnow()
        due = await self.state.get_outbox(status="approved", due_before=now.isoformat(),
                                          channel="sms", limit=MAX_TEXTS_PER_CYCLE * 3)
        dnc = live_do_not_contact(self.config)
        sent = 0
        for item in due:
            if sent >= min(left, MAX_TEXTS_PER_CYCLE):
                break
            prospect = await self.state.get_prospect(item["prospect_id"])
            if not await self._daytime_for(prospect):
                continue  # stays approved; goes in their morning
            phone = item.get("to_phone") or ""
            company = (await self.state.get_company(prospect.company_id)
                       if prospect is not None and prospect.company_id else None)
            check = pre_text_check(
                phone, item["body"], prospect=prospect,
                consent=await self.state.get_sms_consent(phone), config=self.config,
                do_not_contact=dnc, company_domain=getattr(company, "domain", "") or "")
            if not check:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error="gate: " + "; ".join(check.reasons)[:300])
                logger.warning(f"Texter: a text was blocked: {check.reasons}")
                continue
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            body = item["body"].strip()
            if (not await self.state.has_texted(phone, other_than=item["id"])
                    and STOP_LINE.lower() not in body.lower()):
                body = f"{body}\n\n{STOP_LINE}"
            try:
                result = await self.client.send_text(number_id, phone, body)
            except QuoError as e:
                if e.kind == OPTED_OUT:
                    await self.state.update_outbox_item(item["id"], status="failed", error=str(e))
                    await self.state.revoke_sms_consent(phone=phone, reason="opted out with Quo")
                    continue
                if e.kind in CHANNEL_HOLDS or e.kind in (RATE_LIMITED, NO_ANSWER):
                    # Not this text's fault: it waits, approved, for a later cycle.
                    await self.state.update_outbox_item(item["id"], status="approved")
                    if e.kind in CHANNEL_HOLDS:
                        await self._hold(str(e))
                    break
                await self.state.update_outbox_item(item["id"], status="failed", error=str(e)[:300])
                continue
            if not result.ok:
                await self.state.update_outbox_item(item["id"], status="failed",
                                                    error=result.error[:300])
                continue
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=_utcnow().isoformat(), body=body,
                message_id=result.message_id, thread_ref=result.thread_ref, error="")
            await self._add_to_thread(item, body)
            await self.state.log_action(
                action_type="text_sent", agent="texter",
                details={"prospect_id": item["prospect_id"], "outbox_id": item["id"]})
            sent += 1
            await self._sleep(random.uniform(*SEND_JITTER_SECONDS))
        if sent:
            logger.info(f"Texter: sent {sent} text(s).")

    async def _daytime_for(self, prospect) -> bool:
        """Whether it's daytime where they are, or failing that where you are.
        pytz, as the rest of Harvey: zoneinfo has no zone data on Windows
        without the tzdata package, and would quietly read the wrong clock."""
        import pytz

        from harvey.send_window import recipient_zone

        zone = ""
        try:
            zone = await recipient_zone(self.state, prospect, self.config)
        except Exception as e:
            logger.debug(f"Texter: no time zone for a recipient: {e}")
        local = None
        for name in (zone, operator_timezone(self.config)):
            try:
                local = datetime.now(pytz.timezone(name)) if name else None
            except pytz.UnknownTimeZoneError:
                local = None
            if local is not None:
                break
        if local is None:
            return False  # can't tell the hour: wait rather than text at night
        return TEXT_HOURS[0] <= local.hour < TEXT_HOURS[1]

    async def _add_to_thread(self, item: dict, body: str):
        if not item.get("conversation_id"):
            return
        try:
            convo = await self.state.get_conversation(item["conversation_id"])
            if convo:
                convo.thread.append(Message(sender="harvey", content=body, channel="sms"))
                await self.state.update_conversation(convo.id, thread_json=convo.thread_json())
        except Exception as e:
            logger.debug(f"Texter: couldn't add the text to the conversation: {e}")

    async def _hold(self, reason: str):
        until = (_utcnow() + HOLD_RETRY).isoformat()
        await self.state.set_setting(HOLD_KEY, json.dumps({"reason": reason[:300], "until": until}))
        logger.error(f"Texter: texts are held: {reason}")

    async def _current_hold(self) -> str:
        try:
            hold = json.loads(await self.state.get_setting(HOLD_KEY) or "null")
            until = datetime.fromisoformat(hold["until"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return ""
        if _utcnow() >= until:
            return ""  # try again; a failure sets a fresh hold
        return hold.get("reason", "")

    async def _check_delivery(self):
        """Mark texts Quo couldn't deliver, once each, so the Outbox shows it."""
        now = _utcnow()
        sent = await self.state.recent_sent_texts((now - DELIVERY_CHECK_FOR).isoformat())
        for item in sent:
            when = _parse(item.get("sent_at") or "")
            if when is None or not item.get("message_id"):
                continue
            if not (DELIVERY_CHECK_AFTER <= now - when <= DELIVERY_CHECK_FOR):
                continue
            key = DELIVERY_CHECKED_KEY + item["id"]
            if await self.state.get_setting(key):
                continue
            try:
                status = await self.client.message_status(item["message_id"])
            except QuoError:
                return
            if status in ("queued", "sent"):
                continue  # not settled yet; ask again next cycle
            await self.state.set_setting(key, status or "unknown")
            if status in ("failed", "undelivered"):
                await self.state.update_outbox_item(
                    item["id"], error=f"Quo couldn't deliver this text ({status}).")
