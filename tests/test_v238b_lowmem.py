from app.config import settings

def test_full_scan_batch_is_low_memory():
    assert settings.scan_frame_batch_size <= 40

def test_lowmem_settings_survive_later_versions():
    # Low-memory behavior is a configuration capability, not a version-name contract.
    assert settings.scan_frame_batch_size <= 40
