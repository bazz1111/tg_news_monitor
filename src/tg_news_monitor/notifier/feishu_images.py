"""Download CDN images and upload to Feishu to obtain card `img_key`s.

Custom bot webhooks cannot embed images by URL. An open-platform app
(`FEISHU_APP_ID` / `FEISHU_APP_SECRET`) must upload bytes with
`image_type=message`. Credentials are tenant-global so news24 can reuse
this helper later via `card_profile.embed_images`; only wechat_photo
send is wired today. Image bytes never go to the LLM.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import httpx

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)  # type: ignore

from tg_news_monitor.core.wechat_photo import photo_link_urls

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"
MAX_EMBEDDED_IMAGES = 9
MAX_IMAGE_BYTES = 10 * 1024 * 1024
TOKEN_REFRESH_SKEW_SECONDS = 120.0
DEFAULT_TIMEOUT = 20.0
DOWNLOAD_UA = "tg_news_monitor/feishu-images"
KEY_CACHE_MAXSIZE = 1000
MAX_DOWNLOAD_REDIRECTS = 3
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.google.com",
    }
)

# (magic prefix-or-matcher) → (ext, mime)
_JPEG_PREFIX = b"\xff\xd8\xff"
_PNG_PREFIX = b"\x89PNG\r\n\x1a\n"
_GIF_PREFIXES = (b"GIF87a", b"GIF89a")
_BMP_PREFIX = b"BM"
_ICO_PREFIX = b"\x00\x00\x01\x00"
_TIFF_PREFIXES = (b"II*\x00", b"MM\x00*")


def resolve_host_ips(host: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to IP addresses. Tests may monkeypatch this."""
    ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for info in socket.getaddrinfo(host, None):
            addr = info[4][0]
            try:
                ips.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
    except socket.gaierror:
        return []
    return ips


def _ip_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def host_is_blocked(host: str) -> bool:
    """True if host is private/loopback/link-local/metadata or resolves to one."""
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return True
    if name in _BLOCKED_HOSTS or name.endswith(".local") or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        ips = resolve_host_ips(name)
        if not ips:
            return True
        return any(not _ip_public(ip) for ip in ips)
    return not _ip_public(ip)


def url_is_safe_image(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return not host_is_blocked(parsed.hostname or "")


def sniff_image(data: bytes) -> Optional[Tuple[str, str]]:
    """Return (file_ext, mime) for Feishu-accepted image bytes, else None."""
    if not data or len(data) < 12:
        return None
    if data[:3] == _JPEG_PREFIX:
        return "jpg", "image/jpeg"
    if data[:8] == _PNG_PREFIX:
        return "png", "image/png"
    if data[:6] in _GIF_PREFIXES:
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:2] == _BMP_PREFIX:
        return "bmp", "image/bmp"
    if data[:4] == _ICO_PREFIX:
        return "ico", "image/x-icon"
    if data[:4] in _TIFF_PREFIXES:
        return "tiff", "image/tiff"
    return None


