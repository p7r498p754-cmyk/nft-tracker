import io
import json
import tempfile
import unittest
from pathlib import Path
from nft_tracker.webhook import create_app
from tests.helpers import OWNER, FakeTelegram, make_settings


class WebhookTests(unittest.TestCase):
    def test_authenticated_start_replay_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_settings(db_path=Path(directory)/"private.db")
            tg = FakeTelegram()
            secret = "a"*40
            body = json.dumps({"update_id":7, "message":{"from":{"id":OWNER},"chat":{"id":OWNER,"type":"private"},"text":"/start"}}).encode()
            for token,status in [("invalid","403 Forbidden"),(secret,"200 OK"),(secret,"200 OK")]:
                env = {"PATH_INFO":"/telegram","REQUEST_METHOD":"POST","CONTENT_LENGTH":str(len(body)),
                       "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN":token,"wsgi.input":io.BytesIO(body)}
                create_app(cfg,secret,tg)(env,lambda actual,headers:self.assertEqual(actual,status))
            self.assertEqual(len(tg.sent),1)
