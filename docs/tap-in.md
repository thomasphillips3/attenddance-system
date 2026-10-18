# Tap-In

Dancers check in and out by tapping an NFC key fob or card on a staff phone. This doc covers what to buy, how the pieces fit, how to set up tags, and how to roll it out.

## How it works

Each tag holds one thing: a link like `https://attenddance.fly.dev/t/Xq3...` with a random code. No name, no student ID. The database stores only a hash of the code, so a leaked database can't be turned into working tags.

There are three ways to read a tag:

| Station | How a tap works | Best for |
|---|---|---|
| **iPhone** (XS or newer) | Hold the phone's top edge to the tag, tap the banner, and Safari opens the link. The page records the tap and shows the result full screen. | A teacher at the classroom door |
| **Android phone or tablet** (Chrome) | Open Tap-In > Start a tap station and tap Start once. Dancers tap one after another with no banner. Taps queue on the phone if Wi-Fi drops. | Busy drop-off at the front desk |
| **Laptop or tablet + USB reader** | Same station page. The reader types the chip's serial number. | A fixed lobby station |

What a tap does:

- **First tap** during a class window (by default 30 minutes before start until the end) checks the dancer in. Several classes today resolve to one, the same way every time.
- **A repeat read** within 10 seconds (the default) is ignored.
- **A tap 5+ minutes later** (the default) checks them out of that same class.

The window, the repeat-read delay and the check-out delay are settings an admin can change under **Tap-In > Settings**. The numbers above are the defaults, so check Settings for the current values if taps behave differently than described.
- **Back-to-back classes**: a dancer who taps in for ballet and out after jazz is marked present for both.
- **A tap outside any class window** shows a class picker and records nothing until staff pick.
- **Undo** and **Put back in class** are on every result screen.
- **Staff badges**: "Forms needed" for an unsigned waiver, and "Check account" (admins only) when the household owes money. Never a dollar amount, and neither ever blocks a check-in.
- **Parent notices**: an email when the dancer checks in and out. Texts are opt-in from the portal and need a code texted to the parent's phone first.

A tag link opened by anyone who isn't signed-in staff shows a generic "this is a studio tag, please return it" page. That page is the same for a real, unknown or revoked tag.

## What to buy

