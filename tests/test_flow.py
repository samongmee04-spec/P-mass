import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pmass  # noqa: E402

CAPTION = ("พิธีมิสซาวัด น.อันนา ท่าจีน วันจันทร์ที่ 23/11/2026 : 6.30 น สัปดาห์ที่ 34 เทศกาลธรรมดา "
           "ระลึกถึงน.เคลเมนต์ที่1 พระสันตะปาปา และน.โคลัมบัน เจ้าอธิการ ออกอากาศเพื่อผู้เดินไปวัดไม่ได้")


def setup(monkeypatch, tmp_path, ids, infos):
    monkeypatch.setattr(pmass, "STATE_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(pmass, "discover_video_ids", lambda c: list(ids))
    monkeypatch.setattr(pmass, "get_info", lambda url, c: infos[url.split("v=")[-1]])
    now = datetime(2026, 11, 23, 8, 0, tzinfo=pmass.THAI_TZ)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pmass, "datetime", FakeDT)
    emails, uploads, files = [], [], []
    monkeypatch.setattr(pmass, "send_email", lambda s, b: emails.append((s, b)))
    monkeypatch.setattr(pmass, "youtube", lambda: object())
    monkeypatch.setattr(pmass, "channel_info", lambda yt: ("Pongthep", "UC1", []))

    def fake_download(url, c, folder):
        f = folder / "v.mp4"
        f.write_bytes(b"x" * 10)
        files.append(f)
        return f

    monkeypatch.setattr(pmass, "download", fake_download)
    monkeypatch.setattr(pmass, "upload", lambda yt, p, t, d, r: uploads.append(t) or "YT123")
    monkeypatch.setattr(sys, "argv", ["pmass.py"])
    return emails, uploads, files


def info(vid, status="was_live", caption=CAPTION, ts=datetime(2026, 11, 23, 6, 30, tzinfo=pmass.THAI_TZ)):
    return {"id": vid, "live_status": status, "description": caption, "timestamp": ts.timestamp(), "duration": 3600}


def test_full_flow(monkeypatch, tmp_path):
    ids = ["111111111"]
    infos = {"111111111": info("111111111"), "222222222": info("222222222", status="is_live"),
             "333333333": info("333333333")}
    emails, uploads, files = setup(monkeypatch, tmp_path, ids, infos)

    pmass.main()  # first run: only remembers existing videos
    assert uploads == [] and pmass.load_state()["initialized"]

    ids[:0] = ["222222222"]  # new live, still running
    pmass.main()
    assert uploads == [] and pmass.load_state()["videos"]["222222222"]["status"] == "waiting"

    infos["222222222"]["live_status"] = "was_live"  # live finished
    pmass.main()
    assert len(uploads) == 1 and uploads[0].startswith("มิสซาวัด น.อันนา ท่าจีน วันจันทร์ที่ 23 พ.ย. 2026")
    assert pmass.load_state()["videos"]["222222222"]["status"] == "done"
    assert not files[0].exists() and not files[0].parent.exists()  # video deleted
    assert "โพสต์แล้ว" in emails[-1][0] and "YT123" in emails[-1][1]

    pmass.main()  # nothing new -> no second upload
    assert len(uploads) == 1


def test_non_mass_video_is_ignored(monkeypatch, tmp_path):
    ids = ["111111111"]
    infos = {"111111111": info("111111111"), "444444444": info("444444444", caption="ทำบุญวันเกิด")}
    emails, uploads, _ = setup(monkeypatch, tmp_path, ids, infos)
    pmass.main()
    ids.insert(0, "444444444")
    pmass.main()
    assert uploads == [] and pmass.load_state()["videos"]["444444444"]["status"] == "skipped_not_mass"


def test_blocked_facebook_alerts_once_per_day(monkeypatch, tmp_path):
    emails, uploads, _ = setup(monkeypatch, tmp_path, [], {})
    pmass.main()
    pmass.main()
    assert len(emails) == 1 and "Facebook" in emails[0][0]


def test_email_failure_does_not_undo_upload(monkeypatch, tmp_path):
    ids = ["111111111"]
    infos = {"111111111": info("111111111"), "222222222": info("222222222")}
    emails, uploads, _ = setup(monkeypatch, tmp_path, ids, infos)
    pmass.main()
    monkeypatch.setattr(pmass, "send_email", lambda s, b: (_ for _ in ()).throw(OSError("smtp down")))
    ids.insert(0, "222222222")
    pmass.main()
    assert len(uploads) == 1
    assert pmass.load_state()["videos"]["222222222"]["status"] == "done"
    pmass.main()
    assert len(uploads) == 1  # not uploaded twice


def test_send_email_never_raises(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "a@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")

    def boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr(pmass.smtplib, "SMTP_SSL", boom)
    monkeypatch.setattr(pmass.smtplib, "SMTP", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert pmass.send_email("s", "b") is False


LOGIN = ("https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Fstory.php%3Fstory_fbid%3D2350792388791818"
         "%26id%3D100003864790229%26rdid%3DDjg2czI1QpwIOJKF&rdid=Djg2czI1QpwIOJKF&_fb_noscript=1")


def test_login_wall_is_unwrapped_to_the_post():
    urls = pmass.related_urls(LOGIN)
    assert "https://www.facebook.com/100003864790229/posts/2350792388791818/" in urls
    assert all("/login" not in u for u in urls)
    assert pmass.related_urls("https://www.facebook.com/x/videos/1234567890/") == ["https://www.facebook.com/watch/?v=1234567890"]


def test_get_info_retries_with_post_address(monkeypatch):
    import yt_dlp
    seen = []

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, url, download=False):
            seen.append(url)
            if "share/v" in url:
                raise yt_dlp.utils.DownloadError(f"ERROR: Unsupported URL: {LOGIN}")
            return {"id": "987654321", "duration": 10}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(pmass, "resolve_share", lambda u, c: [])
    info = pmass.get_info("https://www.facebook.com/share/v/1Q9J8Goia1/", None)
    assert info["_source_url"] == "https://www.facebook.com/100003864790229/posts/2350792388791818/"
    assert len(seen) == 2
