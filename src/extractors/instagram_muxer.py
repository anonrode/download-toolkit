import os
import re
import json
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
GRAPHQL_URL = "https://www.instagram.com/api/graphql"
DOC_ID = "27130156389949648"
POST_BASE = "https://www.instagram.com/"
IG_APP_ID = "936619743392459"

SHORTCODE_RE = re.compile(
    r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
    re.IGNORECASE
)
LSD_EQMC_RE = re.compile(
    r'<script\b[^>]*\bid="__eqmc"[^>]*>(\{.*?\})</script>',
    re.DOTALL
)
LSD_TOKEN_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')
SJS_RE = re.compile(r'<script\b[^>]+\bdata-sjs>(\{.+?\})</script>')

@dataclass
class MediaParts:
    photo_url: str
    audio_url: str
    duration_ms: int
    title: Optional[str] = None
    artist: Optional[str] = None
    caption: Optional[str] = None
    has_video: bool = False
    carousel_photo_urls: List[str] = field(default_factory=list)


def shortcode_from_url(url: str) -> Optional[str]:
    m = SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def id_to_pk(shortcode: str) -> Optional[str]:
    if not shortcode:
        return None
    pk = 0
    for char in shortcode:
        idx = B64.find(char)
        if idx == -1:
            return None
        pk = (pk * 64) + idx
    return str(pk)


def _best_candidate(candidates: List[Dict[str, Any]]) -> Optional[str]:
    if not candidates:
        return None
    best_url = None
    best_w = -1
    for c in candidates:
        if not isinstance(c, dict):
            continue
        url = c.get("url")
        if not url or not isinstance(url, str) or not url.strip():
            continue
        w = c.get("width", 0) or 0
        if w > best_w:
            best_w = w
            best_url = url
    return best_url


def _all_carousel_photos(m: Dict[str, Any]) -> List[str]:
    carousel = m.get("carousel_media")
    if not isinstance(carousel, list):
        return []
    urls = []
    for child in carousel:
        if not isinstance(child, dict):
            continue
        v = child.get("video_versions")
        if v and isinstance(v, list) and len(v) > 0:
            continue
        cands = child.get("image_versions2", {}).get("candidates", [])
        best = _best_candidate(cands)
        if best:
            urls.append(best)
    return urls


