# Project: download-toolkit Progress UI Redesign

## Architecture
- Core Package: `src/downloader.py` (`LiveProgress` class)
- Test Suite: `test_ui_mock.py` (offline mock test script feeding simulated `yt-dlp` progress lines starting with `download:@@DLP@@`)

## Code Layout
- `src/downloader.py`: Downloader implementation, `LiveProgress` progress bar and terminal renderer
- `test_ui_mock.py`: Offline mock test script feeding simulated `yt-dlp` progress output lines into `LiveProgress`

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Testing Track | Create `test_ui_mock.py` offline mock test harness feeding simulated `yt-dlp` `download:@@DLP@@` lines | none | IN_PROGRESS |
| M1 | Aria2c UI Implementation | Redesign `LiveProgress` in `src/downloader.py` to aria2c single-line compact format `[#<id> <dl>/<total>(<pct>%) DL:<speed> ETA:<eta>]` retaining metrics, zero dependencies | none | IN_PROGRESS |
| M2 | Final E2E Pass & Forensic Audit | 100% test pass on `test_ui_mock.py`, py_compile clean, Challenger adversarial testing, and Forensic Integrity Audit | E2E, M1 | PLANNED |

## Interface Contracts
### LiveProgress & yt-dlp Parser Interface
- Progress output line format: `[#123456 12MiB/50MiB(24%) DL:3.2MiB ETA:11s]` (or with fragment metrics `Frag:12/50` when present).
- Parser input: lines starting with `download:@@DLP@@` emitted by yt-dlp download hook/stdout.
- Zero external libraries (`rich`, `tqdm` prohibited). Standard ANSI / carriage return single-line output.
- Offline mock execution: `test_ui_mock.py` must run with zero network/data usage.
