from app.config import settings

def test_full_scan_batch_is_low_memory():
    assert settings.scan_frame_batch_size <= 40

def test_web_version_lowmem():
    assert "LOWMEM" in settings.web_version
