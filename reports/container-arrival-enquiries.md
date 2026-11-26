# Container / cargo arrival enquiries — WhatsApp CRM report

**Generated:** 2026-11-22 (IST)  
**Source:** Read-only query of live Postgres `yesweigh_crm` (`whatsapp_messages.body` + `transcript`, all YesWeigh instances)  
**CRM:** https://crm.yesweigh.in  
**Scope window used:** **16–22 Nov 2026** (clear Vallarpadam container-strike / new-lot campaign; wider 30–90d search found no earlier matching campaign)  
**Action taken:** None outbound — read-only.

---

## Executive summary

| Metric | Count |
|--------|------:|
| **Distinct contacts who enquired** (inbound ask about stock / arrival / container in a container-topic thread) | **10** |
| Of which **explicitly mentioned container / strike** in their own message | **2** |
| Conversations where staff discussed container/strike (any direction) | **25** |
| Contacts who only received **staff updates** (no clear inbound enquiry matched) | **15** |
| Common ask | When will the new container / stock reach store / be dispatchable — often after staff cite **Vallarpadam container strike** |

**Bottom line:** Dealers are chasing the **latest product lot stuck behind the Cochin/Vallarpadam container (lorry) strike**. Most English “container/strike” text in CRM is **outbound from agents** (Visakh B especially); customers mostly ask in Malayalam voice notes (“വന്നോ?”, “എപ്പോഴത്തേക്ക്?”, stock/availability) or short English (“Bench pc available?”, “Dispatch my order”).

---

## Enquirers (deduplicated by phone, ranked by most recent enquiry)

| # | Contact / dealer | Phone | Owner / WA agent | Enquiry (IST) | Language | Status | What they asked (snippet) | Confidence | Thread `public_id` |
|--:|------------------|------:|------------------|---------------|----------|--------|---------------------------|------------|--------------------|
| 1 | **Sabi Modern Machines** | 919961397915 | **Biju B** (918590601638) | 2026-11-22 10:36 | Malayalam | Open (1 unread) | “കണ്ടെയ്നർ നമുക്ക് വന്നെന്നല്ലായിരുന്നോ… എപ്പോഴത്തേക്കാവും?” | **High** — explicit container ETA | `7d02bb45-91b9-4757-9c17-fcc349c540bd` |
| 2 | **Safe Electronics** | 919747555743 | **Visakh B** (919567753252) | 2026-11-21 12:30 (also 17 Nov) | Malayalam | Open | Strike settled? containers moving? “തിങ്കളാഴ്ച എനിക്ക് സാമി നടക്കുമോ?”; earlier “പി ആർ സി വന്നോ ബെഞ്ച്?” | **High** — explicit container + Monday dispatch | `110fd3e2-a355-440c-af14-a11dd87e3ab0` |
| 3 | **Scale Point** | 919745794030 | Visakh B | 2026-11-20 15:05 | Malayalam | Open | 50 kg PRC stock/photo; then “ഇന്ന് അയക്കാനുള്ള മാർഗ്ഗവും ഇല്ല അല്ലേ?” after strike explanation | Medium–High | `75f5a0ed-e497-4705-a068-b2820babc645` |
| 4 | **National Scale Thiruvalla** | 919447783615 | Visakh B | 2026-11-20 12:43 | Malayalam | Open | “സാധനം വല്ലതും വന്നോ? വന്നിട്ടുണ്ടോ?” | Medium | `7622e77c-9dcd-40df-a3a8-26028d5228dc` |
| 5 | **Prasad Malabar Scale** | 918547508196 | Visakh B | 2026-11-19 11:00 | Malayalam | Open (1 unread) | “മെഷീൻ വന്നോ… എപ്പോഴാണ് അയക്കാൻ പറ്റുന്നത്?” | Medium | `98fb2a9c-b10f-47c2-beba-078ebd126ea4` |
| 6 | **Surya Vypin** | 919946888634 | Visakh B | 2026-11-18 10:45 | Malayalam | Open | Needs machines / “പുതിയ സ്റ്റോക്ക്”; staff later: container at port, strike hold | Medium (stock need in strike thread) | `88e15b33-7fa1-41be-b39b-4af2a4f01725` |
| 7 | **Thrass City** (dealer linked) | 919847890522 | Visakh B | 2026-11-17 17:11 | English | Open | “Redy stock ille” → staff: new container at Kochi port, clearance wait | Medium | `26d47711-352e-4aa5-8886-e15c15ddb9f5` |
| 8 | **Ashfaq Kagale** / SHRI KADASIDDESHWAR INDUSTRIES | 918880637111 | Visakh B | 2026-11-17 15:24 | English | Open | “Sir Dispatch My order” → staff container ETA updates | Medium | `4d384eba-9ad6-48c2-8078-5e2cb5efa693` |
| 9 | **Hamda New** | 919895362362 | Visakh B | 2026-11-17 14:51 | Malayalam | Open (1 unread) | “ബെഞ്ച് ബിആർസി വന്നോ?” → staff: at port, clearance + lorry strike | Medium | `a4397941-a75f-4489-b67a-147e0efccc3b` |
| 10 | **Marvel Electronics** | 919847179343 | Visakh B | 2026-11-17 14:05 | English | Open | “Bench pc available?” → staff: container arrived, ~2 days to dispatch | Medium | `e137f803-79de-4dff-ab9c-d5fba5c909d0` |

