"""Chapter generation for the cut-out pipeline.

Given the transcript's timed segments, this asks a chat model to tile the whole
episode into chapters and flag the discrete advertising segments, returning the
chapter list the encode stage cuts against.

It talks to an **OpenAI-compatible Chat Completions** endpoint — the same
protocol the transcribe stage uses for its service — so it posts to a
configurable URL with the model in the request body and an optional
``Authorization: Bearer`` header, and constrains the reply with a JSON-schema
``response_format``. Any OpenAI-compatible chat service works; it defaults to
Google's Gemini endpoint (https://ai.google.dev/gemini-api/docs/openai).

The HTTP call is a thin injectable seam (``post``), so the formatting and
response-parsing logic can be tested without the network.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx

# The model is slow on a full episode transcript. We request a streamed reply,
# so the read timeout bounds the gap between chunks (which stays small while the
# model is emitting) rather than the whole generation — a non-streamed POST
# would instead have to return the entire body inside one read window.
_TIMEOUT = httpx.Timeout(30.0, read=300.0)

DEFAULT_LANGUAGE = "NZ English"


# Chapter descriptions in the configured language, full coverage, and a narrow
# definition of what counts as an ad so editorial sponsor mentions
# (intros/outros) are kept rather than cut.
def build_prompt(language: str) -> str:
    return (
        f"Analyse the following podcast transcript and generate chapters in {language}. "
        "Each line is prefixed with [START–END] timestamps in HH:MM:SS format. "
        "Return start and end as HH:MM:SS strings copied from those transcript timestamps. "
        "The chapters must cover the entire transcript: the first chapter's start equals "
        "the first line's start, the last chapter's end equals the last line's end, and "
        "chapters tile contiguously with no gaps. "
        "Set is_ad to true only for discrete advertising segments — uninterrupted "
        "sponsor reads or pre-recorded ad spots whose sole purpose is to promote a "
        "product or service. Do NOT set is_ad to true for show intros, outros, or "
        "other editorial content that merely mentions or thanks a sponsor (e.g. "
        '"you\'re listening to X, brought to you by Y"); these are part of the show '
        "even when a sponsor is named. If a chapter is a mix of intro/outro and "
        "sponsor mention, treat it as editorial (is_ad=false). "
        "Respond with raw JSON only — no markdown code fences, no commentary, no extra "
        'fields. The reply is a single JSON object with one key "chapters", an array of '
        "chapter objects. Each chapter object has exactly these four fields: "
        '"start" and "end" (HH:MM:SS strings as above), "title" (a very brief '
        'description of the chapter), and "is_ad" (a boolean).'
    )


def format_reference(context: dict | None) -> str:
    if not context:
        return ""
    lines = []
    if podcast := context.get("podcast"):
        lines.append(f"Podcast: {podcast}")
    if title := context.get("title"):
        lines.append(f"Episode title: {title}")
    if description := context.get("description"):
        lines.append(f"Episode description: {description}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "=== REFERENCE METADATA — CONTEXT ONLY, NOT A SOURCE OF CHAPTERS ===\n"
        "The details below are publisher-supplied. They are provided ONLY to help "
        "you spell names correctly and understand context. They may be inaccurate, "
        "promotional, or out of date.\n"
        "- Base every chapter ENTIRELY on the transcript that follows; it is the "
        "only source of truth for what was said and when.\n"
        "- Use this metadata ONLY as a spelling/naming guide for hosts, guests, the "
        "show, or products that actually appear in the transcript.\n"
        "- Do NOT create, title, time, or order any chapter from this metadata. If "
        "the description contains its own chapter list or timestamps, IGNORE it and "
        "derive the chapters yourself from the transcript.\n"
        "- If the metadata disagrees with the transcript, the transcript wins.\n"
        "- Do not assume anything named here was actually discussed.\n"
        f"{body}\n"
        "=== END REFERENCE METADATA ===\n"
    )


# POST a JSON body to ``url`` with ``headers`` and return the parsed JSON reply;
# injectable so tests exercise the request building and parsing without network.
ChaptersPost = Callable[[str, dict, dict], Awaitable[dict]]


class ChaptersError(RuntimeError):
    """Chapter generation failed (HTTP error, or an undecodable reply)."""


def response_format() -> dict:
    """The ``response_format`` that constrains the reply to the chapter shape.

    OpenAI-style structured output requires the root be an object, so the
    chapter array is wrapped under a ``chapters`` key.
    """
    timestamp = {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}:[0-9]{2}$"}
    chapter = {
        "type": "object",
        "properties": {
            "start": {
                **timestamp,
                "description": "Start time of the chapter in HH:MM:SS format",
            },
            "end": {
                **timestamp,
                "description": "End time of the chapter in HH:MM:SS format",
            },
            "title": {
                "type": "string",
                "description": "Very brief description of the chapter",
            },
            "is_ad": {
                "type": "boolean",
                "description": "Indicates if the chapter is an advertisement",
            },
        },
        "required": ["start", "end", "title", "is_ad"],
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "chapters",
            "schema": {
                "type": "object",
                "properties": {"chapters": {"type": "array", "items": chapter}},
                "required": ["chapters"],
            },
        },
    }


def _format_hms(seconds: float) -> str:
    """Whole-second HH:MM:SS, truncating sub-second."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _parse_hms(value: str) -> int:
    """Parse an HH:MM:SS string back to whole seconds, rejecting malformed input."""
    parts = value.split(":")
    try:
        if len(parts) != 3:
            raise ValueError
        h, m, s = (int(p) for p in parts)
        if h < 0 or not (0 <= m < 60) or not (0 <= s < 60):
            raise ValueError
    except ValueError as exc:
        raise ChaptersError(f"invalid HH:MM:SS timestamp: {value!r}") from exc
    return h * 3600 + m * 60 + s


