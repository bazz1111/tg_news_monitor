"""Unit tests for Telegram web preview scraper and DOM parser."""

from datetime import datetime, timezone
import pytest
import httpx

from tg_news_monitor.core.models import TelegramPost
from tg_news_monitor.scraper.client import TelegramScraperClient
from tg_news_monitor.scraper.parser import TelegramWebParser


SAMPLE_REGULAR_HTML = """
<!DOCTYPE html>
<html>
<head><title>Telegram Channel Preview</title></head>
<body>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="durov/527">
      <div class="tgme_widget_message_text">
        Today we are launching Telegram 10.0 with stories and major improvements.<br><br>
        Read the full announcement at <a href="https://telegram.org/blog/channels-boosts">the official blog</a>.
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/durov/527">
        <time datetime="2026-09-08T18:05:40+00:00" class="time">18:05</time>
      </a>
      <span class="tgme_widget_message_views">2.4M</span>
    </div>
  </div>
</body>
</html>
"""

SAMPLE_FORMATTED_HTML = """
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="crypto_breaking/101">
    <div class="tgme_widget_message_text">
      🚨 <b>CRITICAL ALERT</b>: Protocol security incident reported.<br>
      Attacker drained funds via flash loan exploit.<br>
      See report <a href="https://security.example.com/exploit/101">here</a> and contact @security_team.
    </div>
    <a class="tgme_widget_message_date" href="https://t.me/crypto_breaking/101">
      <time datetime="2026-09-08T20:30:00Z" class="time">20:30</time>
    </a>
  </div>
</div>
"""

SAMPLE_MEDIA_HTML = """
<div class="tgme_widget_message_wrap">
  <!-- Photo post -->
  <div class="tgme_widget_message" data-post="news_feed/201">
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo1.jpg')"></a>
    <div class="tgme_widget_message_text">Chart analysis for Q3 macro trends.</div>
    <time datetime="2026-09-08T21:00:00+00:00" class="time">21:00</time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- Album post -->
  <div class="tgme_widget_message" data-post="news_feed/202">
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo2a.jpg')"></a>
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo2b.jpg')"></a>
    <time datetime="2026-09-08T21:05:00+00:00" class="time">21:05</time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- Video post -->
  <div class="tgme_widget_message" data-post="news_feed/203">
    <video class="tgme_widget_message_video" src="https://cdn4.telesco.pe/file/clip.mp4"></video>
    <div class="tgme_widget_message_text">Keynote live recording excerpt.</div>
    <time datetime="2026-09-08T21:10:00+00:00" class="time">21:10</time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- Document post -->
  <div class="tgme_widget_message" data-post="news_feed/204">
    <div class="tgme_widget_message_document">Whitepaper_v2.pdf</div>
    <time datetime="2026-09-08T21:15:00+00:00" class="time">21:15</time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- Poll post -->
  <div class="tgme_widget_message" data-post="news_feed/205">
    <div class="tgme_widget_message_poll">What is your outlook on rates?</div>
    <time datetime="2026-09-08T21:20:00+00:00" class="time">21:20</time>
  </div>
</div>
"""

SAMPLE_FORWARD_HTML = """
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="aggregator/301">
    <div class="tgme_widget_message_forwarded_from">
      <span class="tgme_widget_message_forwarded_from_text">Forwarded from</span>
      <a class="tgme_widget_message_forwarded_from_name" href="https://t.me/whale_alert">Whale Alert</a>
    </div>
    <div class="tgme_widget_message_text">
      50,000 ETH ($120M) transferred from unknown wallet to Coinbase.
    </div>
    <time datetime="2026-09-08T21:45:00+00:00" class="time">21:45</time>
  </div>
</div>
"""

SAMPLE_SERVICE_HTML = """
<div class="tgme_widget_message_wrap">
  <!-- Standard post -->
  <div class="tgme_widget_message" data-post="announcements/401">
    <div class="tgme_widget_message_text">Important operational update scheduled for midnight.</div>
    <time datetime="2026-09-08T22:00:00+00:00" class="time">22:00</time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- System service action: channel photo changed -->
  <div class="tgme_widget_message tgme_widget_message_service" data-post="announcements/402">
    <div class="tgme_widget_message_service_text">Channel photo updated</div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- System service action: pinned message notification -->
  <div class="tgme_widget_message service_message" data-post="announcements/403">
    <div class="tgme_widget_message_service_text">Pinned a message</div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <!-- Second standard post -->
  <div class="tgme_widget_message" data-post="announcements/404">
    <div class="tgme_widget_message_text">Maintenance completed successfully without downtime.</div>
    <time datetime="2026-09-08T22:30:00+00:00" class="time">22:30</time>
  </div>
</div>
"""