def _music_asset_info(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mm = m.get("music_metadata")
    if not isinstance(mm, dict):
        return None
    mi = mm.get("music_info")
    if isinstance(mi, dict):
        return mi.get("music_asset_info") or mi.get("music_consumption_info")
    return mm.get("music_asset_info") or mm.get("music_consumption_info")


def media_to_parts(m: Dict[str, Any]) -> Optional[MediaParts]:
    video_versions = m.get("video_versions")
    has_video = bool(video_versions and isinstance(video_versions, list) and len(video_versions) > 0)
    if has_video:
        return MediaParts(
            photo_url="",
            audio_url="",
            duration_ms=0,
            has_video=True
        )

    single_photo = _best_candidate(m.get("image_versions2", {}).get("candidates", []))
    is_carousel = "carousel_media" in m
    carousel_photos = _all_carousel_photos(m) if is_carousel else []
    photo_url = single_photo or (carousel_photos[0] if carousel_photos else "")
    if not photo_url:
        return None

    audio = _music_asset_info(m)
    audio_url = ""
    duration_ms = 0
    title = None
    artist = None

    if audio and isinstance(audio, dict):
        audio_url = audio.get("progressive_download_url") or ""
        duration_ms = max(0, int(audio.get("duration_in_ms") or 0))
        title = audio.get("title") or None
        artist = audio.get("display_artist") or audio.get("ig_artist") or None

    caption = None
    raw_cap = m.get("caption")
    if isinstance(raw_cap, dict):
        caption = raw_cap.get("text")
    elif not caption and "edge_media_to_caption" in m:
        edges = m.get("edge_media_to_caption", {}).get("edges", [])
        if edges and isinstance(edges, list) and isinstance(edges[0], dict):
            caption = edges[0].get("node", {}).get("text")

    return MediaParts(
        photo_url=photo_url,
        audio_url=audio_url,
        duration_ms=duration_ms,
        title=title,
        artist=artist,
        caption=caption,
        has_video=False,
        carousel_photo_urls=carousel_photos
    )


def _find_media_objects(root: Any, out: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if out is None:
        out = []
    if isinstance(root, dict):
        if ("image_versions2" in root or "video_versions" in root or
                "carousel_media" in root or ("pk" in root and ("code" in root or "taken_at" in root))):
            out.append(root)
        for v in root.values():
            _find_media_objects(v, out)
    elif isinstance(root, list):
        for item in root:
            _find_media_objects(item, out)
    return out


def pick_muxable(objects: List[Dict[str, Any]]) -> Optional[MediaParts]:
    for o in objects:
        parts = media_to_parts(o)
        if not parts:
            continue
        if parts.has_video:
            continue
        if parts.photo_url:
            return parts
    return None


def probe_media(shortcode: str, session=None) -> Optional[MediaParts]:
    """
    Probes Instagram for media info across surfaces in order:
    1. REST API (/api/v1/media/<pk>/info/)
    2. GraphQL POST (doc_id=27130156389949648)
    3. Web page HTML SJS blob
    """
    pk = id_to_pk(shortcode)
    if not pk:
        return None

    if session is None:
        import requests
        session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": POST_BASE,
        "X-IG-App-ID": IG_APP_ID
    }

    # 1. REST API
    try:
        rest_url = f"https://www.instagram.com/api/v1/media/{pk}/info/"
        r = session.get(rest_url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items") or []
            parts = pick_muxable(items)
            if parts:
                return parts
    except Exception:
        pass

    # 2. GraphQL POST
    try:
        page_url = f"https://www.instagram.com/p/{shortcode}/"
        page_res = session.get(page_url, headers=headers, timeout=15)
        lsd = None
        if page_res.status_code == 200:
            m = LSD_TOKEN_RE.search(page_res.text)
            if m:
                lsd = m.group(1)
            else:
                m2 = LSD_EQMC_RE.search(page_res.text)
                if m2:
                    try:
                        eqmc = json.loads(m2.group(1))
                        lsd = eqmc.get("l")
                    except Exception:
                        pass

        if lsd:
            gql_headers = {
                **headers,
                "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
                "X-FB-LSD": lsd,
                "X-Requested-With": "XMLHttpRequest",
            }
            gql_data = {
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
                "server_timestamps": "true",
                "doc_id": DOC_ID,
                "variables": json.dumps({"media_id": pk}, separators=(',', ':'))
            }
            gql_res = session.post(GRAPHQL_URL, data=gql_data, headers=gql_headers, timeout=15)
            if gql_res.status_code == 200:
                gql_json = gql_res.json()
                objs = _find_media_objects(gql_json)
                parts = pick_muxable(objs)
                if parts:
                    return parts

        # 3. Web page HTML SJS blob
        if page_res.status_code == 200:
            sjs_matches = SJS_RE.findall(page_res.text)
            for raw_sjs in sjs_matches:
                try:
                    blob = json.loads(raw_sjs)
                    objs = _find_media_objects(blob)
                    parts = pick_muxable(objs)
                    if parts:
                        return parts
                except Exception:
                    continue
    except Exception:
        pass

    # 4. Impersonated GraphQL fallback via yt-dlp internal extractor (bypasses Cloudflare / datacenter blocks)
    try:
        import yt_dlp
        ydl = yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True})
        ie = yt_dlp.extractor.instagram.InstagramIE(ydl)
        ie.initialize()
        url = f"https://www.instagram.com/p/{shortcode}/"
        webpage = ie._download_webpage(url, shortcode)
        lsd = ie._lsd_token
        from yt_dlp.utils import filter_dict, urlencode_postdata
        gql_headers = filter_dict({
            **ie._api_headers,
            'X-FB-Friendly-Name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
            'X-FB-LSD': lsd,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': url,
        })
        gql_data = urlencode_postdata({
            'lsd': lsd,
            'fb_api_caller_class': 'RelayModern',
            'fb_api_req_friendly_name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
            'server_timestamps': 'true',
            'variables': json.dumps({'media_id': pk}, separators=(',', ':')),
            'doc_id': DOC_ID,
        })
        gql_json = ie._download_json(
            'https://www.instagram.com/api/graphql', shortcode,
            fatal=False, impersonate=True,
            headers=gql_headers, data=gql_data
        )
        if gql_json:
            objs = _find_media_objects(gql_json)
            parts = pick_muxable(objs)
            if parts:
                return parts
    except Exception:
        pass

    return None


def sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:max_len].strip() or "Instagram"


def build_filename(shortcode: str, parts: MediaParts) -> str:
    source = parts.caption or (f"{parts.title} - {parts.artist}" if parts.title and parts.artist else parts.title) or "Instagram"
    label = sanitize_filename(source)
    return f"{label} [{shortcode}].mp4"


def download_file(url: str, dest_path: str, session=None, timeout: int = 60) -> bool:
    if session is None:
        import requests
        session = requests.Session()
    try:
        with session.get(url, stream=True, timeout=timeout, headers={"Referer": POST_BASE}) as r:
            if r.status_code not in (200, 206):
                return False
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception:
        if os.path.exists(dest_path):
            try: os.remove(dest_path)
            except Exception: pass
        return False


