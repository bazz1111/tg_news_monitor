# Telegram News Monitor (tg_news_monitor)

> 24/7 Automated Telegram Breaking News Monitor & Feishu Interactive Card Alerting Service.

A production-grade, account-free monitoring engine that ingests Telegram channels via public web previews with **zero account ban risk**, evaluates newsworthiness and urgency using **DeepSeek LLM**, and dispatches structured interactive cards to **Feishu (Lark) Webhooks** with threshold gating and rate-limiting resilience.

---

## 🚀 Key Features

- **Zero-Ban Account-Free Ingestion**: Polling public web previews (`https://t.me/s/{channel}`) without phone numbers, session tokens, passwords, or official Telegram Bot API keys. 0% risk of account suspension.
- **Anti-Scraping Defenses**: Modern browser User-Agent header rotation, randomized interval jitter, and exponential backoff on HTTP 429 / 5xx responses.
- **Robust SQLite Persistent Deduplication**: SQLite in Write-Ahead Logging (`WAL`) mode with `UNIQUE(channel, message_id)` composite constraints preventing duplicate alerts across restarts and re-feeds.
- **DeepSeek Intelligence Engine**: Calibrated 1-10 newsworthiness scoring, spam/advertisement filtering, and concise Chinese bulleted briefings using `deepseek-chat` or `deepseek-reasoner`. Multi-stage fallback on API quota exhaustion.
- **Feishu Card Schema 2.0**: 4-tier dynamic color badges (red, orange, blue, grey), permalink action buttons, and markdown bullet summaries with HMAC-SHA256 signature verification.
- **Headless 24/7 Deployment**: Lean non-root Docker container, Docker Compose persistent volume orchestration, log rotation, and comprehensive healthchecks.

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
| `DEEPSEEK_MODEL` | str | `deepseek-chat` | DeepSeek model identifier (`deepseek-chat`, `deepseek-reasoner`). |
| `HOTNESS_THRESHOLD` | int | `7` | Minimum score (1-10) required to trigger a Feishu card dispatch. |
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
DEEPSEEK_MODEL=deepseek-chat
```
Supports both `deepseek-chat` (fast, cost-effective) and `deepseek-reasoner` (deep analysis).

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
