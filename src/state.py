import threading
from .downloader import ProcessContainer


def _quality_str(q):
    q = str(q).lower()
    if '2160' in q or '4k' in q: return 'bestvideo[height<=2160]+bestaudio/best[height<=2160]'
    if '1080' in q: return 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
    if '720'  in q: return 'bestvideo[height<=720]+bestaudio/best[height<=720]'
    if '480'  in q: return 'bestvideo[height<=480]+bestaudio/best[height<=480]'
    if '360'  in q: return 'bestvideo[height<=360]+bestaudio/best[height<=360]'
    if 'best' in q: return 'bestvideo+bestaudio/best'
    return 'bestvideo[height<=480]+bestaudio/best[height<=480]'


class AppState:
    def __init__(self):
        self.stop = threading.Event()
        self.pause = threading.Event()

        self._state_lock = threading.RLock()
        self.current_process = ProcessContainer()
        self._series_url = None
        self._series_name = None
        self._filepath = None
        self._episode_name = None
        self._expected_size = 0

        self._ui_lock = threading.Lock()
        self._output_mode = 'normal'
        self._last_status = {
            'screen': 'Ready', 'status': 'Idle',
            'title': '', 'source': '', 'current': '', 'progress': '',
        }

        self.tmux_active = False
        self.control_stop = threading.Event()

    def set_download_state(self, series_url, series_name, episode_name, filepath, expected_size):
        with self._state_lock:
            self._series_url = series_url
            self._series_name = series_name
            self._episode_name = episode_name
            self._filepath = filepath
            self._expected_size = expected_size or 0

    def reset_download_state(self):
        with self._state_lock:
            self._series_url = None
            self._series_name = None
            self._filepath = None
            self._episode_name = None
            self._expected_size = 0

    def get_download_state(self):
        with self._state_lock:
            return {
                'series_url': self._series_url,
                'series_name': self._series_name,
                'filepath': self._filepath,
                'episode_name': self._episode_name,
                'expected_size': self._expected_size,
            }

    def has_active_download(self):
        with self._state_lock:
            return bool(self._series_url or self.current_process.proc)

    def set_output_mode(self, mode):
        with self._ui_lock:
            self._output_mode = 'debug' if str(mode).lower() == 'debug' else 'normal'

    def is_debug(self):
        with self._ui_lock:
            return self._output_mode == 'debug'

    def update_status(self, **kwargs):
        with self._ui_lock:
            self._last_status.update({k: v for k, v in kwargs.items() if v is not None})

    def get_status(self):
        with self._ui_lock:
            return dict(self._last_status)

    def make_ctx(self, cfg):
        return {
            'stop':            self.stop,
            'pause':           self.pause,
            'bandwidth':       cfg.get('bandwidth', 0),
            'quality':         _quality_str(cfg.get('quality', '480p')),
            'social_quality':  cfg.get('social_quality', '720p'),
            'parallel':        cfg.get('parallel', 1),
            'current_process': self.current_process,
            'disabled_sites':  cfg.get('disabled_sites', []),
            'wait':            lambda: None,
            'download_retries':     cfg.get('download_retries', 3),
            'download_timeout':     cfg.get('download_timeout', 120),
            'aria2c_connections':   cfg.get('aria2c_connections', 16),
            'aria2c_splits':        cfg.get('aria2c_splits', 16),
            'aria2c_min_split_size': cfg.get('aria2c_min_split_size', '1M'),
        }

    def reset_for_next_command(self):
        self.reset_download_state()
        self.stop.clear()
        self.pause.clear()
