"""Texts through Quo: warm contacts only, every text approved by a human.

Covers the number normalizer, the Quo client against a mock transport, the
outbox channel split (email sending never touches a text), the text gate,
and the Texter's read and send paths with a fake Quo. All numbers are
fictional (+1 xxx 555-01xx), all people invented.
"""

import json

import httpx
import pytest

from harvey.agents.texter import HOLD_KEY, READ_CURSOR_KEY, Texter, is_carrier_stop
from harvey.gate import STOP_LINE, pre_text_check, text_content_problems
from harvey.integrations import quo
from harvey.integrations.quo import QuoClient, QuoError
from harvey.models.prospect import Prospect
from harvey.phones import normalize_phone
from harvey.state import Company
from tests.fake_quo import FakeQuo, text
from tests.test_outbox_native import (  # noqa: F401  (fixture)
    Cfg, Env, FakeProvider, StubBrain, _now_iso, make_sender, state,
)

LINE = "+12895550100"      # our Quo number
JANE = "+12895550142"      # a prospect's mobile
STRANGER = "+12895550199"


class TCfg(Cfg):
    class channels(Cfg.channels):
        class sms:
            enabled = True
            provider = "quo"
            from_number = LINE
            max_daily_texts = 20
            default_country = "CA"

    class usage:
        class quiet_hours:
            timezone = "America/Toronto"


async def _no_sleep(_seconds):
    return None


async def _always_day(_prospect):
    return True


def make_texter(state, intent="question", client=None):
    from harvey.agents.handler import Handler
    config = TCfg()
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=config, env=Env())
    handler.provider = FakeProvider()
    texter = Texter(brain=StubBrain(intent=intent), state=state, config=config, env=Env(),
                    handler=handler, client=client or FakeQuo(), sleep=_no_sleep)
    texter._daytime_for = _always_day
    return texter


async def _jane(state, phone="289-555-0142", company_id="", status="contacted"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        company_id=company_id, email="jane@acme.example", email_status="verified",
        email_verified=True, status=status, phone=phone,
    ))


async def _reading(state):
    await state.set_setting(READ_CURSOR_KEY, "2026-11-19T00:00:00")


async def _queue_text(state, pid, phone=JANE, body="Thursday at 2 works. See you then.",
                      status="approved"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="", to_phone=phone, channel="sms", kind="reply",
        subject="", body=body, send_at=_now_iso(), status=status, provider="quo")


async def _row(state, item_id):
    return await state.get_outbox_item(item_id)


# ── numbers ──


@pytest.mark.parametrize("raw,country,want", [
    ("(289) 555-0142", "CA", JANE),
    ("289.555.0142 ext 12", "CA", JANE),
    ("1 289 555 0142", "US", JANE),
    ("+1 289 555 0142", "GB", JANE),
    ("00 44 20 7946 0000", "CA", "+442079460000"),
    ("555-0142", "CA", ""),
    ("020 7946 0000", "GB", ""),        # a local format outside +1 isn't guessed
    ("", "CA", ""),
    ("+1 289 555 01", "CA", ""),
])
def test_normalize_phone(raw, country, want):
    assert normalize_phone(raw, country) == want


@pytest.mark.parametrize("body,stop", [
    ("STOP", True), ("stop.", True), ("Unsubscribe", True), ("opt out", True),
    ("please stop texting me", False), ("Stop by Thursday?", False),
])
def test_carrier_stop_words(body, stop):
    assert is_carrier_stop(body) is stop


# ── the Quo client ──


def _client(handler, sleeps=None):
    async def sleep(s):
        if sleeps is not None:
            sleeps.append(s)
    return QuoClient("test-key", transport=httpx.MockTransport(handler), sleep=sleep)


