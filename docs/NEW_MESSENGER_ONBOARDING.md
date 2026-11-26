# New-messenger WhatsApp onboarding

Automation for **truly new** inbound WhatsApp contacts on YesWeigh CRM.

## Included instances

| Provider | Lines | Included |
|----------|-------|----------|
| `evolution` | Evolution GO personal staff phones (Biju / Shibin / Visakh, etc.) | Yes |
| `meta_cloud` | Care | Yes |

Phone-keyed state (`whatsapp_new_messenger_onboarding.phone_key`) is shared across instances: first live inbound wins; later lines do not re-ask. Prefer Evolution staff + Care when both receive the same new number — both are eligible; send dedupe prevents duplicate asks.

Not included: any other providers; groups / channels; spam-flagged threads; history-sync dumps older than ~15 minutes.

## Detection

On each live inbound DM (async after Evolution / Care webhook commit):

1. Skip if phone matches **dealer** or **lead** (last-10 digit match).
2. Skip if prior inbound DM history exists for that phone (messages older than ~2 minutes — simultaneous Care+Evolution first messages do not count).
3. Otherwise start the state machine.

## State machine

`new` (empty) → `asked_role` → `asked_card` → `lead_created`  
            ↘ `customer`  
         or → `skipped` (known dealer/lead/history)

Idempotent: terminal steps never re-ask.

## Message copy

- **Role:** `Hi! Welcome to YesWeigh. Are you a weighing scale *dealer* or a *customer*? Please reply with Dealer or Customer.`
- **Card:** `Thanks! Please share a photo of your *business card* so we can add your details as a lead.`
- **Customer ack / lead saved:** short thank-you lines (see `whatsapp_new_messenger.py`).

## Lead fields (card OCR)

Reuses `visiting_card.extract_visiting_card` / `build_lead_prefill`:

| Lead field | Source |
|------------|--------|
| name | business_name or contact person |
| company | business_name |
| phone | card phones[0] or WA number |
| contact_person | card names |
| address, city, district, state, pin | OCR + Places fill |
| source | `Visiting Card` |
| business_type | `Weighing Equipment Dealer` |
| product_category | `Electronic Weighing Scales` |
| tags | `Visiting Card, New Messenger, Dealer` |
| notes | tagline / extra phones / OCR dump + auto note |

## Admin visibility

- Conversation: `onboarding_step`, `onboarding_at`, `onboarding_lead_id`, `notes_summary` line, tag.
- Detail API: `onboarding` object with step + recent log.
- Inbox UI: `Auto: <step>` badge on the Customer profile card.
