from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalizers import (  # noqa: E402
    GETYOURGUIDE_ACTOR,
    GETYOURGUIDE_FALLBACK_ACTOR,
    NORMALIZED_ITEM_KEYS,
    TRIPADVISOR_ACTOR,
    TRIPADVISOR_REVIEWS_ACTOR,
    classify_location,
    is_blocked_payload,
    normalize_item,
    normalize_review,
    normalize_reviews,
)


class TripadvisorListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "locationId": 12918215,
            "name": "Benedictine Abbey and Museum (Bences Apatsag)",
            "type": "ATTRACTION",
            "description": "A working abbey, royal crypt and views over Lake Balaton.",
            "webUrl": (
                "https://www.tripadvisor.com/Attraction_Review-g274891-d12918215-"
                "Reviews-Benedictine_Abbey-Tihany_Veszprem_County_Central_Transdanubia.html"
            ),
            "rating": 4.7,
            "numberOfReviews": "17,699 reviews",
            "rankingPosition": 3,
            "price": {"amount": 3400, "currency": "HUF"},
            "addressObj": {
                "street1": "I. Andras ter 1",
                "city": "Tihany",
                "state": "Veszprem County",
                "country": "Hungary",
                "postalcode": "8237",
            },
            "latitude": 46.91378,
            "longitude": 17.88916,
            "category": {"name": "Sights & Landmarks"},
            "subcategories": [
                {"name": "Historic Sites"},
                {"name": "Religious Sites"},
            ],
            "photos": [
                {
                    "id": "photo-1",
                    "url": "https://dynamic-media-cdn.tripadvisor.com/media/photo-o/abbey.jpg",
                    "caption": "Abbey above Lake Balaton",
                    "width": 1200,
                    "height": 800,
                }
            ],
            "unknownActorInternals": {"session": "must-remain-raw-only"},
        }

    def test_realistic_listing_maps_every_database_family(self) -> None:
        before = copy.deepcopy(self.payload)
        item = normalize_item(TRIPADVISOR_ACTOR, self.payload, rank=7)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(tuple(item), NORMALIZED_ITEM_KEYS)
        self.assertEqual(item["source"], "tripadvisor")
        self.assertEqual(item["external_id"], "12918215")
        self.assertEqual(item["kind"], "attraction")
        self.assertEqual(item["title"], self.payload["name"])
        self.assertEqual(item["rating"], 4.7)
        self.assertEqual(item["review_count"], 17699)
        self.assertEqual(item["rank"], 7, "query rank must override an actor-internal position")
        self.assertEqual(item["price"], 3400.0)
        self.assertEqual(item["currency"], "HUF")
        self.assertEqual(item["country"], "HU")
        self.assertEqual(item["locality"], "Tihany")
        self.assertEqual(item["region"], "Veszprem County")
        self.assertIn("I. Andras ter 1", item["address"])
        self.assertEqual(item["lat"], 46.91378)
        self.assertEqual(item["lon"], 17.88916)
        self.assertEqual(item["location_scope"], "outside-budapest")
        self.assertFalse(item["starts_in_budapest"])
        self.assertEqual(
            item["categories"],
            ["Sights & Landmarks", "Historic Sites", "Religious Sites"],
        )
        self.assertEqual(item["media"][0]["external_id"], "photo-1")
        self.assertEqual(item["media"][0]["media_type"], "image")
        self.assertEqual(item["media"][0]["sort_order"], 0)
        self.assertNotIn("unknownActorInternals", item)
        self.assertEqual(self.payload, before, "normalization must not mutate the raw item")

    def test_tripadvisor_id_falls_back_to_d_number_in_url(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload.pop("locationId")
        payload["webUrl"] += "?m=19905&filterLang=ALL"
        item = normalize_item(TRIPADVISOR_ACTOR, payload)
        self.assertEqual(item["external_id"], "12918215")

    def test_localized_review_count_does_not_become_seventeen(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["numberOfReviews"] = "17.699 Bewertungen"
        item = normalize_item(TRIPADVISOR_ACTOR, payload)
        self.assertEqual(item["review_count"], 17699)

    def test_budapest_listing_is_not_misclassified_by_hungary_text(self) -> None:
        payload = {
            "id": "123",
            "name": "Hospital in the Rock Nuclear Bunker Museum",
            "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d123-Reviews.html",
            "addressObj": {"city": "Budapest", "country": "Hungary"},
            "latitude": 47.501,
            "longitude": 19.031,
        }
        item = normalize_item(TRIPADVISOR_ACTOR, payload)
        self.assertEqual(item["location_scope"], "budapest")

    def test_viator_offer_list_is_retained_as_third_party_packages(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["offerGroup"] = {
            "lowestPrice": "HUF 7,955.23",
            "offerList": [
                {
                    "productCode": "53868P20",
                    "title": "Historic countryside bicycle tour",
                    "description": "A separately bookable partner tour.",
                    "price": "HUF 7,955.23",
                    "partner": "Viator",
                    "primaryCategory": "Bike Tours",
                    "url": "https://www.tripadvisor.com/Commerce?offer=53868P20",
                }
            ],
        }

        item = normalize_item(TRIPADVISOR_ACTOR, payload)

        self.assertEqual(len(item["packages"]), 1)
        offer = item["packages"][0]
        self.assertEqual(offer["external_id"], "53868P20")
        self.assertEqual(offer["price"], 7955.23)
        self.assertEqual(offer["currency"], "HUF")
        self.assertEqual(offer["provider"], "Viator")
        self.assertEqual(offer["category"], "Bike Tours")
        self.assertIn("Commerce", offer["url"])


class TripadvisorReviewTests(unittest.TestCase):
    def test_review_batch_preserves_translation_but_never_normalizes_identity(self) -> None:
        payload = {
            "locationId": "12918215",
            "locationName": "Benedictine Abbey and Museum",
            "reviews": [
                {
                    "reviewId": "r-991",
                    "rating": 5,
                    "translatedTitle": "A memorable panorama",
                    "translatedText": "The crypt and view were the highlight of our Balaton day.",
                    "translationLanguage": "en",
                    "originalTitle": "Un panorama memorabile",
                    "originalText": "La cripta e il panorama erano il momento migliore.",
                    "originalLanguage": "it",
                    "publishedDate": "2026-06-14",
                    "travelDate": "2026-06",
                    "helpfulVotes": 8,
                    "url": "https://www.tripadvisor.com/Profile/private-reviewer",
                    "user": {
                        "username": "Private Reviewer",
                        "userId": "sensitive-id",
                        "avatar": "https://example.invalid/private.jpg",
                    },
                }
            ],
        }

        envelope = normalize_item(TRIPADVISOR_REVIEWS_ACTOR, payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope["kind"], "review-batch")
        self.assertEqual(envelope["external_id"], "12918215")
        review = envelope["reviews"][0]
        self.assertEqual(review["external_id"], "r-991")
        self.assertEqual(review["activity_external_id"], "12918215")
        self.assertEqual(review["title"], "A memorable panorama")
        self.assertEqual(review["body"], "The crypt and view were the highlight of our Balaton day.")
        self.assertEqual(review["language"], "en")
        self.assertEqual(review["original_language"], "it")
        self.assertTrue(review["is_translated"])
        self.assertEqual(review["original_title"], "Un panorama memorabile")
        self.assertEqual(review["original_body"], "La cripta e il panorama erano il momento migliore.")
        self.assertEqual(review["review_date"], "2026-06-14")
        self.assertEqual(review["helpful_count"], 8)
        normalized_json = json.dumps(envelope)
        self.assertNotIn("Private Reviewer", normalized_json)
        self.assertNotIn("sensitive-id", normalized_json)
        self.assertNotIn("private.jpg", normalized_json)
        self.assertNotIn("/Profile/", normalized_json)
        self.assertNotIn("reviewer", normalized_json.casefold())

    def test_generated_review_id_is_stable_and_ignores_reviewer_name(self) -> None:
        base = {
            "locationId": "42",
            "rating": 4,
            "title": "Worth the drive",
            "text": "Small but genuinely unusual.",
            "publishedDate": "2026-05-01",
            "language": "en",
            "user": {"username": "Alice"},
        }
        renamed = copy.deepcopy(base)
        renamed["user"]["username"] = "Completely Different Name"
        first = normalize_review(TRIPADVISOR_REVIEWS_ACTOR, base)
        second = normalize_review(TRIPADVISOR_REVIEWS_ACTOR, renamed)
        self.assertRegex(first["external_id"], r"^review-[0-9a-f]{24}$")
        self.assertEqual(first["external_id"], second["external_id"])

    def test_single_actor_review_payload_is_supported(self) -> None:
        reviews = normalize_reviews(
            TRIPADVISOR_REVIEWS_ACTOR,
            {
                "locationId": 81,
                "id": "review-81-a",
                "rating": "4.0 of 5 bubbles",
                "reviewTitle": "Quiet weekday visit",
                "reviewText": "Almost no queue on Thursday.",
                "languageCode": "en",
            },
        )
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["external_id"], "review-81-a")
        self.assertEqual(reviews[0]["rating"], 4.0)

    def test_generated_id_uses_original_text_not_changing_translation(self) -> None:
        base = {
            "locationId": 7,
            "rating": 5,
            "translatedText": "A singular underground experience.",
            "translationLanguage": "en",
            "originalText": "Esperienza sotterranea unica.",
            "originalLanguage": "it",
            "publishedDate": "2026-04-03",
        }
        german = copy.deepcopy(base)
        german["translatedText"] = "Ein einzigartiges Erlebnis unter Tage."
        german["translationLanguage"] = "de"
        self.assertEqual(
            normalize_review(TRIPADVISOR_REVIEWS_ACTOR, base)["external_id"],
            normalize_review(TRIPADVISOR_REVIEWS_ACTOR, german)["external_id"],
        )


class GetYourGuideTests(unittest.TestCase):
    def test_real_listing_actor_shape_maps_rating_count_and_thumbnail_urls(self) -> None:
        payload = {
            # Sanitized values with the exact field shape observed from
            # piotrv1001/getyourguide-listings-scraper in both discovery and
            # includeDetails datasets.
            "activityId": "700001",
            "name": "Eger memorable countryside activity",
            "url": "https://www.getyourguide.com/eger-l1573/example-t700001/",
            "rating": 4.8,
            "ratingCount": 765,
            "price": 24.5,
            "currency": "EUR",
            "location": "Eger",
            "sourceCityUrl": "https://www.getyourguide.com/eger-l1573/",
            "sourcePage": 1,
            "imageUrl": "https://cdn.getyourguide.com/img/example/hero.jpg",
            "thumbnailUrls": [
                "https://cdn.getyourguide.com/img/example/hero.jpg",
                "https://cdn.getyourguide.com/img/example/second.jpg",
                "https://cdn.getyourguide.com/img/example/second.jpg",
            ],
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["review_count"], 765)
        self.assertEqual(
            [media["url"] for media in item["media"]],
            [
                "https://cdn.getyourguide.com/img/example/hero.jpg",
                "https://cdn.getyourguide.com/img/example/second.jpg",
            ],
        )
        self.assertEqual([media["sort_order"] for media in item["media"]], [0, 1])

    def test_shallow_from_budapest_balaton_product_classifies_destination(self) -> None:
        payload = {
            # Exact shallow keys emitted by
            # piotrv1001/getyourguide-listings-scraper.
            "activityId": "t557721",
            "name": "From Budapest: Lake Balaton, Tihany & Herend Day Trip",
            "url": (
                "https://www.getyourguide.com/budapest-l29/from-budapest-lake-balaton-"
                "tihany-herend-t557721/?ranking_uuid=tracking"
            ),
            "rating": "4.8",
            "reviewCount": "1,284 reviews",
            "price": "From €79.90 per person",
            "currency": "EUR",
            "duration": "10 hours",
            "freeCancellation": True,
            "location": "Budapest",
            "imageUrl": "https://cdn.getyourguide.com/img/tour/hero.jpg",
            "sourceCityUrl": "https://www.getyourguide.com/budapest-l29/",
            "sourcePage": 2,
            "categories": ["Day trips", {"name": "Nature & adventure"}],
        }
        item = normalize_item(GETYOURGUIDE_ACTOR, payload, rank=1)

        self.assertEqual(item["external_id"], "557721")
        self.assertEqual(item["source"], "getyourguide")
        self.assertEqual(item["kind"], "experience")
        self.assertEqual(item["rating"], 4.8)
        self.assertEqual(item["review_count"], 1284)
        self.assertEqual(item["price"], 79.9)
        self.assertEqual(item["currency"], "EUR")
        self.assertEqual(item["duration"], "10 hours")
        self.assertEqual(item["cancellation"], "Free cancellation")
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["location_scope"], "outside-budapest")
        self.assertTrue(item["starts_in_budapest"])
        self.assertEqual(item["country"], "HU")
        self.assertEqual(item["locality"], "Budapest", "source geography remains available")
        self.assertEqual(len(item["media"]), 1)

    def test_common_budapest_pickup_typo_still_classifies_paty_destination(self) -> None:
        payload = {
            "activityId": "345116",
            "name": "From Budpaest: Páty Wine Village Tour with Tastings",
            "url": "https://www.getyourguide.com/paty-l1/tour-t345116/",
            "location": "Páty",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertTrue(item["starts_in_budapest"])
        self.assertEqual(item["country"], "HU")
        self.assertEqual(item["location_scope"], "outside-budapest")

    def test_cross_sold_foreign_locality_overrides_hungary_collection_source(self) -> None:
        payload = {
            "activityId": "1042046",
            "name": "From Vienna: Bratislava and Budapest Guided Day Trip",
            "url": "https://www.getyourguide.com/bratislava-l765/example-t1042046/",
            "location": "Bratislava",
            "sourceCityUrl": "https://www.getyourguide.com/gyor-l2558/",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["country"], "SK")
        self.assertEqual(item["locality"], "Bratislava")
        self.assertEqual(item["location_scope"], "foreign")

    def test_multicountry_title_without_geography_is_foreign(self) -> None:
        payload = {
            "activityId": "1047640",
            "name": "Vienna: Bratislava & Budapest Small Group Guided Day Tour",
            "url": "https://www.getyourguide.com/example-l1/tour-t1047640/",
            "description": "Visit two neighboring capitals in one day.",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "foreign")

    def test_hungarian_destination_title_can_correct_foreign_collection_locality(self) -> None:
        payload = {
            "activityId": "1010377",
            "name": "Esztergom: 1-hour Sightseeing cruise",
            "url": "https://www.getyourguide.com/nove-zamky-l1/cruise-t1010377/",
            "location": "Nové Zámky",
            "sourceCityUrl": "https://www.getyourguide.com/visegrad-l1566/",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "outside-budapest")
        self.assertEqual(item["country"], "HU")
        self.assertEqual(item["locality"], "Esztergom")

    def test_detail_description_can_correct_false_outside_collection_assignment(self) -> None:
        payload = {
            "activityId": "832031",
            "name": "Tiny Sculptures, Big Stories: Kolodko mini statue tour",
            "url": (
                "https://www.getyourguide.com/badacsonytomaj-magyarorszag-l246703/"
                "tiny-sculptures-big-stories-kolodko-mini-statue-tour-t832031/"
            ),
            "location": "Badacsonytomaj, Magyarország",
            "sourceCityUrl": (
                "https://www.getyourguide.com/"
                "badacsonytomaj-magyarorszag-l246703/"
            ),
            "description": (
                "Discover small bronze statues by a guerrilla street artist "
                "hidden around Budapest."
            ),
            "sampleReviews": [
                {"rating": 5, "body": "A fun way to explore Budapest."}
            ],
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "budapest")

    def test_budapest_meeting_text_does_not_hide_named_outside_destination(self) -> None:
        payload = {
            "activityId": "557721",
            "name": "Lake Balaton, Tihany & Herend Day Trip",
            "url": (
                "https://www.getyourguide.com/budapest-l29/"
                "lake-balaton-tihany-herend-t557721/"
            ),
            "description": (
                "Meet your guide in Budapest, then travel to Lake Balaton and "
                "spend the day exploring Tihany and Herend."
            ),
            "location": "Budapest",
            "sourceCityUrl": "https://www.getyourguide.com/budapest-l29/",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "outside-budapest")

    def test_detail_payload_maps_packages_media_languages_and_reviews(self) -> None:
        payload = {
            "id": "901245",
            "title": "Tapolca: Lake Cave boat admission",
            "url": "https://www.getyourguide.com/tapolca-l2069/lake-cave-boat-admission-t901245/",
            "fullDescription": "Row a small boat through the illuminated underground lake.",
            "highlights": [
                "Row your own boat beneath the town",
                "See the rare karst lake up close",
            ],
            "rating": {"average": 4.6, "reviewsCount": 2143},
            "priceFrom": {"amount": 18.5, "currency": "EUR"},
            "duration": {"min": 1, "max": 2, "unit": "hours"},
            "cancellationPolicy": "Cancel up to 24 hours in advance for a full refund",
            "languages": [{"name": "English"}, {"name": "Hungarian"}],
            "alternateLanguageUrls": {
                "de": "https://www.getyourguide.de/tapolca-l2069/t901245/",
                "it": "https://www.getyourguide.it/tapolca-l2069/t901245/",
            },
            "location": {
                "city": "Tapolca",
                "region": "Veszprem County",
                "country": {"code": "HU", "name": "Hungary"},
                "coordinates": {"lat": 46.8837, "lng": 17.4411},
                "address": "Kisfaludy Sandor u. 3, Tapolca",
            },
            "images": [
                {"id": "gyg-hero", "url": "https://cdn.getyourguide.com/tapolca-1.jpg", "alt": "Boat in the cave", "width": 1600, "height": 900},
                {"url": "https://cdn.getyourguide.com/tapolca-2.jpg", "caption": "Underground lake"},
            ],
            "options": [
                {
                    "id": "adult",
                    "title": "Adult admission",
                    "description": "Timed entry and boat ride",
                    "price": {"amount": 18.5, "currency": "EUR"},
                    "originalPrice": 21,
                    "duration": "90 minutes",
                    "availability": "Daily time slots",
                },
                {
                    "id": "family",
                    "title": "Family ticket",
                    "price": {"amount": 49, "currency": "EUR"},
                },
            ],
            # Exact detailed actor key. Author stays in private raw storage.
            "sampleReviews": [
                {
                    "reviewId": "gyg-review-1",
                    "rating": 5,
                    "title": "The boat makes it",
                    "body": "Short, but unlike an ordinary walking cave.",
                    "date": "2026-07-02",
                    "language": "en",
                    "author": "Do Not Normalize",
                }
            ],
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)
        self.assertEqual(item["external_id"], "901245")
        self.assertTrue(item["description"].startswith(payload["fullDescription"]))
        self.assertIn("Highlights:", item["description"])
        self.assertIn("Row your own boat beneath the town", item["description"])
        self.assertEqual(item["rating"], 4.6)
        self.assertEqual(item["review_count"], 2143)
        self.assertEqual(item["country"], "HU")
        self.assertEqual(item["locality"], "Tapolca")
        self.assertEqual(item["region"], "Veszprem County")
        self.assertEqual(item["lat"], 46.8837)
        self.assertEqual(item["lon"], 17.4411)
        self.assertEqual(item["duration"], "1–2 hours")
        self.assertEqual(item["language"], ["English", "Hungarian", "de", "it"])
        self.assertEqual(len(item["media"]), 2)
        self.assertEqual(item["media"][0]["caption"], "Boat in the cave")
        self.assertEqual(len(item["packages"]), 2)
        self.assertEqual(item["packages"][0]["external_id"], "adult")
        self.assertEqual(item["packages"][0]["name"], "Adult admission")
        self.assertEqual(item["packages"][0]["price"], 18.5)
        self.assertEqual(item["packages"][0]["original_price"], 21.0)
        self.assertEqual(item["packages"][0]["availability"], "Daily time slots")
        self.assertEqual(item["reviews"][0]["body"], "Short, but unlike an ordinary walking cave.")
        self.assertNotIn("Do Not Normalize", json.dumps(item))

    def test_from_budapest_foreign_destination_is_not_hungary(self) -> None:
        payload = {
            "activityId": 441,
            "activityTitle": "From Budapest: Vienna Full-Day Private Tour",
            "activityUrl": "https://www.getyourguide.com/budapest-l29/vienna-day-trip-t441/",
            "location": {"city": "Budapest", "country": "Hungary"},
        }
        item = normalize_item(GETYOURGUIDE_ACTOR, payload)
        self.assertTrue(item["starts_in_budapest"])
        self.assertEqual(item["location_scope"], "foreign")

    def test_crawlerbros_fallback_shape_is_supported(self) -> None:
        payload = {
            "productId": "GYG-7788",
            "name": "Eger Castle and thermal bath day",
            "productUrl": "https://www.getyourguide.com/eger-l2048/eger-day-t7788/",
            "shortDescription": "Old town followed by thermal pools.",
            "averageRating": 4.72,
            "reviewCount": 987,
            "startingPrice": 65,
            "currencyCode": "EUR",
            "durationText": "9 hours",
            "availableLanguages": ["English", "German"],
            "city": "Eger",
            "countryCode": "HU",
            "lat": 47.903,
            "lng": 20.377,
            "mainImage": {"url": "https://cdn.example.invalid/eger.jpg", "title": "Eger Castle"},
            "tags": ["Thermal", "History"],
        }
        item = normalize_item(GETYOURGUIDE_FALLBACK_ACTOR, payload)
        self.assertEqual(item["external_id"], "GYG-7788")
        self.assertEqual(item["title"], "Eger Castle and thermal bath day")
        self.assertEqual(item["rating"], 4.72)
        self.assertEqual(item["location_scope"], "outside-budapest")
        self.assertEqual(item["language"], ["English", "German"])
        self.assertEqual(item["categories"], ["Thermal", "History"])
        self.assertEqual(item["media"][0]["caption"], "Eger Castle")

    def test_id_is_stable_across_tracking_queries_and_url_fallback(self) -> None:
        first = {
            "title": "Szalajka Valley adventure",
            "url": "https://www.getyourguide.com/szilvasvarad-l1/szalajka-valley-t123456/?ranking_uuid=abc",
        }
        second = copy.deepcopy(first)
        second["url"] = second["url"].replace("abc", "def")
        self.assertEqual(
            normalize_item(GETYOURGUIDE_ACTOR, first)["external_id"],
            normalize_item(GETYOURGUIDE_ACTOR, second)["external_id"],
        )
        self.assertEqual(normalize_item(GETYOURGUIDE_ACTOR, first)["external_id"], "123456")

    def test_hungary_collection_url_infers_country_without_fake_locality(self) -> None:
        payload = {
            "activityId": "6001",
            "name": "Countryside horse show",
            "url": "https://www.getyourguide.com/hungary-l169024/countryside-horse-show-t6001/",
            "sourceCityUrl": "https://www.getyourguide.com/hungary-l169024/",
            "sourcePage": 1,
        }
        item = normalize_item(GETYOURGUIDE_ACTOR, payload)
        self.assertEqual(item["country"], "HU")
        self.assertIsNone(item["locality"], "a country collection is not a city")
        self.assertEqual(item["location_scope"], "outside-budapest")

    def test_hungary_collection_budapest_title_without_location_stays_budapest(self) -> None:
        payload = {
            "activityId": "88421",
            "name": "Budapest: Parliament Building Guided Tour",
            "url": "https://www.getyourguide.com/budapest-l29/parliament-tour-t88421/",
            "sourceCityUrl": "https://www.getyourguide.com/hungary-l169024/",
            "rating": 4.7,
            "reviewCount": 8400,
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["country"], "HU")
        self.assertIsNone(item["locality"])
        self.assertEqual(item["location_scope"], "budapest")

    def test_budapest_colon_named_day_trip_is_an_outside_destination(self) -> None:
        fixtures = (
            {
                "activityId": "892043",
                "name": "Budapest: Danube Bend Private Day Tour",
                "url": (
                    "https://www.getyourguide.com/szentendre-l1568/"
                    "budapest-danube-bend-private-day-tour-t892043/"
                ),
                "location": "Szentendre",
            },
            {
                "activityId": "1108225",
                "name": "Budapest: Discover the Natural Side of Lake Balaton & Tihany",
                "url": (
                    "https://www.getyourguide.com/tihany-l105767/"
                    "budapest-discover-the-natural-side-of-lake-balaton-t1108225/"
                ),
                "location": "Tihany",
            },
        )

        for payload in fixtures:
            with self.subTest(title=payload["name"]):
                item = normalize_item(GETYOURGUIDE_ACTOR, payload)
                self.assertEqual(item["location_scope"], "outside-budapest")
                self.assertTrue(item["starts_in_budapest"])

    def test_budapest_to_title_marks_the_real_origin(self) -> None:
        payload = {
            "activityId": "1133143",
            "name": "Budapest to Esztergom Basilica Private Day Trip with Tickets",
            "url": (
                "https://www.getyourguide.com/budapest-l29/"
                "budapest-to-esztergom-basilica-private-day-trip-with-tickets-t1133143/"
            ),
            "location": "Budapest",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "outside-budapest")
        self.assertTrue(item["starts_in_budapest"])

    def test_budapest_activity_using_tokaj_wine_is_not_a_tokaj_destination(self) -> None:
        payload = {
            "activityId": "427370",
            "name": "Budapest: Premium Sightseeing Cruise with Tokaj Frizzante",
            "url": "https://www.getyourguide.com/budapest-l29/cruise-t427370/",
            "location": "Budapest",
            "sourceCityUrl": "https://www.getyourguide.com/tokaj-l111945/",
        }

        item = normalize_item(GETYOURGUIDE_ACTOR, payload)

        self.assertEqual(item["location_scope"], "budapest")
        self.assertFalse(item["starts_in_budapest"])


class ClassificationAndSentinelTests(unittest.TestCase):
    def test_coordinates_can_classify_when_actor_omits_country(self) -> None:
        self.assertEqual(
            classify_location(
                title="Hilltop activity",
                description=None,
                country=None,
                locality=None,
                region=None,
                address=None,
                lat=47.4979,
                lon=19.0402,
                starts_in_budapest=False,
            ),
            "budapest",
        )
        self.assertEqual(
            classify_location(
                title="Hilltop activity",
                description=None,
                country=None,
                locality=None,
                region=None,
                address=None,
                lat=46.9,
                lon=17.9,
                starts_in_budapest=False,
            ),
            "unknown",
        )

    def test_neighboring_country_coordinates_are_not_labeled_hungary(self) -> None:
        nearby_foreign = {
            "Rust, Austria": (47.801, 16.672),
            "Komarno, Slovakia": (47.763, 18.122),
            "Oradea, Romania": (47.046, 21.918),
        }
        for label, (lat, lon) in nearby_foreign.items():
            with self.subTest(label=label):
                self.assertEqual(
                    "unknown",
                    classify_location(
                        title="Unlocated activity",
                        description=None,
                        country=None,
                        locality=None,
                        region=None,
                        address=None,
                        lat=lat,
                        lon=lon,
                        starts_in_budapest=False,
                    ),
                )

    def test_blocked_actor_sentinels_are_skipped(self) -> None:
        sentinels = [
            {"url": "https://www.tripadvisor.com/x", "title": "Access Denied", "error": "HTTP 403"},
            {"isBlocked": True, "message": "proxy request blocked"},
            {"statusCode": 429, "error": "Too Many Requests"},
            {"statusCode": 403, "url": "https://www.tripadvisor.com/blocked"},
            {"captcha": True, "message": "Verify you are human"},
        ]
        for payload in sentinels:
            with self.subTest(payload=payload):
                self.assertTrue(is_blocked_payload(payload))
                self.assertIsNone(normalize_item(TRIPADVISOR_ACTOR, payload))

    def test_incidental_warning_does_not_hide_a_real_listing(self) -> None:
        payload = {
            "locationId": 99,
            "name": "Real attraction",
            "description": "The historical exhibit explains how access was denied to locals.",
            "rating": 4.5,
            "url": "https://www.tripadvisor.com/Attraction_Review-g1-d99-Reviews.html",
        }
        self.assertFalse(is_blocked_payload(payload))
        self.assertIsNotNone(normalize_item(TRIPADVISOR_ACTOR, payload))

    def test_unsupported_actor_is_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            normalize_item("unknown/actor", {"id": 1, "title": "No guessing"})


if __name__ == "__main__":
    unittest.main()
