import html
"""Example Web Studio's own homepage: built and reviewed like a preview, held to archlight_home.yaml, and
published at the root (indexable), with its privacy page at /privacy/. Fake `claude`, fake Astro and fake
measuring: nothing real runs."""
import json
from pathlib import Path

from packs.websites.builder import builds_today, homepage_subject, run_builder
from packs.websites.builder_check import facts_check
from packs.websites.builder_content import homepage_words, make_homepage_brief, privacy_lines
from packs.websites.critic import HOMEPAGE_REVIEW, review_build
from packs.websites.critic_measure import hard_failures, other_pages
from tests.test_builder import DESIGN, FakeClaude, fake_site, town  # noqa: F401
from tests.test_critic import PASS, critic_line, measured  # noqa: F401

HOME = homepage_subject().lead
BRIEF = make_homepage_brief()
OWN_WORDS = homepage_words(BRIEF)
PRIVACY = privacy_lines(BRIEF)
HOME_PAGE = ('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width">'
             '<title>Example Web Studio</title></head><body><h1>Example Web Studio</h1>'
             + ''.join('<p>' + html.escape(fact) + '</p>' for fact in BRIEF['our_facts'])
             + '<a href="mailto:town@example.com">town@example.com</a>'
             '<footer><a href="/privacy/">Privacy</a></footer></body></html>')


def privacy_page(brief=BRIEF):
    """The privacy page the way the Builder should make it: every line of the yaml, a link home."""
    page = brief["privacy_page"]
    sections = "".join(f"<h2>{s['heading']}</h2>" + "".join(f"<p>{p}</p>" for p in s["paragraphs"])
                       for s in page["sections"])
    return (f'<!doctype html><html lang="en"><head><title>{page["title"]} | Example Web Studio</title></head><body>'
            f'<header><a href="/">Example Web Studio</a></header><main><h1>{page["title"]}</h1>'
            f'<p>Last updated: {page["last_updated"]}</p><p>{page["intro"]}</p>{sections}</main>'
            f'<footer><a href="/privacy/">Privacy</a> <a href="mailto:town@example.com">'
            f'town@example.com</a></footer></body></html>')


PRIVACY_PAGE = privacy_page()


def check(tmp_path, page, privacy=PRIVACY_PAGE):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(page, encoding="utf-8")
    if privacy is not None:
        (dist / "privacy").mkdir(exist_ok=True)
        (dist / "privacy" / "index.html").write_text(privacy, encoding="utf-8")
    return facts_check(dist, HOME, OWN_WORDS, homepage=True, privacy=PRIVACY)


def test_the_homepage_brief_holds_charlies_approved_facts():
    brief = make_homepage_brief()
    facts = ' '.join(brief['our_facts'])
    assert brief['site'] == 'homepage'
    assert brief['business']['public_email'] == 'town@example.com'
    assert 'fictional businesses' in facts and 'A person reviews every site' in facts
    assert brief['business']['phone_tel_link'] == '' and brief['business']['address_exactly'] == ''
    assert any('endorses' in rule for rule in brief['rules_for_this_page'])
    assert 'Charlie' not in facts


def test_a_good_homepage_passes_the_facts_check(tmp_path):
    result = check(tmp_path, HOME_PAGE)
    assert result["passed"], result["problems"]          # "reviews" is in our own approved words


def test_the_homepage_must_be_indexable_and_carry_no_preview_note(tmp_path):
    noindex = HOME_PAGE.replace("<title>", '<meta name="robots" content="noindex"><title>')
    assert any("noindex" in p for p in check(tmp_path, noindex)["problems"])
    note = HOME_PAGE.replace("</body>", "<p>Preview made by Example Web Studio for Example Web Studio.</p></body>")
    assert any("preview note" in p for p in check(tmp_path, note)["problems"])


def test_the_homepage_is_held_to_its_own_facts(tmp_path):
    assert any("email" in p for p in check(tmp_path, HOME_PAGE.replace("town@example.com", "x"))["problems"])
    for invented in ("<p>Trusted by 40 local businesses.</p>", "<p>Sites from $750.</p>",
                     '<a href="tel:+13145550199">(314) 555-0199</a>', "<p>Read our testimonials.</p>"):
        result = check(tmp_path, HOME_PAGE.replace("</body>", invented + "</body>"))
        assert not result["passed"], invented


def test_the_privacy_text_is_honest_and_says_what_charlie_asked_for():
    page = BRIEF['privacy_page']
    text = ' '.join(PRIVACY)
    assert page['path'] == '/privacy/' and page['use_word_for_word'] and page['last_updated']
    assert 'fictional showcase' in text and 'placeholder' in text
    for never in ('GDPR', 'CCPA', 'Charlie', 'Baker', '$', 'Suite', 'Street'):
        assert never not in text


