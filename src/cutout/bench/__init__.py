"""Smoke bench for cutout's chapter generation.

The deterministic half of the bench: transcribe a fixed corpus once (cached),
fan ``generate_chapters`` across the configured contender models using the exact
production prompt and schema, run programmatic checks, and save every artifact
under a results directory. The subjective grading (agent-as-judge) is driven
separately by the ``smoke-bench`` skill, which consumes the manifest written
here.

Run with ``uv run python -m cutout.bench --config <cfg.toml> --out <dir>``.
"""
