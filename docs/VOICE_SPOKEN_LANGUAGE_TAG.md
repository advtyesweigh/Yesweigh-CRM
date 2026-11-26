# Voice source-language tag (chat UI)

Staff see the **spoken language of the audio**, not the script of the Malayalam transcript/translation.

## Where language is stored

| Field | Table / column | Meaning |
| --- | --- | --- |
| **`spoken_language`** | `whatsapp_messages.spoken_language` | Short ISO code from Sarvam STT (`hi`, `ta`, `ml`, …). Source of the UI pill. |
| **`transcript_language`** | `whatsapp_messages.transcript_language` | Language of the **displayed** transcript after translate (often `ml` / `ml-IN`). Do **not** use for the pill. |
| **`conversation.language`** | `whatsapp_conversations.language` | Contact-level preference synced from inbound voice via `apply_voice_spoken_language`. |

Pipeline (`backend/app/sarvam_transcribe.py` → `process_transcription`):

1. Sarvam STT returns `language_code` for the audio.
2. `apply_voice_spoken_language` (`contact_spoken_language.py`) normalizes and writes `msg.spoken_language`.
3. Transcript text may then be translated to Malayalam; `transcript_language` becomes the target.

API messages already include both fields (`inbox.py` serializer: `spoken_language`, `transcript_language`). Manual retry `POST /api/whatsapp/messages/{id}/transcribe` also returns `spoken_language` / `source_language_code`.

Backfill missing tags without re-translating:

```bash
PYTHONPATH=/app python -m app.backfill_voice_spoken_language --limit 100
```

## How the tag is rendered

- Shared helper: `voiceSpokenLanguageLabel()` in `frontend/src/lib/waMessageUtils.ts` (known codes → English names; unknown → empty string).
- Pill CSS: `.wa-voice-lang` in `frontend/src/styles.css` (small teal pill next to the player).
- Surfaces:
  - Intelligence chat modal → `WaMessageBubble.tsx` (uses `m.spoken_language`).
  - WhatsApp Inbox → inline bubble in `WhatsApp.tsx` (same field + helper).

Example: Hindi audio with Malayalam transcript shows pill **Hindi** beside the mic/player; the blue transcript text stays Malayalam. If `spoken_language` is empty/unknown, no pill is shown (no guess from transcript script).
