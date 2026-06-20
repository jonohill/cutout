"""CLI entry point for the smoke bench's deterministic harness.

Usage::

    uv run python -m cutout.bench --config cutout/bench/config.toml --out cutout/bench-results/run1

For each audio file in the configured corpus it transcribes once (cached on
disk), then for each configured model it generates chapters via the production
``generate_chapters`` path and runs the programmatic checks. Outputs land under
``<out>/<audio-stem>/`` and an overall ``<out>/manifest.json`` the agent judge
reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cutout.bench.checks import run_checks
from cutout.worker.chapters import (
    ChaptersError,
    _post_chat_completion,
    generate_chapters,
)
from cutout.worker.fetch import post_transcription
from cutout.worker.ffmpeg import compress_audio
from cutout.worker.media import _SIZE_HEADROOM

_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mp4"}

# Default upload cap, matching the production transcribe stage
# (Settings.transcribe_max_mb). Overridable per run via [transcribe].max_mb.
_DEFAULT_MAX_MB = 25


def _load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _load_dotenv(path: Path) -> int:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ`` without
    a third-party dependency. Existing environment variables win, so an explicit
    ``export FOO=...`` in the shell still overrides the file. Returns the number
    of variables set. Missing file is a no-op.

    Supported: blank lines, ``#`` comments, an optional ``export`` prefix, and
    single/double-quoted values. Deliberately minimal — no interpolation.
    """
    if not path.exists():
        return 0
    set_count = 0
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            set_count += 1
    return set_count


def _resolve_key(env_name: str | None) -> str | None:
    """Look up an API key from the environment; never read inline from config."""
    return os.environ.get(env_name) if env_name else None


def _discover_audio(audio_dir: Path, only: list[str] | None) -> list[Path]:
    files = sorted(
        p
        for p in audio_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTENSIONS
    )
    if only:
        wanted = set(only)
        files = [p for p in files if p.name in wanted or p.stem in wanted]
    return files


async def _fit_upload(audio: Path, dest: Path, tcfg: dict, *, compress=compress_audio) -> Path:
    """Return the file to upload to the transcription endpoint, re-encoding an
    oversized source down to fit the cap exactly as the production transcribe
    stage does (``MediaWorker.transcribe``).

    The real endpoint rejects uploads over its size cap, and even when it didn't
    the chapter model would be scored against audio of a different quality than
    production ever sees. So anything over the cap is compressed to mono-Opus
    webm aimed at ``int(limit * _SIZE_HEADROOM)`` — the same target the worker
    uses. The webm is cached beside the transcript so a rerun (e.g. after a
    transcription failure) skips the re-encode.
    """
    max_mb = int(tcfg.get("max_mb", _DEFAULT_MAX_MB))
    limit = max_mb * 1000 * 1000  # decimal MB, matching the API's size maths
    size = audio.stat().st_size
    if size <= limit:
        return audio
    upload = dest / "upload.webm"
    if upload.exists():
        return upload
    target = int(limit * _SIZE_HEADROOM)
    print(f"    {audio.name}: {size} bytes over {limit} cap; compressing to {upload.name}")
    await compress(audio, upload, target)
    return upload


async def _transcribe(audio: Path, dest: Path, cfg: dict) -> dict:
    """Return the transcript dict for ``audio``, reusing a cached ``transcript.json``
    if one already sits in ``dest`` (so reruns are cheap and hand-curated
    transcripts can be dropped in)."""
    cached = dest / "transcript.json"
    if cached.exists():
        return json.loads(cached.read_text())
    tcfg = cfg["transcribe"]
    upload = await _fit_upload(audio, dest, tcfg)
    transcript = await post_transcription(
        upload,
        url=tcfg["url"],
        model=tcfg["model"],
        api_key=_resolve_key(tcfg.get("api_key_env")),
    )
    cached.write_text(json.dumps(transcript, indent=2))
    return transcript


def _make_capturing_post(store: dict):
    """A ``generate_chapters`` ``post`` that stashes the raw reply for inspection
    and schema scoring, then returns it unchanged to the real parser."""

    async def post(url: str, headers: dict, body: dict) -> dict:
        data = await _post_chat_completion(url, headers, body)
        store["raw"] = data
        return data

    return post


