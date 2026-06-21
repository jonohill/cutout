import asyncio

import pytest

from cutout.worker.chapters import ChaptersError, generate_chapters

# Three timed segments standing in for an OpenAI verbose_json transcript:
# an intro, a sponsor read, then back to the topic.
SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": " Welcome to the show."},
    {"start": 5.0, "end": 65.0, "text": "This episode is brought to you by ACME."},
    {"start": 65.0, "end": 120.0, "text": "Back to the main topic."},
]

URL = "https://g.example/v1beta/openai/chat/completions"


def _reply(content: str) -> dict:
    """A chat-completions reply whose assistant message content is the chapters JSON."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_formats_transcript_targets_the_model_and_parses_the_reply():
    captured = {}

    async def post(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return _reply(
            '{"chapters":['
            '{"start":"00:00:00","end":"00:01:05","title":"Intro","is_ad":false},'
            '{"start":"00:01:05","end":"00:02:00","title":"Topic","is_ad":true}]}'
        )

    chapters = asyncio.run(
        generate_chapters(SEGMENTS, url=URL, model="gemini-x", api_key="k", post=post)
    )

    # The configured URL is hit verbatim; the model rides in the body (OpenAI
    # style), not the path; the key is a Bearer header.
    assert captured["url"] == URL
    assert captured["body"]["model"] == "gemini-x"
    assert captured["headers"]["Authorization"] == "Bearer k"
    # The segments are rendered as timestamped lines (whole seconds, truncated).
    prompt = captured["body"]["messages"][0]["content"]
    assert "[00:00:00–00:00:05] Welcome to the show." in prompt
    assert "[00:01:05–00:02:00] Back to the main topic." in prompt
    # A response schema is requested so the reply is the chapter JSON.
    assert captured["body"]["response_format"]["type"] == "json_schema"
    # HH:MM:SS strings become whole-second ints, order preserved.
    assert chapters == [
        {"start": 0, "end": 65, "title": "Intro", "is_ad": False},
        {"start": 65, "end": 120, "title": "Topic", "is_ad": True},
    ]


def test_reference_context_precedes_the_transcript_as_a_guide():
    captured = {}

    async def post(url, headers, body):
        captured["body"] = body
        return _reply('{"chapters":[]}')

    asyncio.run(
        generate_chapters(
            SEGMENTS,
            url=URL,
            model="m",
            api_key="k",
            context={
                "podcast": "The Show",
                "title": "Episode 7",
                "description": "A chat with Renée.",
            },
            post=post,
        )
    )

    prompt = captured["body"]["messages"][0]["content"]
    # The metadata is rendered, framed as context-only, and sits before the
    # transcript lines.
    assert "Podcast: The Show" in prompt
    assert "Episode title: Episode 7" in prompt
    assert "Episode description: A chat with Renée." in prompt
    assert "NOT A SOURCE OF CHAPTERS" in prompt
    assert prompt.index("REFERENCE METADATA") < prompt.index("Welcome to the show.")


def test_no_context_leaves_the_prompt_metadata_free():
    captured = {}

    async def post(url, headers, body):
        captured["body"] = body
        return _reply('{"chapters":[]}')

    asyncio.run(generate_chapters(SEGMENTS, url=URL, model="m", api_key="k", post=post))

    prompt = captured["body"]["messages"][0]["content"]
    assert "REFERENCE METADATA" not in prompt


def test_drops_blank_segments_from_the_prompt():
    captured = {}

    async def post(url, headers, body):
        captured["body"] = body
        return _reply('{"chapters":[]}')

    asyncio.run(
        generate_chapters(
            [
                {"start": 0.0, "end": 1.0, "text": "   "},
                {"start": 1.0, "end": 2.0, "text": "hi"},
            ],
            url=URL,
            model="m",
            api_key="k",
            post=post,
        )
    )

    prompt = captured["body"]["messages"][0]["content"]
    assert "[00:00:01–00:00:02] hi" in prompt
    assert "00:00:00" not in prompt  # the blank segment produced no line


def test_omits_the_auth_header_without_a_key():
    captured = {}

    async def post(url, headers, body):
        captured["headers"] = headers
        return _reply('{"chapters":[]}')

    asyncio.run(generate_chapters(SEGMENTS, url=URL, model="m", api_key=None, post=post))

    # A local/unauthenticated endpoint is supported: no Bearer header is sent.
    assert "Authorization" not in captured["headers"]


def test_raises_when_the_reply_has_no_choices():
    async def post(url, headers, body):
        return {"choices": []}

    with pytest.raises(ChaptersError):
        asyncio.run(generate_chapters(SEGMENTS, url=URL, model="m", api_key="k", post=post))


def test_raises_on_a_malformed_timestamp():
    async def post(url, headers, body):
        return _reply(
            '{"chapters":[{"start":"00:99:00","end":"00:01:00","title":"x","is_ad":false}]}'
        )

    with pytest.raises(ChaptersError):
        asyncio.run(generate_chapters(SEGMENTS, url=URL, model="m", api_key="k", post=post))
