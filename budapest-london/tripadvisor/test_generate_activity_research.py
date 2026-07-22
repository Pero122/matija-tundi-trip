import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "generate_activity_research", HERE / "generate_activity_research.py"
)
research = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = research
SPEC.loader.exec_module(research)


def completed(command, *, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def accept_detail_audit(_inventory, _contexts):
    return True


def visible_item(key="AttractionProductReview:100", *, item_type="experience"):
    return {
        "key": key,
        "name": "Example Adventure",
        "category": "Tours",
        "subtype": "Walking Tours",
        "rating": 4.8,
        "reviewCount": 50,
        "url": "https://www.tripadvisor.com/AttractionProductReview-g1-d100-Example.html",
        "type": item_type,
        "group": "tours",
    }


def geocaching_item():
    return {
        "key": "idea:geocaching",
        "name": "Try geocaching together",
        "category": "Flexible trip ideas",
        "subtype": "Geocaching",
        "rating": None,
        "reviewCount": 0,
        "url": "https://geocaching.hu/maps.geo",
        "type": "idea",
        "group": "quests",
    }


def context(key="AttractionProductReview:100", *, packages=None, ready=True):
    row = {
        "key": key,
        "name": "Example Adventure",
        "url": "https://www.tripadvisor.com/AttractionProductReview-g1-d100-Example.html",
        "canonical_url": "https://www.tripadvisor.com/AttractionProductReview-g1-d100-Example.html",
        "description": "A guided walk through central Budapest with several landmark stops.",
        "reviews": [
            {
                "title": "Useful tour",
                "text": "The guide explained the landmarks clearly and kept the group moving at a comfortable pace.",
                "rating": 5,
            }
        ],
    }
    if ready:
        row.update(
            {
                "checked_at": "2026-07-16",
                "pricing_evidence": {
                    "base_price": "$34.97",
                    "booking_date": "Friday, July 17, 2026",
                    "travelers": "2",
                    "packages": packages or [],
                },
            }
        )
    return row


def model_item(item):
    reviews_used = min(1, len(item.get("reviews", [])))
    return {
        "key": item["key"],
        "researchStatus": "grounded" if item.get("description") else "limited",
        "what": "A guided sightseeing walk through central Budapest.",
        "do": "Follow a local guide between landmarks and listen to the explanations.",
        "why": "Choose it for an efficient introduction; skip it if you prefer exploring independently.",
        "reviewSummary": "The supplied review praises the clear guide and comfortable pace." if reviews_used else None,
        "reviewsUsed": reviews_used,
        "packageExplanations": [
            {
                "packageId": package["packageId"],
                "explanation": "This is the named guided option; the source gives no clearer distinction.",
            }
            for package in item.get("packageEvidence", [])
        ],
    }


class FakeRunner:
    def __init__(self, visible, *, event_array=False):
        self.visible = visible
        self.event_array = event_array
        self.calls = []
        self.model_calls = 0

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "node":
            return completed(command, stdout=json.dumps(self.visible))
        if command[1:3] == ["auth", "status"]:
            return completed(
                command,
                stdout=json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "subscriptionType": "max",
                    }
                ),
            )
        if command[:3] == ["codex", "login", "status"]:
            return completed(command, stdout="Logged in using ChatGPT\n")
        if command[:2] == ["codex", "exec"]:
            self.model_calls += 1
            match = re.search(
                r"<untrusted_evidence_json>\s*(.*?)\s*</untrusted_evidence_json>",
                kwargs["input"],
                re.S,
            )
            if match is None:
                raise AssertionError("Codex prompt omitted the evidence envelope")
            payload = json.loads(match.group(1))
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(
                json.dumps({"items": [model_item(item) for item in payload["items"]]}),
                encoding="utf-8",
            )
            return completed(command)
        if "-p" in command:
            self.model_calls += 1
            payload = json.loads(kwargs["input"])
            structured = {"items": [model_item(item) for item in payload["items"]]}
            envelope = {"type": "result", "structured_output": structured}
            if self.event_array:
                envelope = [{"type": "system"}, envelope]
            return completed(command, stdout=json.dumps(envelope))
        raise AssertionError(f"unexpected command: {command}")


class ClaudeBlockedCodexRunner:
    def __init__(self, visible=None):
        self.visible = visible
        self.calls = []
        self.claude_model_calls = 0
        self.codex_model_calls = 0
        self.active_codex_calls = 0
        self.max_concurrent_codex_calls = 0
        self.lock = threading.Lock()

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "node" and self.visible is not None:
            return completed(command, stdout=json.dumps(self.visible))
        if command[0] == "claude" and "-p" in command:
            self.claude_model_calls += 1
            return completed(
                command,
                returncode=1,
                stdout=(
                    "Your organization has disabled Claude subscription access for Claude Code · "
                    "Use an Anthropic API key instead, or ask your admin to enable access"
                ),
            )
        if command[:3] == ["codex", "login", "status"]:
            return completed(command, stdout="Logged in using ChatGPT\n")
        if command[:2] == ["codex", "exec"]:
            self.codex_model_calls += 1
            with self.lock:
                self.active_codex_calls += 1
                self.max_concurrent_codex_calls = max(
                    self.max_concurrent_codex_calls, self.active_codex_calls
                )
            try:
                time.sleep(0.03)
                match = re.search(
                    r"<untrusted_evidence_json>\s*(.*?)\s*</untrusted_evidence_json>",
                    kwargs["input"],
                    re.S,
                )
                if match is None:
                    raise AssertionError("Codex prompt omitted the evidence envelope")
                payload = json.loads(match.group(1))
                structured = {"items": [model_item(item) for item in payload["items"]]}
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(json.dumps(structured), encoding="utf-8")
                return completed(command)
            finally:
                with self.lock:
                    self.active_codex_calls -= 1
        raise AssertionError(f"unexpected command: {command}")


