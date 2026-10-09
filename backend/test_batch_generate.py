import argparse
import base64
import io
import urllib.error

import pytest
from PIL import Image

import batch_generate


def image_bytes(image_format):
    buffer = io.BytesIO()
    image = Image.new("RGB", (2300, 17), "green")
    save_kwargs = {"quality": 91} if image_format in {"JPEG", "WEBP"} else {}
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("image_format", "expected_mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_to_data_url_preserves_original_bytes(image_format, expected_mime):
    original = image_bytes(image_format)

    data_url = batch_generate.to_data_url(original)
    prefix, encoded = data_url.split(",", 1)

    assert prefix == f"data:{expected_mime};base64"
    assert base64.b64decode(encoded) == original


def test_to_data_url_encodes_memory_image_as_lossless_png():
    data_url = batch_generate.to_data_url(Image.new("RGB", (24, 18), "blue"))
    prefix, encoded = data_url.split(",", 1)

    assert prefix == "data:image/png;base64"
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (24, 18)
        assert image.format == "PNG"


def make_http_error(code, detail):
    return urllib.error.HTTPError(
        "https://example.invalid",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(detail.encode()),
    )


def test_call_with_retry_retries_transient_http_error(monkeypatch):
    calls = []
    delays = []

    def fake_call_api(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise make_http_error(429, "rate limit")
        return b"image"

    monkeypatch.setattr(batch_generate, "call_api", fake_call_api)
    monkeypatch.setattr(batch_generate, "retry_delay", delays.append)

    result, model = batch_generate.call_with_retry(
        "key",
        argparse.Namespace(retries=3),
        [],
        "prompt",
        1,
        models=["model-a"],
    )

    assert result == b"image"
    assert model == "model-a"
    assert len(calls) == 2
    assert delays == [1]


def test_call_with_retry_retries_network_error(monkeypatch):
    calls = []
    delays = []

    def fake_call_api(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("temporary network error")
        return b"image"

    monkeypatch.setattr(batch_generate, "call_api", fake_call_api)
    monkeypatch.setattr(batch_generate, "retry_delay", delays.append)

    result, _ = batch_generate.call_with_retry(
        "key",
        argparse.Namespace(retries=2),
        [],
        "prompt",
        1,
        models=["model-a"],
    )

    assert result == b"image"
    assert len(calls) == 2
    assert delays == [1]


@pytest.mark.parametrize("status_code", [400, 401, 403, 422])
def test_call_with_retry_fails_immediately_for_permanent_http_error(monkeypatch, status_code):
    calls = []
    delays = []

    def fake_call_api(*args, **kwargs):
        calls.append(1)
        raise make_http_error(status_code, "invalid request")

    monkeypatch.setattr(batch_generate, "call_api", fake_call_api)
    monkeypatch.setattr(batch_generate, "retry_delay", delays.append)

    with pytest.raises(RuntimeError, match=f"HTTP {status_code}"):
        batch_generate.call_with_retry(
            "key",
            argparse.Namespace(retries=4),
            [],
            "prompt",
            1,
            models=["model-a"],
        )

    assert len(calls) == 1
    assert delays == []


def test_retry_delay_uses_exponential_backoff_and_jitter(monkeypatch):
    sleeps = []
    monkeypatch.setattr(batch_generate.random, "uniform", lambda start, end: 0.25)
    monkeypatch.setattr(batch_generate.time, "sleep", sleeps.append)

    batch_generate.retry_delay(1)
    batch_generate.retry_delay(2)
    batch_generate.retry_delay(5)

    assert sleeps == [2.25, 4.25, 30.25]
