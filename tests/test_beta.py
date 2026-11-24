import tempfile
import unittest
from pathlib import Path

from nft_tracker import beta
from nft_tracker.backup import snapshot
from nft_tracker.service import effective_access
from nft_tracker.storage import Storage
from tests.helpers import NOW, make_settings, ev


class ClosedBeta(unittest.TestCase):
    def test_invitation_limit_timings_and_expiry(self):
        store = Storage(":memory:")
        self.addCleanup(store.close)
        cfg = make_settings(closed_beta=True)
        stamp = int(NOW.timestamp())
        self.assertFalse(beta.allowed(cfg, store, 1))
        for i in range(1,21):
            self.assertTrue(beta.invite(store,i,"ULTRA",stamp+60))
        self.assertFalse(beta.invite(store,21,"PLUS",stamp+60))
        self.assertTrue(beta.allowed(cfg,store,1))
        self.assertEqual(effective_access(cfg,store,1,stamp)[0],"ULTRA")
        self.assertEqual(effective_access(cfg,store,1,stamp+60)[0],"FREE")
        u = store.ensure_user("1",1,30,NOW)
        store.db.execute("INSERT INTO beta_ticks VALUES (?,?)",("1",stamp))
        for plan, minutes in beta.MINUTES.items():
            self.assertFalse(beta.due(store,u,plan,stamp+minutes*60-1))
            self.assertTrue(beta.due(store,u,plan,stamp+minutes*60))
        for _ in range(20):
            self.assertTrue(beta.admit_request(store,1,stamp))
        self.assertFalse(beta.admit_request(store,1,stamp))

    def test_restore_private_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, backup, restored = [Path(tmp)/n for n in ("live","backup","restored")]
            store = Storage(live)
            beta.invite(store,1,"PRO",12345)
            store.ensure_user("1",1,30,NOW)
            store.set_min_discount("1",40)
            store.mark_sent("1","signal",NOW)
            store.db.execute("INSERT INTO price_history VALUES (?,?)",(NOW.isoformat(),'{"rows":[]}'))
            store.db.commit()
            beta.feed(store,[ev()],NOW)
            snapshot(live,backup)
            store.close()
            snapshot(backup,restored)
            store = Storage(restored)
            self.addCleanup(store.close)
            self.assertEqual(store.user("1").min_discount,40)
            self.assertTrue(store.was_sent("1","signal"))
            self.assertEqual(store.db.execute("SELECT plan FROM beta_invites").fetchone()[0],"PRO")
            self.assertEqual(store.db.execute("SELECT count(*) FROM price_history").fetchone()[0],1)
            self.assertEqual(beta.feed(store,[],NOW),[ev()])
            with self.assertRaises(FileExistsError):
                snapshot(backup,restored)
