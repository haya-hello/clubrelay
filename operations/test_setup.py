import io
import json
import tempfile
from pathlib import Path
from django.contrib.auth.models import User, Group
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from .models import AnalysisRun
from .security import MANAGER_GROUP

class InitialSetupTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        override=override_settings(DATA_DIR=Path(self.temp.name))
        override.enable()
        self.addCleanup(override.disable)
    def test_empty_database_creates_one_manager_only(self):
        out=io.StringIO()
        call_command("seed_manager",stdout=out)
        self.assertEqual(User.objects.count(),1)
        user=User.objects.get()
        data=json.loads((Path(self.temp.name)/"demo-accounts.json").read_text(encoding="utf-8"))
        self.assertTrue(user.check_password(data["manager"]["password"]))
        self.assertNotIn(data["manager"]["password"],out.getvalue())
        self.assertTrue(user.groups.filter(name=MANAGER_GROUP).exists())
    def test_existing_manager_password_and_accounts_not_reset(self):
        call_command("seed_manager",stdout=io.StringIO())
        first=User.objects.get().password
        call_command("seed_manager",stdout=io.StringIO())
        self.assertEqual(User.objects.count(),1)
        self.assertEqual(User.objects.get().password,first)
    def test_no_auto_restore_revoked_manager(self):
        user=User.objects.create_user("revoked")
        with self.assertRaises(CommandError):
            call_command("seed_manager",stdout=io.StringIO())
        self.assertFalse(user.groups.exists())
    def test_interrupted_jobs_marked_without_network(self):
        call_command("seed_manager",stdout=io.StringIO())
        user=User.objects.get()
        for status in ["pending","running","complete"]:
            AnalysisRun.objects.create(actor=user,question="test",status=status,fingerprint="a"*64,config_fingerprint="b"*64)
        call_command("recover_analysis",stdout=io.StringIO())
        self.assertEqual(AnalysisRun.objects.filter(status="failed").count(),2)
        self.assertEqual(AnalysisRun.objects.filter(status="complete").count(),1)

