import os
import re
from .base import *

# Import all extractor functions
from .nkiri import extract_nkiri, extract_dramakey_com
from .jarocks import extract_9jarocks
from .naijaprey import extract_naijaprey
from .myasiantv import extract_myasiantv
from .dramarain import extract_dramarain
from .naijavault import extract_naijavault
from .anitaku import extract_anitaku
from .plutomovies import extract_plutomovies
from .social import extract_social
from .allanime import extract_allanime, search_allanime, _get_episode_list

# Map site domains to their respective extractor function
SITE_MAP = {
    THENKIRI_DOMAIN:   extract_nkiri,
    NKIRI_DOMAIN:      extract_nkiri,
    DRAMAKEY_COM:      extract_dramakey_com,
    DRAMAKEY_CC:       extract_dramarain,
    DRAMARAIN_DOMAIN:  extract_dramarain,
    JAROCKS_DOMAIN:    extract_9jarocks,
    NAIJAPREY_DOMAIN:  extract_naijaprey,
    MYASIANTV_DOMAIN:  extract_myasiantv,
    'myasiantv9.com.ro': extract_myasiantv,
    NAIJAVAULT_DOMAIN: extract_naijavault,
    ANITAKU_DOMAIN:    extract_anitaku,
    PLUTO_DOMAIN:      extract_plutomovies,
}

def _social_alias(domain):
    """Map social domains to canonical alias for disabled-site checking."""
    if domain in ('youtube.com', 'youtu.be'):
        return 'youtube'
    if domain in ('instagram.com',):
        return 'instagram'
    if domain in ('tiktok.com',):
        return 'tiktok'
    if domain in ('facebook.com', 'fb.watch'):
        return 'facebook'
    if domain in ('pinterest.com', 'pin.it'):
        return 'pinterest'
    return domain.split('.')[0]

def detect_site(url, disabled=None):
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
    except Exception:
        netloc = url
    disabled = [d.lower() for d in (disabled or [])]
    for domain, extractor in SITE_MAP.items():
        if netloc == domain.lower() or netloc.endswith('.' + domain.lower()):
            if any(d in domain.lower() for d in disabled):
                return 'disabled'
            return extractor
    for domain in SOCIAL_DOMAINS:
        if netloc == domain or netloc.endswith('.' + domain):
            alias = _social_alias(domain)
            if 'socials' in disabled or domain in disabled or alias in disabled:
                return 'disabled'
            return extract_social
    return None

def process_link_queue(links, session, ctx=None):
    ctx      = ctx or {}
    disabled = ctx.get('disabled_sites', [])
    for i, url in enumerate(links, 1):
        if _stopped(ctx):
            safe_print("[*] Stopped by user")
            break
        _wait(ctx)
        if len(links) > 1:
            safe_print(f"\n{'─'*50}")
            safe_print(f"  Queue [{i}/{len(links)}]: {url[:60]}")
            safe_print(f"{'─'*50}")
        extractor = detect_site(url, disabled)
        if extractor == 'disabled':
            safe_print(f"[!] Site is disabled in settings — skipping: {url[:50]}")
            continue
        if not extractor:
            safe_print(f"[!] Unknown site: {url[:70]}")
            safe_print("[*] Supported: NKiri, DramaKey, DramaRain, NaijaVault, 9jaRocks, NaijaPrey, MyAsianTV, Anitaku, PlutoMovies, YouTube, Instagram, TikTok, Facebook, Pinterest")
            continue
        try:
            # Save the source URL before resolving episode links. The extractor
            # clears it on a complete series; a network/resolver failure leaves
            # it visible to the `resume` command.
            if extractor is not extract_social:
                from src.downloader import mark_series_waiting_for_network
                mark_series_waiting_for_network(url)
            update_status(screen='Download', status='Preparing', source=extractor.__name__.replace('extract_', ''), current=url[:80])
            extractor(url, session, ctx)
            update_status(status='Idle', current='')
        except Exception as e:
            safe_print(f"[!] Extractor Error: {e}")
            import traceback
            traceback.print_exc()
            update_status(status='Failed', current=url[:80])
            safe_print(f"\n[!] Unexpected error: {e}")
            safe_print("[!] Please check the URL and try again")