def test_a_good_homepage_with_its_privacy_page_passes(tmp_path):
    result = check(tmp_path, HOME_PAGE)
    assert result["passed"], result["problems"]


def test_the_privacy_page_must_exist(tmp_path):
    assert any("privacy page is missing" in p for p in check(tmp_path, HOME_PAGE, privacy=None)["problems"])


def test_every_privacy_line_must_be_there_word_for_word(tmp_path):
    paragraph = BRIEF['privacy_page']['sections'][0]['paragraphs'][0]
    reworded = PRIVACY_PAGE.replace(paragraph, paragraph.replace('placeholder', 'example'))
    assert any('word for word' in p and 'placeholder' in p for p in check(tmp_path, HOME_PAGE, reworded)['problems'])
    undated = PRIVACY_PAGE.replace(BRIEF['privacy_page']['last_updated'], 'recently')
    assert any(BRIEF['privacy_page']['last_updated'] in p for p in check(tmp_path, HOME_PAGE, undated)['problems'])
    curly = PRIVACY_PAGE.replace("doesn't", 'doesn\u2019t')
    assert check(tmp_path, HOME_PAGE, curly)['passed']


def test_the_two_pages_must_link_to_each_other(tmp_path):
    no_footer = HOME_PAGE.replace('<a href="/privacy/">Privacy</a>', "")
    assert any("no link to the privacy page" in p for p in check(tmp_path, no_footer)["problems"])
    no_way_home = PRIVACY_PAGE.replace('<a href="/">', '<a href="#top">')
    assert any("no link back to the homepage" in p for p in check(tmp_path, HOME_PAGE, no_way_home)["problems"])


def test_the_privacy_page_is_held_to_the_same_rules_as_the_homepage(tmp_path):
    for invented in ("<p>We guarantee your data is safe.</p>", "<p>Trusted by 40 local businesses.</p>",
                     '<a href="tel:+13145550199">(314) 555-0199</a>', '<form><input name="email"></form>'):
        result = check(tmp_path, HOME_PAGE, PRIVACY_PAGE.replace("</main>", invented + "</main>"))
        assert not result["passed"], invented
    noindex = PRIVACY_PAGE.replace("<title>", '<meta name="robots" content="noindex"><title>')
    assert any("privacy/index.html: has a noindex" in p for p in check(tmp_path, HOME_PAGE, noindex)["problems"])


def test_every_page_gets_the_browser_checks_and_a_failure_names_the_page(tmp_path):
    from tests.test_critic import CLEAN
    (tmp_path / "privacy").mkdir()
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "privacy" / "index.html").write_text("x", encoding="utf-8")
    assert other_pages(tmp_path) == {"privacy/": "privacy/index.html"}
    page = {k: CLEAN[k] for k in ("screens", "axe", "reduced_motion", "lighthouse")}
    assert hard_failures({**CLEAN, "other_pages": {"privacy/": page}}, 90, 95) == []
    bad = {**page, "axe": {"violations": [{"id": "color-contrast", "impact": "serious", "help": "Text needs contrast",
                                           "count": 1, "where": ["p"]}]},
           "screens": {**page["screens"], "768": {"phone_wraps": [], "overlaps": [{"text": "Privacy", "graphic": "svg"}]}},
           "lighthouse": {"performance": 70, "accessibility": 100}}
    fails = hard_failures({**CLEAN, "other_pages": {"privacy/": bad}}, 90, 95)
    for problem in ("On /privacy/: Accessibility (serious)", "On /privacy/: A graphic (svg) overlaps",
                    "On /privacy/: Lighthouse on a phone: performance 70"):
        assert any(f.startswith(problem) for f in fails), (problem, fails)
    unmeasured = hard_failures({**CLEAN, "other_pages": {"privacy/": {}}}, 90, 95)      # fail closed
    assert any("On /privacy/: Lighthouse couldn't" in f for f in unmeasured)
    assert any("On /privacy/: The accessibility check" in f for f in unmeasured)
    assert any("On /privacy/: Reduced motion" in f for f in unmeasured)