class TestTelegramWebParser:
    """Test suite verifying TelegramWebParser DOM extraction, formatting, and filtering."""

    def test_parse_regular_post(self):
        posts = TelegramWebParser.parse_html(SAMPLE_REGULAR_HTML)
        assert len(posts) == 1
        post = posts[0]

        assert post.channel == "durov"
        assert post.message_id == 527
        assert post.direct_url == "https://t.me/durov/527"
        assert post.views == "2.4M"
        assert post.published_at.year == 2026
        assert post.published_at.month == 9
        assert post.published_at.day == 8
        assert post.published_at.tzinfo == timezone.utc

        # Check line break preservation and markdown link conversion
        assert "Today we are launching Telegram 10.0" in post.text
        assert "\n" in post.text
        assert "[the official blog](https://telegram.org/blog/channels-boosts)" in post.text
        assert not post.has_media
        assert post.media_type is None
        assert post.forward_from is None

    def test_parse_formatted_markdown_and_user_tags(self):
        posts = TelegramWebParser.parse_html(SAMPLE_FORMATTED_HTML)
        assert len(posts) == 1
        post = posts[0]

        assert post.channel == "crypto_breaking"
        assert post.message_id == 101
        assert "[here](https://security.example.com/exploit/101)" in post.text
        assert "@security_team" in post.text
        assert "CRITICAL ALERT" in post.text

    def test_parse_media_types(self):
        posts = TelegramWebParser.parse_html(SAMPLE_MEDIA_HTML)
        assert len(posts) == 5

        # Photo
        p1 = posts[0]
        assert p1.message_id == 201
        assert p1.has_media is True
        assert p1.media_type == "photo"
        assert "https://cdn4.telesco.pe/file/photo1.jpg" in p1.media_urls
        assert "Chart analysis" in p1.text

        # Album
        p2 = posts[1]
        assert p2.message_id == 202
        assert p2.has_media is True
        assert p2.media_type == "album"
        assert len(p2.media_urls) == 2
        assert p2.text == "[ALBUM]"  # Auto-generated indicator when text is empty

        # Video
        p3 = posts[2]
        assert p3.message_id == 203
        assert p3.has_media is True
        assert p3.media_type == "video"
        assert "https://cdn4.telesco.pe/file/clip.mp4" in p3.media_urls

        # Document
        p4 = posts[3]
        assert p4.message_id == 204
        assert p4.has_media is True
        assert p4.media_type == "document"

        # Poll
        p5 = posts[4]
        assert p5.message_id == 205
        assert p5.has_media is True
        assert p5.media_type == "poll"

    def test_parse_forwarded_post(self):
        posts = TelegramWebParser.parse_html(SAMPLE_FORWARD_HTML)
        assert len(posts) == 1
        post = posts[0]

        assert post.channel == "aggregator"
        assert post.message_id == 301
        assert post.forward_from == "Whale Alert"
        assert "50,000 ETH" in post.text

    def test_filter_service_messages(self):
        posts = TelegramWebParser.parse_html(SAMPLE_SERVICE_HTML)
        # Should contain posts 401 and 404, but completely ignore service posts 402 and 403
        assert len(posts) == 2
        msg_ids = [p.message_id for p in posts]
        assert msg_ids == [401, 404]

    def test_parse_channel_page_alias(self):
        posts = TelegramWebParser.parse_channel_page("durov", SAMPLE_REGULAR_HTML)
        assert len(posts) == 1
        assert posts[0].channel == "durov"


class TestTelegramScraperClient:
    """Test suite verifying TelegramScraperClient anti-scraping mechanics and resilience."""

    def test_header_rotation_and_stealth(self):
        client = TelegramScraperClient()
        headers1 = client.get_random_headers()
        headers2 = client.get_random_headers()

        assert "User-Agent" in headers1
        assert "Sec-Ch-Ua" in headers1
        assert "Accept-Language" in headers1

        # Must NOT contain Telegram authentication tokens or sessions
        assert "Authorization" not in headers1
        assert "Cookie" not in headers1
        assert "X-Telegram-Auth" not in headers1

    def test_jitter_interval_calculation(self):
        client = TelegramScraperClient(base_interval=60.0, jitter_ratio=0.25)
        delays = [client.calculate_jittered_delay() for _ in range(50)]

        # All delays must fall strictly within [45.0, 75.0]
        for d in delays:
            assert 45.0 <= d <= 75.0

    def test_inter_channel_delay(self):
        client = TelegramScraperClient()
        for _ in range(20):
            d = client.calculate_inter_channel_delay(1.5, 3.5)
            assert 1.5 <= d <= 3.5

    def test_channel_url_formatting(self):
        client = TelegramScraperClient()
        assert client.format_channel_url("@durov") == "https://t.me/s/durov"
        assert client.format_channel_url("durov", before=500) == "https://t.me/s/durov?before=500"

    def test_fetch_success_200(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == "https://t.me/s/test_channel"
            assert "User-Agent" in request.headers
            # Zero-credential assert: no auth tokens
            assert "Authorization" not in request.headers
            return httpx.Response(200, text=SAMPLE_REGULAR_HTML)

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport)
        scraper = TelegramScraperClient(http_client=mock_http)

        html = scraper.fetch_channel_html("test_channel")
        assert html == SAMPLE_REGULAR_HTML
        assert scraper.consecutive_errors == 0

    def test_fetch_429_backoff_and_retry(self, monkeypatch):
        attempts = 0
        slept_durations = []

        def mock_sleep(seconds: float):
            slept_durations.append(seconds)

        monkeypatch.setattr("time.sleep", mock_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, text=SAMPLE_REGULAR_HTML)

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport)
        scraper = TelegramScraperClient(max_retries=3, http_client=mock_http)

        html = scraper.fetch_channel_html("test_channel")
        assert attempts == 2
        assert len(slept_durations) == 1
        assert slept_durations[0] >= 2.0
        assert html == SAMPLE_REGULAR_HTML

    def test_fetch_500_server_error_exhaustion(self, monkeypatch):
        slept_durations = []
        monkeypatch.setattr("time.sleep", lambda s: slept_durations.append(s))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        mock_http = httpx.Client(transport=transport)
        scraper = TelegramScraperClient(max_retries=3, backoff_factor=1.5, http_client=mock_http)

        html = scraper.fetch_channel_html("test_channel")
        assert html is None
        assert scraper.consecutive_errors >= 3
        assert len(slept_durations) == 2  # slept between attempts 1->2 and 2->3
