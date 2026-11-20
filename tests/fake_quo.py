"""A stand-in Quo client: no test ever calls Quo.

Numbers in tests are fictional: +1 (area) 555-01xx is reserved for fiction
in the North American numbering plan.
"""

from harvey.integrations.mail_provider import SendResult
from harvey.integrations.quo import InboundText, QuoError


class FakeQuo:
    name = "quo"

    def __init__(self, numbers=None):
        self.numbers = numbers if numbers is not None else [
            {"id": "PNtest0001", "number": "+12895550100", "name": "Test line"}]
        self.inbound: list[InboundText] = []
        self.sent: list[dict] = []
        self.fail_send: QuoError | None = None
        self.fail_read: QuoError | None = None
        self.statuses: dict[str, str] = {}
        self.read_calls = 0

    def is_configured(self):
        return True

    async def phone_numbers(self):
        return list(self.numbers)

    async def resolve_number(self, e164):
        for n in self.numbers:
            if n["number"] == e164:
                return n["id"]
        raise QuoError("not_found")

    async def send_text(self, phone_number_id, to, body):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append({"from": phone_number_id, "to": to, "body": body})
        return SendResult(ok=True, message_id=f"ACtest{len(self.sent):04d}", thread_ref="CNtest")

    async def incoming_messages(self, phone_number_id, since):
        self.read_calls += 1
        if self.fail_read is not None:
            raise self.fail_read
        return list(self.inbound)

    async def message_status(self, message_id):
        return self.statuses.get(message_id, "delivered")

    async def a2p_status(self):
        return "none"

    async def test_connection(self, from_number):
        return True, "fake"


def text(msg_id, from_number, body, created_at="2026-11-19T15:00:00.000Z"):
    return InboundText(id=msg_id, from_number=from_number, body=body,
                       created_at=created_at, conversation_id="CNtest")
