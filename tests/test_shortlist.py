"""Discovery to shortlist (ADR-049): find businesses with sources, find public contacts on their own pages, and let the user confirm or reject each address.

Only fakes: no live search, no live fetch, no live send.
"""

import json

import pytest

from eventmate.integrations.places import PlacesNotConfigured, PlacesSource
from eventmate.integrations.web_access import FetchError, RawPage
from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from eventmate.sourcing.search import SourceNotSupported
from tests.conftest import TODAY
from tests.test_conversations import Clock, FakeChannel
from tests.test_discovery import Transport as OsmTransport, src as osm_src
from tests.test_places import KEY, Net, place
from eventmate.web.app import create_app

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
OSM = {"elements": [
    {"type": "node", "id": 11, "lat": 18.52, "lon": 73.85, "tags": {"name": "Orchid Hall", "amenity": "events_venue", "website": "https://orchid.example", "contact:email": "Bookings@Orchid.example", "addr:city": "Pune"}},
    {"type": "way", "id": 22, "center": {"lat": 18.53, "lon": 73.86}, "tags": {"name": "Pune Conference Centre", "amenity": "conference_centre", "email": "a@x.example;b@x.example"}},
    {"type": "node", "id": 33, "lat": 18.5, "lon": 73.8, "tags": {"name": "Lakeside <b>Hall</b>", "amenity": "events_venue", "website": "javascript:alert(1)", "email": "not an address"}},
]}


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def fetch_html(self, url):
        self.asked.append(url)
        if url not in self.pages:
            raise FetchError("the page answered HTTP 404")
        return RawPage(url=url, title="", text="", html=self.pages[url])


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    def build(places=None, osm=OSM, key=KEY):
        e = EventMate(
            log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="sl", require_symbolic=False, discovery=osm_src(OsmTransport(payload=osm)),
            places=PlacesSource(api_key=key, transport=places or Net([place("Sharma Caterers")])),
            channels={"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp", False, False)},
        )
        e.outreach_store.now = e.conversations.now = clock
        e.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=100, whatsapp_gap_s=0)
        return e
    return build


@pytest.fixture
def em(make):
    return make()


@pytest.fixture
def pid(em):
    return em.plan(COLLEGE, today=TODAY).plan.plan_id


def ids(items):
    return [i.business.name for i in items]


# ---- OpenStreetMap: venues only

def test_an_osm_venue_search_shortlists_discovered_businesses_with_their_source_and_never_touches_the_vendor_database(em, pid):
    seeds = [p.id for c in C for p in em.repo.providers(c)]
    out = em.search_vendors(pid, C.VENUE, "osm")
    items = em.shortlist_view(pid)
    assert out.added == 3 and out.source == "osm" and set(ids(items)) == {"Orchid Hall", "Pune Conference Centre", "Lakeside Hall"}
    orchid = next(i for i in items if i.business.name == "Orchid Hall")
    assert orchid.business.external_id == "osm:node/11" and orchid.business.source == "osm" and not orchid.business.is_test and orchid.business.origin == "discovered"
    assert orchid.business.source_url == "https://www.openstreetmap.org/node/11" and orchid.business.website == "https://orchid.example" and orchid.business.city == "Pune"
    assert [p.id for c in C for p in em.repo.providers(c)] == seeds and all(em.repo.get(C.VENUE, i.business.vendor_id) is None for i in items)


def test_a_listing_email_becomes_a_candidate_attributed_to_the_listing_and_nothing_is_confirmed(em, pid):
    em.search_vendors(pid, C.VENUE, "osm")
    items = {i.business.name: i for i in em.shortlist_view(pid)}
    c = items["Orchid Hall"].candidates[0]
    assert (c.value, c.source_kind, c.source_url, c.status) == ("bookings@orchid.example", "listing_field", "https://www.openstreetmap.org/node/11", "found")
    assert "different domain" not in " ".join(c.flags) and items["Orchid Hall"].contact is None
    assert items["Pune Conference Centre"].candidates == () and items["Lakeside Hall"].candidates == ()  # a list of addresses or a non-address in the tag is not offered
    assert items["Lakeside Hall"].business.website is None  # a javascript: website is dropped, the business is kept


def test_hostile_listing_text_is_inert_and_a_name_is_cleaned(em, pid):
    em.search_vendors(pid, C.VENUE, "osm")
    names = ids(em.shortlist_view(pid))
    assert "Lakeside Hall" in names and all("<" not in n and "\n" not in n for n in names)


@pytest.mark.parametrize("category", [C.CATERER, C.PHOTOGRAPHER])
def test_unsupported_category_and_source_combinations_say_so_instead_of_an_empty_success(em, pid, category):
    with pytest.raises(SourceNotSupported, match="not supported by OpenStreetMap"):
        em.search_vendors(pid, category, "osm")
    assert em.shortlist_view(pid) == []


