from hr_email_finder.tools.extract import decode_cfemail, extract_emails
from hr_email_finder.tools.html import parse_html


def emails(html: str) -> dict[str, tuple[str, str]]:
    found = extract_emails(parse_html(html, "https://acme.test/careers"))
    return {f.email: (f.method, f.snippet) for f in found}


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in email)


def test_plain_text_email_with_context():
    found = emails("<p>Send your CV to Careers@Acme.test and we will reply.</p>")
    method, snippet = found["careers@acme.test"]
    assert method == "text"
    assert "Send your CV to" in snippet


def test_mailto_with_subject_and_entities():
    found = emails('<p>Questions? <a href="mailto:hr&#64;acme.test?subject=Application">Write to HR</a></p>')
    assert found["hr@acme.test"][0] == "mailto"
    assert "Write to HR" in found["hr@acme.test"][1]


def test_bracket_obfuscation():
    found = emails("<p>Recruiting: jobs [at] acme [dot] test</p>")
    assert found["jobs@acme.test"][0] == "deobfuscated"
    found = emails("<p>hr(at)acme(dot)co(dot)in</p>")
    assert "hr@acme.co.in" in found


def test_cloudflare_protected_emails():
    assert decode_cfemail(cf_encode("hr@acme.test")) == "hr@acme.test"
    html = (
        f'<p>Apply: <span class="__cf_email__" data-cfemail="{cf_encode("talent@acme.test")}">[email&#160;protected]</span></p>'
        f'<p><a href="/cdn-cgi/l/email-protection#{cf_encode("people@acme.test", 0x1f)}">email us</a></p>'
    )
    found = emails(html)
    assert found["talent@acme.test"][0] == "cloudflare"
    assert "Apply: talent@acme.test" in found["talent@acme.test"][1]
    assert found["people@acme.test"][0] == "cloudflare"


def test_json_ld_email():
    html = '<script type="application/ld+json">{"@type": "Organization", "email": "contact@acme.test"}</script>'
    assert emails(html)["contact@acme.test"][0] == "json_ld"


def test_filters_non_mailbox_matches():
    html = (
        '<img src="/img/logo@2x.png"> <p>e.g. you@example.com</p>'
        "<p>0123456789abcdef0123@o1.ingest.sentry.io</p>"
    )
    assert emails(html) == {}


def test_hr_context_comes_from_prose_not_menu_links():
    html = """<body><nav><a href="/careers">Careers</a> <a href="/about">About</a></nav>
    <footer><a href="/careers">Careers</a> · <a href="/press">Press</a>
    <p>© Acme Corp · <a href="mailto:info@acme.test">info@acme.test</a></p></footer>
    <section><p>For job applications and internships, please write to office@acme.test
    with your CV attached.</p></section></body>"""
    found = {f.email: f for f in extract_emails(parse_html(html, "https://acme.test/"))}
    assert not found["info@acme.test"].hr_context  # only menu links say "Careers"
    assert "© Acme Corp" in found["info@acme.test"].snippet
    assert found["office@acme.test"].hr_context
    assert "job applications" in found["office@acme.test"].snippet


def test_deduplicates_across_methods():
    found = extract_emails(parse_html('<p>hr@acme.test <a href="mailto:hr@acme.test">mail</a></p>', "https://acme.test/"))
    assert [f.email for f in found] == ["hr@acme.test"]
    assert found[0].method == "text"
