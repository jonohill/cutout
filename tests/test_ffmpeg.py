import pytest

from cutout.worker.ffmpeg import _aac_quality_args, _output_sample_rate


@pytest.mark.parametrize(
    ("source_rate", "expected"),
    [
        (96_000, 48_000),
        (48_000, 48_000),
        (44_100, 44_100),
        (22_050, 44_100),
        (None, 48_000),
    ],
)
def test_xhe_sample_rate_is_44k1_or_48k(source_rate, expected):
    assert _output_sample_rate("libfdk_xhe_aac", source_rate) == expected


@pytest.mark.parametrize(
    ("source_rate", "expected"),
    [(48_000, 32_000), (22_050, 22_050), (None, 32_000)],
)
def test_aac_sample_rate_is_capped(source_rate, expected):
    assert _output_sample_rate("libfdk_aac", source_rate) == expected


@pytest.mark.parametrize(("channels", "kbps"), [(1, 24), (2, 32), (6, 32)])
def test_xhe_default_bitrate_by_channels(channels, kbps):
    assert _aac_quality_args("libfdk_xhe_aac", None, channels) == ["-b:a", f"{kbps}k"]


def test_configured_bitrate_overrides_xhe_default():
    assert _aac_quality_args("libfdk_xhe_aac", 40, 1) == ["-b:a", "40k"]
