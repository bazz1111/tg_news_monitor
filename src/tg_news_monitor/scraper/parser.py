"""DOM parser for Telegram public channel web previews (https://t.me/s/{channel})."""

import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup, Tag

from tg_news_monitor.core.models import TelegramPost


class TelegramWebParser:
    """Robust HTML parser extracting structured posts from Telegram public web preview HTML."""

    RE_PHOTO_STYLE = re.compile(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", re.IGNORECASE)

    @classmethod
    def parse_html(
        cls,
        html_content: str,
        default_channel: Optional[str] = None,
    ) -> List[TelegramPost]:
        """Parses full Telegram web preview HTML and returns list of TelegramPost instances."""
        soup = BeautifulSoup(html_content, "html.parser")
        posts: List[TelegramPost] = []

        # Find all message elements (either .tgme_widget_message_wrap or direct .tgme_widget_message)
        message_elements = soup.select(".tgme_widget_message")
        if not message_elements:
            # Fallback if wraps are used
            message_elements = [
                el for wrap in soup.select(".tgme_widget_message_wrap")
                if (el := wrap.select_one(".tgme_widget_message"))
            ]

        for msg_el in message_elements:
            post = cls._parse_message_element(msg_el, default_channel)
            if post is not None:
                posts.append(post)

        return posts

    @classmethod
    def parse_channel_page(
        cls,
        channel: str,
        html_content: str,
    ) -> List[TelegramPost]:
        """Convenience alias accepting channel name explicitly."""
        return cls.parse_html(html_content, default_channel=channel)

    @classmethod
    def _parse_message_element(
        cls,
        msg_el: Tag,
        default_channel: Optional[str] = None,
    ) -> Optional[TelegramPost]:
        # 1. Skip system service messages (e.g. pinned message notifications, photo changes)
        classes = msg_el.get("class", [])
        if any(c in classes for c in ("tgme_widget_message_service", "service_message")):
            return None

        # 2. Extract channel slug and message_id from data-post (e.g. "durov/527")
        data_post = msg_el.get("data-post", "")
        if not data_post or "/" not in data_post:
            return None

        channel_slug, msg_id_str = data_post.split("/", 1)
        try:
            message_id = int(msg_id_str)
        except ValueError:
            return None

        channel_name = (channel_slug or default_channel or "").lower().lstrip("@")
        if not channel_name:
            return None

        direct_url = f"https://t.me/{channel_name}/{message_id}"

        # 3. Extract publication timestamp from <time datetime="...">
        published_at = cls._extract_timestamp(msg_el)

        # 4. Extract and format message body text
        text_el = msg_el.select_one(".tgme_widget_message_text")
        cleaned_text = cls._extract_formatted_text(text_el) if text_el else ""

        # 5. Extract forward attribution
        forward_from = cls._extract_forward_attribution(msg_el)

        # 6. Extract media indicators and URLs
        has_media, media_type, media_urls = cls._extract_media(msg_el)

        # If text is empty and there's no media, but text_el was None, this might be a pure media or empty post
        # If there is media but no caption text, set a descriptive indicator if text is empty
        if not cleaned_text and has_media:
            cleaned_text = f"[{media_type.upper() if media_type else 'MEDIA'}]"

        # 7. Extract view count badge
        views_el = msg_el.select_one(".tgme_widget_message_views")
        views = views_el.get_text(strip=True) if views_el else None

        return TelegramPost(
            channel=channel_name,
            message_id=message_id,
            published_at=published_at,
            text=cleaned_text,
            has_media=has_media,
            media_type=media_type,
            direct_url=direct_url,
            forward_from=forward_from,
            media_urls=media_urls,
            views=views,
            raw_html=str(msg_el),
        )

    @classmethod
    def _extract_timestamp(cls, msg_el: Tag) -> datetime:
        time_el = msg_el.select_one("time[datetime]")
        if time_el and time_el.get("datetime"):
            raw_dt = time_el["datetime"].strip()
            try:
                # Handle ISO formats with Z or +00:00
                return datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @classmethod
    def _extract_formatted_text(cls, text_el: Tag) -> str:
        """Extracts text while preserving line breaks and converting links to markdown [text](url)."""
        # Create a working copy so we don't mutate the parent DOM tree
        soup_copy = BeautifulSoup(str(text_el), "html.parser")
        root = soup_copy.find()
        if not root:
            return text_el.get_text(strip=True)

        # Replace <br> and <br/> with newline characters
        for br in root.find_all("br"):
            br.replace_with("\n")

        # Convert anchor tags to markdown format [text](href)
        for a in root.find_all("a"):
            href = a.get("href", "").strip()
            link_text = a.get_text().strip()
            if href and link_text:
                # Avoid redundant [url](url) if text is identical to link
                if href == link_text:
                    a.replace_with(href)
                elif link_text.startswith("@"):
                    # Telegram user/channel tag
                    a.replace_with(link_text)
                else:
                    a.replace_with(f"[{link_text}]({href})")

        text = root.get_text()
        # Normalize excessive blank lines (>2) while preserving paragraphs
        lines = [line.strip() for line in text.splitlines()]
        cleaned = "\n".join(lines).strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned

    @classmethod
    def _extract_forward_attribution(cls, msg_el: Tag) -> Optional[str]:
        fwd_name_el = msg_el.select_one(".tgme_widget_message_forwarded_from_name")
        if fwd_name_el:
            return fwd_name_el.get_text(strip=True)
        return None

    @classmethod
    def _extract_media(cls, msg_el: Tag) -> Tuple[bool, Optional[str], List[str]]:
        media_urls: List[str] = []
        media_type: Optional[str] = None

        # Photos & Albums
        photo_wraps = msg_el.select(".tgme_widget_message_photo_wrap")
        if photo_wraps:
            for wrap in photo_wraps:
                style = wrap.get("style", "")
                m = cls.RE_PHOTO_STYLE.search(style)
                if m:
                    media_urls.append(m.group(1))
            media_type = "album" if len(photo_wraps) > 1 else "photo"

        # Videos
        video_el = msg_el.select_one(
            ".tgme_widget_message_video, .tgme_widget_message_video_player, video"
        )
        if video_el:
            media_type = "video"
            src = video_el.get("src")
            if src:
                media_urls.append(src)

        # Documents & Files
        doc_el = msg_el.select_one(".tgme_widget_message_document")
        if doc_el:
            media_type = "document"

        # Polls
        poll_el = msg_el.select_one(".tgme_widget_message_poll")
        if poll_el:
            media_type = "poll"

        has_media = media_type is not None or len(media_urls) > 0
        return has_media, media_type, media_urls
