from hr_finder import pipeline, tools as T


def test_extract_emails_handles_obfuscation_and_assets():
    html = 'Mail jobs [at] acme [dot] com or <a href="mailto:Hr@Acme.com">x</a> logo@2x.png'
    assert T.extract_emails(html) == ["hr@acme.com", "jobs@acme.com"]


def test_apply_pattern():
    assert T.apply_pattern("{f}{last}", "Jane", "O'Neil", "acme.com") == "joneil@acme.com"
    assert T.apply_pattern("{bad}", "a", "b", "acme.com") is None


def test_fetch_blocks_private_hosts():
    assert "error" in T.fetch_page("http://127.0.0.1/")
    assert "error" in T.fetch_page("file:///etc/passwd")


def test_rank_prefers_published(monkeypatch):
    monkeypatch.setattr(T, "mx_lookup", lambda d: {"accepts_mail": True})
    res = pipeline.verify_and_rank(
        {"domain": "acme.com"},
        {"emails": [{"email": "talent@acme.com", "source_url": "u"}, {"email": "x@evil.com", "source_url": "u"}],
         "people": [{"first_name": "Jane", "last_name": "Doe", "title": "Recruiter", "source_url": "u"}]},
        {"pattern": "{first}.{last}", "confidence": "high"},
    )
    kinds = [r["kind"] for r in res]
    assert kinds[0] == "published" and res[0]["email"] == "talent@acme.com"
    assert "jane.doe@acme.com" in [r["email"] for r in res]
    assert all("evil.com" not in r["email"] for r in res)
