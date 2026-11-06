"""Sample sections: the honest showcase on every preview (Charlie, 2026-11-02).

A preview is built only from a public listing, so on its own it looks thin ("something anyone could put
together"). Sample sections show the owner the full product, the way the finished site will look, without
claiming anything about the business: each one sits inside an element marked data-sample="<kind>" and
carries its small tag, word for word, in an element marked data-sample-tag.

What may be in a sample section: placeholder words that say what goes there ("Your hours", "Your
customers' reviews appear here"), licensed sample photos labeled "Sample photo", and drawings. What may
never be in one, tagged or not: invented facts. The facts check (builder_check.facts_check) still runs every
banned-claim rule on sample text; the one word it allows there is "reviews" (the reviews sample has to say
what goes in it). Times of day, ratings, stars, prices, names and quotes fail there exactly like anywhere
else, so there are no fake reviews and no made-up hours, ever.

The site template's components (site_template/src/components/samples/) use these exact words, and a test
keeps the two in step.
"""

# kind -> the tag, word for word. The facts check compares them as plain words (case and punctuation aside).
SAMPLE_TAGS = {
    "gallery": "Sample: your photos go here",
    "story": "Sample: your story goes here",
    "hours": "Sample: your hours",
    "reviews": "Sample: your reviews appear here",
    "booking": "Sample: online booking comes with your site",
    "map": "Sample: map illustration",
}
# A preview should show all of these (the Builder is told so, and the Critic sees which are there). "hours" is
# only expected when the business doesn't publish its own hours: then the real hours go up instead.
SHOWCASE = ("gallery", "story", "hours", "reviews", "booking", "map")
# The only banned-claim words a tagged sample section may contain (see builder_check.BANNED_CLAIMS).
ALLOWED_IN_SAMPLES = ("reviews or testimonials",)