def test_the_overpass_tag_map_still_has_no_caterer_or_photographer():
    from eventmate.integrations.discovery import TAGS

    assert set(TAGS) == {"venue", "hotel", "restaurant"}


def test_an_unknown_source_is_refused(em, pid):
    with pytest.raises(ValueError, match="source"):
        em.search_vendors(pid, C.VENUE, "yellow-pages")


# ---- Google Places: every category, websites when published

def test_a_places_search_shortlists_caterers_with_the_listings_own_website(make):
    net = Net([place("Sharma Caterers") | {"id": "ChIJabcdef123456", "websiteUri": "https://sharma-caterers.example/"}, place("No Site Caterers", uri="https://maps.google.com/?cid=2") | {"id": "ChIJzzzzzz654321"}])
    e = make(places=net)
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    out = e.search_vendors(pid, C.CATERER, "places")
    assert out.added == 2 and "places.websiteUri" in net.calls[0][1]["X-Goog-FieldMask"] and "places.id" in net.calls[0][1]["X-Goog-FieldMask"]
    by = {i.business.name: i.business for i in e.shortlist_view(pid)}
    assert by["Sharma Caterers"].external_id == "gplaces:ChIJabcdef123456" and by["Sharma Caterers"].website == "https://sharma-caterers.example/" and by["Sharma Caterers"].website_source == "listing"
    assert by["Sharma Caterers"].source_url and by["No Site Caterers"].website is None and by["Sharma Caterers"].rating == 4.5
    assert all(b.source == "gplaces" and not b.is_test for b in by.values())


def test_a_place_without_an_id_is_skipped_with_a_warning_and_a_missing_key_is_a_clear_error(make):
    e = make(places=Net([place("No Id Caterers")]))
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    out = e.search_vendors(pid, C.CATERER, "places")
    assert out.added == 0 and any("without a place id" in w for w in out.warnings) and e.shortlist_view(pid) == []
    nokey = make(key="")
    nokey.places = PlacesSource(api_key="")
    with pytest.raises(PlacesNotConfigured):
        nokey.search_vendors(pid, C.CATERER, "places")


def test_the_places_key_never_appears_in_a_result(make):
    e = make(places=Net([place("Sharma Caterers") | {"id": "ChIJabcdef123456"}]))
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    e.search_vendors(pid, C.CATERER, "places")
    assert KEY not in json.dumps([i.model_dump(mode="json") for i in e.shortlist_view(pid)])


def test_searching_twice_does_not_duplicate_and_each_plan_has_its_own_shortlist(em, pid):
    em.search_vendors(pid, C.VENUE, "osm")
    again = em.search_vendors(pid, C.VENUE, "osm")
    assert again.added == 0 and len(em.shortlist_view(pid)) == 3
    other = em.plan(COLLEGE.replace("150", "90"), today=TODAY).plan.plan_id
    assert em.shortlist_view(other) == [] and len(em.externals.all()) == 3
    em.search_vendors(other, C.VENUE, "osm")
    assert len(em.shortlist_view(other)) == 3 and len(em.externals.all()) == 3  # the same business is one record across plans


def test_the_shortlist_can_be_filtered_by_category(make, pid=None):
    e = make(places=Net([place("Sharma Caterers") | {"id": "ChIJabcdef123456"}]))
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    e.search_vendors(pid, C.VENUE, "osm")
    e.search_vendors(pid, C.CATERER, "places")
    assert len(e.shortlist_view(pid)) == 4 and ids(e.shortlist_view(pid, C.CATERER)) == ["Sharma Caterers"]


# ---- contacts from the business's own pages

def pages(**kw):
    return {"https://sharma-caterers.example/": "<html><body><a href='/contact'>Contact</a><p>hello@sharma-caterers.example</p></body></html>",
            "https://sharma-caterers.example/contact": "<html><body><a href='mailto:orders@sharma-caterers.example'>Orders</a></body></html>"} | kw


@pytest.fixture
def caterer(make):
    e = make(places=Net([place("Sharma Caterers") | {"id": "ChIJabcdef123456", "websiteUri": "https://sharma-caterers.example/"}]))
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    e.search_vendors(pid, C.CATERER, "places")
    vid = e.shortlist_view(pid)[0].business.vendor_id
    return e, pid, vid


