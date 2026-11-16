"""Evolution GO adapter — all remote calls stay server-side."""
from __future__ import annotations

from typing import Any

import httpx


class EvolutionService:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self, instance_token: str | None = None) -> dict[str, str]:
        # Prefer instance token for instance-scoped ops when available
        key = instance_token or self.api_key
        return {"apikey": key, "Content-Type": "application/json"}

    async def test_connection(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{self.base_url}/server/ok", headers=self._headers())
                if r.is_success:
                    return {"ok": True, "status": "Connected", "detail": r.json() if r.content else {}}
                if r.status_code in (401, 403):
                    return {"ok": False, "status": "Authentication Failed", "detail": r.text}
                return {"ok": False, "status": "Server Unavailable", "detail": r.text}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "Server Unavailable", "detail": str(exc)}

    async def create_instance(self, name: str, token: str, instance_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "token": token}
        if instance_id:
            body["instanceId"] = instance_id
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(f"{self.base_url}/instance/create", headers=self._headers(), json=body)
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "Create failed")
            return data

    async def connect(self, instance_token: str, webhook_url: str) -> dict[str, Any]:
        body = {
            "immediate": True,
            "webhookUrl": webhook_url,
            "subscribe": [
                "MESSAGE",
                "SEND_MESSAGE",
                "CONNECTION",
                "QRCODE",
                "CONTACT",
                "READ_RECEIPT",
                "PRESENCE",
            ],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{self.base_url}/instance/connect",
                headers=self._headers(instance_token),
                json=body,
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Connect failed")
            return data

    async def qr(self, instance_token: str, retries: int = 12, delay_s: float = 1.2) -> dict[str, Any]:
        """Poll until Evolution GO has a QR ready (often unavailable right after connect)."""
        import asyncio

        last_err = "QR not ready"
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(retries):
                r = await client.get(f"{self.base_url}/instance/qr", headers=self._headers(instance_token))
                data = r.json() if r.content else {}
                if r.is_success:
                    # normalize common shapes to include a usable code/image field
                    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
                    if isinstance(payload, dict):
                        code = payload.get("qrcode") or payload.get("base64") or payload.get("code") or payload.get("qr")
                        if code:
                            return {"qrcode": code, "raw": data}
                        # success body but empty qr — keep polling
                        last_err = "QR empty, waiting…"
                    else:
                        return {"qrcode": payload, "raw": data}
                else:
                    err = ""
                    if isinstance(data, dict):
                        err = str(data.get("error") or data.get("message") or "")
                    last_err = err or r.text or "QR failed"
                    # hard auth errors should not retry forever
                    if r.status_code in (401, 403):
                        raise RuntimeError(last_err)
                if attempt < retries - 1:
                    await asyncio.sleep(delay_s)
        raise RuntimeError(last_err or "QR not available yet. Click Refresh QR.")

    async def status(self, instance_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/instance/status", headers=self._headers(instance_token))
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Status failed")
            return data

    async def info(self, evolution_instance_id: str, instance_token: str | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{self.base_url}/instance/info/{evolution_instance_id}",
                headers=self._headers(instance_token),
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Info failed")
            return data

    async def disconnect(self, instance_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{self.base_url}/instance/disconnect",
                headers=self._headers(instance_token),
                json={},
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Disconnect failed")
            return data

    async def restart(self, evolution_instance_id: str, instance_token: str | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{self.base_url}/instance/forcereconnect/{evolution_instance_id}",
                headers=self._headers(instance_token),
                json={},
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Restart failed")
            return data

    async def delete(self, evolution_instance_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.delete(
                f"{self.base_url}/instance/delete/{evolution_instance_id}",
                headers=self._headers(),
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or r.text or "Delete failed")
            return data

    async def send_text(self, instance_token: str, number: str, text: str, msg_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"number": number, "text": text, "formatJid": True}
        if msg_id:
            body["id"] = msg_id
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.base_url}/send/text",
                headers=self._headers(instance_token),
                json=body,
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "Send failed")
            return data

    async def check_user(self, instance_token: str, numbers: list[str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{self.base_url}/user/check",
                headers=self._headers(instance_token),
                json={"number": numbers},
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "User check failed")
            return data

    async def get_avatar(self, instance_token: str, number: str, preview: bool = True) -> dict[str, Any]:
        # Prefer LID form — PN @s.whatsapp.net often times out on Evolution GO / WhatsApp
        candidates = [number]
        digits = "".join(c for c in (number or "") if c.isdigit())
        if digits and not str(number).endswith("@lid"):
            try:
                checked = await self.check_user(instance_token, [digits])
                users = []
                if isinstance(checked, dict):
                    raw = checked.get("data") if isinstance(checked.get("data"), list) else checked.get("users")
                    if isinstance(raw, list):
                        users = raw
                    elif isinstance(checked.get("data"), dict):
                        inner = checked["data"]
                        users = inner.get("users") if isinstance(inner.get("users"), list) else [inner]
                for u in users:
                    if not isinstance(u, dict):
                        continue
                    lid = str(
                        u.get("lid")
                        or u.get("Lid")
                        or u.get("remoteJid")
                        or u.get("RemoteJID")
                        or u.get("jid")
                        or ""
                    )
                    if lid:
                        if "@" not in lid and lid.isdigit():
                            lid = f"{lid}@lid"
                        candidates.insert(0, lid)
                        break
            except Exception:  # noqa: BLE001
                pass
            candidates.append(f"{digits}@lid")

        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=18.0) as client:
            for cand in candidates:
                try:
                    r = await client.post(
                        f"{self.base_url}/user/avatar",
                        headers=self._headers(instance_token),
                        json={"number": cand, "preview": preview},
                    )
                    data = r.json() if r.content else {}
                    if r.is_success:
                        return data
                    last_err = RuntimeError(data.get("message") or data.get("error") or r.text or "Avatar failed")
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    continue
        raise RuntimeError(str(last_err) if last_err else "Avatar failed")

    async def send_media(
        self,
        instance_token: str,
        number: str,
        *,
        media_type: str,
        url: str,
        filename: str = "",
        caption: str = "",
        msg_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "number": number,
            "type": media_type,
            "url": url,
            "formatJid": True,
        }
        if filename:
            body["filename"] = filename
        if caption:
            body["caption"] = caption
        if msg_id:
            body["id"] = msg_id
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                f"{self.base_url}/send/media",
                headers=self._headers(instance_token),
                json=body,
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "Media send failed")
            return data

    async def mark_read(self, instance_token: str, number: str, message_ids: list[str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{self.base_url}/message/markread",
                headers=self._headers(instance_token),
                json={"number": number, "id": message_ids},
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "Mark read failed")
            return data

    async def download_media(self, instance_token: str, message: dict[str, Any]) -> dict[str, Any]:
        """Download/decrypt inbound media via Evolution GO. `message` is the Message node."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{self.base_url}/message/downloadmedia",
                headers=self._headers(instance_token),
                json={"message": message},
            )
            data = r.json() if r.content else {}
            if not r.is_success:
                raise RuntimeError(data.get("message") or data.get("error") or r.text or "Media download failed")
            return data
