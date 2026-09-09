# Telegram News Monitor (tg_news_monitor)

> 24/7 Automated Telegram Breaking News Monitor & Feishu Interactive Card Alerting Service.

A production-grade, account-free monitoring engine that ingests Telegram channels via public web previews with **zero account ban risk**, evaluates newsworthiness and urgency using **DeepSeek LLM**, and dispatches structured interactive cards to **Feishu (Lark) Webhooks** with threshold gating and rate-limiting resilience.

---

## 🚀 Key Features

- **Zero-Ban Account-Free Ingestion**: Polling public web previews (`https://t.me/s/{channel}`) without phone numbers, session tokens, passwords, or official Telegram Bot API keys. 0% risk of account suspension.
- **Anti-Scraping Defenses**: Modern browser User-Agent header rotation, randomized interval jitter, and exponential backoff on HTTP 429 / 5xx responses.
- **Robust SQLite Persistent Deduplication**: SQLite in Write-Ahead Logging (`WAL`) mode with `UNIQUE(channel, message_id)` composite constraints preventing duplicate alerts across restarts and re-feeds.
- **Batch Digest Mode**: Pending posts buffer until `DIGEST_MIN_CANDIDATES` (default 3) or `DIGEST_MAX_WAIT_SECONDS`; one DeepSeek LLM call evaluates the batch.
- **Multi Single Feishu Cards**: Each selected digest item becomes its own Feishu card, spaced by `DIGEST_CARD_INTERVAL_SECONDS` (default **10s** / `card_gap`). Cards contain **no Telegram links / channel / message IDs**.
- **Local Spam + Always-On Crypto Filter**: Cheap zero-LLM coarse filter drops spam/noise; crypto/blockchain content is **always banned in code** (no env toggle) before and after the LLM.
- **DeepSeek Intelligence Engine**: Batch digest evaluation with non-thinking mode (`thinking: disabled`) to save tokens. Default model `deepseek-v4-flash`. Multi-stage fallback on API quota exhaustion.
- **Feishu Card Schema 2.0**: Structured investment-impact cards with HMAC-SHA256 signature verification.
- **Headless 24/7 Deployment**: Lean non-root Docker container, Docker Compose persistent volume orchestration, log rotation. Compose uses `network_mode: host` so nested Docker can reach `t.me`.

---

## 🏛 Architecture

```
                       ┌─────────────────────────────────┐
                       │  Target Telegram Channels       │
                       │  https://t.me/s/{channel_name}  │
                       └───────────────┬─────────────────┘
                                       │
                         [Scraper & Anti-Scrape Engine]
                         (User-Agent Rotation, Jitter)
                                       │
                                       ▼
                       ┌─────────────────────────────────┐
                       │ TelegramWebParser (HTML DOM)    │
                       │  Extracts IDs, UTC Time, Text   │
                       └───────────────┬─────────────────┘
                                       │
                       ┌───────────────▼─────────────────┐
                       │ SQLite PostRepository (WAL Mode)│◄── De-duplication
                       │  filter_unprocessed(posts)      │    UNIQUE(ch, id)
                       └───────────────┬─────────────────┘
                                       │ (New Posts Only)
                                       ▼
                       ┌─────────────────────────────────┐
                       │ Grok (xAI) Evaluator Engine     │
                       │  Score 1-10, Filter Spam, Brief │
                       └───────────────┬─────────────────┘
                                       │
                                       ▼
                       ┌─────────────────────────────────┐
                       │ Hotness Threshold Gating        │
                       │  Score >= HOTNESS_THRESHOLD?    │
                       └───────┬─────────────────┬───────┘
                               │ YES             │ NO
                               ▼                 ▼
                    ┌─────────────────────┐  ┌─────────────────────┐
                    │ FeishuCardBuilder   │  │ Stored in SQLite DB │
                    │ Schema 2.0 Card JSON│  │ (Alert suppressed)  │
                    └──────────┬──────────┘  └─────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ FeishuWebhookSender │
                    │ Rate-limit Retries  │
                    └─────────────────────┘
```

---

## 📋 Configuration Options

Configuration can be provided via environment variables, `.env` file, or `config.yaml`.

