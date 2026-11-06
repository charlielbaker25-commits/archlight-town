"""Builder v2 (Charlie, 2026-11-02): tagged sample sections and the licensed sample-photo library.

The facts check lets tagged sample content through and still fails any untagged, unsourced claim; it never lets a
fake review, rating, time or price through, tagged or not. The photo library refuses any photo without a complete
license row. No browser, no network, no model."""
import re
from pathlib import Path

import pytest

from packs.websites import photo_library
from packs.websites.builder_check import facts_check
from packs.websites.builder_content import make_brief
from packs.websites.photo_library import (SAMPLE_PHOTO_LABEL, copy_into, folder_for, library_check, license_rows,
                                          photos_for, row_problems)
from packs.websites.samples import SAMPLE_TAGS, SHOWCASE
from tests.test_builder import GOOD_PAGE, LEAD

TEMPLATE = Path(photo_library.__file__).parent / "site_template" / "src"


def page(extra: str) -> str:
    return GOOD_PAGE.replace("<p class=\"preview-note\">", extra + "<p class=\"preview-note\">")


def sample(kind: str, body: str, tag: str | None = None) -> str:
    tag = SAMPLE_TAGS.get(kind, "Sample") if tag is None else tag
    return f'<section data-sample="{kind}"><p data-sample-tag>{tag}</p>{body}</section>'


def check(html: str, tmp_path: Path) -> dict:
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    return facts_check(tmp_path, LEAD)


# ---------------- the facts check ----------------

def test_every_tagged_sample_section_passes_and_is_reported(tmp_path):
    sections = "".join([
        sample("gallery", "<ul><li>The space</li><li>Your photo</li></ul>"),
        sample("story", "<p>This is where the owner's own story goes.</p>"),
        sample("hours", "<table><tr><th>Monday</th><td>Your hours</td></tr><tr><th>Saturday</th>"
                        "<td>Your hours</td></tr></table>"),
        sample("reviews", "<p>A real review from one of your customers</p><p>Your reviews appear here.</p>"),
        sample("booking", "<button popovertarget=b>Book online</button><div id=b popover>Online booking comes "
                          "with your finished site.</div>"),
        sample("map", "<svg role=img aria-label=\"Illustrated map\"></svg><address>7340 Manchester Rd, Maplewood, "
                      "MO 63143</address>"),
    ])
    result = check(page(sections), tmp_path)
    assert result["passed"], result["problems"]
    assert result["sample_sections"] == sorted(SHOWCASE)


def test_the_same_placeholder_words_untagged_fail(tmp_path):
    result = check(page("<section><h2>What people say</h2><p>Your reviews appear here.</p></section>"), tmp_path)
    assert not result["passed"] and any("reviews or testimonials" in p for p in result["problems"])


def test_a_sample_section_without_its_tag_fails(tmp_path):
    result = check(page(sample("reviews", "<p>Your reviews appear here.</p>", tag="")), tmp_path)
    assert any("missing its tag \"Sample: your reviews appear here\"" in p for p in result["problems"])
    reworded = check(page(sample("gallery", "<p>Photos</p>", tag="Example photos")), tmp_path)
    assert any("gallery sample section is missing its tag" in p for p in reworded["problems"])


def test_the_tag_must_be_in_the_tag_element_not_just_anywhere(tmp_path):
    html = page('<section data-sample="story"><p>Sample: your story goes here</p></section>')
    assert any("story sample section is missing its tag" in p for p in check(html, tmp_path)["problems"])


def test_an_unknown_sample_kind_fails(tmp_path):
    result = check(page(sample("awards", "<p>Your awards</p>", tag="Sample: your awards")), tmp_path)
    assert any("unknown kind" in p for p in result["problems"])


@pytest.mark.parametrize("fake", [
    "★★★★★", "Rated 4.9 by our clients", "5 stars", "Haircuts from $25", "Open 9:00 AM - 6:00 PM",
    "Serving Maplewood since 1998", "Over 20 years of experience", "Award-winning stylists", "Ask for Dr. Patel",
    "Licensed and insured", "Satisfaction guaranteed", "Open Saturdays",
])
def test_a_fake_fact_fails_even_inside_a_tagged_sample(tmp_path, fake):
    kind = "hours" if "Open" in fake else "reviews"
    result = check(page(sample(kind, f"<p>{fake}</p>")), tmp_path)
    assert not result["passed"], fake