class FeishuImageUploader:
    """Process-lifetime tenant token + image_key cache. Safe to share across groups."""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        http_client: Optional[httpx.Client] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_images: int = MAX_EMBEDDED_IMAGES,
        key_cache_maxsize: int = KEY_CACHE_MAXSIZE,
        host_checker: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_images = max_images
        self._cache_max = max(1, int(key_cache_maxsize))
        self._host_blocked = host_checker or host_is_blocked
        self._external_client = http_client
        self._lock = threading.Lock()
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._key_by_url: OrderedDict[str, str] = OrderedDict()

    @classmethod
    def from_settings(cls, settings: Any, http_client: Optional[httpx.Client] = None) -> FeishuImageUploader:
        return cls(
            app_id=str(getattr(settings, "feishu_app_id", "") or ""),
            app_secret=str(getattr(settings, "feishu_app_secret", "") or ""),
            http_client=http_client,
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def embed_keys(self, urls: Sequence[str] | None) -> List[str]:
        """Download + upload http(s) images. Skip t.me. Partial success is kept."""
        if not self.configured:
            return []
        candidates = photo_link_urls(urls)[: self.max_images]
        if not candidates:
            return []

        keys: List[str] = []
        if self._external_client is not None:
            client = self._external_client
            should_close = False
        else:
            client = httpx.Client(timeout=self.timeout, follow_redirects=False, max_redirects=MAX_DOWNLOAD_REDIRECTS)
            should_close = True
        try:
            for url in candidates:
                key = self._key_for_url(client, url)
                if key:
                    keys.append(key)
        finally:
            if should_close:
                client.close()
        return keys

    def _cache_get(self, url: str) -> str:
        with self._lock:
            cached = self._key_by_url.get(url)
            if cached:
                self._key_by_url.move_to_end(url)
            return cached or ""

    def _cache_put(self, url: str, key: str) -> None:
        with self._lock:
            self._key_by_url[url] = key
            self._key_by_url.move_to_end(url)
            while len(self._key_by_url) > self._cache_max:
                self._key_by_url.popitem(last=False)

    def _key_for_url(self, client: httpx.Client, url: str) -> str:
        cached = self._cache_get(url)
        if cached:
            return cached
        blob = self._download(client, url)
        if blob is None:
            return ""
        sniffed = sniff_image(blob)
        if sniffed is None:
            logger.warning(f"Skip Feishu image upload, unsupported format: {url}")
            return ""
        ext, mime = sniffed
        key = self._upload(client, blob, ext=ext, mime=mime)
        if not key:
            return ""
        self._cache_put(url, key)
        return key

    def _url_blocked(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return True
        return self._host_blocked(parsed.hostname or "")

    def _download(self, client: httpx.Client, url: str) -> Optional[bytes]:
        current = url
        try:
            for _hop in range(MAX_DOWNLOAD_REDIRECTS + 1):
                if self._url_blocked(current):
                    logger.warning(f"Skip Feishu image download, blocked host: {current}")
                    return None
                host = urlparse(current).hostname or ""
                with client.stream(
                    "GET",
                    current,
                    headers={"User-Agent": DOWNLOAD_UA},
                    follow_redirects=False,
                    timeout=self.timeout,
                ) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        loc = (resp.headers.get("location") or "").strip()
                        if not loc:
                            logger.warning(f"Skip Feishu image download, empty redirect: {current}")
                            return None
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            f"Skip Feishu image download HTTP {resp.status_code}: {current}"
                        )
                        return None
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > self.max_bytes:
                        logger.warning(
                            f"Skip Feishu image upload, Content-Length {cl} > {self.max_bytes}: {current}"
                        )
                        return None
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            logger.warning(
                                f"Skip Feishu image upload, body > {self.max_bytes} bytes host={host}"
                            )
                            return None
                    return bytes(buf)
            logger.warning(f"Skip Feishu image download, too many redirects: {url}")
            return None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError, ValueError) as exc:
            logger.warning(f"Skip Feishu image download ({exc.__class__.__name__}): {url}")
            return None

    def _upload(self, client: httpx.Client, blob: bytes, *, ext: str, mime: str) -> str:
        token = self._tenant_token(client)
        if not token:
            return ""
        key = self._post_image(client, token, blob, ext=ext, mime=mime)
        if key:
            return key
        # Token may have expired mid-batch; refresh once.
        self._invalidate_token()
        token = self._tenant_token(client, force=True)
        if not token:
            return ""
        return self._post_image(client, token, blob, ext=ext, mime=mime)

    def _post_image(self, client: httpx.Client, token: str, blob: bytes, *, ext: str, mime: str) -> str:
        try:
            resp = client.post(
                UPLOAD_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (f"image.{ext}", blob, mime)},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            logger.warning(f"Feishu image upload network error: {exc.__class__.__name__}")
            return ""
        try:
            body = resp.json()
        except Exception:
            logger.warning(f"Feishu image upload non-JSON HTTP {resp.status_code}")
            return ""
        if resp.status_code != 200 or body.get("code") != 0:
            logger.warning(
                f"Feishu image upload failed HTTP {resp.status_code} "
                f"code={body.get('code')} msg={body.get('msg')}"
            )
            return ""
        data = body.get("data") or {}
        key = str(data.get("image_key") or "").strip()
        if not key:
            logger.warning("Feishu image upload returned empty image_key")
        return key

    def _tenant_token(self, client: httpx.Client, force: bool = False) -> str:
        now = time.monotonic()
        with self._lock:
            if not force and self._token and now < self._token_expire_at:
                return self._token
        try:
            resp = client.post(
                TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout,
            )
            body = resp.json()
        except Exception as exc:
            logger.warning(f"Feishu tenant token request failed: {exc.__class__.__name__}")
            return ""
        if resp.status_code != 200 or body.get("code") != 0:
            logger.warning(
                f"Feishu tenant token failed HTTP {resp.status_code} "
                f"code={body.get('code')} msg={body.get('msg')}"
            )
            return ""
        token = str(body.get("tenant_access_token") or "").strip()
        if not token:
            logger.warning("Feishu tenant token response missing tenant_access_token")
            return ""
        try:
            expire = float(body.get("expire") or 7200)
        except (TypeError, ValueError):
            expire = 7200.0
        ttl = max(60.0, expire - TOKEN_REFRESH_SKEW_SECONDS)
        with self._lock:
            self._token = token
            self._token_expire_at = time.monotonic() + ttl
        return token

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token = ""
            self._token_expire_at = 0.0


__all__ = [
    "KEY_CACHE_MAXSIZE",
    "MAX_DOWNLOAD_REDIRECTS",
    "MAX_EMBEDDED_IMAGES",
    "MAX_IMAGE_BYTES",
    "FeishuImageUploader",
    "host_is_blocked",
    "resolve_host_ips",
    "sniff_image",
    "url_is_safe_image",
]