Buy **NTAG213** tags if you can find a trusted source. iPhones, Android phones and USB readers can all read them, and 144 bytes is plenty for the link. **NTAG215 also works** with the app (it has more memory than the link needs), and it is what most Amazon fob and card listings sell. NTAG215 listings sold for Amiibo cloning are the ones to be careful of, since counterfeit chips turn up there, so check every batch with TagInfo. Skip NTAG216, and skip MIFARE Classic and 125 kHz tags (iPhones can't read those two).

Prices are from GoToTags on 2026-10-14, before shipping.

| Step | Item | Qty | Price |
|---|---|---|---|
| 1. Samples | NFC Key Fob, NTAG213, red (SKU TRKBRTUL2M) | 10 | $0.51 each |
| 1. Samples | Glossy White PVC NFC Card, NTAG213, CR80 | 10 | $0.30 each |
| 2. Bulk, after samples pass | White PVC card, NTAG213, 200-pack | 200 | $47.98 |
| 2. Bulk, after samples pass | Key fob, NTAG213 | 100 | $0.43 each |
| Optional | ACS ACR1281U-C2 USB reader | 1 | $40.79 |

- Fobs for the youngest dancers (clip to the dance bag). Cards for teens, adults and staff.
- At least one **Android phone** is needed to write tags from the app. An iPhone can write them with the free **NXP TagWriter** app.
- Check every sample with the free **NXP TagInfo** app (originality signature) before the bulk order. On Amazon, only buy listings that say genuine NXP NTAG213, and check the first few with TagInfo.
- Custom printed cards with the logo are possible (GoToTags, 15-20 business days, quote needed). Start with blank ones.

## Before any tag is locked: pick the permanent address

Every tag carries the site address. Locking a tag (making it read-only) is permanent, so the address on a locked tag can never change. Before locking anything:

1. Decide the address the studio will keep for years: `attenddance.fly.dev`, or a custom domain.
2. An admin saves it in **Tap-In > Settings > Permanent tag address**. Until then the app refuses to lock any tag.
3. If the address ever changes, keep the old one redirecting to the new one, or every locked tag stops working.

Pilot tags stay unlocked. They can be rewritten with a new address any time.

## Setting up tags

From a dancer's page, open **Check-in Tags > Set up a tag**:

- **Android (Chrome)**:
  1. Tap Start and hold a blank tag to the back of the phone.
  2. The app checks the tag isn't someone else's, writes the link, then asks you to tap again to confirm it saved.
- **iPhone or desktop**:
  1. Choose "Write it with an app" to get the tag's link (shown once).
  2. Write the link with NXP TagWriter.
  3. Tap the tag on a signed-in staff phone and choose **Finish setup**.
- **USB reader only**: choose "Scan on the USB reader" and tap the tag. That tag gets no link, so it works on the USB station and the Android station (which reads its serial number) but not on an iPhone. Use a blank tag: setup doesn't erase what's already on it, and an old link on a reused tag can still open on a phone and makes the Android station go by that link instead of the serial number.

Write the tag's label (like `T-0014`) on its box or a sticker. Never write the dancer's name on the tag.

**Lost tag**: on the dancer's page, choose **Replace**. That marks the old tag lost (it stops working immediately) and starts setting up the new one. A found tag can be reactivated.

**Tap-In > Dancers without a tag** lists everyone who still needs one.

## USB reader setup (ACS ACR1281U-C2)

The reader acts like a keyboard: it types the chip serial and presses Enter. The station page listens for that burst anywhere on the page, so no box needs to be selected.

Configure it with the ACS configuration tool:

| Setting | Value |
|---|---|
| Output | Hexadecimal |
| Case | Uppercase |
| Byte order | Normal (not reversed) |
| Separator | None |
| Terminator | Enter |
| UID length | Full (7 bytes for NTAG213 = 14 hex characters) |

**Bench test** before relying on it:

1. Set up three tags on an Android phone.
2. Open the station page on the USB laptop and tap each tag on the reader.
3. Each should show the right dancer. If you get "Tag not recognized", the reader is probably typing decimal, a reversed byte order, or only 4 bytes. Fix the settings above.

Cheap no-name keyboard-wedge readers often only output 4 bytes as 10 decimal digits. Those can't read NTAG213 properly, so don't use them.

## Staff phone checklist

**iPhone**
1. Sign in to the site in **Safari** and check "Remember me". Tag links open in Safari, and the Home Screen app keeps a separate sign-in.
2. Set Auto-Lock to 5 minutes or more during drop-off. A locked phone doesn't read tags.
3. Close the Camera and Wallet. iPhones don't read tags while either is open.
4. Hold the top edge of the phone to the tag, then tap the banner.

**Android**
1. Turn on NFC in Settings.
2. In Chrome: Tap-In > Start a tap station > Start. Keep the page open; the screen stays on by itself.
3. Dancers hold the tag to the middle of the back of the phone.

If a phone shows **NOT RECORDED**, it couldn't confirm the tap, so the tap may or may not have saved (the request can reach the server and the reply get lost). Open **Today's taps** and compare the dancer's In and Out times with the tap you just made. Tap again, or mark the attendance card, only if the expected time is missing. Tapping again blindly can undo a check-in that did save: a second tap after the check-out delay (5 minutes by default, see Tap-In > Settings) checks the dancer out.

## Parent notices

- **Who gets an email:** each dancer's own parent addresses, plus the household's addresses on the family record. Household addresses count as guardians of every child in the family (the same rule balance reminders use), so they hear about all siblings. A guardian who should only hear about one child belongs on that child's parent email, not on the family. Each address gets one message naming only the children it's a contact for.
- **Email** is on by default, sent about 45 seconds after the tap so siblings who tap together get one email. A household can turn it off in the portal, or staff can switch it off on the dancer's page.
- **Texts** stay off until an admin turns them on in Tap-In settings. Only do that after the studio's Twilio number is registered for 10DLC, or carriers will block the messages. After that, a parent turns texts on in the portal by entering their mobile number and the code texted to it. Texts go only to that number, and replying STOP turns them off for good.
- Notices are skipped for adult dancers, for undone taps, and for anything that would arrive more than 20 minutes late (for example, taps replayed from an offline station). Settings has a pause switch and a daily limit. Keep the limit under the studio Gmail's daily send cap (about 500), or receipts stop for the day.

## Rollout

1. Order the samples. Run TagInfo, then test on one iPhone and one Android phone.
2. Check that the Twilio number's 10DLC registration is approved before turning texts on.
3. Pilot for 2-3 weeks with unlocked tags on one or two classes. Watch **Today's taps** for "never tapped out" and "needs a look".
4. Decide the permanent address and save it in settings.
5. Place the bulk order, set up and lock the tags, and hand them out.
6. Decide whether Fly should keep one machine running during class hours. With `min_machines_running = 0` the first tap after a quiet stretch waits for the machine to wake.

## Where the code is

| Piece | File |
|---|---|
| Tag tokens, serials, lookup | `app/tap/credentials.py` |
| Tap rules (in/out, dedup, dwell, rollover, undo) | `app/tap/service.py` |
| Staff badges | `app/tap/flags.py` |
| Notice outbox and sender thread | `app/tap/outbox.py` |
| API | `app/api/tap_routes.py` |
| Tag link page, result screen, station | `app/templates/tap/`, `app/static/js/tap-*.js` |
| Tests | `tests/test_tap.py`, `tests/test_tap_flows.py`, `tests/test_tap_notices.py` |