def test_finding_contacts_reads_the_business_site_and_stores_candidates_with_their_pages(caterer):
    e, pid, vid = caterer
    f = FakeFetcher(pages())
    e.contact_fetcher = lambda: f
    res = e.find_business_contacts(vid)
    assert {c.value for c in res.added} == {"hello@sharma-caterers.example", "orders@sharma-caterers.example"} and f.asked == ["https://sharma-caterers.example/", "https://sharma-caterers.example/contact"]
    by = {c.value: c for c in e.shortlist.candidates(vid)}
    assert by["orders@sharma-caterers.example"].source_kind == "page_mailto" and by["orders@sharma-caterers.example"].source_url.endswith("/contact")
    assert all(c.status == "found" and c.confirmed_by is None for c in by.values()) and e.outreach_store.contacts().get(vid) is None  # found is not saved


def test_finding_again_adds_nothing_new_and_a_rejected_address_is_not_offered_again(caterer):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    e.find_business_contacts(vid)
    assert e.find_business_contacts(vid).added == ()
    cand = next(c for c in e.shortlist.candidates(vid) if c.value.startswith("hello"))
    e.reject_contact(vid, cand.candidate_id)
    e.find_business_contacts(vid)
    assert next(c for c in e.shortlist.candidates(vid) if c.value.startswith("hello")).status == "rejected" and len(e.shortlist.candidates(vid)) == 2


def test_a_business_without_a_website_needs_one_typed_and_the_typed_one_is_marked_as_the_users(make):
    e = make(places=Net([place("No Site Caterers") | {"id": "ChIJzzzzzz654321"}]))
    pid = e.plan(COLLEGE, today=TODAY).plan.plan_id
    e.search_vendors(pid, C.CATERER, "places")
    vid = e.shortlist_view(pid)[0].business.vendor_id
    with pytest.raises(ValueError, match="website"):
        e.find_business_contacts(vid)
    for bad in ("javascript:alert(1)", "ftp://x.example", "not a url", "http://x.example/\nBcc: a@b.example", "https://" + "a" * 3000, ""):
        with pytest.raises(ValueError):
            e.set_business_website(vid, bad)
    biz = e.set_business_website(vid, "https://nosite-caterers.example/")
    assert biz.website == "https://nosite-caterers.example/" and biz.website_source == "user_input"
    e.contact_fetcher = lambda: FakeFetcher({"https://nosite-caterers.example/": "<p>hi@nosite-caterers.example</p>"})
    assert [c.value for c in e.find_business_contacts(vid).added] == ["hi@nosite-caterers.example"]


def test_a_page_that_cannot_be_read_gives_a_note_not_a_crash(caterer):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher({})
    res = e.find_business_contacts(vid)
    assert res.added == () and any("could not be read" in n for n in res.notes)


def test_an_unknown_or_test_business_cannot_be_searched_for_contacts(caterer, em):
    e, pid, vid = caterer
    with pytest.raises(ValueError):
        e.find_business_contacts("ext_" + "0" * 24)
    with pytest.raises(ValueError):
        e.find_business_contacts("cat-pune-rasoi")
    tv, _ = e.add_test_vendor("Test Caterer", "Pune", C.CATERER, "owner@personal.test", owns_address=True, confirmed_by="Asha")
    with pytest.raises(ValueError, match="test vendor"):
        e.find_business_contacts(tv.vendor_id)


# ---- confirm or reject

def test_confirming_creates_the_contact_with_provenance_and_the_human_edit_is_recorded(caterer):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    e.find_business_contacts(vid)
    cand = next(c for c in e.shortlist.candidates(vid) if c.value.startswith("orders"))
    contact = e.confirm_contact(vid, cand.candidate_id, "Asha", edited_value="Events@Sharma-Caterers.example")
    assert contact.email == "Events@Sharma-Caterers.example" and contact.edited_by_user and contact.origin == "discovered"
    assert contact.source_url.endswith("/contact") and contact.confirmed_by == "Asha" and contact.confirmed_at
    done = e.shortlist.get_candidate(cand.candidate_id)
    assert done.status == "confirmed" and done.confirmed_by == "Asha" and done.confirmed_value == "Events@Sharma-Caterers.example"
    plain = e.confirm_contact(vid, next(c for c in e.shortlist.candidates(vid) if c.value.startswith("hello")).candidate_id, "Asha")
    assert plain.email == "hello@sharma-caterers.example" and not plain.edited_by_user  # the latest confirmation is the address in use


@pytest.mark.parametrize("bad", ["nobody", "a@b.example,c@d.example", "a@b.example\nBcc: x@y.example", "<a@b.example>", "x" * 300 + "@b.example"])
def test_an_edited_address_must_be_a_single_valid_address(caterer, bad):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    e.find_business_contacts(vid)
    cand = e.shortlist.candidates(vid)[0]
    with pytest.raises(ValueError):
        e.confirm_contact(vid, cand.candidate_id, "Asha", edited_value=bad)
    assert e.outreach_store.contacts().get(vid) is None and e.shortlist.get_candidate(cand.candidate_id).status == "found"


