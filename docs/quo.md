# Texts through Quo

Harvey can read and answer texts sent to a [Quo](https://www.quo.com) number (formerly OpenPhone). It only texts **warm** contacts, and **every text waits for your approval**.

## Who Harvey will text

Only someone who agreed to it:

- **They texted your Quo number first.** Texting you is consent to be texted back, and Harvey records it as consent with what they wrote.
- **You recorded their consent** yourself, with a note on how they agreed. A dashboard button for this is coming. For now, consent comes only from texting first.

Harvey **never texts anyone cold**. A phone number found on a website is not consent. Quo's terms require consent before the first message to each person, plus a record of that consent. Its fair-use policy forbids "cold calling and/or messaging". Canada's CASL and the US TCPA say much the same. No setting turns cold texting on.

A text from a number Harvey can't match to exactly one person is never answered. It shows on **Today** for you to answer in Quo. One example is a company's front-desk line shared by several contacts.

## What happens to a text

A text from someone Harvey knows goes through the same steps as an email reply:

- **Opt-outs win.** STOP, UNSUBSCRIBE, CANCEL, END, QUIT, or "please remove me" revoke texting consent. They also cancel anything queued for that person and opt them out of email too: it's one person saying stop.
- **Stop-on-reply.** A text from anyone at a company stops the cold email sequence to everyone there, exactly like an email reply.
- **A draft, never a send.** Harvey drafts a short answer: under 300 characters, plain, with at most one link, and only your booking link. The draft waits in the **Outbox** with a *Text* tag. Approve, edit or reject it there. Approve-all skips texts, so each one is read and approved on its own. There is no autopilot for texts, whatever `channels.email.require_approval` says.
- **Complaints and hand-offs** go to *Needs you*, as they would by email.

Before an approved text goes out, Harvey checks all of this again:

- Consent is granted.
- They haven't opted out and aren't on the do-not-contact list.
- The text is plain, under 480 characters, with at most one link, and that link is yours.
- It's daytime (08:00–21:00) where they are.
- You're under `max_daily_texts` today.

The first text Harvey sends to each number ends with "Reply STOP to unsubscribe.", as Quo's terms require. `harvey sending pause` holds texts too.

Texts are read every heartbeat right after email replies. If Quo can't be reached, queued email waits that cycle too, as it does when the mailbox can't be read. A text saying "stop" must be read before a follow-up email goes out. A rejected API key doesn't hold email: it shows on Today until you fix it.

## Set it up

1. **Pick the number.** In Quo, use a **local** number to text from. A toll-free number needs its own verification before it can text. Texting **US** numbers also needs the workspace's A2P 10DLC registration, which you complete in Quo.
2. **Create an API key.** In Quo, open Workspace Settings → API and create a key for Harvey. Put it in `.env` and nowhere else (not in a chat):

   ```
   QUO_API_KEY=...
   ```

3. **Turn it on** in `harvey.yaml` (or `harvey.local.yaml` if you trained Harvey):

   ```yaml
   channels:
     sms:
       enabled: true
       from_number: "+12895550100"   # your Quo number, E.164
       max_daily_texts: 20
       default_country: "CA"         # for numbers saved without +1
   ```

4. **Check it.** This sends nothing:

   ```bash
   harvey quo numbers   # the workspace's numbers
   harvey quo test      # key, number and A2P status
   ```

Harvey starts reading texts from the moment texting is first switched on. Texts from before that are yours to answer.

## What Harvey reads and stores

- It reads only **incoming texts to `from_number`**, one list request per heartbeat. It polls because Quo sends webhooks only to a public web address, and Harvey runs on your computer.
- It stores texts in its local database (`data/harvey.db`), in the person's conversation, and keeps a consent record per number: where the consent came from, the message that showed it, when it was given, and when and why it was revoked.
- It sends only the texts you approved, and only to the one person each is addressed to.
- Calls, voicemails and other numbers in the workspace are untouched for now.
