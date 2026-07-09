from .base import *

def extract_social(url, session, ctx=None):
    ctx  = ctx or {}
    stop, wait, bw, quality, parallel, cur_proc, pause = _ctx(ctx)

    def _user_stopped():
        return _stopped({'stop': stop})

    bd     = base_domain(url).replace('https://', '').replace('www.', '')
    is_yt  = 'youtube.com' in url or 'youtu.be' in url
    is_pin = 'pinterest.com' in url or 'pin.it' in url

    # ── Pinterest ──────────────────────────────────────────────
    if is_pin:
        safe_print(f"[*] Pinterest")
        # Boards: pinterest.com/user/boardname/  — multiple pins
        # Single pin: pinterest.com/pin/12345/
        is_board = bool(re.search(r'pinterest\.com/[^/]+/[^/]+/?$', url)) and '/pin/' not in url
        folder = os.path.join(BASE_DIR, 'Pinterest')
        summary = DownloadSummary()
        if is_board:
            board_slug = safe_filename(url.rstrip('/').split('/')[-1] or 'board')
            folder = os.path.join(folder, board_slug)
            safe_print(f"[*] Board: {board_slug}")
            safe_print(f"[*] Saving to: {folder}")
            fmt = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            os.makedirs(folder, exist_ok=True)
            out_template = os.path.join(folder, '%(playlist_index)s - %(title)s.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--yes-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--no-warnings', '--progress', '--newline',
                url
            ]
        else:
            pin_id  = re.search(r'/pin/(\d+)', url)
            slug    = pin_id.group(1) if pin_id else 'pin'
            filename = safe_filename(f"{slug}.mp4")
            safe_print(f"[*] Pin: {slug}")
            safe_print(f"[*] Saving to: {folder}")
            fmt = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            os.makedirs(folder, exist_ok=True)
            out_template = os.path.join(folder, safe_filename(slug) + '.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--no-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--no-warnings', '--progress', '--newline',
                url
        ]
            download_social_ytdlp(url, folder, filename, summary,
                                  current_process=cur_proc,
                                  out_template=out_template,
                                  stop_flag=stop,
                                  preferred_quality=ctx.get('social_quality', '720p'),
                                  smart_select=True)
            summary.report()
            return
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            register_process(proc)
            if cur_proc is not None:
                cur_proc.proc = proc
            while proc.poll() is None:
                if _user_stopped():
                    proc.terminate()
                    break
                time.sleep(0.5)
            finish_process(proc)
            if proc.returncode == 0 and not _user_stopped():
                summary.add_success()
            else:
                summary.add_failed('pinterest')
        except Exception as e:
            safe_print(f'[✗] Pinterest error: {e}')
            summary.add_failed('pinterest')
        finally:
            unregister_process(proc)
            if cur_proc is not None:
                cur_proc.proc = None
        summary.report()
        return

    # ── YouTube ────────────────────────────────────────────────
    if is_yt:
        has_list    = bool(re.search(r'[?&]list=', url))
        has_watch   = 'watch?v=' in url or 'youtu.be/' in url

        # Single video that is part of a playlist
        if has_watch and has_list:
            safe_print(f"\n  This video is part of a playlist")
            safe_print(f"  [1] This video only")
            safe_print(f"  [2] Full playlist")
            safe_print(f"  [0] Cancel")
            try:
                choice = input("\n  Pick: ").strip()
            except EOFError:
                choice = '1'
            if choice == '0':
                return
            if choice == '2':
                # strip to just the list URL
                list_id = re.search(r'list=([^&]+)', url)
                if list_id:
                    url = f'https://www.youtube.com/playlist?list={list_id.group(1)}'
                has_watch = False  # fall through to playlist flow below
            else:
                has_list = False   # fall through to single video flow below

        # Pure playlist
        if has_list and not has_watch:
            count, pl_title = _yt_get_playlist_count(url)
            items_sel   = _yt_playlist_items_prompt(count)
            if items_sel is None:
                return
            fmt         = _yt_quality_prompt(quality)
            list_id     = re.search(r'[?&]list=([^&]+)', url)
            # Use playlist title as folder name if available, else list ID
            if pl_title:
                folder_name = safe_filename(pl_title)
            elif list_id:
                folder_name = safe_filename(list_id.group(1))
            else:
                folder_name = 'playlist'
            folder      = os.path.join(BASE_DIR, 'YouTube', folder_name)
            os.makedirs(folder, exist_ok=True)
            safe_print(f'[*] Saving to: {folder}')
            out_template = os.path.join(folder, '%(playlist_index)s - %(title)s.%(ext)s')
            cmd = [
                'yt-dlp', '-f', fmt,
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--yes-playlist',
                '--retries', '3', '--fragment-retries', '3',
                '--no-warnings', '--progress', '--newline',
            ]
            if items_sel != 'all':
                cmd += ['--playlist-items', items_sel]
            cmd.append(url)
            summary = DownloadSummary()
            proc = None
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
                register_process(proc)
                if cur_proc is not None:
                    cur_proc.proc = proc
                while proc.poll() is None:
                    if _user_stopped():
                        proc.terminate()
                        break
                    time.sleep(0.5)
                finish_process(proc)
                if proc.returncode == 0 and not _user_stopped():
                    summary.add_success()
                else:
                    summary.add_failed('playlist')
            except Exception as e:
                safe_print(f'[✗] Playlist error: {e}')
                summary.add_failed('playlist')
            finally:
                unregister_process(proc)
                if cur_proc is not None:
                    cur_proc.proc = None
            summary.report()
            return

        # Single YouTube video
        fmt      = _yt_quality_prompt(quality)
        folder   = os.path.join(BASE_DIR, 'YouTube')
        os.makedirs(folder, exist_ok=True)
        out_template = os.path.join(folder, '%(title)s.%(ext)s')
        safe_print(f'[*] Saving to: {folder}')
        summary = DownloadSummary()
        download_social_ytdlp(url, folder, 'video.mp4', summary,
                              current_process=cur_proc,
                              quality_override=fmt,
                              out_template=out_template,
                              stop_flag=stop,
                              smart_select=False)
        summary.report()
        return

    # ── Everything else (Instagram, TikTok, Facebook, etc.) ───
    safe_print(f"[*] Social/Generic mode: {bd}")
    name     = bd.split('.')[0].title()
    folder   = os.path.join(BASE_DIR, 'Social', safe_filename(name))
    slug     = url.rstrip('/').split('/')[-1] or 'video'
    slug     = re.sub(r'[^\w-]', '_', slug)[:50]
    filename = safe_filename(f"{slug}.mp4")
    safe_print(f"[*] Downloading: {filename}")
    safe_print(f"[*] Saving to: {folder}")
    summary  = DownloadSummary()
    out_template = os.path.join(folder, '%(uploader)s - %(title).80s [%(id)s].%(ext)s')
    download_social_ytdlp(url, folder, filename, summary,
                          current_process=cur_proc,
                          out_template=out_template,
                          stop_flag=stop,
                          preferred_quality=ctx.get('social_quality', '720p'),
                          smart_select=True)
    summary.report()
