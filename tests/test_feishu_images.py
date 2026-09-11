"""Feishu image download/upload for in-card img_key (mocked HTTP)."""

from __future__ import annotations

import httpx

from tg_news_monitor.notifier.feishu_images import (
    MAX_EMBEDDED_IMAGES,
    FeishuImageUploader,
    sniff_image,
)

MINI_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    + b"\x00" * 8
)


def _uploader(handler, **kwargs) -> FeishuImageUploader:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return FeishuImageUploader(
        app_id="cli_test_app",
        app_secret="test_app_secret",
        http_client=client,
        **kwargs,
    )


class TestSniffImage:
    def test_jpeg_png_webp_and_reject_unknown(self):
        assert sniff_image(MINI_JPEG) == ("jpg", "image/jpeg")
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        assert sniff_image(png) == ("png", "image/png")
        webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4
        assert sniff_image(webp) == ("webp", "image/webp")
        assert sniff_image(b"not-an-image!!!!") is None
        assert sniff_image(b"\xff\xd8") is None


class TestFeishuImageUploader:
    def test_missing_creds_returns_empty(self):
        up = FeishuImageUploader(app_id="", app_secret="")
        assert up.configured is False
        assert up.embed_keys(["https://cdn.example.com/a.jpg"]) == []

    def test_download_and_upload_returns_image_key(self):
        seen = {"token": 0, "upload": 0, "get": []}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tenant_access_token" in url:
                seen["token"] += 1
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-test-token"},
                )
            if url.endswith("/im/v1/images"):
                seen["upload"] += 1
                auth = request.headers.get("authorization", "")
                assert auth == "Bearer t-test-token"
                return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v2_ok"}})
            if request.method == "GET":
                seen["get"].append(url)
                return httpx.Response(200, content=MINI_JPEG)
            return httpx.Response(404)

        up = _uploader(handler)
        keys = up.embed_keys(
            [
                "https://cdn.example.com/a.jpg",
                "https://t.me/oldpix/1",
                "https://cdn.example.com/a.jpg",
            ]
        )
        assert keys == ["img_v2_ok"]
        assert seen["token"] == 1
        assert seen["upload"] == 1
        assert seen["get"] == ["https://cdn.example.com/a.jpg"]

        # Process-lifetime cache: same URL does not re-download or re-upload.
        again = up.embed_keys(["https://cdn.example.com/a.jpg"])
        assert again == ["img_v2_ok"]
        assert seen["token"] == 1
        assert seen["upload"] == 1
        assert seen["get"] == ["https://cdn.example.com/a.jpg"]

    def test_token_cached_across_urls(self):
        seen = {"token": 0, "upload": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tenant_access_token" in url:
                seen["token"] += 1
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-cached"},
                )
            if url.endswith("/im/v1/images"):
                seen["upload"] += 1
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"image_key": f"img_v2_{seen['upload']}"}},
                )
            return httpx.Response(200, content=MINI_JPEG)

        up = _uploader(handler)
        keys = up.embed_keys(
            ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
        )
        assert keys == ["img_v2_1", "img_v2_2"]
        assert seen["token"] == 1
        assert seen["upload"] == 2

    def test_partial_upload_failure_keeps_successes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tenant_access_token" in url:
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-x"},
                )
            if url.endswith("/im/v1/images"):
                if getattr(handler, "n", 0) == 0:
                    handler.n = 1  # type: ignore[attr-defined]
                    return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v2_one"}})
                return httpx.Response(200, json={"code": 999, "msg": "fail"})
            return httpx.Response(200, content=MINI_JPEG)

        handler.n = 0  # type: ignore[attr-defined]
        up = _uploader(handler)
        keys = up.embed_keys(
            ["https://cdn.example.com/ok.jpg", "https://cdn.example.com/bad.jpg"]
        )
        assert keys == ["img_v2_one"]

    def test_upload_all_fail_returns_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "tenant_access_token" in str(request.url):
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-x"},
                )
            if str(request.url).endswith("/im/v1/images"):
                return httpx.Response(200, json={"code": 99991401, "msg": "denied"})
            return httpx.Response(200, content=MINI_JPEG)

        up = _uploader(handler)
        assert up.embed_keys(["https://cdn.example.com/a.jpg"]) == []

    def test_token_failure_returns_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "tenant_access_token" in str(request.url):
                return httpx.Response(200, json={"code": 10014, "msg": "app secret invalid"})
            return httpx.Response(200, content=MINI_JPEG)

        up = _uploader(handler)
        assert up.embed_keys(["https://cdn.example.com/a.jpg"]) == []

    def test_skips_oversized_and_unknown_format(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "tenant_access_token" in str(request.url):
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-x"},
                )
            if str(request.url).endswith("/im/v1/images"):
                return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v2_ok"}})
            if path.endswith("/huge.jpg"):
                return httpx.Response(
                    200,
                    content=MINI_JPEG + b"x" * 50,
                    headers={"Content-Length": "80"},
                )
            if path.endswith("/txt.jpg"):
                return httpx.Response(200, content=b"this is not an image file!!")
            return httpx.Response(200, content=MINI_JPEG)

        up = _uploader(handler, max_bytes=40)
        keys = up.embed_keys(
            [
                "https://cdn.example.com/huge.jpg",
                "https://cdn.example.com/txt.jpg",
                "https://cdn.example.com/ok.jpg",
            ]
        )
        assert keys == ["img_v2_ok"]

    def test_caps_first_n_images(self):
        uploads = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "tenant_access_token" in str(request.url):
                return httpx.Response(
                    200,
                    json={"code": 0, "expire": 7200, "tenant_access_token": "t-x"},
                )
            if str(request.url).endswith("/im/v1/images"):
                uploads.append(1)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"image_key": f"img_v2_{len(uploads)}"}},
                )
            return httpx.Response(200, content=MINI_JPEG)

        urls = [f"https://cdn.example.com/{i}.jpg" for i in range(12)]
        up = _uploader(handler, max_images=9)
        keys = up.embed_keys(urls)
        assert len(keys) == 9
        assert len(uploads) == 9
        assert MAX_EMBEDDED_IMAGES == 9