def test_a_fake_review_with_a_quote_and_rating_fails_inside_the_reviews_sample(tmp_path):
    body = "<blockquote>Best haircut ever!</blockquote><p>Sarah K., rated 5/5</p>"
    assert not check(page(sample("reviews", body)), tmp_path)["passed"]


def test_untagged_claims_still_fail_next_to_tagged_samples(tmp_path):
    html = page(sample("story", "<p>Your story goes here.</p>") + "<p>Award-winning stylists</p>")
    result = check(html, tmp_path)
    assert any("an award" in p and "sample" not in p for p in result["problems"])


def test_a_sample_photo_must_be_labeled(tmp_path):
    labeled = ('<figure data-sample-photo><img src="/_astro/sample-photo-salon-01.Ab12.webp" alt="Sample photo: a '
               'styling chair"><figcaption>Sample photo</figcaption></figure>')
    assert check(page(sample("gallery", labeled)), tmp_path)["passed"]
    bare = '<img src="/_astro/sample-photo-salon-01.Ab12.webp" alt="A styling chair">'
    result = check(page(bare), tmp_path)
    assert any("isn't labeled \"Sample photo\"" in p for p in result["problems"])
    in_srcset = '<picture><source srcset="/_astro/sample-photo-x.1.avif 640w"><img src="/a.svg" alt=""></picture>'
    assert not check(page(in_srcset), tmp_path)["passed"]


def test_their_own_photos_need_no_sample_label(tmp_path):
    assert check(page('<img src="/_astro/photo-1.Zz9.webp" alt="Inside the salon">'), tmp_path)["passed"]


# ---------------- the template keeps the same words ----------------

def test_the_template_tag_component_uses_the_exact_tags():
    text = (TEMPLATE / "components" / "samples" / "SampleTag.astro").read_text(encoding="utf-8")
    for kind, tag in SAMPLE_TAGS.items():
        assert f'{kind}: "{tag}"' in text


def test_every_showcase_section_has_a_template_component_marked_as_a_sample():
    found = set()
    for path in (TEMPLATE / "components" / "samples").glob("*.astro"):
        found |= set(re.findall(r'data-sample="(\w+)"', path.read_text(encoding="utf-8")))
    assert found == set(SHOWCASE)


