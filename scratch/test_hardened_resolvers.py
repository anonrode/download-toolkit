import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.resolvers import (
    VikingFileResolver,
    LoadedfilesResolver,
    VidsrcResolver,
    DoodstreamResolver,
    VidhideResolver,
    MixdropResolver,
    DownloadwellaResolver,
    StreamtapeResolver,
    StreamwishResolver,
    VidmolyResolver,
    WildshareResolver,
    DramaGatewayResolver,
    NaijaVaultGatewayResolver,
    ResolverRegistry
)
from src.downloader import check_url_alive

class TestHardenedResolvers(unittest.TestCase):

    def test_vikingfile_can_resolve(self):
        # /d/ in path allowed even if ending with .mkv or .mp4
        self.assertTrue(VikingFileResolver.can_resolve("https://vikingfile.com/d/abc1234/Movie.Title.2024.mkv"))
        self.assertTrue(VikingFileResolver.can_resolve("https://vikingfile.com/d/abc1234/Movie.Title.2024.mp4"))
        # Non-/d/ ending with .mkv should still return False
        self.assertFalse(VikingFileResolver.can_resolve("https://vikingfile.com/files/Movie.Title.2024.mkv"))

    def test_vikingfile_r2_redirect(self):
        session = MagicMock()
        r1 = MagicMock()
        r1.status_code = 302
        r1.headers = {'location': 'https://bucket.r2.cloudflarestorage.com/token/file.mkv?presigned=1'}
        session.get.return_value = r1
        res = VikingFileResolver.resolve("https://vikingfile.com/d/abc/file.mkv", session)
        self.assertEqual(res, 'https://bucket.r2.cloudflarestorage.com/token/file.mkv?presigned=1')

        # Test .r2.dev
        r1.headers = {'location': 'https://pub-123.r2.dev/file.mp4'}
        res2 = VikingFileResolver.resolve("https://vikingfile.com/d/abc/file.mp4", session)
        self.assertEqual(res2, 'https://pub-123.r2.dev/file.mp4')

    def test_loadedfiles_helpers_and_extraction(self):
        # Unescape JS URL
        unescaped = LoadedfilesResolver._unescape_js_url(r"https:\/\/loadedfiles.st\/d\/abc\u0026token=123")
        self.assertEqual(unescaped, "https://loadedfiles.st/d/abc&token=123")

        # dlTimer Alpine.js extraction
        html_dltimer = """
        <div x-data="dlTimer({ seconds: 5, link: 'https:\\/\\/loadedfiles.net\\/d\\/xyz\\u0026key=99' })">
        """
        link = LoadedfilesResolver._extract_link(html_dltimer, "loadedfiles.net")
        self.assertEqual(link, "https://loadedfiles.net/d/xyz&key=99")

        # Anchor fallback
        html_anchor = """
        <div>
            <a href="/token/download/abcdef12345">Download</a>
        </div>
        """
        link2 = LoadedfilesResolver._extract_link(html_anchor, "loadedfiles.org")
        self.assertEqual(link2, "https://loadedfiles.org/token/download/abcdef12345")

    def test_vidsrc_nepu_cookie(self):
        session = MagicMock()
        r = MagicMock()
        r.status_code = 200
        r.text = '<iframe id="playerFrame" src="https://vidsrc.me/embed/movie/12345"></iframe>'
        session.get.return_value = r

        # Call resolve on nepu.gd url
        # Mocking the remainder of resolve
        with patch.object(VidsrcResolver, 'resolve', wraps=VidsrcResolver.resolve):
            try:
                VidsrcResolver.resolve("https://nepu.gd/watch/movie/12345", session)
            except Exception:
                pass
            # Check session.get calls for nepu.gd
            nepu_call = None
            for call in session.get.call_args_list:
                args, kwargs = call
                if 'nepu.gd' in args[0]:
                    nepu_call = call
                    break
            self.assertIsNotNone(nepu_call)
            self.assertEqual(nepu_call[1]['headers'].get('Cookie'), 'hv=1')

    def test_doodstream_mirror_rotation(self):
        session = MagicMock()
        # First call to dood.to fails with 403, next call to doodstream.com succeeds
        r_fail = MagicMock()
        r_fail.status_code = 403
        r_ok = MagicMock()
        r_ok.status_code = 200
        r_ok.text = "<html><body>/pass_md5/xyz123abc</body></html>"
        r_pass = MagicMock()
        r_pass.status_code = 200
        r_pass.text = "https://cdn.dood.com/direct/"

        def side_effect(url, **kwargs):
            if 'dood.to' in url:
                return r_fail
            if 'pass_md5' in url:
                return r_pass
            return r_ok

        session.get.side_effect = side_effect
        res = DoodstreamResolver.resolve("https://dood.to/d/sample123", session)
        self.assertIsNotNone(res)
        self.assertTrue(res.startswith("https://cdn.dood.com/direct/"))

    def test_vidhide_mirror_hosts(self):
        self.assertIn('ryderjet.com', VidhideResolver._MIRROR_HOSTS)
        self.assertTrue(VidhideResolver.can_resolve("https://ryderjet.com/v/12345"))

    def test_mixdrop_ssl_fallback(self):
        import requests
        session = MagicMock()
        r_ok = MagicMock()
        r_ok.status_code = 200
        r_ok.text = 'MDCore.wurl = "//delivery.mixdrop.co/video.mp4";'

        first_call = True
        def get_side_effect(url, **kwargs):
            nonlocal first_call
            if first_call and kwargs.get('verify', True) is not False:
                first_call = False
                raise requests.exceptions.SSLError("SSL Certificate verify failed")
            return r_ok

        session.get.side_effect = get_side_effect
        res = MixdropResolver.resolve("https://mixdrop.co/f/abc123", session)
        self.assertEqual(res, "https://delivery.mixdrop.co/video.mp4")

    def test_downloadwella_form_absent_anchor_fallback(self):
        session = MagicMock()
        r = MagicMock()
        r.status_code = 200
        r.text = """
        <html><body>
            <p>No form here</p>
            <a href="https://downloadwella.com/d/direct-download-token/file.mp4">Download Now</a>
        </body></html>
        """
        session.get.return_value = r
        res = DownloadwellaResolver.resolve("https://downloadwella.com/file123", session)
        self.assertEqual(res, "https://downloadwella.com/d/direct-download-token/file.mp4")

    def test_streamtape_host_and_regex(self):
        self.assertTrue(StreamtapeResolver.can_resolve("https://strtape.tech/v/abc123"))
        
        # Test with single quotes and parens
        html1 = """
        document.getElementById('robotlink').innerHTML = '//streamtape.com/get_video?id=123&stream=1' + ('&token=xyz');
        """
        # Test with double quotes and no parens
        html2 = """
        document.getElementById("robotlink").innerHTML = "//streamtape.com/get_video?id=456&stream=1" + "&token=abc";
        """
        import re
        pattern = r'''getElementById\(['"]robotlink['"]\)[^;]*innerHTML\s*=\s*['"]([^'"]+)['"]\s*\+\s*(?:\(['"]|['"])([^'"\)]+)(?:['"]\)|['"])'''
        m1 = re.search(pattern, html1)
        self.assertIsNotNone(m1)
        self.assertEqual(m1.group(1), '//streamtape.com/get_video?id=123&stream=1')
        self.assertEqual(m1.group(2), '&token=xyz')

        m2 = re.search(pattern, html2)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(1), '//streamtape.com/get_video?id=456&stream=1')
        self.assertEqual(m2.group(2), '&token=abc')

    def test_streamwish_filelions(self):
        self.assertTrue(StreamwishResolver.can_resolve("https://filelions.to/v/abc12345"))
        self.assertTrue(StreamwishResolver.can_resolve("https://filelions.com/e/abc12345"))

    def test_vidmoly_hosts_and_packed_js(self):
        self.assertTrue(VidmolyResolver.can_resolve("https://vidmoly.to/w/abc123"))
        self.assertTrue(VidmolyResolver.can_resolve("https://vidmoly.net/w/abc123"))
        self.assertTrue(VidmolyResolver.can_resolve("https://vidmoly.biz/w/abc123"))

        session = MagicMock()
        r = MagicMock()
        r.status_code = 200
        # Dean Edwards packed JS returning file: "https://cdn.vidmoly.to/hls/test.m3u8"
        # eval(function(p,a,c,k,e,d){...}('file:"https://cdn.vidmoly.to/hls/test.m3u8"',10,1,'file'.split('|')))
        r.text = """
        <script>
        eval(function(p,a,c,k,e,d){return p;}('var x = { file: "https://cdn.vidmoly.to/hls/test.m3u8" };',10,1,'file'.split('|')))
        </script>
        """
        session.get.return_value = r
        res = VidmolyResolver.resolve("https://vidmoly.to/w/abc123", session)
        self.assertEqual(res, "https://cdn.vidmoly.to/hls/test.m3u8")

    def test_wildshare_js_json_pt(self):
        session = MagicMock()
        r1 = MagicMock()
        r1.status_code = 200
        r1.text = """
        <script>
            window.config = {
                "id": "abc1234",
                "pt": "eyJ0b2tlbiI6InNlc3Npb24ifQ=="
            };
        </script>
        """
        r2 = MagicMock()
        r2.status_code = 302
        r2.headers = {'location': 'https://wildshare.net/d/abc1234?download_token=secret'}

        session.get.side_effect = [r1, r2]
        with patch('curl_cffi.requests.Session', return_value=session):
            res = WildshareResolver.resolve("https://wildshare.net/abc1234/Video.mkv", session)
            self.assertEqual(res, 'https://wildshare.net/d/abc1234?download_token=secret')

    def test_gateway_resolvers(self):
        # DramaGateway single quote & window.location =
        session = MagicMock()
        r = MagicMock()
        r.status_code = 200
        r.text = "window.location = 'https://waffi.cloud/d/xyz';"
        session.get.return_value = r
        res = DramaGatewayResolver.resolve("https://dramakey.cc/download?id=1", session)
        self.assertEqual(res, 'https://waffi.cloud/d/xyz')

        # DramaGateway download-btn element
        r.text = '<a class="download-btn" href="https://vikingfile.com/d/abc">Download</a>'
        res2 = DramaGatewayResolver.resolve("https://dramarain.com/download?id=2", session)
        self.assertEqual(res2, 'https://vikingfile.com/d/abc')

        # NaijaVaultGateway single quote & lockers
        r1 = MagicMock()
        r1.headers = {'location': 'https://www.naijavault.com/temp/123'}
        r2 = MagicMock()
        r2.status_code = 200
        r2.text = "var downloadURL = 'https://loadedfiles.st/d/final123';"
        session.get.side_effect = [r1, r2]
        res3 = NaijaVaultGatewayResolver.resolve("https://www.naijavault.com/dl-sample", session)
        self.assertEqual(res3, 'https://loadedfiles.st/d/final123')

    def test_check_url_alive_sniff(self):
        session = MagicMock()

        # Case 1: 200 OK but HTML error page
        r_html = MagicMock()
        r_html.status_code = 200
        r_html.iter_content.return_value = iter([b'<!DOCTYPE html><html><head><title>Error 404</title></head></html>'])
        session.get.return_value = r_html
        status = check_url_alive("https://cdn.example.com/movie.mp4", session)
        self.assertEqual(status, 'expired')

        # Case 2: 200 OK with json error
        r_json = MagicMock()
        r_json.status_code = 200
        r_json.iter_content.return_value = iter([b'{"error": "Access Denied", "code": 403}'])
        session.get.return_value = r_json
        status2 = check_url_alive("https://cdn.example.com/movie.mp4", session)
        self.assertEqual(status2, 'expired')

        # Case 3: 206 Partial Content with real video bytes (ftyp)
        r_video = MagicMock()
        r_video.status_code = 206
        r_video.iter_content.return_value = iter([b'\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41'])
        session.get.return_value = r_video
        status3 = check_url_alive("https://cdn.example.com/movie.mp4", session)
        self.assertEqual(status3, 'ok')

if __name__ == '__main__':
    unittest.main()
