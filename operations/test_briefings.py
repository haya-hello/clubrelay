"""虚构数据的准备助手边界回归。 / Preparation boundary regression using fictional data."""
import copy
import json
import uuid
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase, Client
from django.urls import reverse
from . import test_handoffs as fixtures
from .models import BriefingSession, HandoffPack, AnalysisPermission
from . import briefings
from .handoffs import Conflict
from .ai_client import AIError, _request_payload, _validated_result


class BriefingTests(TestCase):
    setUpTestData = classmethod(fixtures.HandoffTests.setUpTestData.__func__)
    material = classmethod(fixtures.HandoffTests.material.__func__)
    setUp = fixtures.HandoffTests.setUp
    result = fixtures.HandoffTests.result
    grant = fixtures.HandoffTests.grant
    make = fixtures.HandoffTests.make
    review = fixtures.HandoffTests.review
    confirm = fixtures.HandoffTests.confirm

    def session(self):
        self.pack = self.confirm(self.make())
        self.entry = self.pack.items.filter(section="practice").first()
        return briefings.create(self.manager, self.pack.pk, {"goal":"Prepare the next fictional meetup","expected_attendees":40,"constraints":"First-time team","language":"en"}, uuid.uuid4())

    def answer(self):
        return {"answers":[{"kind":"suggestion","text":"Separate check-in and QR codes; applicability still needs review.","entry_ids":[str(self.entry.pk)]}],"unknowns":["Who lends the equipment?"]}

    def ask(self, session, *, token=None, question="What should I prepare?", revision=None):
        return briefings.ask(self.manager, session.pack_id, session.pk, question, token or uuid.uuid4(), session.revision if revision is None else revision)

    def test_complete_roundtrip_persists_context_selection_and_export(self):
        session=self.session()
        with patch("operations.briefings.analyze_sources",return_value=self.answer()) as model:
            session=self.ask(session)
        self.assertEqual(model.call_args.kwargs["purpose"],"briefing")
        query=json.loads(model.call_args.kwargs["question"])
        self.assertEqual(query["event_context"]["expected_attendees"],40)
        self.assertNotIn("PRIVATE_UNSELECTED",json.dumps(model.call_args.kwargs))
        session=briefings.update(self.manager,self.pack.pk,session.pk,session.revision,selected=[str(self.entry.pk)])
        text=briefings.export(self.manager,self.pack.pk,session.pk)
        self.assertIn("plan, not actual",text)
        self.assertIn(self.entry.suggestion,text)
        self.assertIn("Who lends the equipment?",text)
        self.assertNotIn("127.0.0.1",text)
        session.refresh_from_db()
        self.assertEqual(session.selected,[str(self.entry.pk)])
        self.assertEqual(session.turns[0]["response"],self.answer())
        self.pack.refresh_from_db()
        self.assertEqual(self.pack.status,"confirmed")

    def test_history_only_latest_five_successful_turns(self):
        session=self.session()
        with patch("operations.briefings.analyze_sources",return_value=self.answer()) as model:
            for n in range(7): session=self.ask(session,question=f"Follow-up {n}")
        history=json.loads(model.call_args.kwargs["question"])["history"]
        self.assertEqual([h["question"] for h in history],[f"Follow-up {n}" for n in range(1,6)])

    def test_duplicate_success_does_not_call_model_twice(self):
        session=self.session();token=uuid.uuid4()
        with patch("operations.briefings.analyze_sources",return_value=self.answer()) as model:
            session=self.ask(session,token=token)
            self.ask(session,token=token,revision=1)
            self.assertEqual(model.call_count,1)
            with self.assertRaises(Conflict):self.ask(session,token=token,question="different")

    def test_pending_blocks_concurrent_question(self):
        session=self.session()
        def during(**kwargs):
            session.refresh_from_db()
            with self.assertRaises(Conflict):self.ask(session)
            return self.answer()
        with patch("operations.briefings.analyze_sources",side_effect=during):self.ask(session)

    def test_reset_during_call_discards_late_answer(self):
        session=self.session()
        def during(**kwargs):
            session.refresh_from_db()
            briefings.update(self.manager,self.pack.pk,session.pk,session.revision,reset=True)
            return self.answer()
        with patch("operations.briefings.analyze_sources",side_effect=during):
            with self.assertRaises(Conflict):self.ask(session)
        session.refresh_from_db();self.assertEqual(session.turns,[])

    def test_timeout_preserves_selection_and_question_without_retry(self):
        session=self.session()
        session=briefings.update(self.manager,self.pack.pk,session.pk,session.revision,selected=[str(self.entry.pk)])
        token=uuid.uuid4()
        with patch("operations.briefings.analyze_sources",side_effect=AIError("timeout")) as model:
            with self.assertRaises(AIError):self.ask(session,token=token)
            session.refresh_from_db()
            with self.assertRaises(AIError):self.ask(session,token=token)
            self.assertEqual(model.call_count,1)
        self.assertIsNone(session.pending)
        self.assertEqual(session.selected,[str(self.entry.pk)])
        self.assertEqual(session.turns[0]["question"],"What should I prepare?")

    def test_stale_before_call_blocks_model_and_export(self):
        session=self.session();self.a.text+=" Changed";self.a.save()
        with patch("operations.briefings.analyze_sources") as model:
            with self.assertRaises(ValidationError):self.ask(session)
            model.assert_not_called()
        with self.assertRaises(ValidationError):briefings.export(self.manager,self.pack.pk,session.pk)

    def test_change_during_call_drops_result_and_clears_pending(self):
        session=self.session()
        def change(**kwargs):self.b.text+=" Changed";self.b.save();return self.answer()
        with patch("operations.briefings.analyze_sources",side_effect=change):
            with self.assertRaises(ValidationError):self.ask(session)
        session.refresh_from_db();self.assertIsNone(session.pending);self.assertEqual(session.turns,[])

    def test_revoked_consent_blocks_access_to_new_sources(self):
        session=self.session();AnalysisPermission.objects.filter(event=self.event).delete()
        with self.assertRaises(ValidationError):self.ask(session)
        state=briefings.public_state(self.manager,self.pack.pk,session)
        self.assertTrue(state["error"]);self.assertEqual(state["entries"],[])

    def test_changed_config_during_call_is_discarded(self):
        session=self.session()
        def change(**kwargs):self.config.model="other-model";self.config.save();return self.answer()
        with patch("operations.briefings.analyze_sources",side_effect=change):
            with self.assertRaises(ValidationError):self.ask(session)
        session.refresh_from_db();self.assertEqual(session.turns,[])

    def test_owner_isolation_and_member_rejection(self):
        session=self.session();other=User.objects.create_user("other-manager");other.groups.set(self.manager.groups.all())
        with self.assertRaises(BriefingSession.DoesNotExist):briefings.owned(other,self.pack.pk,session.pk)
        with self.assertRaises(PermissionDenied):briefings.owned(self.member,self.pack.pk,session.pk)
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("briefing_state",args=[self.pack.pk,session.pk])).status_code,404)

    def test_stale_selection_revision_and_foreign_ids_rejected(self):
        session=self.session()
        with self.assertRaises(Conflict):briefings.update(self.manager,self.pack.pk,session.pk,0,selected=[])
        with self.assertRaises(ValidationError):briefings.update(self.manager,self.pack.pk,session.pk,session.revision,selected=[str(uuid.uuid4())])

    def test_draft_pack_rejected(self):
        pack=self.make()
        with self.assertRaises(ValidationError):briefings.guard(self.manager,pack.pk)

    def test_json_schema_and_fabricated_citation_rejected(self):
        session=self.session()
        _,_,_,entries=briefings.guard(self.manager,self.pack.pk)
        sources={e["id"]:json.dumps(e) for e in entries}
        def validate(result):return _validated_result({"choices":[{"message":{"content":json.dumps(result)}}]},sources,"briefing")
        self.assertEqual(validate(self.answer()),self.answer())
        bad=self.answer();bad["answers"][0]["entry_ids"]=[str(uuid.uuid4())]
        with self.assertRaises(AIError):validate(bad)
        bad=self.answer();bad["answers"][0]["entry_ids"]=[str(self.pack.items.get(section="question").pk)]
        with self.assertRaises(AIError):validate(bad)
        for bad in ({"answers":[],"unknowns":[]},{"answers":{},"unknowns":[]},{"answers":[{"kind":[],"text":"x","entry_ids":[]}],"unknowns":[]}):
            with self.assertRaises(AIError):validate(bad)

    def test_csrf_and_method_protection(self):
        session=self.session();c=Client(enforce_csrf_checks=True);c.force_login(self.manager)
        url=reverse("briefing_turn",args=[self.pack.pk,session.pk])
        self.assertEqual(c.post(url,data={},content_type="application/json").status_code,403)
        self.assertEqual(self.client.get(url).status_code,405)

    def test_page_escapes_untrusted_titles_and_json(self):
        session=self.session();self.pack.title='<script>alert("bad")</script>';self.pack.save()
        response=self.client.get(reverse("briefing",args=[self.pack.pk])+"?lang=en")
        self.assertContains(response,"Alexa+ simulated experience")
        self.assertNotContains(response,'<script>alert("bad")</script>')

    def test_context_validation_and_creation_idempotency(self):
        session=self.session()
        self.assertEqual(briefings.create(self.manager,self.pack.pk,session.context,session.pk).pk,session.pk)
        bad=copy.deepcopy(session.context);bad["expected_attendees"]=True
        with self.assertRaises(ValidationError):briefings.create(self.manager,self.pack.pk,bad,uuid.uuid4())

    def test_reset_keeps_context_and_original_handover(self):
        session=self.session()
        with patch("operations.briefings.analyze_sources",return_value=self.answer()):session=self.ask(session)
        session=briefings.update(self.manager,self.pack.pk,session.pk,session.revision,reset=True)
        self.assertEqual(session.turns,[]);self.assertEqual(session.context["expected_attendees"],40)
        self.assertEqual(HandoffPack.objects.get(pk=self.pack.pk).status,"confirmed")

    def test_print_has_semantic_content_and_saved_selection(self):
        session=self.session()
        session=briefings.update(self.manager,self.pack.pk,session.pk,session.revision,selected=[str(self.entry.pk)])
        response=self.client.get(reverse("briefing_export",args=[self.pack.pk,session.pk,"print"]))
        self.assertContains(response,"<blockquote>")
        self.assertContains(response,self.entry.suggestion)
        self.assertNotContains(response,"<pre>")

    def test_empty_checklist_cannot_be_exported(self):
        session=self.session()
        with self.assertRaises(ValidationError):briefings.export(self.manager,self.pack.pk,session.pk)
