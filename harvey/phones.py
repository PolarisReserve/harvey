"""Phone numbers in one form: E.164 ("+12895550100").

Harvey stores numbers as businesses publish them ("(289) 555-0100",
"289.555.0100 ext 2"), and Quo speaks E.164, so every comparison goes
through normalize_phone first.
"""

import re

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_NANP = {"US", "CA"}


def normalize_phone(raw, country: str = "CA") -> str:
    """``raw`` as an E.164 number ("+12895550100"), or "" if it isn't one.

    Accepts "+1 (289) 555-0100", "289-555-0100", "1 289 555 0100" and
    "00 44 20 7946 0000". A number with no country code is read as one in
    ``country``; only the US and Canada (+1) are read that way, since other
    countries' local formats need more than digits to read safely.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+\s*$", text)[0]
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        candidate = "+" + digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
    elif (country or "CA").upper() in _NANP and len(digits) == 10:
        candidate = "+1" + digits
    elif (country or "CA").upper() in _NANP and len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        return ""
    if candidate.startswith("+1") and len(candidate) != 12:
        return ""
    return candidate if E164_RE.match(candidate) else ""
