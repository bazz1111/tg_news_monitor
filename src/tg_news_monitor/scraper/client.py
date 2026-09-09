"""Resilient account-free HTTP client for Telegram public channel web previews."""

import random
import time
from typing import Dict, List, Optional
import httpx


# Modern real-world desktop and mobile user agents for rotating headers
DEFAULT_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]


class TelegramScraperClient:
    """Resilient HTTP client for Telegram public web previews (https://t.me/s/{channel}).

    Defenses:
    - 0% Account ban risk: Strictly account-free unauthenticated HTTP GET requests.
    - User-Agent header rotation across modern browsers.
    - Randomized jitter intervals to break timing fingerprints.
    - Exponential backoff respecting Retry-After on HTTP 429/5xx responses.
    """

    def __init__(
        self,
        base_interval: float = 60.0,
        jitter_ratio: float = 0.25,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        max_backoff: float = 300.0,
        timeout: float = 15.0,
        user_agents: Optional[List[str]] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.base_interval = base_interval
        self.jitter_ratio = jitter_ratio
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.user_agents = user_agents or DEFAULT_USER_AGENTS
        self._external_client = http_client
        self.consecutive_errors = 0

    def get_random_headers(self) -> Dict[str, str]:
        """Generates realistic modern browser headers with rotating User-Agent."""
        ua = random.choice(self.user_agents)
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Chromium";v="129", "Not=A?Brand";v="8"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def calculate_jittered_delay(self, interval: Optional[float] = None) -> float:
        """Calculates a randomized polling delay around base_interval ± jitter."""
        base = interval if interval is not None else self.base_interval
        jitter_delta = base * self.jitter_ratio
        delay = base + random.uniform(-jitter_delta, jitter_delta)
        return max(1.0, delay)

    def calculate_inter_channel_delay(self, min_delay: float = 1.5, max_delay: float = 3.5) -> float:
        """Calculates a randomized pause between consecutive channel requests."""
        return random.uniform(min_delay, max_delay)

    @staticmethod
    def format_channel_url(channel: str, before: Optional[int] = None) -> str:
        """Formats the public web preview URL https://t.me/s/{channel}."""
        clean_channel = channel.lower().lstrip("@")
        url = f"https://t.me/s/{clean_channel}"
        if before is not None:
            url += f"?before={before}"
        return url

    def fetch_channel_html(
        self,
        channel: str,
        before: Optional[int] = None,
    ) -> Optional[str]:
        """Fetches server-rendered HTML for a channel with retries, jitter, and backoff."""
        url = self.format_channel_url(channel, before)

        for attempt in range(1, self.max_retries + 1):
            headers = self.get_random_headers()

            # Use external client if injected (e.g. for testing), or ephemeral client
            if self._external_client is not None:
                client = self._external_client
                should_close = False
            else:
                client = httpx.Client(timeout=self.timeout, follow_redirects=True)
                should_close = True

            try:
                response = client.get(url, headers=headers)

                if response.status_code == 200:
                    self.consecutive_errors = 0
                    return response.text

                elif response.status_code == 429:
                    self.consecutive_errors += 1
                    retry_after_str = response.headers.get("Retry-After")
                    if retry_after_str and retry_after_str.isdigit():
                        sleep_seconds = float(retry_after_str) + random.uniform(0.5, 2.0)
                    else:
                        sleep_seconds = min(
                            self.max_backoff,
                            (self.backoff_factor ** self.consecutive_errors) * 5.0,
                        ) + random.uniform(0.5, 2.0)

                    if attempt < self.max_retries:
                        time.sleep(sleep_seconds)
                        continue
                    return None

                elif 500 <= response.status_code < 600:
                    self.consecutive_errors += 1
                    sleep_seconds = min(
                        60.0,
                        (self.backoff_factor ** self.consecutive_errors) * 2.0,
                    ) + random.uniform(0.2, 1.0)
                    if attempt < self.max_retries:
                        time.sleep(sleep_seconds)
                        continue
                    return None

                elif response.status_code in (403, 404):
                    # Channel does not exist, is restricted or private
                    return None

                else:
                    # Other unexpected status code
                    return None

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError):
                self.consecutive_errors += 1
                sleep_seconds = min(30.0, (self.backoff_factor ** self.consecutive_errors) * 1.5)
                if attempt < self.max_retries:
                    time.sleep(sleep_seconds)
                    continue
                return None

            finally:
                if should_close:
                    client.close()

        return None