def run_ffmpeg(cmd: List[str], timeout_sec: int = 120) -> bool:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        return res.returncode == 0
    except Exception:
        return False


def mux_slideshow(
    photo_urls: List[str],
    audio_url: Optional[str],
    duration_ms: int,
    output_path: str,
    session=None,
    temp_dir: Optional[str] = None
) -> bool:
    """
    Downloads photo(s) and audio, then muxes them into a video using FFmpeg.
    If multiple photos: builds a slideshow via the concat demuxer.
    If single photo: builds a looped still-image video.
    """
    work = tempfile.mkdtemp(prefix="igmux_", dir=temp_dir)
    try:
        # 1. Download audio if present
        audio_file = None
        if audio_url and audio_url.strip():
            ext = ".m4a" if ".m4a" in audio_url.lower() else ".mp3"
            target_aud = os.path.join(work, f"audio{ext}")
            if download_file(audio_url, target_aud, session=session):
                audio_file = target_aud

        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        is_slideshow = len(photo_urls) >= 2

        if is_slideshow:
            # 2. Multi-image slideshow path
            slide_files = []
            for idx, u in enumerate(photo_urls):
                ext = ".jpg" if ".jpg" in u.lower() or ".jpeg" in u.lower() else ".png"
                slide_p = os.path.join(work, f"slide_{idx}{ext}")
                if download_file(u, slide_p, session=session):
                    slide_files.append(slide_p)

            if not slide_files:
                return False

            total_dur_ms = duration_ms if duration_ms > 0 else (len(slide_files) * 5000)
            per_slide_sec = max(3.0, (total_dur_ms / len(slide_files) / 1000.0))

            concat_file = os.path.join(work, "slides.txt")
            with open(concat_file, 'w', encoding='utf-8') as f:
                for sf in slide_files:
                    escaped_sf = sf.replace('\\', '/')
                    f.write(f"file '{escaped_sf}'\n")
                    f.write(f"duration {per_slide_sec:.3f}\n")
                if slide_files:
                    f.write(f"file '{slide_files[-1].replace('\\', '/')}'\n")

            common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                      "-f", "concat", "-safe", "0", "-i", concat_file]
            if audio_file:
                common.extend(["-i", audio_file, "-vf", scale, "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-b:a", "192k", "-shortest"])
            else:
                common.extend(["-vf", scale, "-pix_fmt", "yuv420p"])

            x264 = common + ["-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", output_path]
            mpeg4 = common + ["-c:v", "mpeg4", "-vtag", "xvid", "-q:v", "4", output_path]

            if run_ffmpeg(x264) and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
            if run_ffmpeg(mpeg4) and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
            return False

        else:
            # 3. Single-image path
            single_url = photo_urls[0] if photo_urls else None
            if not single_url:
                return False
            cover_ext = ".jpg" if ".jpg" in single_url.lower() or ".jpeg" in single_url.lower() else ".png"
            cover_file = os.path.join(work, f"cover{cover_ext}")
            if not download_file(single_url, cover_file, session=session):
                return False

            common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            if audio_file:
                common.extend(["-framerate", "1", "-loop", "1", "-i", cover_file,
                               "-i", audio_file, "-vf", scale, "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-b:a", "192k", "-shortest"])
            else:
                common.extend(["-framerate", "1", "-loop", "1", "-i", cover_file, "-t", "5",
                               "-vf", scale, "-pix_fmt", "yuv420p"])

            x264 = common + ["-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", output_path]
            mpeg4 = common + ["-c:v", "mpeg4", "-vtag", "xvid", "-q:v", "4", output_path]

            if run_ffmpeg(x264) and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
            if run_ffmpeg(mpeg4) and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
            return False

    finally:
        shutil.rmtree(work, ignore_errors=True)


def try_mux_instagram(
    url: str,
    output_dir: str,
    session=None,
    custom_filename: Optional[str] = None
) -> Optional[str]:
    """
    Entry point for downloading Instagram photo or carousel post with music
    and muxing it into an MP4 video.
    Returns the path to the created video, or None if not applicable / failed.
    """
    shortcode = shortcode_from_url(url)
    if not shortcode:
        return None

    parts = probe_media(shortcode, session=session)
    if not parts or parts.has_video or not parts.photo_url:
        return None

    os.makedirs(output_dir, exist_ok=True)
    filename = custom_filename or build_filename(shortcode, parts)
    output_path = os.path.join(output_dir, filename)

    photos = parts.carousel_photo_urls if parts.carousel_photo_urls else [parts.photo_url]
    ok = mux_slideshow(
        photo_urls=photos,
        audio_url=parts.audio_url,
        duration_ms=parts.duration_ms,
        output_path=output_path,
        session=session
    )
    if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path
    if os.path.exists(output_path):
        try: os.remove(output_path)
        except Exception: pass
    return None