@pytest.mark.asyncio
async def test_send_text_sends_one_recipient_with_the_version_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["version"] = request.headers.get("Quo-Api-Version")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"data": {"id": "ACtest1", "conversationId": "CN1",
                                                  "status": "queued"}})

    result = await _client(handler).send_text("PNtest0001", JANE, "Hello there")
    assert result.ok and result.message_id == "ACtest1" and result.thread_ref == "CN1"
    assert seen["auth"] == "test-key"            # no "Bearer"
    assert seen["version"] == quo.API_VERSION
    assert seen["body"] == {"from": "PNtest0001", "to": [JANE], "content": "Hello there"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,kind", [
    (400, {"title": "User Opted Out", "message": "User has opted out of receiving messages"},
     quo.OPTED_OUT),
    (400, {"title": "A2P Registration Not Approved", "message": "x"}, quo.A2P_NOT_APPROVED),
    (403, {"title": "A2P 10DLC Daily Message Cap Reached", "message": "x"}, quo.DAILY_CAP),
    (400, {"title": "Not TFN Registered", "message": "x"}, quo.TOLL_FREE_UNVERIFIED),
    (402, {"title": "Not Enough Credits", "message": "x"}, quo.NO_CREDITS),
    (401, {"message": "Unauthorized"}, quo.AUTH),
])
async def test_send_errors_are_named(status, body, kind):
    client = _client(lambda r: httpx.Response(status, json=body))
    with pytest.raises(QuoError) as e:
        await client.send_text("PNtest0001", JANE, "Hello")
    assert e.value.kind == kind


@pytest.mark.asyncio
async def test_rate_limit_backs_off_then_succeeds():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"message": "Too many requests"})
        return httpx.Response(200, json={"data": [{"id": "PNtest0001", "phoneNumber": LINE}]})

    numbers = await _client(handler, sleeps).phone_numbers()
    assert numbers[0]["number"] == LINE and len(calls) == 3 and len(sleeps) == 2


@pytest.mark.asyncio
async def test_incoming_messages_follow_pages_and_come_oldest_first():
    pages = {
        None: {"data": [{"id": "AC2", "from": JANE, "text": "second",
                         "createdAt": "2026-11-19T15:02:00Z"}], "nextCursor": "p2"},
        "p2": {"data": [{"id": "AC1", "from": JANE, "text": "first",
                         "createdAt": "2026-11-19T15:01:00Z"}], "nextCursor": None},
    }
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("after")])

    from datetime import datetime
    got = await _client(handler).incoming_messages("PNtest0001", datetime(2026, 11, 19))
    assert [m.body for m in got] == ["first", "second"]
    assert seen[0]["direction"] == "incoming" and seen[0]["phoneNumberId"] == "PNtest0001"
    assert seen[0]["createdAt[gt]"].startswith("2026-11-19T00:00:00")


@pytest.mark.asyncio
async def test_no_a2p_registration_reads_as_none():
    client = _client(lambda r: httpx.Response(
        404, json={"title": "Not Found", "message": "No A2P registration found for this workspace"}))
    assert await client.a2p_status() == "none"


@pytest.mark.asyncio
async def test_send_text_refuses_more_than_a_number():
    client = _client(lambda r: pytest.fail("nothing should be sent"))
    assert not (await client.send_text("PNtest0001", "289-555-0142", "Hi")).ok
    assert not (await client.send_text("PNtest0001", JANE, "x" * 1601)).ok


# ── the outbox channel ──


@pytest.mark.asyncio
async def test_a_text_needs_a_number_and_no_email(state):
    with pytest.raises(ValueError):
        await state.add_outbox_item(prospect_id="p", to_email="a@b.example", to_phone=JANE,
                                    channel="sms", subject="", body="b", send_at=_now_iso())
    with pytest.raises(ValueError):
        await state.add_outbox_item(prospect_id="p", to_email="", channel="sms",
                                    subject="", body="b", send_at=_now_iso())


@pytest.mark.asyncio
async def test_email_queries_never_see_texts(state):
    pid = await _jane(state)
    tid = await _queue_text(state, pid)
    await state.update_outbox_item(tid, status="sent", sent_at=_now_iso())
    await _queue_text(state, pid, body="Another one")
    assert await state.get_outbox(status="approved") == []
    assert len(await state.get_outbox(status="approved", channel=None)) == 1
    assert len(await state.get_outbox(status="approved", channel="sms")) == 1
    assert await state.count_outbox_sent() == 0
    assert await state.count_outbox_sent_today() == 0
    assert await state.count_outbox_sent_today(channel="sms") == 1