def format_segments(segments: list[dict]) -> str:
    """Render the transcript's timed segments as the ``[START–END] text`` lines
    the model is prompted to read; blank segments are dropped."""
    lines = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = _format_hms(seg.get("start", 0))
        end = _format_hms(seg.get("end", 0))
        lines.append(f"[{start}–{end}] {text}")
    return "\n".join(lines)


def _extract_content(data: dict) -> str:
    """Pull the assistant message text out of a chat-completions reply."""
    choices = data.get("choices")
    if not choices:
        raise ChaptersError("chat completion had no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise ChaptersError("chat completion had empty content")
    return content


async def _post_chat_completion(url: str, headers: dict, body: dict) -> dict:
    """Stream the chat completion and reassemble it into a non-streaming reply.

    The endpoint emits Server-Sent Events (``data: {chunk}`` lines terminated by
    ``data: [DONE]``); we concatenate each chunk's ``delta.content`` back into the
    ``{choices: [{message: {content}}]}`` shape the caller parses, so streaming is
    invisible above this seam.

    A server that ignores ``stream`` and answers with a single JSON body
    (content-type ``application/json`` rather than ``text/event-stream``) is
    handled too: we read the whole body and return it as-is.
    """
    body = {**body, "stream": True}
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.is_error:
                await resp.aread()
                resp.raise_for_status()
            if "text/event-stream" not in resp.headers.get("content-type", ""):
                await resp.aread()
                return resp.json()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
    return {"choices": [{"message": {"content": "".join(parts)}}]}


async def generate_chapters(
    segments: list[dict],
    *,
    url: str,
    model: str,
    api_key: str | None = None,
    language: str = DEFAULT_LANGUAGE,
    context: dict | None = None,
    post: ChaptersPost = _post_chat_completion,
) -> list[dict]:
    """Ask the model to chapter ``segments``; return ``{start, end, title,
    is_ad}`` dicts with whole-second times, ordered as the model returned them.

    The ``response_format`` schema makes the model reply with a JSON object whose
    ``chapters`` array matches the chapter shape, so the message content is
    itself JSON. Auth is optional — the Bearer header is sent only when
    ``api_key`` is given, so a local/unauthenticated endpoint works too.

    ``context`` is optional publisher metadata (``podcast``/``title``/
    ``description``); when given it is rendered as a reference-only block before
    the transcript to steer name spellings, never as a chapter source.
    """
    prompt = (
        build_prompt(language)
        + "\n"
        + format_reference(context)
        + format_segments(segments)
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": response_format(),
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    data = await post(url, headers, body)
    content = _extract_content(data)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ChaptersError(
            f"could not decode chapters reply: {content[:500]!r}"
        ) from exc

    raw = payload.get("chapters") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise ChaptersError(f"unexpected chapters payload: {content[:500]!r}")

    try:
        return [
            {
                "start": _parse_hms(c["start"]),
                "end": _parse_hms(c["end"]),
                "title": c["title"],
                "is_ad": bool(c["is_ad"]),
            }
            for c in raw
        ]
    except (KeyError, TypeError) as exc:
        raise ChaptersError(f"unexpected chapter shape: {exc}") from exc
