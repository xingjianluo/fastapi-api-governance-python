def test_skeleton_imports():
    import app.main  # noqa: F401
    from app.governance import errors, idempotency, ratelimit, signing, versioning  # noqa: F401
