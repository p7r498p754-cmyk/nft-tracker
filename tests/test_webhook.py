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

    def test_separate_test_transport_and_db(self):
        from unittest.mock import patch
        from nft_tracker.webhook import test_from_env
        from nft_tracker.telegram import TelegramClient
        from tests.helpers import json_response
        from unittest.mock import Mock
        opener = Mock(return_value=json_response({"ok":True,"result":{"is_bot":True}}))
        TelegramClient("fixture",opener,test_server=True).call("getMe")
        self.assertEqual(opener.call_args.args[0].full_url,"https://api.telegram.org/botfixture/test/getMe")
        with patch.dict("os.environ",{"TEST_DB_PATH":"state/tracker.sqlite3","DB_PATH":"state/tracker.sqlite3"},clear=True):
            with self.assertRaises(ValueError):
                test_from_env()