def _yt_quality_prompt(default_quality):
    """Ask user to pick a quality. Returns a yt-dlp format string."""
    QUALITY_MAP = {
        '1': ('360p',  'bestvideo[height<=360]+bestaudio/best[height<=360]'),
        '2': ('480p',  'bestvideo[height<=480]+bestaudio/best[height<=480]'),
        '3': ('720p',  'bestvideo[height<=720]+bestaudio/best[height<=720]'),
        '4': ('1080p', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'),
        '5': ('2160p', 'bestvideo[height<=2160]+bestaudio/best[height<=2160]'),
    }
    label_to_num = {'360p': '1', '480p': '2', '720p': '3', '1080p': '4', '2160': '5', '2160p': '5', '4k': '5'}
    default_label = '480p'
    for label in label_to_num:
        if label in default_quality:
            default_label = '2160p' if label in ('2160', '4k') else label
            break
    default_num = label_to_num.get(default_label, '2')

    safe_print(f"\n  Quality: [1] 360p  [2] 480p  [3] 720p  [4] 1080p  [5] 4K  (default: {default_label})")
    try:
        choice = input("  Pick [1-5] or Enter for default: ").strip()
    except EOFError:
        choice = ''
    if not choice:
        choice = default_num
    _, fmt = QUALITY_MAP.get(choice, QUALITY_MAP[default_num])
    return fmt

def _yt_get_playlist_count(url):
    import shutil
    if not shutil.which('yt-dlp'):
        return None, None
    try:
        import subprocess
        result = subprocess.run(
            ['yt-dlp', '--flat-playlist',
             '--print', 'id',
             '--print', 'playlist_title',
             '--no-warnings', '--quiet',
             '--no-check-certificates',
             url],
            capture_output=True, text=True, timeout=25,
            stdin=subprocess.DEVNULL
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        ids    = lines[0::2]
        titles = [t for t in lines[1::2] if t and t != 'NA']
        count  = len(ids) if ids else None
        title  = titles[0] if titles else None
        return count, title
    except Exception:
        return None, None

def _yt_playlist_items_prompt(count):
    count_str = str(count) if count else '?'
    safe_print(f"\n  Playlist detected - {count_str} videos")
    safe_print(f"  [1] Download all")
    safe_print(f"  [2] Range      (e.g. 5-10)")
    safe_print(f"  [3] Specific   (e.g. 1,3,7)")
    safe_print(f"  [0] Cancel")
    try:
        choice = input("\n  Pick: ").strip()
    except EOFError:
        return None
    if choice == '0' or not choice:
        return None
    if choice == '1':
        return 'all'
    if choice == '2':
        try:
            r = input("  Range (e.g. 5-10): ").strip()
            parts = r.split('-')
            if len(parts) != 2:
                raise ValueError("need exactly two numbers")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                raise ValueError("start must be less than end")
            return r
        except Exception:
            safe_print("  [!] Invalid range - use format: 5-10")
            return None
    if choice == '3':
        try:
            items = input("  Items (e.g. 1,3,7): ").strip()
            [int(x) for x in items.split(',')]
            return items
        except Exception:
            safe_print("  [!] Invalid items - use format: 1,3,7")
            return None
    return None