@pytest.mark.asyncio
async def test_approve_all_leaves_texts_for_one_by_one(state):
    pid = await _jane(state)
    tid = await _queue_text(state, pid, status="pending_review")
    eid = await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.example",
                                      subject="s", body="b", send_at=_now_iso())
    assert await state.approve_outbox() == 1
    assert (await _row(state, eid))["status"] == "approved"
    assert (await _row(state, tid))["status"] == "pending_review"
    assert await state.approve_outbox(tid) == 1


@pytest.mark.asyncio
async def test_existing_rows_are_email_after_the_migration(state):
    eid = await state.add_outbox_item(prospect_id="p", to_email="a@b.example",
                                      subject="s", body="b", send_at=_now_iso())
    row = await _row(state, eid)
    assert row["channel"] == "email" and row["to_phone"] == ""


@pytest.mark.asyncio
async def test_the_email_sender_never_sends_a_text(state):
    pid = await _jane(state)
    tid = await _queue_text(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    assert provider.sent == []
    assert (await _row(state, tid))["status"] == "approved"


# ── consent ──


@pytest.mark.asyncio
async def test_revoked_consent_stays_revoked_unless_you_restore_it(state):
    assert await state.grant_sms_consent(JANE, "p1", "inbound_text", "hi")
    assert await state.revoke_sms_consent(phone=JANE, reason="STOP") == 1
    assert not await state.grant_sms_consent(JANE, "p1", "inbound_text", "hi again")
    assert (await state.get_sms_consent(JANE))["status"] == "revoked"
    assert await state.grant_sms_consent(JANE, "p1", "manual", "asked me at the event")
    assert (await state.get_sms_consent(JANE))["status"] == "granted"


@pytest.mark.asyncio
async def test_revoking_cancels_queued_texts(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    tid = await _queue_text(state, pid)
    await state.revoke_sms_consent(prospect_id=pid, reason="they opted out")
    assert (await _row(state, tid))["status"] == "cancelled"


# ── the text gate ──


def _p(status="replied"):
    return Prospect(first_name="Jane", last_name="Doe", email="jane@acme.example", status=status)


GRANTED = {"phone": JANE, "status": "granted"}


def test_gate_passes_a_plain_consented_text():
    assert pre_text_check(JANE, "Thursday at 2 works.", prospect=_p(), consent=GRANTED)


@pytest.mark.parametrize("kwargs,why", [
    ({"consent": None}, "no consent"),
    ({"consent": {"phone": JANE, "status": "proposed"}}, "no consent"),
    ({"consent": {"phone": JANE, "status": "revoked"}}, "no consent"),
    ({"prospect": _p("opted_out")}, "opted out"),
    ({"do_not_contact": ["acme.example"]}, "do-not-contact"),
    ({"body": "x" * 481}, "too long"),
    ({"body": "Hi {{first_name}}"}, "merge tags"),
    ({"body": "Act now, limited time"}, "banned"),
    ({"to_phone": "289-555-0142"}, "invalid phone"),
])
def test_gate_blocks(kwargs, why):
    args = {"to_phone": JANE, "body": "Thursday at 2 works.", "prospect": _p(),
            "consent": GRANTED}
    args.update(kwargs)
    result = pre_text_check(args.pop("to_phone"), args.pop("body"), **args)
    assert not result and any(why in r for r in result.reasons)


def test_gate_blocks_a_link_that_isnt_yours():
    class C(Cfg):
        pass
    problems = text_content_problems("Details at https://elsewhere.example/x", C())
    assert any("isn't yours" in p for p in problems)
    assert text_content_problems("Details at https://x.co/pricing", C()) == []


# ── reading texts ──


@pytest.mark.asyncio
async def test_the_first_read_starts_from_now(state):
    texter = make_texter(state)
    texter.client.inbound = [text("AC1", JANE, "old text")]
    assert await texter.read() is True
    assert await state.get_setting(READ_CURSOR_KEY)
    assert texter.client.read_calls == 0


@pytest.mark.asyncio
async def test_a_text_from_a_prospect_is_consent_and_gets_a_draft(state):
    pid = await _jane(state)
    await _reading(state)
    texter = make_texter(state, intent="question")
    texter.client.inbound = [text("AC1", JANE, "Do you do airport runs on Sundays?")]
    assert await texter.read() is True

    consent = await state.get_sms_consent(JANE)
    assert consent["status"] == "granted" and consent["source"] == "inbound_text"
    [draft] = await state.get_outbox(status="pending_review", channel="sms")
    assert draft["to_phone"] == JANE and draft["to_email"] == "" and draft["kind"] == "reply"
    [convo] = await state.get_conversations_by_status("open")
    assert convo.prospect_id == pid and convo.thread[-1].channel == "sms"
    # Read once: the same text isn't drafted twice.
    assert await texter.read() is True
    assert len(await state.get_outbox(status="pending_review", channel="sms")) == 1


@pytest.mark.asyncio
async def test_a_text_stops_the_cold_email_to_their_company(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await _jane(state, company_id=cid)
    bob = await state.add_prospect(Prospect(
        first_name="Bob", last_name="Roe", company="Acme", company_id=cid,
        email="bob@acme.example", email_status="verified", status="contacted"))
    follow_up = await state.add_outbox_item(
        prospect_id=bob, to_email="bob@acme.example", subject="s", body="b",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=2)
    await _reading(state)
    texter = make_texter(state)
    texter.client.inbound = [text("AC1", JANE, "What would this cost us?")]
    await texter.read()
    assert (await _row(state, follow_up))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_unknown_number_is_never_answered(state):
    await _jane(state)
    await _reading(state)
    texter = make_texter(state)
    texter.client.inbound = [text("AC1", STRANGER, "Hi, who is this?")]
    assert await texter.read() is True
    assert await state.get_outbox(status="pending_review", channel=None) == []
    assert await state.get_sms_consent(STRANGER) is None
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT action_type FROM actions") as cursor:
            types = [r[0] for r in await cursor.fetchall()]
    assert "text_unknown" in types


@pytest.mark.asyncio
async def test_a_front_desk_number_shared_by_two_people_is_not_guessed(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example",
                                          phone="(289) 555-0150"))
    from harvey.models.conversation import Conversation
    for first in ("Ann", "Bob"):
        p = await state.add_prospect(Prospect(first_name=first, company_id=cid,
                                              email=f"{first.lower()}@acme.example"))
        await state.add_conversation(Conversation(id="", prospect_id=p))
    assert await state.find_prospects_by_phone("+12895550150") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["STOP", "Please remove me from your list"])
async def test_stop_by_text_ends_texts_and_email(state, body):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    queued = await _queue_text(state, pid)
    email = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.example", subject="s", body="b",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=2)
    await _reading(state)
    texter = make_texter(state, intent="question")
    texter.client.inbound = [text("AC1", JANE, body)]
    await texter.read()

    assert (await state.get_sms_consent(JANE))["status"] == "revoked"
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await _row(state, queued))["status"] == "cancelled"
    assert (await _row(state, email))["status"] == "cancelled"
    assert await state.get_outbox(status="pending_review", channel="sms") == []


