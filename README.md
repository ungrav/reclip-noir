# ReClip

> Enhanced fork of [averygan/reclip](https://github.com/averygan/reclip) — a self-hosted media downloader with a clean web UI.

This fork adds **VPN proxy routing**, **anti-bot bypass via browser sidecar**, **Apple-native codec selection**, **optimized audio extraction**, and **metadata embedding** — all running inside Docker.

![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-powered-red)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

---

## Credits

**Original project by [averygan](https://github.com/averygan/reclip)** — a lightweight, self-hosted video downloader with a beautiful web UI. All credit for the core concept, original UI design, and the initial Flask + yt-dlp architecture goes to the original author.

This fork builds upon that foundation with infrastructure and compatibility improvements documented below.

---

## What This Fork Adds

### 🛡️ Anti-Bot Bypass (YouTube CAPTCHA)
YouTube blocks cloud-based `yt-dlp` requests with "Sign in to confirm you're not a bot" challenges. This fork solves it with a **two-layer authentication strategy**:

1. **POT (Proof of Origin Token)** — Automatic token generation via the `bgutil-ytdlp-pot-provider` plugin with Deno runtime.
2. **Browser Sidecar** — A persistent Chromium container ([linuxserver/chromium](https://docs.linuxserver.io/images/docker-chromium/)) shares its authenticated cookies with `yt-dlp`. One manual YouTube login = persistent session across all downloads.

The backend automatically tries POT first, then falls back to browser cookies if authentication fails.

### 🌐 VPN Proxy Routing
All download traffic is routed through a VPN proxy chain with automatic failover:
- **Primary:** NordVPN proxy (port `8891`)
- **Fallback:** ProtonVPN proxy (port `8892`)
- **Last resort:** Direct connection (optional, per-request)

The browser sidecar also routes through the same VPN chain via a dynamically-generated PAC file.

### 🍎 Apple-Native Codec Selection (H.264)
The original project uses yt-dlp's default format selection, which may produce VP9/AV1 video inside MP4 containers. These codecs are **not natively supported** by macOS QuickTime, iOS, or Safari — resulting in black screens or playback failures.

This fork forces H.264 (AVC) codec selection by heavily weighting AVC formats during quality analysis (`is_avc * 1000000` scoring), ensuring every downloaded MP4 plays natively on Apple devices without needing VLC.

### ⚡ Optimized Audio Extraction
The original audio extraction downloads the **full video stream** (potentially gigabytes) just to discard it and keep the audio track. This fork adds `-f bestaudio` before the extraction flags, downloading **only the audio stream** (~5MB instead of ~5GB), making MP3 conversion near-instant.

### 🎵 Metadata & Cover Art Embedding
Downloads now include embedded metadata (title, artist, year) and thumbnail cover art via `--embed-metadata` and `--embed-thumbnail`. The `mutagen` library is included for reliable MP3 cover art injection that `ffmpeg` alone often corrupts.

### 📋 Smart Clipboard Paste
The frontend extracts only valid URLs from clipboard content using regex, ignoring any surrounding text or formatting artifacts.

### 📱 Mobile Share Extension
Includes a declarative web extension manifest (`mobile-ext/`) for sharing URLs directly from mobile browsers to the ReClip instance.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Docker Network                │
│                                                 │
│  ┌──────────────┐       ┌────────────────────┐  │
│  │   reclip     │       │  reclip-browser    │  │
│  │  (Flask)     │◄─────►│  (Chromium)        │  │
│  │  Port 8899   │cookies│  Port 3000         │  │
│  │              │       │  (KasmVNC Web UI)  │  │
│  │  yt-dlp      │       │                    │  │
│  │  + ffmpeg    │       │  Persistent login  │  │
│  │  + Deno/POT  │       │  session & cookies │  │
│  └──────┬───────┘       └────────┬───────────┘  │
│         │                        │              │
│         └───────┬────────────────┘              │
│                 │ VPN Proxy Chain               │
│         ┌───────▼───────┐                       │
│         │  PAC Router   │                       │
│         │ NordVPN:8891  │                       │
│         │ ProtonVPN:8892│                       │
│         │ DIRECT (opt)  │                       │
│         └───────────────┘                       │
└─────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
git clone https://github.com/ungrav/reclip.git
cd reclip
docker compose up -d
```

Open **http://localhost:8899** for the downloader UI.
Open **http://localhost:3000** for the browser sidecar (first-time YouTube login).

### First-Time Setup

1. Open the browser sidecar at `http://localhost:3000`
2. Log in to YouTube with any Google account
3. Close the browser tab — the session persists
4. All future downloads will use that authenticated session

---

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

---

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube · TikTok · Instagram · Twitter/X · Reddit · Facebook · Vimeo · Twitch · Dailymotion · SoundCloud · Loom · Streamable · Pinterest · Tumblr · Threads · LinkedIn · and [1000+ more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

---

## Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.12 + Flask |
| **Frontend** | Vanilla HTML/CSS/JS (single file, no build step) |
| **Download Engine** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/) |
| **Anti-Bot** | [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) + [Deno](https://deno.land/) |
| **Browser Sidecar** | [linuxserver/chromium](https://docs.linuxserver.io/images/docker-chromium/) |
| **Metadata** | [mutagen](https://mutagen.readthedocs.io/) |

---

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

---

## License

[MIT](LICENSE) — Same as the [original project](https://github.com/averygan/reclip).
