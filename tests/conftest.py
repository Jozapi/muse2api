collect_ignore = [
    # Not pytest tests: standalone CLI scripts that read sys.argv (a path to
    # engine.py, flags like --root/--probe-only/--assert) and launch a real
    # headless Chromium. Importing them under pytest feeds them pytest's own
    # argv instead, which crashes at import time (argparse SystemExit turns
    # into an INTERNALERROR, bare sys.argv[1] indexing raises elsewhere).
    # Run them directly, e.g. `.venv/bin/python tests/test_vm_wait.py engine.py`.
    "test_reference_upload_browser.py",
    "test_vm_wait.py",
    "test_media_selection.py",
    "test_quota_account_binding.py",
    "test_wake_confirmation.py",
    # Upstream 1.6.8+ standalone scripts (argparse / module-level runners).
    "test_admin_layout.py",
    "test_proxy_status_receipt.py",
    "test_proxy_url_import.py",
    # Upstream 1.6.10: real-Chromium send-button script (argparse --root).
    "test_composer_send_browser.py",
    # Upstream 1.6.12: standalone Chromium script for #62 (argparse-like sys.argv[1:]).
    "test_full_image_viewer.py",
]