async def _run_model(
    model_cfg: dict,
    segments: list[dict],
    dest: Path,
    language: str,
) -> dict:
    name = model_cfg["name"]
    store: dict = {}
    chapters: list[dict] | None = None
    error: str | None = None
    try:
        chapters = await generate_chapters(
            segments,
            url=model_cfg["url"],
            model=model_cfg["model"],
            api_key=_resolve_key(model_cfg.get("api_key_env")),
            language=language,
            post=_make_capturing_post(store),
        )
    except ChaptersError as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # network/HTTP/etc. — record, don't abort the bench
        error = f"{type(exc).__name__}: {exc}"

    raw = store.get("raw")
    checks = run_checks(chapters, segments, error=error, raw=raw)

    chapters_path = dest / f"chapters.{name}.json"
    raw_path = dest / f"raw.{name}.json"
    checks_path = dest / f"checks.{name}.json"
    chapters_path.write_text(json.dumps(chapters if chapters is not None else [], indent=2))
    if raw is not None:
        raw_path.write_text(json.dumps(raw, indent=2))
    checks_path.write_text(json.dumps(checks, indent=2))

    status = "ok" if error is None else "error"
    print(f"    [{name}] {status}: {checks['n_chapters']} chapters"
          + (f" ({error})" if error else ""))
    return {
        "name": name,
        "model": model_cfg["model"],
        "chapters": str(chapters_path),
        "raw": str(raw_path) if raw is not None else None,
        "checks": str(checks_path),
        "error": error,
    }


async def _run_file(
    audio: Path,
    out: Path,
    cfg: dict,
    models: list[dict],
    sem: asyncio.Semaphore,
) -> dict:
    dest = out / audio.stem
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  {audio.name}: transcribing…")
    transcript = await _transcribe(audio, dest, cfg)
    segments = transcript.get("segments", [])
    language = cfg.get("language", "NZ English")

    async def guarded(m: dict) -> dict:
        async with sem:
            return await _run_model(m, segments, dest, language)

    model_results = await asyncio.gather(*(guarded(m) for m in models))
    return {
        "stem": audio.stem,
        "audio": str(audio),
        "transcript": str(dest / "transcript.json"),
        "n_segments": len(segments),
        "models": model_results,
    }


async def _main_async(args: argparse.Namespace) -> None:
    cfg = _load_config(Path(args.config))
    audio_dir = Path(args.audio_dir or cfg["audio_dir"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files = _discover_audio(audio_dir, args.audio)
    if not files:
        raise SystemExit(f"no audio files found in {audio_dir}")

    models = cfg.get("models", [])
    if args.models:
        wanted = set(args.models)
        models = [m for m in models if m["name"] in wanted]
    if not models:
        raise SystemExit("no models selected to run")

    sem = asyncio.Semaphore(int(cfg.get("concurrency", 2)))
    print(f"Benching {len(files)} file(s) across {len(models)} model(s) → {out}")

    file_results = []
    for audio in files:  # files serial (transcription is the cached bottleneck)
        file_results.append(await _run_file(audio, out, cfg, models, sem))

    manifest = {
        "created": datetime.now(timezone.utc).isoformat(),
        "audio_dir": str(audio_dir),
        "language": cfg.get("language", "NZ English"),
        "models": [{"name": m["name"], "model": m["model"]} for m in models],
        "files": file_results,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote {manifest_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="cutout chapter smoke bench (harness)")
    parser.add_argument("--config", required=True, help="path to the bench TOML config")
    parser.add_argument("--out", required=True, help="results directory to write")
    parser.add_argument("--audio-dir", help="override audio_dir from the config")
    parser.add_argument(
        "--audio", nargs="*", help="only these audio files (by name or stem)"
    )
    parser.add_argument("--models", nargs="*", help="only these model names")
    parser.add_argument(
        "--env",
        default=".env",
        help="path to a .env file to load API keys from (default: .env; "
        "shell environment takes precedence)",
    )
    args = parser.parse_args()
    n = _load_dotenv(Path(args.env))
    if n:
        print(f"Loaded {n} variable(s) from {args.env}")
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