@pytest.mark.asyncio
async def test_a_quo_outage_holds_email_but_a_bad_key_does_not(state):
    await _reading(state)
    texter = make_texter(state)
    texter.client.fail_read = QuoError(quo.NO_ANSWER)
    assert await texter.read() is False
    texter.client.fail_read = QuoError(quo.AUTH)
    assert await texter.read() is True
    assert "Quo" in texter.last_read_problem


# ── sending texts ──


@pytest.mark.asyncio
async def test_an_approved_text_goes_with_the_stop_line_first_time_only(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    first = await _queue_text(state, pid)
    texter = make_texter(state)
    await texter.send()
    second = await _queue_text(state, pid, body="Perfect, booked.")
    await texter.send()

    sent = texter.client.sent
    assert [s["to"] for s in sent] == [JANE, JANE] and sent[0]["from"] == "PNtest0001"
    assert sent[0]["body"].endswith(STOP_LINE) and STOP_LINE not in sent[1]["body"]
    assert (await _row(state, first))["status"] == "sent"
    assert (await _row(state, second))["message_id"] == "ACtest0002"
    assert await state.count_outbox_sent_today() == 0          # not an email


@pytest.mark.asyncio
async def test_a_text_without_consent_is_blocked(state):
    pid = await _jane(state)
    tid = await _queue_text(state, pid)
    texter = make_texter(state)
    await texter.send()
    assert texter.client.sent == []
    row = await _row(state, tid)
    assert row["status"] == "failed" and "no consent" in row["error"]


@pytest.mark.asyncio
async def test_a_pending_text_never_goes(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    await _queue_text(state, pid, status="pending_review")
    texter = make_texter(state)
    await texter.send()
    assert texter.client.sent == []


@pytest.mark.asyncio
async def test_a2p_not_approved_holds_every_text(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    tid = await _queue_text(state, pid)
    texter = make_texter(state)
    texter.client.fail_send = QuoError(quo.A2P_NOT_APPROVED)
    await texter.send()
    assert (await _row(state, tid))["status"] == "approved"
    assert "A2P" in json.loads(await state.get_setting(HOLD_KEY))["reason"]
    texter.client.fail_send = None
    await texter.send()                       # held for an hour: nothing tried
    assert texter.client.sent == []


@pytest.mark.asyncio
async def test_quo_saying_opted_out_revokes_consent(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    tid = await _queue_text(state, pid)
    texter = make_texter(state)
    texter.client.fail_send = QuoError(quo.OPTED_OUT)
    await texter.send()
    assert (await _row(state, tid))["status"] == "failed"
    assert (await state.get_sms_consent(JANE))["status"] == "revoked"


@pytest.mark.asyncio
async def test_the_kill_switch_and_the_daily_cap_hold_texts(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    await _queue_text(state, pid)
    texter = make_texter(state)
    await state.set_setting("sending_paused", "paused by you")
    await texter.send()
    assert texter.client.sent == []
    await state.delete_setting("sending_paused")
    texter.config.channels.sms.max_daily_texts = 0
    try:
        await texter.send()
        assert texter.client.sent == []
    finally:
        texter.config.channels.sms.max_daily_texts = 20


@pytest.mark.asyncio
async def test_texts_wait_for_the_recipients_daytime(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    tid = await _queue_text(state, pid)
    texter = make_texter(state)

    async def night(_p):
        return False
    texter._daytime_for = night
    await texter.send()
    assert texter.client.sent == [] and (await _row(state, tid))["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["America/Toronto", "Asia/Tokyo", "Europe/Oslo"])
async def test_daytime_is_read_on_the_recipients_clock(state, zone):
    import pytz
    from datetime import datetime

    from harvey.agents.texter import TEXT_HOURS
    pid = await _jane(state)
    prospect = await state.get_prospect(pid)
    prospect.timezone = zone
    texter = make_texter(state)
    del texter._daytime_for                  # the real check, not the stand-in
    hour = datetime.now(pytz.timezone(zone)).hour
    assert await texter._daytime_for(prospect) is (TEXT_HOURS[0] <= hour < TEXT_HOURS[1])


@pytest.mark.asyncio
async def test_undelivered_texts_are_marked(state):
    pid = await _jane(state)
    await state.grant_sms_consent(JANE, pid, "inbound_text")
    tid = await _queue_text(state, pid)
    texter = make_texter(state)
    await texter.send()
    from datetime import datetime, timedelta, timezone
    earlier = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)).isoformat()
    await state.update_outbox_item(tid, sent_at=earlier)
    texter.client.statuses["ACtest0001"] = "undelivered"
    await texter._check_delivery()
    assert "couldn't deliver" in (await _row(state, tid))["error"]


@pytest.mark.asyncio
async def test_texting_off_does_nothing(state):
    texter = make_texter(state)
    texter.config.channels.sms.enabled = False
    try:
        assert await texter.read() is True
        await texter.send()
        assert texter.client.read_calls == 0 and texter.client.sent == []
    finally:
        texter.config.channels.sms.enabled = True
