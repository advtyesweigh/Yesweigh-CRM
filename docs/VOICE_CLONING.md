# Employee Voice Cloning (Sarvam Instant Voice Cloning)

Admin feature for YesWeigh CRM: clone **employee** Evolution WhatsApp voices and optionally send TTS replies as voice notes.

## Consent & safety

- Samples are **outbound** agent voice notes only (never customer inbound).
- Create / delete / map `voice_id`: **admin or management** only.
- Customer send requires **per-instance** `voice_tts_enabled` **and** an explicit inbox confirm (`confirm_send`).
- Do **not** enable auto-reply voice blasts; this path is manual / opt-in.
- Sarvam commercial use of cloned audio has consent requirements — see [Sarvam Commercial Licensing](https://docs.sarvam.ai) / Instant Voice Cloning docs.

## Official API (no dashboard scraping)

| Step | Endpoint |
|------|----------|
| Create saved clone | `POST https://api.sarvam.ai/voices/create` (multipart: `file`, `name`, `language`) |
| TTS with clone | `POST https://api.sarvam.ai/voices/clone` (`voice_id` + `text`) |
| List | `GET https://api.sarvam.ai/voices` |
| Delete | `DELETE https://api.sarvam.ai/voices/delete/{voice_id}` |

Auth header: `api-subscription-key` (same `SARVAM_API_KEY` as STT).

Reference audio: clean single speaker, ideally **10–15 seconds** (OPUS/OGG from WhatsApp is accepted; WAV preferred). Create allows up to 50 MB; inline `ref_audio` on generate is capped at 15 s / 10 MB — we always use saved `voice_id`.

## CRM mapping

On `whatsapp_instances`:

- `sarvam_voice_id` — Sarvam `svc-…` id  
- `sarvam_voice_name`, `sarvam_voice_sample_url`, `sarvam_voice_sample_message_id`, `sarvam_voice_created_at`  
- `voice_tts_enabled` — default **false**

## UI

**Settings → Voice Clones**

1. Show samples (duration-scored outbound clips).  
2. Create / recreate clone (calls Sarvam).  
3. Preview TTS (audio only — **not** sent on WhatsApp).  
4. Toggle **Allow send as clone voice**.  

**Inbox:** when TTS is enabled for that line and a clone exists, the composer shows **Send as voice with my clone** (confirm dialog).

## Cost (pay-as-you-go, excl. GST)

From Sarvam pricing docs:

- Instant Voice Cloning TTS: **₹2.00 / 1,000 characters** of `text`.  
- Creating / listing / deleting saved voices is **not** billed per character.  
- STT / translate paths are unchanged.

## Bootstrap script

On the API container / VPS after deploy:

```bash
docker exec yesweigh-crm-api python -m app.bootstrap_voice_clones
```

Creates clones for Biju / Visakh / Shibin from best outbound samples. Leaves `voice_tts_enabled=false`.

## Manual dashboard fallback

If API create fails (tier limit, quality rejection):

1. Open Sarvam Content Studio → Voice Cloning / My voices.  
2. Upload a 10–15 s employee outbound clip.  
3. Copy `voice_id` (`svc-…`).  
4. Settings → Voice Clones → or `PATCH /api/voice-clones/instances/{id}` with `{ "sarvam_voice_id": "svc-…" }`.