def test_a_preview_still_needs_noindex_and_its_note(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(HOME_PAGE, encoding="utf-8")
    problems = facts_check(dist, HOME, OWN_WORDS)["problems"]
    assert any("noindex" in p for p in problems) and any("preview note" in p for p in problems)


def test_a_homepage_build_without_its_privacy_page_fails(town, fake_site):
    fake_site["html"] = HOME_PAGE
    town.cli._launch = FakeClaude(DESIGN)
    [b] = run_builder(town, homepage=True).builds
    assert b["status"] == "failed" and "privacy page is missing" in b["failure"]


def test_town_build_homepage_builds_it_as_the_homepage(town, fake_site):
    fake_site["html"], fake_site["pages"] = HOME_PAGE, {"privacy/index.html": PRIVACY_PAGE}
    fake = FakeClaude(DESIGN)
    town.cli._launch = fake
    [b] = run_builder(town, homepage=True).builds
    row = town.conn.execute("SELECT kind, lead_id, status FROM builds WHERE id = ?", (b["id"],)).fetchone()
    assert tuple(row) == ("homepage", None, "built"), b.get("failure")
    brief = json.loads((Path(b["workspace"]) / "brief.json").read_text(encoding="utf-8"))
    assert brief["site"] == "homepage" and brief["privacy_page"] == BRIEF["privacy_page"]
    system = fake.calls[0]["command"][fake.calls[0]["command"].index("--append-system-prompt") + 1]
    assert "Example Web Studio's OWN homepage" in system and "Remove the noindex" in system
    assert "src/pages/privacy.astro" in system and "WORD FOR WORD" in system and 'href="/privacy/"' in system
    assert builds_today(town) == 1                      # it uses Charlie's plan, so it counts toward the cap


def test_the_homepage_build_respects_the_daily_cap(town, fake_site, limits_file):
    limits_file.write_text(limits_file.read_text(encoding="utf-8").replace("sites_per_day: 5", "sites_per_day: 0"),
                           encoding="utf-8")
    town.cli._launch = FakeClaude(DESIGN)
    result = run_builder(town, homepage=True)
    assert result.builds == [] and "daily cap 0" in result.message


def test_the_critic_reviews_the_homepage_against_our_facts(town, fake_site, measured):
    fake_site["html"], fake_site["pages"] = HOME_PAGE, {"privacy/index.html": PRIVACY_PAGE}
    town.cli._launch = FakeClaude(DESIGN)
    [b] = run_builder(town, homepage=True).builds
    fake = FakeClaude(([critic_line(PASS)], {}))
    town.cli._launch = fake
    assert review_build(town, b["id"])["verdict"] == "pass"
    facts = (Path(fake.calls[0]["cwd"]) / "facts" / "archlight_facts.txt").read_text(encoding="utf-8")
    assert "fictional businesses" in facts and "endorses" in facts
    assert "Privacy page (at /privacy/; used word for word)" in facts and all(line in facts for line in PRIVACY)
    assert (Path(fake.calls[0]["cwd"]) / "site" / "built" / "privacy" / "index.html").is_file()
    assert "privacy-375px-01.png" in HOMEPAGE_REVIEW and "other_pages" in HOMEPAGE_REVIEW
    system = fake.calls[0]["command"][fake.calls[0]["command"].index("--append-system-prompt") + 1]
    assert "Example Web Studio's OWN homepage" in system


def test_an_empty_homepage_phone_means_no_phone_at_all(tmp_path):
    assert make_homepage_brief()["business"]["phone_display"] == ""      # empty until Charlie adds Google Voice
    with_phone = HOME_PAGE.replace("</body>", '<a href="tel:+13145550199">(314) 555-0199</a></body>')
    assert any("phone" in p for p in check(tmp_path, with_phone)["problems"])


def test_a_homepage_phone_from_the_yaml_is_the_only_number_allowed_and_must_show(tmp_path):
    home = Path(__file__).resolve().parents[1] / "packs" / "websites" / "archlight_home.yaml"
    yaml_with_phone = tmp_path / "home.yaml"
    yaml_with_phone.write_text(home.read_text(encoding="utf-8").replace('phone: ""', 'phone: "314-555-0142"'),
                               encoding="utf-8")
    brief = make_homepage_brief(yaml_with_phone)
    assert brief["business"]["phone_display"] == "(314) 555-0142"
    assert brief["business"]["phone_tel_link"] == "tel:+13145550142"
    lead = {**HOME, "phone": "+13145550142"}
    dist = tmp_path / "dist"
    dist.mkdir()

    def problems(page):
        (dist / "index.html").write_text(page, encoding="utf-8")
        return facts_check(dist, lead, OWN_WORDS, homepage=True)["problems"]

    shown = HOME_PAGE.replace("</body>", '<a href="tel:+13145550142">(314) 555-0142</a></body>')
    assert problems(shown) == []
    assert any("isn't on the site" in p for p in problems(HOME_PAGE))                # set, so it must show
    assert any("isn't the business's" in p for p in problems(shown.replace("0142", "0199")))


def test_a_homepage_build_is_reported_as_the_homepage_not_a_test_case(town, fake_site, capsys):
    import launcher
    fake_site["html"], fake_site["pages"] = HOME_PAGE, {"privacy/index.html": PRIVACY_PAGE}
    town.cli._launch = FakeClaude(DESIGN)
    [b] = run_builder(town, homepage=True).builds
    launcher._report_build(b)
    out = capsys.readouterr().out
    assert "Example Web Studio's homepage" in out and "None" not in out