class ActivityResearchTests(unittest.TestCase):
    def test_curated_loader_accepts_both_tripadvisor_route_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "curated.json"
            entry = {"brief": {}, "pricing": {}}
            expected = {
                "Attraction_Review:123": entry,
                "AttractionProductReview:456": entry,
            }
            path.write_text(json.dumps(expected), encoding="utf-8")
            self.assertEqual(research.load_curated_research(path), expected)

            path.write_text(
                json.dumps({"idea:unbound": entry}), encoding="utf-8"
            )
            with self.assertRaisesRegex(research.ResearchError, "invalid activity key"):
                research.load_curated_research(path)

    def test_geocaching_hash_binds_its_code_owned_brief_and_pricing(self):
        self.assertEqual(
            research.GEOCACHING_EVIDENCE_HASH,
            research._geocaching_evidence_hash(
                research._GEOCACHING_BRIEF_CONTENT,
                research._GEOCACHING_PRICING_CONTENT,
            ),
        )
        changed_brief = dict(research._GEOCACHING_BRIEF_CONTENT)
        changed_brief["why"] += " Changed after publication."
        changed_pricing = json.loads(
            json.dumps(research._GEOCACHING_PRICING_CONTENT)
        )
        changed_pricing["packages"][0]["availability"] = "unavailable"
        self.assertNotEqual(
            research.GEOCACHING_EVIDENCE_HASH,
            research._geocaching_evidence_hash(
                changed_brief, research._GEOCACHING_PRICING_CONTENT
            ),
        )
        self.assertNotEqual(
            research.GEOCACHING_EVIDENCE_HASH,
            research._geocaching_evidence_hash(
                research._GEOCACHING_BRIEF_CONTENT, changed_pricing
            ),
        )

    def test_bundle_loader_reads_optional_shared_revision_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activity-briefs.js"
            value = {"idea:geocaching": research.GEOCACHING_BRIEF}

            path.write_text(
                research.bundle_text(research.BRIEFS_PREFIX, value),
                encoding="utf-8",
            )
            legacy_value, legacy_revision = research.load_bundle_with_revision(
                path, research.BRIEFS_PREFIX
            )
            self.assertEqual(legacy_value, value)
            self.assertIsNone(legacy_revision)

            revision = "a" * 64
            source = research.bundle_text(
                research.BRIEFS_PREFIX, value, revision
            )
            path.write_text(source, encoding="utf-8")
            loaded, loaded_revision = research.load_bundle_with_revision(
                path, research.BRIEFS_PREFIX
            )

            self.assertEqual(loaded, value)
            self.assertEqual(loaded_revision, revision)
            self.assertTrue(
                source.endswith(
                    f'window.ACTIVITY_BRIEFS_REVISION="{revision}";\n'
                )
            )

    def test_generator_output_lock_contends_across_work_dirs_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            briefs = root / "site" / "activity-briefs.js"
            pricing = root / "site" / "activity-pricing.js"
            alias_briefs = root / "site" / "nested" / ".." / "activity-briefs.js"
            outer_args = [
                "--briefs", str(briefs),
                "--pricing", str(pricing),
                "--work-dir", str(root / "work-a"),
            ]
            inner_args = [
                "--briefs", str(alias_briefs),
                "--pricing", str(pricing),
                "--work-dir", str(root / "work-b"),
            ]
            inner_results = []

            def hold_outer_lock(_args):
                inner_results.append(research.main(inner_args))
                return 0

            with mock.patch.object(
                research, "_run_main_loop", side_effect=hold_outer_lock
            ), mock.patch("builtins.print"):
                self.assertEqual(research.main(outer_args), 0)

            self.assertEqual(inner_results, [2])
            self.assertEqual(
                research.generator_lock_path(briefs, pricing),
                research.generator_lock_path(alias_briefs, pricing),
            )
            with mock.patch.object(
                research, "_run_main_loop", return_value=0
            ):
                self.assertEqual(research.main(inner_args), 0)

    def test_generator_output_lock_contends_for_partially_overlapping_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "site" / "activity-briefs.js"
            pricing_a = root / "site" / "activity-pricing-a.js"
            pricing_b = root / "other" / "activity-pricing-b.js"
            outer_args = [
                "--briefs", str(shared),
                "--pricing", str(pricing_a),
                "--work-dir", str(root / "work-a"),
            ]
            inner_args = [
                "--briefs", str(shared),
                "--pricing", str(pricing_b),
                "--work-dir", str(root / "work-b"),
            ]
            inner_results = []

            def hold_outer_lock(_args):
                inner_results.append(research.main(inner_args))
                return 0

            with mock.patch.object(
                research, "_run_main_loop", side_effect=hold_outer_lock
            ), mock.patch("builtins.print"):
                self.assertEqual(research.main(outer_args), 0)

            self.assertEqual(inner_results, [2])
            outer_locks = set(
                research.generator_output_lock_paths(shared, pricing_a)
            )
            inner_locks = set(
                research.generator_output_lock_paths(shared, pricing_b)
            )
            self.assertEqual(len(outer_locks & inner_locks), 1)

    def test_one_shot_unpublished_cycle_exits_nonzero_without_model_failures(self):
        args = research.parse_args([])
        incomplete = research.CycleResult(
            visible=2,
            contexts_ready=1,
            briefs_ready=1,
            generated=0,
            failures=0,
            published=False,
        )
        with mock.patch.object(
            research, "run_cycle", return_value=incomplete
        ), mock.patch("builtins.print"):
            self.assertEqual(research._run_main_loop(args), 2)

    def test_watch_mode_waits_through_incomplete_cycle_then_exits_on_publish(self):
        args = research.parse_args(["--watch", "--watch-interval", "1"])
        incomplete = research.CycleResult(
            visible=2,
            contexts_ready=1,
            briefs_ready=1,
            generated=0,
            failures=0,
            published=False,
        )
        complete = research.CycleResult(
            visible=2,
            contexts_ready=2,
            briefs_ready=2,
            generated=1,
            failures=0,
            published=True,
        )
        with mock.patch.object(
            research, "run_cycle", side_effect=[incomplete, complete]
        ) as run_cycle, mock.patch.object(
            research.time, "sleep"
        ) as sleep, mock.patch("builtins.print"):
            self.assertEqual(research._run_main_loop(args), 0)

        self.assertEqual(run_cycle.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_claude_uses_max_oauth_environment_and_safe_structured_invocation(self):
        runner = FakeRunner([visible_item()])
        item = research.prepare_item(visible_item(), context())
        base_env = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "must-not-leak",
            "ANTHROPIC_AUTH_TOKEN": "must-not-leak",
            "ANTHROPIC_BASE_URL": "https://paid.invalid",
        }

        research.verify_claude_max("claude", runner=runner, env=base_env)
        result = research.call_claude_once([item], runner=runner, env=base_env)

        self.assertEqual(result[0]["key"], item["key"])
        auth_command, auth_kwargs = runner.calls[0]
        model_command, model_kwargs = runner.calls[1]
        self.assertEqual(auth_command, ["claude", "auth", "status"])
        for name in research.METERED_ENV_VARS:
            self.assertNotIn(name, auth_kwargs["env"])
            self.assertNotIn(name, model_kwargs["env"])
        self.assertIn("--json-schema", model_command)
        self.assertIn("--tools", model_command)
        self.assertNotIn("--bare", model_command)
        self.assertEqual(model_command[model_command.index("--tools") + 1], "")

    def test_parses_new_claude_event_array_envelope(self):
        runner = FakeRunner([visible_item()], event_array=True)
        item = research.prepare_item(visible_item(), context())
        output = research.call_claude_once([item], runner=runner)
        self.assertEqual(output[0]["researchStatus"], "grounded")

    def test_permanent_claude_403_switches_once_to_chatgpt_without_sleep_or_api_key(self):
        runner = ClaudeBlockedCodexRunner()
        item = research.prepare_item(visible_item(), context())
        provider = {"name": "claude"}
        sleeps = []
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "OPENAI_API_KEY": "must-not-leak",
            "ANTHROPIC_API_KEY": "must-not-leak",
        }

        first, failures = research.generate_resilient(
            [item],
            provider_state=provider,
            runner=runner,
            sleeper=sleeps.append,
            delays=(30, 60),
            env=environment,
        )
        second, second_failures = research.generate_resilient(
            [item],
            provider_state=provider,
            runner=runner,
            sleeper=sleeps.append,
            delays=(30, 60),
            env=environment,
        )

        self.assertEqual(first[0]["key"], item["key"])
        self.assertEqual(second[0]["key"], item["key"])
        self.assertEqual(failures, {})
        self.assertEqual(second_failures, {})
        self.assertEqual(provider, {"name": "codex"})
        self.assertEqual(sleeps, [])
        self.assertEqual(runner.claude_model_calls, 1)
        self.assertEqual(runner.codex_model_calls, 2)
        for command, kwargs in runner.calls:
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
            if command[:2] == ["codex", "exec"]:
                self.assertIn("--ephemeral", command)
                self.assertIn("--output-schema", command)

    def test_rejects_wrong_order_package_ids_and_model_price_claims(self):
        first = research.prepare_item(
            visible_item("AttractionProductReview:100"),
            context(
                "AttractionProductReview:100",
                packages=[{"name": "Standard", "description": "Guided entry"}],
            ),
        )
        second = dict(first, key="AttractionProductReview:101")
        wrong_order = {"items": [model_item(second), model_item(first)]}
        with self.assertRaisesRegex(research.ClaudeError, "keys/order"):
            research.validate_model_output(wrong_order, [first, second])

        bad_package = model_item(first)
        bad_package["packageExplanations"][0]["packageId"] = "pkg-invented"
        with self.assertRaisesRegex(research.ClaudeError, "package IDs/order"):
            research.validate_model_output({"items": [bad_package]}, [first])

        price_claim = model_item(first)
        price_claim["why"] = "Choose it because admission is USD 34.97."
        with self.assertRaisesRegex(research.ClaudeError, "price claim"):
            research.validate_model_output({"items": [price_claim]}, [first])

    def test_rejects_long_verbatim_review_copy(self):
        item = research.prepare_item(visible_item(), context())
        copied_words = " ".join(
            "This review contains a deliberately long exact passage with enough separate words to trigger the raw review leakage detector before publishing any generated browser bundle".split()
        )
        item["reviews"][0]["text"] = copied_words
        output = model_item(item)
        output["reviewSummary"] = copied_words
        with self.assertRaisesRegex(research.ClaudeError, "copied a long review"):
            research.validate_model_output({"items": [output]}, [item])

    def test_drops_model_review_summary_when_no_reviews_were_used(self):
        item = research.prepare_item(visible_item(), context())
        item["reviews"] = []
        output = model_item(item)
        output["reviewSummary"] = "A generic summary that has no review evidence."

        cleaned = research.validate_model_output({"items": [output]}, [item])

        self.assertEqual(cleaned[0]["reviewsUsed"], 0)
        self.assertIsNone(cleaned[0]["reviewSummary"])

    def test_rejects_truncated_or_corrupted_prose(self):
        item = research.prepare_item(visible_item(), context())
        truncated = model_item(item)
        truncated["why"] = "A useful option, but this sentence is abruptly cut"
        with self.assertRaisesRegex(research.ClaudeError, "complete sentence"):
            research.validate_model_output({"items": [truncated]}, [item])

        corrupted = model_item(item)
        corrupted["why"] = "A useful option with a corrupted ending乗."
        with self.assertRaisesRegex(research.ClaudeError, "unexpected writing system"):
            research.validate_model_output({"items": [corrupted]}, [item])

    def test_rejects_non_english_latin_prose_but_allows_hungarian_names(self):
        item = research.prepare_item(visible_item(), context())
        foreign_sentences = {
            "Hungarian": "Ez egy nagyszerű budapesti élmény, és a vendégek nagyon élvezik.",
            "Italian": "La mostra è molto interessante e ben organizzata.",
            "German": "Die Ausstellung ist sehr interessant und gut organisiert.",
        }
        for language, sentence in foreign_sentences.items():
            with self.subTest(language=language):
                output = model_item(item)
                output["why"] = sentence
                with self.assertRaisesRegex(
                    research.ClaudeError,
                    rf"non-English Latin-script prose \({language}\)",
                ):
                    research.validate_model_output({"items": [output]}, [item])

        output = model_item(item)
        output["what"] = (
            "Fő a kávé is a compact coffee exhibition in central Budapest."
        )
        output["do"] = (
            "Walk from Széchenyi Lánchíd to Római Part, then visit the exhibition."
        )
        output["why"] = (
            "Choose it if you enjoy Hungarian coffee culture. The title Fő a kávé "
            "remains untranslated as a proper name."
        )
        output["reviewSummary"] = (
            "La Notte delle Stelle e del Cinema is an Italian-titled event. "
            "The supplied review praises its organization."
        )
        cleaned = research.validate_model_output({"items": [output]}, [item])
        self.assertEqual(cleaned[0]["what"], output["what"])

    def test_generated_brief_labels_all_language_review_provenance(self):
        item_context = context()
        item = research.prepare_item(visible_item(), item_context)
        output = model_item(item)

        with_reviews = research.generated_brief(output, item_context)
        self.assertEqual(
            with_reviews["sourceLabel"],
            "Tripadvisor activity page + all-language sampled reviews",
        )

        output["reviewsUsed"] = 0
        output["reviewSummary"] = None
        without_reviews = research.generated_brief(output, item_context)
        self.assertEqual(
            without_reviews["sourceLabel"],
            "Tripadvisor activity page",
        )
        self.assertNotIn("reviewSource", without_reviews)

    def test_review_evidence_clips_on_sentences_and_recovers_title_only_bodies(self):
        long_body = ("A complete review sentence with useful detail. " * 70) + "cut tail"
        ctx = context()
        ctx["reviews"] = [
            {"title": long_body, "text": "", "rating": 5},
            {"title": "Normal title", "text": long_body, "rating": 4},
        ]

        prepared = research.prepare_item(visible_item(), ctx)

        self.assertEqual(prepared["reviews"][0]["title"], "")
        for review in prepared["reviews"]:
            self.assertLessEqual(len(review["text"]), research.MAX_REVIEW_CHARS)
            self.assertRegex(review["text"], r"[.!?…]$")

    def test_description_money_is_kept_out_of_model_evidence(self):
        ctx = context()
        ctx["description"] = "Fixprice from 25 EUR; lockers cost 1€/hour."

        prepared = research.prepare_item(visible_item(), ctx)

        self.assertNotIn("25 EUR", prepared["description"])
        self.assertNotIn("1€", prepared["description"])
        self.assertEqual(
            prepared["description"].count("[price listed separately]"), 2
        )

    def test_review_and_package_money_is_kept_out_of_model_evidence(self):
        ctx = context(
            packages=[
                {
                    "name": "$150 premium option",
                    "description": "Includes a 20 EUR add-on.",
                    "available_times": "Book before paying the €30 surcharge.",
                }
            ]
        )
        ctx["reviews"] = [
            {
                "title": "Worth $40",
                "text": "We paid 50 EUR and later saw a €60 quote.",
                "rating": 4,
            }
        ]

        prepared = research.prepare_item(visible_item(), ctx)
        serialized = json.dumps(prepared, ensure_ascii=False)

        for claim in ("$150", "20 EUR", "€30", "$40", "50 EUR", "€60"):
            self.assertNotIn(claim, serialized)
        self.assertGreaterEqual(serialized.count("[price listed separately]"), 6)

    def test_publication_hash_binds_description_derived_prices(self):
        first_context = context()
        first_context["description"] = (
            "Fixprice from 25 EUR for 1 to 4 persons between the airport and city."
        )
        second_context = json.loads(json.dumps(first_context))
        second_context["description"] = first_context["description"].replace(
            "25 EUR", "35 EUR"
        )
        inventory = visible_item("Attraction_Review:11774901", item_type="venue")
        first_item = research.prepare_item(inventory, first_context)
        second_item = research.prepare_item(inventory, second_context)

        self.assertEqual(first_item, second_item)
        self.assertNotEqual(
            research.publication_evidence_hash(first_item, first_context),
            research.publication_evidence_hash(second_item, second_context),
        )

    def test_no_bundle_is_replaced_until_every_visible_context_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            briefs = root / "activity-briefs.js"
            pricing = root / "activity-pricing.js"
            old_source = research.bundle_text(
                research.BRIEFS_PREFIX, {"idea:geocaching": research.GEOCACHING_BRIEF}
            )
            briefs.write_text(old_source, encoding="utf-8")
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([context(ready=False)]), encoding="utf-8")
            runner = FakeRunner([visible_item(), geocaching_item()])

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=briefs,
                pricing_path=pricing,
                work_dir=root / "research-work",
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            self.assertFalse(result.published)
            self.assertEqual(briefs.read_text(encoding="utf-8"), old_source)
            self.assertFalse(pricing.exists())
            self.assertEqual(runner.model_calls, 0)

    def test_full_cycle_checkpoints_publishes_exact_coverage_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            briefs = root / "activity-briefs.js"
            pricing = root / "activity-pricing.js"
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([context()]), encoding="utf-8")
            runner = FakeRunner([visible_item(), geocaching_item()])
            kwargs = dict(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=briefs,
                pricing_path=pricing,
                work_dir=root / "research-work",
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            first = research.run_cycle(**kwargs)
            self.assertTrue(first.published)
            self.assertEqual(first.generated, 1)
            self.assertEqual(runner.model_calls, 1)
            brief_bundle = research.load_bundle(briefs, research.BRIEFS_PREFIX)
            price_bundle = research.load_bundle(pricing, research.PRICING_PREFIX)
            _, brief_revision = research.load_bundle_with_revision(
                briefs, research.BRIEFS_PREFIX
            )
            _, price_revision = research.load_bundle_with_revision(
                pricing, research.PRICING_PREFIX
            )
            expected = {"AttractionProductReview:100", "idea:geocaching"}
            self.assertEqual(set(brief_bundle), expected)
            self.assertEqual(set(price_bundle), expected)
            current_hash = research.publication_evidence_hash(
                research.prepare_item(visible_item(), context()),
                context(),
            )
            self.assertEqual(
                brief_bundle["AttractionProductReview:100"]["evidenceHash"],
                current_hash,
            )
            self.assertEqual(
                price_bundle["AttractionProductReview:100"]["evidenceHash"],
                current_hash,
            )
            self.assertEqual(
                price_bundle["idea:geocaching"]["evidenceHash"],
                research.GEOCACHING_EVIDENCE_HASH,
            )
            self.assertEqual(
                brief_bundle["AttractionProductReview:100"]["sourceLabel"],
                "Tripadvisor activity page + all-language sampled reviews",
            )
            self.assertEqual(
                brief_bundle["idea:geocaching"]["source"],
                "https://geocaching.hu/?lang=en",
            )
            self.assertTrue(brief_bundle["idea:geocaching"]["curated"])
            self.assertEqual(
                brief_bundle["idea:geocaching"]["provenance"], "curated"
            )
            self.assertRegex(
                brief_bundle["idea:geocaching"]["evidenceHash"], r"^[0-9a-f]{64}$"
            )
            self.assertIsNotNone(brief_revision)
            self.assertEqual(brief_revision, price_revision)
            self.assertTrue(
                briefs.read_text(encoding="utf-8").endswith(
                    f'window.ACTIVITY_BRIEFS_REVISION="{brief_revision}";\n'
                )
            )
            self.assertTrue(
                pricing.read_text(encoding="utf-8").endswith(
                    f'window.ACTIVITY_PRICING_REVISION="{price_revision}";\n'
                )
            )
            self.assertEqual(price_bundle["AttractionProductReview:100"]["status"], "priced")
            self.assertEqual(
                price_bundle["AttractionProductReview:100"]["startingPrice"],
                {"kind": "from", "amount": "34.97", "currency": "USD"},
            )
            self.assertEqual(price_bundle["AttractionProductReview:100"]["packages"], [])
            self.assertTrue((root / "research-work" / "manifest.json").exists())
            self.assertFalse(list(root.rglob("*.part")))

            second = research.run_cycle(**kwargs)
            self.assertTrue(second.published)
            self.assertEqual(second.generated, 0)
            self.assertEqual(runner.model_calls, 1)

            self.assertEqual(
                research.verify_published_bundles(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=briefs,
                    pricing_path=pricing,
                    runner=runner,
                ),
                2,
            )
            price_changed = context()
            price_changed["pricing_evidence"]["base_price"] = "$999.99"
            context_path.write_text(json.dumps([price_changed]), encoding="utf-8")
            with self.assertRaisesRegex(research.ResearchError, "stale"):
                research.verify_published_bundles(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=briefs,
                    pricing_path=pricing,
                    runner=runner,
                )
            changed = context()
            changed["description"] += " Evidence changed after the bundles were built."
            context_path.write_text(json.dumps([changed]), encoding="utf-8")
            with self.assertRaisesRegex(research.ResearchError, "stale"):
                research.verify_published_bundles(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=briefs,
                    pricing_path=pricing,
                    runner=runner,
                )

    def test_checked_in_curated_research_publishes_without_model_and_binds_current_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "Attraction_Review:100"
            url = "https://www.tripadvisor.com/Attraction_Review-g1-d100-Reviews-Example.html"
            item = visible_item(key, item_type="venue")
            item["url"] = url
            item_context = context(key)
            item_context.update(url=url, canonical_url=url)
            curated_path = root / "curated-activity-research.json"
            curated_path.write_text(
                json.dumps(
                    {
                        key: {
                            "brief": {
                                "researchStatus": "grounded",
                                "what": "A small official exhibition about a clearly identified subject.",
                                "do": "Walk through the displays and allow about an hour for the visit.",
                                "why": "Choose it for the focused theme; skip it if the subject is not interesting to you.",
                                "source": "https://example.com/official",
                                "sourceLabel": "Official visitor page",
                                "checkedAt": "2026-07-16",
                            },
                            "pricing": {
                                "status": "free",
                                "checkedAt": "2026-07-16",
                                "source": "https://example.com/official/tickets",
                                "sourceLabel": "Official ticket page",
                                "packages": [
                                    {
                                        "name": "General admission",
                                        "description": "The official visitor page states that public admission is free.",
                                        "availability": "available",
                                        "price": {"kind": "free"},
                                    }
                                ],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([item_context]), encoding="utf-8")
            runner = FakeRunner([item, geocaching_item()])

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=root / "activity-briefs.js",
                pricing_path=root / "activity-pricing.js",
                curated_research_path=curated_path,
                work_dir=root / "research-work",
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            self.assertTrue(result.published)
            self.assertEqual(result.generated, 0)
            self.assertEqual(runner.model_calls, 0)
            curated_entry = json.loads(curated_path.read_text(encoding="utf-8"))[key]
            expected_hash = research.publication_evidence_hash(
                research.prepare_item(item, item_context),
                item_context,
                curated_entry,
            )
            brief = research.load_bundle(
                root / "activity-briefs.js", research.BRIEFS_PREFIX
            )[key]
            pricing = research.load_bundle(
                root / "activity-pricing.js", research.PRICING_PREFIX
            )[key]
            self.assertTrue(brief["curated"])
            self.assertEqual(brief["provenance"], "curated")
            self.assertEqual(brief["evidenceHash"], expected_hash)
            self.assertEqual(pricing["evidenceHash"], expected_hash)
            self.assertEqual(pricing["status"], "free")
            self.assertEqual(
                research.verify_published_bundles(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=root / "activity-briefs.js",
                    pricing_path=root / "activity-pricing.js",
                    curated_research_path=curated_path,
                    runner=runner,
                ),
                2,
            )
            curated = json.loads(curated_path.read_text(encoding="utf-8"))
            curated[key]["brief"]["why"] += " Updated after publication."
            curated_path.write_text(json.dumps(curated), encoding="utf-8")
            with self.assertRaisesRegex(research.ResearchError, "stale"):
                research.verify_published_bundles(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=root / "activity-briefs.js",
                    pricing_path=root / "activity-pricing.js",
                    curated_research_path=curated_path,
                    runner=runner,
                )

    def test_codex_batches_can_checkpoint_in_parallel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys = [f"AttractionProductReview:{number}" for number in range(100, 103)]
            visible = [visible_item(key) for key in keys] + [geocaching_item()]
            context_path = root / "contexts.json"
            context_path.write_text(
                json.dumps([context(key) for key in keys]), encoding="utf-8"
            )
            work_dir = root / "research-work"
            work_dir.mkdir()
            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": research.MANIFEST_VERSION,
                        "model": research.MANIFEST_MODEL,
                        "promptVersion": research.PROMPT_VERSION,
                        "provider": "codex",
                        "inventoryHash": "",
                        "outputs": {},
                        "errors": {},
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner(visible)

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=root / "activity-briefs.js",
                pricing_path=root / "activity-pricing.js",
                work_dir=work_dir,
                batch_size=1,
                workers=3,
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            self.assertTrue(result.published)
            self.assertEqual(result.generated, 3)
            self.assertEqual(runner.model_calls, 3)
            manifest = json.loads((work_dir / "manifest.json").read_text())
            self.assertEqual(set(manifest["outputs"]), set(keys))

    def test_claude_fallback_persists_and_parallelizes_remaining_codex_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys = [f"AttractionProductReview:{number}" for number in range(100, 103)]
            visible = [visible_item(key) for key in keys] + [geocaching_item()]
            context_path = root / "contexts.json"
            context_path.write_text(
                json.dumps([context(key) for key in keys]), encoding="utf-8"
            )
            runner = ClaudeBlockedCodexRunner(visible)
            work_dir = root / "research-work"

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=root / "activity-briefs.js",
                pricing_path=root / "activity-pricing.js",
                work_dir=work_dir,
                batch_size=1,
                workers=3,
                runner=runner,
                sleeper=lambda _: None,
                check_auth=False,
                detail_auditor=accept_detail_audit,
            )

            self.assertTrue(result.published)
            self.assertEqual(runner.claude_model_calls, 1)
            self.assertEqual(runner.codex_model_calls, 3)
            self.assertGreaterEqual(runner.max_concurrent_codex_calls, 2)
            manifest = json.loads((work_dir / "manifest.json").read_text())
            self.assertEqual(manifest["provider"], "codex")

    def test_preserves_curated_brief_but_generates_package_explanations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "AttractionProductReview:100"
            package = {
                "name": "Extended option",
                "description": "Adds a longer guided route.",
                "unit_price": "$45.00",
                "unit": "adults",
            }
            item_context = context(packages=[package])
            current_hash = research.publication_evidence_hash(
                research.prepare_item(visible_item(), item_context),
                item_context,
            )
            curated = {
                "what": "A carefully curated existing explanation.",
                "do": "Keep the existing action summary.",
                "why": "Keep the existing balanced verdict.",
                "curated": True,
                "provenance": "curated",
                "researchStatus": "grounded",
                "evidenceHash": current_hash,
                "source": "https://example.com/official",
                "sourceLabel": "Official source",
                "checkedAt": "2026-07-16",
            }
            briefs = root / "activity-briefs.js"
            briefs.write_text(
                research.bundle_text(
                    research.BRIEFS_PREFIX,
                    {key: curated, "idea:geocaching": research.GEOCACHING_BRIEF},
                ),
                encoding="utf-8",
            )
            context_path = root / "contexts.json"
            context_path.write_text(
                json.dumps([item_context]), encoding="utf-8"
            )
            runner = FakeRunner([visible_item(), geocaching_item()])

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=briefs,
                pricing_path=root / "activity-pricing.js",
                work_dir=root / "research-work",
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            self.assertTrue(result.published)
            self.assertEqual(runner.model_calls, 1)
            published_briefs = research.load_bundle(briefs, research.BRIEFS_PREFIX)
            self.assertEqual(published_briefs[key], curated)
            published_prices = research.load_bundle(
                root / "activity-pricing.js", research.PRICING_PREFIX
            )
            self.assertIn(
                "named guided option",
                published_prices[key]["packages"][0]["description"],
            )

    def test_stale_curated_brief_is_regenerated_instead_of_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "AttractionProductReview:100"
            stale_curated = {
                "what": "Stale curated prose.",
                "do": "Use stale instructions.",
                "why": "This must not survive refreshed evidence.",
                "curated": True,
                "provenance": "curated",
                "researchStatus": "grounded",
                "evidenceHash": "0" * 64,
                "source": "https://example.com/official",
                "sourceLabel": "Official source",
                "checkedAt": "2026-07-16",
            }
            briefs = root / "activity-briefs.js"
            briefs.write_text(
                research.bundle_text(
                    research.BRIEFS_PREFIX,
                    {key: stale_curated, "idea:geocaching": {}},
                ),
                encoding="utf-8",
            )
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([context()]), encoding="utf-8")
            runner = FakeRunner([visible_item(), geocaching_item()])

            result = research.run_cycle(
                validator=root / "validator.mjs",
                context_path=context_path,
                briefs_path=briefs,
                pricing_path=root / "activity-pricing.js",
                work_dir=root / "research-work",
                runner=runner,
                sleeper=lambda _: None,
                detail_auditor=accept_detail_audit,
            )

            self.assertTrue(result.published)
            self.assertEqual(runner.model_calls, 1)
            published = research.load_bundle(briefs, research.BRIEFS_PREFIX)
            self.assertNotEqual(published[key]["what"], stale_curated["what"])
            self.assertNotIn("curated", published[key])
            self.assertEqual(
                published["idea:geocaching"], research.GEOCACHING_BRIEF
            )

    def test_package_generation_failure_blocks_publication_and_hashes_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "AttractionProductReview:100"
            item_context = context(
                packages=[
                    {
                        "name": "Extended option",
                        "description": "Adds a longer guided route.",
                        "unit_price": "$45.00",
                        "unit": "adults",
                    }
                ]
            )
            prepared = research.prepare_item(visible_item(), item_context)
            model_hash = research.evidence_hash(prepared)
            current_hash = research.publication_evidence_hash(
                prepared,
                item_context,
            )
            curated = {
                "what": "Current curated explanation.",
                "do": "Keep the current action summary.",
                "why": "Keep the current balanced verdict.",
                "curated": True,
                "provenance": "curated",
                "researchStatus": "grounded",
                "evidenceHash": current_hash,
                "source": "https://example.com/official",
                "sourceLabel": "Official source",
                "checkedAt": "2026-07-16",
            }
            briefs = root / "activity-briefs.js"
            old_source = research.bundle_text(
                research.BRIEFS_PREFIX,
                {key: curated, "idea:geocaching": research.GEOCACHING_BRIEF},
            )
            briefs.write_text(old_source, encoding="utf-8")
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([item_context]), encoding="utf-8")
            work_dir = root / "research-work"
            runner = FakeRunner([visible_item(), geocaching_item()])

            with mock.patch.object(
                research,
                "generate_resilient",
                return_value=([], {key: "model failed"}),
            ):
                result = research.run_cycle(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=briefs,
                    pricing_path=root / "activity-pricing.js",
                    work_dir=work_dir,
                    runner=runner,
                    sleeper=lambda _: None,
                    check_auth=False,
                    detail_auditor=accept_detail_audit,
                )

            self.assertFalse(result.published)
            self.assertEqual(result.failures, 1)
            self.assertEqual(briefs.read_text(encoding="utf-8"), old_source)
            self.assertFalse((root / "activity-pricing.js").exists())
            manifest = json.loads((work_dir / "manifest.json").read_text())
            self.assertEqual(
                manifest["errors"][key]["evidenceHash"], model_hash
            )

    def test_complete_inventory_must_pass_strict_detail_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path = root / "contexts.json"
            context_path.write_text(json.dumps([context()]), encoding="utf-8")
            runner = FakeRunner([visible_item(), geocaching_item()])
            audit_calls = []

            def reject_audit(inventory, contexts):
                audit_calls.append((list(inventory), list(contexts)))
                return False

            with self.assertRaisesRegex(
                research.ResearchError, "strict detail-context audit"
            ):
                research.run_cycle(
                    validator=root / "validator.mjs",
                    context_path=context_path,
                    briefs_path=root / "activity-briefs.js",
                    pricing_path=root / "activity-pricing.js",
                    work_dir=root / "research-work",
                    runner=runner,
                    check_auth=False,
                    detail_auditor=reject_audit,
                )

            self.assertEqual(len(audit_calls), 1)
            self.assertEqual(runner.model_calls, 0)
            self.assertFalse((root / "activity-briefs.js").exists())

    def test_normalized_pricing_keeps_amounts_out_of_model_and_explicit_missing_state(self):
        package = {
            "name": "Private group",
            "description": "One booking for the group.",
            "total_price": "€120.00",
            "party": "4 adults",
        }
        ctx = context(packages=[package])
        prepared = research.prepare_item(visible_item(), ctx)
        self.assertNotIn("120", json.dumps(prepared["packageEvidence"]))
        output = model_item(prepared)
        pricing = research.normalize_pricing(visible_item(), ctx, output)
        self.assertEqual(
            pricing["packages"][0]["price"],
            {"kind": "exact", "amount": "120.00", "currency": "EUR", "unit": "group"},
        )

        venue = visible_item("Attraction_Review:200", item_type="venue")
        venue_context = context("Attraction_Review:200")
        venue_context["pricing_evidence"] = {
            "base_price": "",
            "booking_date": "",
            "travelers": "",
            "packages": [],
        }
        missing = research.normalize_pricing(venue, venue_context, None)
        self.assertEqual(missing["status"], "not-published")
        self.assertEqual(missing["packages"], [])

    def test_standalone_package_total_normalizes_as_group_total(self):
        package = {
            "name": "Budapest City Walk in Jewish Quarter",
            "description": "Pickup included.",
            "total_price": "$344.69",
            "party": "",
            "unit_price": "",
            "unit": "",
            "availability": "available",
        }
        item_context = context(packages=[package])
        item_context["pricing_evidence"]["base_price"] = ""
        prepared = research.prepare_item(visible_item(), item_context)

        normalized = research.normalize_pricing(
            visible_item(), item_context, model_item(prepared)
        )

        self.assertEqual(normalized["status"], "priced")
        self.assertEqual(
            normalized["packages"][0]["price"],
            {
                "kind": "exact",
                "amount": "344.69",
                "currency": "USD",
                "unit": "group",
            },
        )

    def test_explicit_hourly_booking_deposit_overrides_standalone_total_unit(self):
        item = visible_item("AttractionProductReview:28032940")
        item_context = context(
            "AttractionProductReview:28032940",
            packages=[
                {
                    "name": "Budapest Hidden Gems - Roman Aquincum Private Tour",
                    "total_price": "$70.11",
                    "party": "",
                    "unit_price": "",
                    "unit": "",
                    "availability": "available",
                }
            ],
        )
        item_context["pricing_evidence"]["base_price"] = ""
        item_context["description"] = (
            "Please read EVERYTHING before booking. The booking price is the hourly rate "
            "and fnunctions as a deposit. The route depends on opening times and pace."
        )

        normalized = research.normalize_pricing(item, item_context, None)

        self.assertEqual(
            normalized["packages"][0]["price"],
            {
                "kind": "exact",
                "amount": "70.11",
                "currency": "USD",
                "unit": "hour",
                "scope": "deposit",
            },
        )
        self.assertIn(
            "The displayed amount is the hourly rate and functions as the booking deposit; "
            "the final cost depends on total tour hours.",
            normalized["packages"][0]["conditions"],
        )
        self.assertNotIn("startingPrice", normalized)

    def test_hourly_words_without_explicit_booking_deposit_do_not_override_group_total(self):
        item = visible_item("AttractionProductReview:28032941")
        item_context = context(
            "AttractionProductReview:28032941",
            packages=[
                {
                    "name": "Private tour",
                    "total_price": "$70.11",
                    "availability": "available",
                }
            ],
        )
        item_context["pricing_evidence"]["base_price"] = ""
        item_context["description"] = (
            "This private tour has flexible booking and may last several hours."
        )

        normalized = research.normalize_pricing(item, item_context, None)

        self.assertEqual(normalized["packages"][0]["price"]["unit"], "group")
        self.assertNotIn("scope", normalized["packages"][0]["price"])

    def test_package_additional_cost_is_deterministic_and_searchable_condition(self):
        package = {
            "name": "Walking option",
            "description": "Transport tickets cost [price omitted]/person.",
            "source_description": "The additional cost for transport tickets is: 8 EUR/person.",
            "additional_costs": [
                {
                    "amount": "8 EUR",
                    "unit": "person",
                    "source_text": "The additional cost for transport tickets is: 8 EUR/person.",
                }
            ],
            "unit_price": "USD 34.97",
            "unit": "adult",
            "availability": "available",
        }
        item_context = context(packages=[package])
        prepared = research.prepare_item(visible_item(), item_context)

        self.assertNotIn("8 EUR", json.dumps(prepared["packageEvidence"]))
        normalized = research.normalize_pricing(
            visible_item(), item_context, model_item(prepared)
        )
        self.assertIn(
            "Additional cost for transport tickets: EUR 8 per person",
            normalized["packages"][0]["conditions"],
        )

    def test_booking_fee_is_not_presented_as_the_full_activity_price(self):
        package = {
            "name": "Reservation",
            "description": "booking fee",
            "unit_price": "USD 3.51",
            "unit": "adult",
            "availability": "available",
        }
        item_context = context(packages=[package])
        item_context["pricing_evidence"]["base_price"] = "USD 3.51"
        prepared = research.prepare_item(visible_item(), item_context)

        normalized = research.normalize_pricing(
            visible_item(), item_context, model_item(prepared)
        )

        self.assertEqual(normalized["packages"][0]["price"]["scope"], "booking-fee")
        self.assertEqual(normalized["startingPrice"]["scope"], "booking-fee")

    def test_free_and_pay_what_you_like_tours_separate_booking_charge_from_guide_payment(self):
        cases = [
            {
                "key": "AttractionProductReview:32704138",
                "name": "Budapest free walking tour: Parliament and Shoes Memorial",
                "description": (
                    "A small-group walking tour past Parliament and the Shoes Memorial."
                ),
                "amount": "$3.51",
                "base": "$3.51",
                "condition": "labels this a free or pay-what-you-like tour",
            },
            {
                "key": "AttractionProductReview:25176300",
                "name": (
                    "Free Walking tour in the Buda Castle incl. Fisherman's Bastion"
                ),
                "description": (
                    "Join this group walking tour with the freedom to pay what you like, "
                    "depending on what you think of the tour."
                ),
                "amount": "$3.51",
                "base": "$3.51",
                "condition": "pay-what-you-like after the tour",
            },
            {
                "key": "AttractionProductReview:16912973",
                "name": "Budapest Historical Sightseeing - Free Walking Tour",
                "description": (
                    "You can tip the guides as little or much as you wish depending on the "
                    "experience. There is a booking fee payable to Tripadvisor."
                ),
                "amount": "$3.51",
                "base": "$3.51",
                "condition": "identifies the displayed amount as a booking fee",
            },
            {
                "key": "AttractionProductReview:25549520",
                "name": "Free Tour Budapest Essential in Spanish",
                "description": (
                    "When booking, you make a small payment that does not correspond to the "
                    "guide, but to the management costs and the metro ticket for the tour. "
                    "This is a Free Tour: you decide how much to pay the guide at the end."
                ),
                "amount": "$3.49",
                "base": "",
                "condition": "covers management costs and a transport ticket",
            },
        ]

        for case in cases:
            with self.subTest(key=case["key"]):
                item = visible_item(case["key"])
                item["name"] = case["name"]
                item_context = context(
                    case["key"],
                    packages=[
                        {
                            "name": case["name"],
                            "description": "",
                            "unit_price": case["amount"],
                            "unit": "adults",
                            "availability": "available",
                        }
                    ],
                )
                item_context["name"] = case["name"]
                item_context["description"] = case["description"]
                item_context["pricing_evidence"]["base_price"] = case["base"]
                prepared = research.prepare_item(item, item_context)

                normalized = research.normalize_pricing(
                    item, item_context, model_item(prepared)
                )

                self.assertEqual(
                    normalized["packages"][0]["price"]["scope"], "booking-fee"
                )
                if case["base"]:
                    self.assertEqual(
                        normalized["startingPrice"]["scope"], "booking-fee"
                    )
                package_conditions = " ".join(
                    normalized["packages"][0].get("conditions", [])
                )
                self.assertIn(case["condition"], package_conditions)
                self.assertIn("guide", package_conditions.lower())

    def test_low_amount_alone_does_not_imply_booking_fee(self):
        item = visible_item("AttractionProductReview:300")
        item["name"] = "Affordable Budapest Walking Tour"
        item_context = context(
            "AttractionProductReview:300",
            packages=[
                {
                    "name": "Guided walk",
                    "description": "Standard guided option with free cancellation.",
                    "unit_price": "$3.51",
                    "unit": "adults",
                    "availability": "available",
                }
            ],
        )
        item_context["name"] = item["name"]
        item_context["description"] = (
            "A low-cost guided walking tour with an advance reservation."
        )
        item_context["pricing_evidence"]["base_price"] = "$3.51"
        prepared = research.prepare_item(item, item_context)

        normalized = research.normalize_pricing(
            item, item_context, model_item(prepared)
        )

        self.assertNotIn("scope", normalized["packages"][0]["price"])
        self.assertNotIn("scope", normalized["startingPrice"])

    def test_free_tour_title_does_not_relabel_large_ambiguous_charge(self):
        item = visible_item("AttractionProductReview:301")
        item["name"] = "Free Walking Tour and Private Upgrade"
        item_context = context(
            "AttractionProductReview:301",
            packages=[
                {
                    "name": "Unexplained option",
                    "description": "No fee details are published for this option.",
                    "unit_price": "$75.00",
                    "unit": "adults",
                    "availability": "available",
                }
            ],
        )
        item_context["name"] = item["name"]
        item_context["description"] = "Choose a walking route through central Budapest."
        item_context["pricing_evidence"]["base_price"] = "$75.00"
        prepared = research.prepare_item(item, item_context)

        normalized = research.normalize_pricing(
            item, item_context, model_item(prepared)
        )

        self.assertNotIn("scope", normalized["packages"][0]["price"])
        self.assertNotIn("scope", normalized["startingPrice"])

    def test_starting_price_never_exceeds_comparable_available_option(self):
        packages = [
            {
                "name": "Lower available option",
                "unit_price": "USD 139.71",
                "unit": "adults",
                "availability": "available",
            },
            {
                "name": "Advertised option",
                "unit_price": "USD 153.00",
                "unit": "adults",
                "availability": "available",
            },
        ]
        item_context = context(packages=packages)
        item_context["pricing_evidence"]["base_price"] = "USD 153.00"
        prepared = research.prepare_item(visible_item(), item_context)

        normalized = research.normalize_pricing(
            visible_item(), item_context, model_item(prepared)
        )

        self.assertEqual(
            normalized["startingPrice"],
            {"kind": "from", "amount": "139.71", "currency": "USD", "unit": "adult"},
        )

    def test_explicit_venue_description_prices_become_named_options(self):
        venue = visible_item("Attraction_Review:18131050", item_type="venue")
        venue_context = context("Attraction_Review:18131050")
        venue_context["pricing_evidence"] = {"packages": []}
        venue_context["description"] = (
            "Minimum 3hours Medium1€/hour Large 2€/hour "
            "AIRPORT TRANSFER PRICE: 1-4 person 39euro 5-6 person 49euro 7-8 person 59euro "
            "BUS AND TRAIN STATIONS PRICE: 1-4 person 25euro 5-8 person 45euro"
        )

        normalized = research.normalize_pricing(venue, venue_context, None)

        self.assertEqual(normalized["status"], "priced")
        self.assertEqual(len(normalized["packages"]), 7)
        self.assertEqual(
            normalized["packages"][0]["price"],
            {"kind": "exact", "amount": "1", "currency": "EUR", "unit": "hour"},
        )
        self.assertEqual(
            normalized["packages"][-1]["name"],
            "Bus or train station transfer — 5–8 people",
        )

        transfer = visible_item("Attraction_Review:11774901", item_type="venue")
        transfer_context = context("Attraction_Review:11774901")
        transfer_context["pricing_evidence"] = {"packages": []}
        transfer_context["description"] = (
            "Fixprice from 25 EUR for 1 to 4 persons to the Budapest city center"
        )
        transfer_price = research.normalize_pricing(transfer, transfer_context, None)
        self.assertEqual(transfer_price["status"], "priced")
        self.assertEqual(
            transfer_price["packages"][0]["price"],
            {"kind": "from", "amount": "25", "currency": "EUR", "unit": "group"},
        )

    def test_semantic_luggage_storage_price_becomes_hourly_option(self):
        venue = visible_item("Attraction_Review:24859352", item_type="venue")
        venue_context = context("Attraction_Review:24859352")
        venue_context["pricing_evidence"] = {"packages": []}
        venue_context["description"] = (
            "You can store your luggage after checkout. Koffer awaits you with fully "
            "secured storage boxes for 1€/hour (3 hours min.)"
        )

        normalized = research.normalize_pricing(venue, venue_context, None)

        self.assertEqual(normalized["status"], "priced")
        self.assertEqual(
            normalized["packages"][0]["price"],
            {"kind": "exact", "amount": "1", "currency": "EUR", "unit": "hour"},
        )
        self.assertIn("Minimum 3 hours", normalized["packages"][0]["conditions"])
        self.assertNotIn("startingPrice", normalized)

    def test_plural_no_entry_fees_is_free_without_treating_retail_art_as_admission(self):
        venue = visible_item("Attraction_Review:34004756", item_type="venue")
        venue_context = context("Attraction_Review:34004756")
        venue_context["pricing_evidence"] = {"packages": []}
        venue_context["description"] = (
            "A local international artist community and commercial gallery. No entry "
            "fees! Original artworks and sketches from 50€, with frames in many sizes."
        )

        normalized = research.normalize_pricing(venue, venue_context, None)

        self.assertEqual(normalized["status"], "free")
        self.assertEqual(
            [package["price"] for package in normalized["packages"]],
            [{"kind": "free"}],
        )
        self.assertNotIn("startingPrice", normalized)

    def test_visiting_exhibitions_for_free_is_admission_evidence(self):
        venue = visible_item("Attraction_Review:12574523", item_type="venue")
        venue_context = context("Attraction_Review:12574523")
        venue_context["pricing_evidence"] = {"packages": []}
        venue_context["description"] = (
            "Our Gallery has high-level exhibition rooms where guests and collectors can "
            "visit exclusive, thematic exhibitions for free."
        )

        normalized = research.normalize_pricing(venue, venue_context, None)

        self.assertEqual(normalized["status"], "free")
        self.assertEqual(normalized["packages"][0]["price"], {"kind": "free"})

    def test_for_free_amenities_do_not_imply_free_admission(self):
        venue = visible_item("Attraction_Review:12574524", item_type="venue")
        examples = [
            "Visit the gallery and use Wi-Fi for free.",
            "Visit the exhibition; parking is available for free.",
            "Visit the exhibition and enjoy welcome drinks for free.",
            "Visit the exhibition with an audio guide for free.",
        ]

        for description in examples:
            with self.subTest(description=description):
                venue_context = context("Attraction_Review:12574524")
                venue_context["pricing_evidence"] = {"packages": []}
                venue_context["description"] = description

                normalized = research.normalize_pricing(venue, venue_context, None)

                self.assertEqual(normalized["status"], "not-published")
                self.assertEqual(normalized["packages"], [])

    def test_large_group_charges_are_conditions_not_headline_prices(self):
        cases = [
            {
                "key": "AttractionProductReview:24124620",
                "description": (
                    "Groups of 8 or more people are required to contact us at least 24 "
                    "hours in advance and they have to pay a minimum 8 €/ person minimum "
                    "fee for the tour, whether booked together or separately."
                ),
                "package_price": "$3.51",
                "base_price": "$3.51",
                "condition": (
                    "Groups of 8 or more: minimum EUR 8 per person; contact the operator "
                    "at least 24 hours in advance."
                ),
            },
            {
                "key": "AttractionProductReview:25549520",
                "description": (
                    "This is a Free Tour. Maximum allowed 6 people per reservation. "
                    "(if you are more, 8 euros per adult is required)"
                ),
                "package_price": "$3.49",
                "base_price": "",
                "condition": "Reservations over 6 people: EUR 8 per adult.",
            },
        ]

        for case in cases:
            with self.subTest(key=case["key"]):
                item = visible_item(case["key"])
                item_context = context(
                    case["key"],
                    packages=[
                        {
                            "name": "Standard booking",
                            "unit_price": case["package_price"],
                            "unit": "adult",
                            "availability": "available",
                        }
                    ],
                )
                item_context["description"] = case["description"]
                item_context["pricing_evidence"]["base_price"] = case["base_price"]

                normalized = research.normalize_pricing(item, item_context, None)

                self.assertEqual(len(normalized["packages"]), 1)
                self.assertIn(
                    case["condition"], normalized["packages"][0]["conditions"]
                )
                if case["base_price"]:
                    self.assertEqual(normalized["startingPrice"]["amount"], "3.51")

    def test_optional_sauna_and_winter_castle_refunds_are_package_conditions(self):
        item = visible_item("AttractionProductReview:27119659")
        item_context = context(
            "AttractionProductReview:27119659",
            packages=[
                {
                    "name": "Hike with optional sauna",
                    "unit_price": "$163.58",
                    "unit": "adult",
                    "availability": "available",
                }
            ],
        )
        item_context["pricing_evidence"]["base_price"] = "$163.58"
        item_context["description"] = (
            "The sauna is optional; if you prefer to miss it or go to a Danube beach, "
            "we retrun 15 euro from the fee. In winter (Dec-Feb) the castle is open from "
            "Fri-Sun, but the view terrace remains available. In this case I give you "
            "back 2500 huf/6 euro."
        )

        normalized = research.normalize_pricing(item, item_context, None)

        self.assertEqual(len(normalized["packages"]), 1)
        self.assertEqual(normalized["startingPrice"]["amount"], "163.58")
        self.assertEqual(normalized["packages"][0]["price"]["amount"], "163.58")
        self.assertIn(
            "If you skip the sauna or choose the Danube beach: EUR 15 refund.",
            normalized["packages"][0]["conditions"],
        )
        self.assertIn(
            "For a winter date when the castle is closed: HUF 2,500 refund "
            "(also stated as EUR 6).",
            normalized["packages"][0]["conditions"],
        )

    def test_price_fallback_uses_stable_route_key_not_unreliable_inventory_type(self):
        product = visible_item("AttractionProductReview:201", item_type="venue")
        product_context = context("AttractionProductReview:201")
        product_context["pricing_evidence"] = {
            "base_price": "",
            "booking_date": "",
            "travelers": "",
            "packages": [],
        }
        self.assertEqual(
            research.normalize_pricing(product, product_context, None)["status"],
            "date-required",
        )

        venue = visible_item("Attraction_Review:202", item_type="experience")
        venue_context = context("Attraction_Review:202")
        venue_context["pricing_evidence"] = {
            "base_price": "",
            "booking_date": "",
            "travelers": "",
            "packages": [],
        }
        self.assertEqual(
            research.normalize_pricing(venue, venue_context, None)["status"],
            "not-published",
        )
        self.assertEqual(
            research.normalize_pricing(venue, venue_context, None)["sourceLabel"],
            "Tripadvisor activity page",
        )

    def test_free_venue_requires_explicit_activity_description_not_review_claims(self):
        venue = visible_item("Attraction_Review:203", item_type="venue")
        venue_context = context("Attraction_Review:203")
        venue_context["pricing_evidence"] = {"packages": []}

        venue_context["description"] = "A riverside gallery with free entry for all visitors."
        free = research.normalize_pricing(venue, venue_context, None)
        self.assertEqual(free["status"], "free")
        self.assertEqual(free["packages"][0]["price"], {"kind": "free"})

        for unconditional in (
            "It is free to enter.",
            "The entrance is free of charge.",
        ):
            venue_context["description"] = unconditional
            self.assertEqual(
                research.normalize_pricing(venue, venue_context, None)["status"],
                "free",
                unconditional,
            )

        venue_context["description"] = "A riverside gallery with rotating exhibitions."
        venue_context["reviews"] = [
            {"text": "We liked the free admission and compact exhibition."},
            {"text": "Free admission made this an easy spontaneous stop."},
        ]
        self.assertEqual(
            research.normalize_pricing(venue, venue_context, None)["status"],
            "not-published",
        )

        for conditional in (
            "Children under 6 receive free admission. Adults pay at the door.",
            "Free entry with a Budapest Card.",
            "Admission is free on Sundays.",
            "Museum members receive free entry.",
        ):
            venue_context["description"] = conditional
            self.assertEqual(
                research.normalize_pricing(venue, venue_context, None)["status"],
                "not-published",
                conditional,
            )

        venue_context["description"] = "Book with free cancellation until the day before."
        venue_context["reviews"] = [
            {"text": "Free cancellation was useful."},
            {"text": "We appreciated the free cancellation policy."},
        ]
        self.assertEqual(
            research.normalize_pricing(venue, venue_context, None)["status"],
            "not-published",
        )

    def test_price_packages_preserve_options_party_context_and_known_base(self):
        item = visible_item("AttractionProductReview:204")
        item_context = context(
            "AttractionProductReview:204",
            packages=[
                {
                    "name": "Private morning",
                    "description": "Morning departure.",
                    "party": "4 adults",
                },
                {
                    "name": "Private evening",
                    "description": "Evening departure.",
                    "party": "4 adults",
                },
            ],
        )
        prepared = research.prepare_item(item, item_context)
        normalized = research.normalize_pricing(item, item_context, model_item(prepared))

        self.assertEqual(normalized["status"], "priced")
        self.assertEqual(
            normalized["startingPrice"],
            {"kind": "from", "amount": "34.97", "currency": "USD"},
        )
        self.assertEqual(
            normalized["packages"][0]["price"],
            {"kind": "date-required"},
        )
        self.assertIn("Party: 4 adults", normalized["packages"][0]["conditions"])
        self.assertIn("Party: 4 adults", normalized["packages"][1]["conditions"])

        total_context = context(
            "AttractionProductReview:204",
            packages=[
                {
                    "name": "Private evening",
                    "description": "Evening departure.",
                    "total_price": "€120.00",
                    "party": "4 adults",
                }
            ],
        )
        total_context["pricing_evidence"]["base_price"] = ""
        total_prepared = research.prepare_item(item, total_context)
        total = research.normalize_pricing(item, total_context, model_item(total_prepared))
        self.assertEqual(total["packages"][0]["price"]["unit"], "group")
        self.assertIn("Party: 4 adults", total["packages"][0]["conditions"])

    def test_date_required_keeps_named_options_and_unavailable_has_note(self):
        item = visible_item("AttractionProductReview:205")
        item_context = context(
            "AttractionProductReview:205",
            packages=[{"name": "Evening option", "description": "Select a date first."}],
        )
        item_context["pricing_evidence"]["base_price"] = ""
        prepared = research.prepare_item(item, item_context)
        normalized = research.normalize_pricing(item, item_context, model_item(prepared))
        self.assertEqual(normalized["status"], "date-required")
        self.assertEqual(normalized["packages"][0]["name"], "Evening option")
        self.assertTrue(normalized["packages"][0]["description"])
        self.assertTrue(normalized["note"])

        item_context["pricing_evidence"]["status"] = "unavailable"
        item_context["pricing_evidence"]["base_price"] = "$34.97"
        unavailable = research.normalize_pricing(item, item_context, model_item(prepared))
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["packages"][0]["name"], "Evening option")
        self.assertEqual(unavailable["packages"][0]["availability"], "unavailable")
        self.assertNotIn("startingPrice", unavailable)
        self.assertTrue(unavailable["note"])

    def test_advertised_starting_price_survives_package_lookup_failure_without_claiming_unavailable(self):
        item = visible_item("AttractionProductReview:2051")
        cases = (
            {
                "status": "unavailable",
                "availability": {
                    "status": "unavailable",
                    "reason": "pax_failed",
                    "message": "Package options were unavailable for the selected date.",
                    "source": "graphql:paxMix",
                },
                "packages": [],
            },
            {
                "status": "priced",
                "availability": {
                    "status": "unavailable",
                    "message": "Unavailable",
                },
                "packages": [
                    {
                        "name": "Unconfirmed option",
                        "unit_price": "USD 24.00",
                        "unit": "adult",
                        "availability": "available",
                    }
                ],
            },
        )
        for case in cases:
            with self.subTest(evidence_status=case["status"]):
                item_context = context("AttractionProductReview:2051")
                item_context["pricing_evidence"] = {
                    "status": case["status"],
                    "base_price": "USD 19.86",
                    "booking_date": "2026-07-18",
                    "travelers": "",
                    "availability": case["availability"],
                    "packages": case["packages"],
                }

                normalized = research.normalize_pricing(item, item_context, None)

                self.assertEqual(normalized["status"], "priced")
                self.assertEqual(
                    normalized["startingPrice"],
                    {"kind": "from", "amount": "19.86", "currency": "USD"},
                )
                self.assertEqual(normalized["packageAvailability"], "unknown")
                self.assertIn("package", normalized["note"].lower())
                self.assertIn("starting price", normalized["note"].lower())
                self.assertEqual(normalized["context"]["date"], "2026-07-18")
                self.assertTrue(
                    all(
                        package["availability"] in {"available", "sold-out", "unavailable"}
                        for package in normalized["packages"]
                    )
                )

    def test_zero_placeholder_is_never_published_as_a_starting_price(self):
        item = visible_item("AttractionProductReview:2052")
        item_context = context("AttractionProductReview:2052")
        item_context["pricing_evidence"] = {
            "status": "priced",
            "base_price": "USD 0.00",
            "booking_date": "2026-07-18",
            "travelers": "",
            "availability": {
                "status": "unavailable",
                "reason": "pax_failed",
                "message": "Package options were unavailable for the selected date.",
                "source": "graphql:paxMix",
            },
            "packages": [],
        }

        normalized = research.normalize_pricing(item, item_context, None)

        self.assertEqual(normalized["status"], "date-required")
        self.assertEqual(normalized["packageAvailability"], "unknown")
        self.assertNotIn("startingPrice", normalized)

    def test_mixed_package_availability_preserves_sold_out_options(self):
        item = visible_item("AttractionProductReview:207")
        item_context = context(
            "AttractionProductReview:207",
            packages=[
                {
                    "name": "Small group",
                    "description": "Shared tour.",
                    "unit_price": "$79.00",
                    "unit": "adults",
                    "availability": "available",
                },
                {
                    "name": "Private tour",
                    "description": "Private option.",
                    "unit_price": "$55.00",
                    "unit": "adults",
                    "availability": "sold-out",
                    "availability_message": "Sold out",
                },
            ],
        )
        prepared = research.prepare_item(item, item_context)
        normalized = research.normalize_pricing(item, item_context, model_item(prepared))
        self.assertEqual(normalized["status"], "priced")
        self.assertEqual(normalized["packages"][0]["availability"], "available")
        self.assertEqual(normalized["packages"][1]["availability"], "sold-out")

        item_context["pricing_evidence"]["status"] = "unavailable"
        item_context["pricing_evidence"]["availability"] = {
            "status": "closed",
            "message": "Temporarily closed until further notice.",
            "source": "tripadvisor-status-banner",
        }
        unavailable = research.normalize_pricing(item, item_context, model_item(prepared))
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertNotIn("startingPrice", unavailable)
        self.assertEqual(unavailable["packages"][0]["availability"], "unavailable")
        self.assertEqual(unavailable["packages"][1]["availability"], "sold-out")
        self.assertEqual(unavailable["note"], "Temporarily closed until further notice.")

    def test_numeric_evidence_overrides_stale_nonpriced_status_and_impossible_priced_demotes(self):
        product = visible_item("AttractionProductReview:206")
        product_context = context("AttractionProductReview:206")
        product_context["pricing_evidence"]["status"] = "date-required"
        priced = research.normalize_pricing(product, product_context, None)
        self.assertEqual(priced["status"], "priced")
        self.assertIn("startingPrice", priced)

        product_context["pricing_evidence"] = {"status": "priced", "packages": []}
        no_number = research.normalize_pricing(product, product_context, None)
        self.assertEqual(no_number["status"], "date-required")

        venue = visible_item("Attraction_Review:206", item_type="venue")
        venue_context = context("Attraction_Review:206")
        venue_context["description"] = "General admission is free."
        venue_context["pricing_evidence"] = {
            "base_price": "$20.00",
            "packages": [],
        }
        mixed = research.normalize_pricing(venue, venue_context, None)
        self.assertEqual(mixed["status"], "priced")
        self.assertEqual(mixed["packages"][0]["price"], {"kind": "free"})
        self.assertEqual(mixed["startingPrice"]["amount"], "20.00")

    def test_money_parser_handles_locale_decimals_and_non_us_dollar_prefixes(self):
        self.assertEqual(research._money("€34,97"), {"amount": "34.97", "currency": "EUR"})
        self.assertEqual(
            research._money("1.234,56 €"),
            {"amount": "1234.56", "currency": "EUR"},
        )
        self.assertEqual(
            research._money("A$ 42.50"),
            {"amount": "42.50", "currency": "AUD"},
        )
        self.assertIsNone(research._money("€1.23.4"))


if __name__ == "__main__":
    unittest.main()