def test_confirmation_needs_a_name_a_real_candidate_of_that_business_and_a_found_state(caterer, make):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    e.find_business_contacts(vid)
    cand = e.shortlist.candidates(vid)[0]
    for who in ("", "  ", None):
        with pytest.raises(ValueError):
            e.confirm_contact(vid, cand.candidate_id, who)
    with pytest.raises(ValueError):
        e.confirm_contact(vid, "nope", "Asha")
    with pytest.raises(ValueError):
        e.confirm_contact("ext_" + "0" * 24, cand.candidate_id, "Asha")  # a candidate of another business
    e.confirm_contact(vid, cand.candidate_id, "Asha")
    with pytest.raises(ValueError, match="already confirmed"):
        e.confirm_contact(vid, cand.candidate_id, "Asha")
    other = e.shortlist.candidates(vid)[1]
    e.reject_contact(vid, other.candidate_id)
    with pytest.raises(ValueError, match="already rejected"):
        e.confirm_contact(vid, other.candidate_id, "Asha")


def test_nothing_is_confirmed_by_searching_extracting_or_viewing(caterer):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    e.find_business_contacts(vid)
    e.shortlist_view(pid)
    assert e.outreach_store.contacts() == {} and all(c.status == "found" for c in e.shortlist.candidates(vid))


# ---- API

@pytest.fixture
def api(caterer):
    e, pid, vid = caterer
    e.contact_fetcher = lambda: FakeFetcher(pages())
    return create_app(e, today_fn=lambda: TODAY, poll_seconds=0).test_client(), e, pid, vid


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


def test_api_search_find_confirm_and_list(api):
    c, e, pid, vid = api
    r = post(c, f"/api/plans/{pid}/shortlist/search", {"category": "venue", "source": "osm"})
    assert r.status_code == 200 and r.get_json()["added"] == 3
    f = post(c, f"/api/plans/{pid}/shortlist/{vid}/find-contacts", {})
    assert f.status_code == 200 and len(f.get_json()["added"]) == 2
    items = c.get(f"/api/plans/{pid}/shortlist").get_json()
    sharma = next(i for i in items if i["business"]["vendor_id"] == vid)
    assert sharma["business"]["origin"] == "discovered" and len(sharma["candidates"]) == 2 and sharma["contact"] is None
    cid = sharma["candidates"][0]["candidate_id"]
    ok = post(c, f"/api/plans/{pid}/shortlist/{vid}/contacts/{cid}/confirm", {"by": "Asha"})
    assert ok.status_code == 201 and ok.get_json()["confirmed_by"] == "Asha"
    rej = post(c, f"/api/plans/{pid}/shortlist/{vid}/contacts/{sharma['candidates'][1]['candidate_id']}/reject", {})
    assert rej.status_code == 200 and rej.get_json()["status"] == "rejected"
    site = post(c, f"/api/plans/{pid}/shortlist/{vid}/website", {"url": "https://sharma-caterers.example/about"})
    assert site.status_code == 200 and site.get_json()["website_source"] == "user_input"


def test_api_says_not_supported_for_an_unsupported_source_and_rejects_bad_input(api):
    c, e, pid, vid = api
    r = post(c, f"/api/plans/{pid}/shortlist/search", {"category": "caterer", "source": "osm"})
    assert r.status_code == 422 and r.get_json()["status"] == "not_supported" and "not supported by OpenStreetMap" in r.get_json()["error"]
    for url, body in (
        (f"/api/plans/{pid}/shortlist/search", {"category": "yacht", "source": "osm"}), (f"/api/plans/{pid}/shortlist/search", {"category": "venue", "source": "x"}),
        (f"/api/plans/{pid}/shortlist/search", {"category": "venue", "limit": 99}), (f"/api/plans/{pid}/shortlist/search", {"category": "venue", "surprise": 1}),
        (f"/api/plans/{pid}/shortlist/{vid}/website", {"url": "javascript:alert(1)"}), (f"/api/plans/{pid}/shortlist/{vid}/contacts/nope/confirm", {"by": "Asha"}),
        (f"/api/plans/{pid}/shortlist/{vid}/contacts/nope/confirm", {}), (f"/api/plans/{pid}/shortlist/ext_zz/find-contacts", {}), ("/api/plans/nope/shortlist/search", {"category": "venue"}),
    ):
        assert post(c, url, body).status_code in (400, 404, 422), (url, body)
    raw = c.post(f"/api/plans/{pid}/shortlist/search", data=json.dumps({"category": "venue"}), headers={"Content-Type": "application/json"})
    assert raw.status_code in (400, 403)
    assert c.get("/api/plans/nope/shortlist").status_code in (400, 404)
