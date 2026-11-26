"""Gemini adapter using the official Google Gen AI Python SDK (`google-genai`)."""
from __future__ import annotations

import base64
import json
from typing import Any

from app.ai.provider import AIProvider, ProviderResponse, ProviderUsage, ToolCallRequest, ToolSpec


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()

    async def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        max_output_tokens: int,
    ) -> ProviderResponse:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        # Official SDK: https://googleapis.github.io/python-genai/
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)

        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in tools
        ]
        tool_cfg = [types.Tool(function_declarations=declarations)] if declarations else None

        contents: list[types.Content] = []
        for msg in messages:
            role = msg.get("role") or "user"
            if role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=msg.get("name") or "tool",
                                response=msg.get("response") or {"result": msg.get("content")},
                            )
                        ],
                    )
                )
                continue
            if role == "assistant":
                role = "model"
            parts: list[types.Part] = []
            text = msg.get("content")
            if text:
                parts.append(types.Part.from_text(text=str(text)))
            # Optional inline images from /work attach (base64 + mime).
            for img in msg.get("images") or []:
                if not isinstance(img, dict):
                    continue
                raw_b64 = str(img.get("data") or "").strip()
                mime = str(img.get("mime") or "image/jpeg").strip() or "image/jpeg"
                if not raw_b64:
                    continue
                try:
                    blob = base64.b64decode(raw_b64, validate=False)
                except Exception:  # noqa: BLE001
                    continue
                if not blob or len(blob) > 3_500_000:
                    continue
                try:
                    parts.append(types.Part.from_bytes(data=blob, mime_type=mime))
                except Exception:  # noqa: BLE001
                    # Older SDK fallback
                    parts.append(
                        types.Part(
                            inline_data=types.Blob(mime_type=mime, data=blob),
                        )
                    )
            for tc in msg.get("tool_calls") or []:
                part = types.Part.from_function_call(
                    name=tc.get("name") or "",
                    args=tc.get("args") or {},
                )
                # Gemini 2.5+/3 thinking models: must echo thought_signature on functionCall parts
                # or round 2+ returns 400 INVALID_ARGUMENT.
                sig = tc.get("thought_signature")
                if sig is not None:
                    if isinstance(sig, str):
                        try:
                            sig = base64.b64decode(sig)
                        except Exception:  # noqa: BLE001
                            sig = sig.encode("latin-1", errors="ignore")
                    part.thought_signature = sig
                parts.append(part)
            if parts:
                contents.append(types.Content(role=role if role in {"user", "model"} else "user", parts=parts))

        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_output_tokens,
            tools=tool_cfg,
            temperature=0.2,
        )

        # SDK generate_content is sync; run in thread to avoid blocking event loop heavily.
        import asyncio

        def _call():
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        response = await asyncio.to_thread(_call)

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        if response.candidates:
            for part in response.candidates[0].content.parts or []:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    args = dict(fc.args) if fc.args else {}
                    sig = getattr(part, "thought_signature", None)
                    tool_calls.append(
                        ToolCallRequest(
                            id=getattr(fc, "id", None) or fc.name,
                            name=fc.name,
                            args=args,
                            thought_signature=sig if isinstance(sig, (bytes, type(None))) else bytes(sig),
                        )
                    )
                elif getattr(part, "text", None):
                    text_parts.append(part.text)

        usage_meta = getattr(response, "usage_metadata", None)
        prompt = int(getattr(usage_meta, "prompt_token_count", 0) or 0) if usage_meta else 0
        candidates = int(getattr(usage_meta, "candidates_token_count", 0) or 0) if usage_meta else 0
        thoughts = int(
            getattr(usage_meta, "thoughts_token_count", None)
            or getattr(usage_meta, "thoughts_tokens", 0)
            or 0
        ) if usage_meta else 0
        total = int(getattr(usage_meta, "total_token_count", 0) or 0) if usage_meta else 0
        if total <= 0:
            total = prompt + candidates + thoughts
        usage = ProviderUsage(
            input_tokens=prompt,
            output_tokens=candidates,
            total_tokens=total,
            thoughts_tokens=thoughts,
        )
        return ProviderResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=usage,
            raw=None,
        )


def dump_json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