| Variable | Type | Default | Description |
|---|---|---|---|
| `TELEGRAM_CHANNELS` | List[str] | `[]` | Comma-separated channel handles (e.g. `durov,telegram,whale_alert`). |
| `POLL_INTERVAL_SECONDS` | int | `60` | Polling frequency in seconds (minimum 5s). |
| `MAX_JITTER_SECONDS` | int | `15` | Maximum randomized delay added to interval to prevent pattern detection. |
| `INTER_CHANNEL_DELAY_SECONDS` | float | `2.0` | Pause in seconds between consecutive channel queries. |
| `DEEPSEEK_API_KEY` | str | `""` | DeepSeek API key (`https://platform.deepseek.com/`). |
| `DEEPSEEK_API_BASE` | str | `https://api.deepseek.com` | DeepSeek API base endpoint URL (default: `https://api.deepseek.com`). |
| `DEEPSEEK_MODEL` | str | `deepseek-v4-flash` | DeepSeek model identifier (recommended: `deepseek-v4-flash`). |
| `HOTNESS_THRESHOLD` | int | `7` | Legacy per-post threshold (batch digest mode uses LLM selection instead). |
| `DIGEST_MIN_CANDIDATES` | int | `3` | Open digest gate when pending candidates reach this count. |
| `DIGEST_MAX_WAIT_SECONDS` | int | `900` | Or open gate when oldest pending candidate exceeds this age. |
| `DIGEST_CARD_INTERVAL_SECONDS` | float | `10` | Pause (`card_gap`) between multi single Feishu cards in one batch. |
| `FEISHU_WEBHOOK_URL` | str | `""` | Feishu custom bot webhook endpoint URL. |
| `FEISHU_WEBHOOK_SECRET` | str | `None` | Optional HMAC-SHA256 signature verification secret. |
| `DB_PATH` | str | `data/tg_news.db` | Path to persistent SQLite database file. |
| `LOG_LEVEL` | str | `INFO` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `HTTP_PROXY` / `HTTPS_PROXY`| str | `None` | Optional outbound HTTP/HTTPS proxy. |

---

## 🤖 DeepSeek API Configuration

Configure your DeepSeek API credentials in `.env`:
```ini
DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```
Default is `deepseek-v4-flash`. Request payloads disable DeepSeek thinking mode to save tokens.
Crypto/blockchain posts are always filtered in code (not configurable via env).

---

## ⚡ Quickstart

### 1. Local Environment

```bash
# Clone the repository
git clone <repo-url>
cd tg_news_monitor

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy configuration template
cp .env.example .env
# Edit .env with your DEEPSEEK_API_KEY and FEISHU_WEBHOOK_URL

# Initialize database schema
python -m tg_news_monitor.main --init-db

# Run a single test monitoring pass
python -m tg_news_monitor.main --once

# Start continuous 24/7 background daemon
python -m tg_news_monitor.main
```

---

## 🐳 Docker Deployment

### 1. Docker Compose (Recommended)

```bash
# Prepare environment file
cp .env.example .env
vim .env

# Build and start background service
# Note: docker-compose.yml already sets network_mode: host (needed for t.me egress
# from nested Docker / custom bridges that otherwise TCP-timeout).
docker compose up -d

# View real-time logs
docker compose logs -f

# Stop service gracefully
docker compose down
```

### 2. Standalone Docker Run

```bash
# Build image
docker build -t tg_news_monitor:latest .

# Run container with volume mount for persistent database
docker run -d \
  --name tg_news_monitor \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  tg_news_monitor:latest
```

---

## 🛠 CLI Options Reference

```
usage: tg_news_monitor [-h] [--once] [--config CONFIG] [--init-db] [--version]

24/7 automated Telegram news monitoring and breaking-event alerting service.

options:
  -h, --help            show this help message and exit
  --once                Run a single polling pass across target channels and exit.
  --config CONFIG, -c CONFIG
                        Path to YAML, JSON, or .env configuration file.
  --init-db             Initialize SQLite database schema and tables, then exit immediately.
  --version, -v         Show version information and exit.
```

---

## 🧪 Testing

Run the test suite:

```bash
# Run unit tests
pytest tests/test_config.py tests/test_runner.py -v

# Run full test suite
pytest tests/ -v
```

---

## 📄 License

MIT License.
