# Project Rules: Download Toolkit (ANONRODE)

This project is a CLI download toolkit built in Python to download movies and series from supported streaming services and social media sites.

## Workspace Conventions
- **Language**: Python 3.x
- **Core modules**: `main.py`, `downloader.py`, `extractors.py`, `search.py`
- **External tools**: `aria2c` for high-speed multi-threaded downloads, `yt-dlp` for media streaming sources.
- **Save directory**: Downloads are saved under `~/Downloads/Anon` on desktop and `/storage/emulated/0/Anon` on Android Termux.