Open threads in CRM inbox via conversation id above (WhatsApp module → select thread by contact / `public_id`).

---

## Common ask (short)

1. **Has the new lot / bench / PRC / BRC arrived?** (“വന്നോ?”, stock available?)  
2. **When can you dispatch?** (today / Monday / 2 days)  
3. **Is the container strike over / settled so goods leave port?** (Safe Electronics, Sabi)  
4. Staff narrative repeated across chats: container **reached port** but **Vallarpadam container-lorry strike** blocks move to YesWeigh store; Manorama article often forwarded.

---

## Related: staff-updated dealers without a clear inbound “enquiry” hit

These phones appear in container/strike threads mainly because **agents pushed updates** (not because a keyword-matched inbound ask was found). Still useful follow-up list:

| Contact | Phone | Agent | Last topic msg (IST) |
|---------|------:|-------|----------------------|
| Gk Scale | 918086568201 | Visakh B | 2026-11-22 — “container strike over” |
| Royal Kannur | 919895339647 | Visakh B | 2026-11-21 — strike article |
| Accurate Scale | 919447244297 | Visakh B | 2026-11-21 — port arrived, store not yet |
| Nadeem Pasha FB | 919341655876 | Biju B | 2026-11-21 — container strike delay |
| Jasir Solutions | 919747393915 | Visakh B | 2026-11-21 — strike + dispatch ETA |
| Sai Traders | 919303677766 | Visakh B | 2026-11-21 — stock arriving / price |
| Alfa System | 919846500877 | Visakh B | 2026-11-20 — container + glory/lorry strike |
| Maha Enterprises Chennai | 919884019633 | Biju B | 2026-11-20 — dispatch hold for strike |
| mathascales1997 | 917306903354 | Visakh B | 2026-11-20 — strike article |
| New Scale Land | 919447868922 | Visakh B | 2026-11-20 — strike article |
| Rajeev G Bharath Scale G | 919447184264 | Visakh B | 2026-11-20 — strike article |
| Easyway | 919074147355 | Visakh B | 2026-11-16 — container not arrived yet |
| Accuratetradelinks | 919847141007 | Shibin K P | 2026-11-17 — (weaker / parts thread) |

*Excluded from “customer” lists: internal numbers such as Biju / Shalima Admin chats that only relayed the news article.*

---

## Instances searched

| Instance | Owner | Connected phone | Status | Msgs in DB (approx) |
|----------|-------|----------------:|--------|--------------------:|
| Visakh B | Visakh B | 919567753252 | Connected | 1246 |
| Biju B | Biju B | 918590601638 | Connected | 1038 |
| Shibin K P | Shibin K P | 919567933252 | Connected | 514 |
| Care (Meta Cloud) | — | 918803333444 | Connected | 414 |
| Namrata Kumari | Namrata Kumari | — | Disconnected | 0 |

Almost all container-arrival traffic is on **Visakh B** and **Biju B**.

---

## Methodology & caveats

- Matched `body` + Sarvam `transcript` (Malayalam + English) for: container/cargo/shipment/strike/arrival, കണ്ടെയ്നർ, സ്ട്രൈക്ക്/സമരം, വല്ലാർപാടം, stock/വന്നോ/എപ്പോ timing near those threads.  
- **Deduped by `customer_phone`**; ranked by latest inbound enquiry.  
- Inbound English literal `container`/`strike` was **rare**; customers ask about **goods arrival**; agents name the container/strike.  
- DB message coverage overall: ~2026-10-11 → 2026-11-22 (~3.2k messages). Campaign signal is concentrated **16–22 Nov 2026**.  
- Some “medium” rows are availability/dispatch asks that sit inside a container-strike conversation (staff answer framed as container delay).  
- Courier “ഷിപ്പ്മെന്റ് + invoice” noise (e.g. Amal ST-courier) excluded.  
- **No WhatsApp messages were sent** by this report job.

---

## Suggested next actions (human)

1. Prioritize open/unread: **Sabi Modern Machines**, **Prasad Malabar Scale**, **Hamda New**.  
2. Align agent reply script: strike status + realistic store ETA + which SKUs (bench / PRC / BRC) clear first.  
3. Optional CRM follow-up task list from the 10 enquirers + high-touch staff-update dealers (Gk, Accurate, Jasir, Safe).