def test_the_starter_page_uses_no_outside_import_and_keeps_reduced_motion():
    css = (TEMPLATE / "styles" / "global.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in css and "animation: none !important" in css
    base = (TEMPLATE / "layouts" / "Base.astro").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in base and 'name="robots" content="noindex' in base


def test_the_brief_carries_the_tags_and_the_sample_photos():
    brief = make_brief(LEAD, ["no_site"], None, [{"file": "src/assets/sample-photos/sample-photo-a.jpg"}])
    assert brief["sample_sections"]["tags"] == SAMPLE_TAGS
    assert brief["sample_photos"][0]["file"].endswith("sample-photo-a.jpg")
    assert make_brief(LEAD, [], None)["sample_photos"] == []


# ---------------- the photo library ----------------

ROW = "| salon/salon-01-chair.jpg | an empty styling chair | https://unsplash.com/photos/abc123 | Jane Doe | Unsplash License |"


def library(tmp_path: Path, rows: list[str], files: dict[str, bytes]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "types.yaml").write_text("salon: [hair_salon, beauty_salon]\nbakery: [bakery]\n", encoding="utf-8")
    (tmp_path / "LICENSES.md").write_text(
        "# Licenses\n\n| File | Shows | Source page | Author | License |\n|---|---|---|---|---|\n" + "\n".join(rows),
        encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(body)
    return tmp_path


def test_a_photo_with_a_complete_license_row_is_usable(tmp_path):
    lib = library(tmp_path, [ROW], {"salon/salon-01-chair.jpg": b"x" * 2000})
    [photo] = photos_for("beauty_salon", 6, 1500, lib)
    assert photo.author == "Jane Doe" and photo.credit == "Photo by Jane Doe on Unsplash"


def test_a_photo_without_a_license_entry_can_never_be_used(tmp_path):
    lib = library(tmp_path, [], {"salon/salon-02-wash.jpg": b"x" * 2000})
    assert photos_for("hair_salon", 6, 1500, lib) == []
    assert any("no row in LICENSES.md" in p for p in library_check(lib)["problems"])


@pytest.mark.parametrize("row, why", [
    (ROW.replace("Unsplash License", "CC BY-NC"), "isn't one we accept"),
    (ROW.replace("Unsplash License", "Pexels License"), "isn't on pexels.com"),
    (ROW.replace("https://unsplash.com/photos/abc123", "https://example.com/photo"), "isn't on unsplash.com"),
    (ROW.replace("https://unsplash.com", "http://unsplash.com"), "https://"),
    (ROW.replace("Jane Doe", "TODO"), "author is missing"),
    (ROW.replace("an empty styling chair", "?"), "shows is missing"),
])
def test_an_incomplete_or_wrong_license_row_makes_the_photo_unusable(tmp_path, row, why):
    lib = library(tmp_path, [row], {"salon/salon-01-chair.jpg": b"x" * 2000})
    assert photos_for("hair_salon", 6, 1500, lib) == []
    assert any(why in p for p in library_check(lib)["problems"])


def test_a_photo_too_big_is_refused(tmp_path):
    lib = library(tmp_path, [ROW], {"salon/salon-01-chair.jpg": b"x" * 4096})
    assert photos_for("hair_salon", 6, 2, lib) == []


def test_a_listed_photo_not_downloaded_yet_is_waiting_not_usable(tmp_path):
    lib = library(tmp_path, [ROW], {})
    check = library_check(lib)
    assert check["waiting"] == ["salon/salon-01-chair.jpg"] and check["usable"]["salon"] == []


def test_types_map_to_folders_and_unknown_types_get_nothing(tmp_path):
    lib = library(tmp_path, [ROW], {"salon/salon-01-chair.jpg": b"x"})
    assert folder_for("beauty_salon", lib / "types.yaml") == "salon"
    assert photos_for("nail_salon", 6, 1500, lib) == [] and photos_for("hair_salon", 0, 1500, lib) == []


def test_copied_photos_are_renamed_and_carry_the_label(tmp_path):
    lib = library(tmp_path / "lib", [ROW], {"salon/salon-01-chair.jpg": b"x" * 10})
    entries = copy_into(tmp_path / "ws", photos_for("hair_salon", 6, 1500, lib))
    assert entries == [{"file": "src/assets/sample-photos/sample-photo-salon-01-chair.jpg",
                        "shows": "an empty styling chair", "label_required": SAMPLE_PHOTO_LABEL,
                        "credit": "Photo by Jane Doe on Unsplash"}]
    assert (tmp_path / "ws" / entries[0]["file"]).read_bytes() == b"x" * 10


def test_the_real_library_has_every_folder_and_only_licensed_photos_are_usable():
    folders = photo_library.folders()
    assert set(folders) == {"salon", "barber", "dentist", "hvac", "landscaper", "bakery", "florist", "auto_repair"}
    check = library_check(max_kb=None)
    for photos in check["usable"].values():
        for photo in photos:
            assert row_problems(license_rows().get(f"{photo.folder}/{photo.path.name}")) == []


# ---------------- booking: the salon's own app, or a free one we set up (Charlie, 2026-11-04) ----------------

BOOK_NOTE = "Your finished site links the booking app you already use, or a free one we set up with you."


def test_the_book_online_note_says_how_booking_is_delivered_and_the_facts_check_accepts_it(tmp_path):
    component = (Path(__file__).resolve().parents[1] / "packs/websites/site_template/src/components/samples/"
                 "BookOnline.astro").read_text(encoding="utf-8")
    assert BOOK_NOTE in component and "Online booking comes with your finished site." in component
    sections = sample("booking", "<button popovertarget=b>Book online</button><div id=b popover>Online booking comes "
                                 f"with your finished site. {BOOK_NOTE}</div>")
    result = check(page(sections), tmp_path)
    assert not [p for p in result["problems"] if "booking" in p.lower() or "free" in p.lower()], result["problems"]


def test_the_booking_fact_is_an_approved_fact_for_the_guards_soft_check():
    from packs.websites.offer import load_offer
    facts = " ".join(load_offer().facts())
    assert "booking app the business already uses" in facts and "Square Appointments" in facts
    assert "no client booking data held by us" in facts
