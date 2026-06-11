# <div > AnimeNewsBot

> Automated Telegram bot that fetches anime news from RSS feeds and broadcasts them to channels.

[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.0+-purple?style=flat-square)](https://pyrogram.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-green?style=flat-square)](https://mongodb.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

---

## ✨ Features

- 🔁 **Multi-feed RSS polling** — configurable interval, no duplicate posts
- 📢 **Global channels** — admin-managed broadcast with BotifyX branding footer
- 👤 **User channels** — any user can connect their own channel via `/sudo`
- 🛡️ **Force subscription** — FSUB guard with owner/admin bypass
- 🎬 **YouTube embeds** — auto-downloads videos found in articles via yt-dlp
- 👮 **Admin hierarchy** — Owner → Admin → User with ban/unban system
- ⚡ **Smooth animations** — on `/start` and all commands

---

## 🛠️ Tech Stack

| Tool | Purpose |
|---|---|
| Python 3.10+ | Core language |
| PyroFork (Pyrogram) | Telegram bot framework |
| MongoDB Atlas | Database — users, channels, feeds |
| feedparser | RSS feed parsing |
| BeautifulSoup | Article scraping |
| yt-dlp | YouTube video downloads |
| aiohttp | Async HTTP requests |

---

## 🚀 Setup

**1. Clone**
```bash
git clone https://github.com/botifyx-bots/AnimeNews-Bot.git
cd AnimeNews-Bot
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Configure `config.py`**
```python
API_ID    = "your_api_id"
API_HASH  = "your_api_hash"
BOT_TOKEN = "your_bot_token"
MONGO_URI = "your_mongo_uri"
OWNER_ID  = 123456789
ADMINS    = [123456789]

FSUB_CHANNEL_ID       = -1001234567890
FSUB_CHANNEL_USERNAME = "your_channel"

URL_A = "https://myanimelist.net/rss/news.xml"
# URL_B = "https://another-feed.com/rss"
```

**4. Run**
```bash
python bot.py
```

**5. Docker (optional)**
```bash
docker build -t animenewsbot .
docker run -d --name animenewsbot animenewsbot
```

---

## 📋 Commands

### Public
| Command | Description |
|---|---|
| `/start` | Welcome animation + bot info |
| `/help` | Full command reference |
| `/sudo` | Inline panel to manage your channel |

### Admin
| Command | Description |
|---|---|
| `/add_chnl <ch>` | Add global broadcast channel |
| `/del_chnl <ch>` | Remove global broadcast channel |
| `/addfeed <url>` | Add RSS feed to database |
| `/setinterval <5min/2hr>` | Set global fetch interval |
| `/news <url> [pos]` | Manually send a specific entry |
| `/ban <id>` / `/unban <id>` | Ban or unban a user |
| `/broadcast` | Blast a message to all users |
| `/stats` | Live system resource usage |

### Owner Only
| Command | Description |
|---|---|
| `/add_admin <id>` | Grant admin role |
| `/del_admin <id>` | Revoke admin role |
| `/admin_list` | List all admins |
| `/sudo_delchnl <ch>` | Remove any user's channel |

---

## 👑 Credits

| Role | Contact |
|---|---|
| Developer & Owner | [彡 ΔNI_OTΔKU 彡](https://t.me/ITsANIMEN) |
| Main Dev & Base repo | [DARKXSIDE](https://t.me/DARKXSIDE78) |
| Main Channel | [Bᴏᴛɪғʏx ʙᴏᴛs](https://t.me/BotifyX_Pro_Botz) |
| Support Group | [Bᴏᴛɪғʏx-Bᴏᴛ Sᴜᴘᴘᴏʀᴛ](https://t.me/+ij3pcPOXv2U4MDll) |

---

*© 2025 BotifyX Pro Botz · MIT License*
