"""Test bootstrap. Sets env BEFORE app/database import so the app runs against a
throwaway SQLite file and a known webhook secret (so the secret gate is real)."""
import os
import tempfile

_dir = tempfile.mkdtemp(prefix="soulful-authz-")
os.environ.setdefault("DB_PATH", os.path.join(_dir, "authz_test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MAKE_WEBHOOK_SECRET", "ci-webhook-secret")
