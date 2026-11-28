import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import ai_pipeline as ai, database as db, main


class KhataTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(db, "DATA_DIR", folder))
        self.enterContext(patch.object(db, "DB_PATH", folder / "test.db"))
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("""CREATE TABLE inventory (id INTEGER PRIMARY KEY, item_name TEXT UNIQUE,
                         quantity REAL, unit TEXT, price REAL, units_sold REAL)""")
            conn.execute("INSERT INTO inventory VALUES (1, 'rice', 37, 'kg', 45, 12)")
        db.initialize_database()

    def test_migration_concurrent_credits_and_jama(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: db.update_khata("  ramesh ", "4521", 1.25, "UDHAAR"), range(8)))
        db.initialize_database()
        ledger = db.get_khata_ledger()
        self.assertEqual(len(ledger), 3)
        self.assertEqual(next(r for r in ledger if r["customer_name"] == "Ramesh")["balance"], 460)
        self.assertEqual(db.update_khata("Ramesh", "4521", 500, "JAMA")["balance"], 0)
        self.assertEqual(db.update_khata("Ramesh", "0021", 10, "UDHAAR")["balance"], 10)
        logs = db.get_recent_logs(2)
        self.assertEqual([r["action_type"] for r in logs], ["UDHAAR", "JAMA"])
        rice, = db.read_inventory()
        self.assertEqual((rice["quantity"], rice["price"], rice["units_sold"]), (37, 45, 12))

    def test_inventory_batch_logs_and_rollback(self):
        command = "add two rice and set price to 50"
        db.update_inventory_batch([("rice", 2, "ADD"), ("rice", 50, "UPDATE_PRICE")], raw_command=command)
        logs = db.get_recent_logs()
        self.assertEqual([(row["action_type"], row["raw_command"]) for row in logs],
                         [("UPDATE_PRICE", command), ("ADD", command), ("WELCOME", "")])
        with self.assertRaises(db.UnknownInventoryItem):
            db.update_inventory_batch([("rice", 5, "SELL"), ("missing", 3, "SELL")])
        self.assertEqual(db.get_recent_logs(), logs)
        self.assertEqual(db.read_inventory()[0]["quantity"], 39)

    def test_guardrails_leave_both_tables_untouched(self):
        initial = db.get_khata_ledger()
        for changes, command in [
            ({"customer_name": ""}, "Add 50 udhaar to 4521"),
            ({"last4_digits": None}, "Add 50 udhaar to Ramesh"),
            ({"last4_digits": "521"}, "Ramesh 521 udhaar 50"),
            ({"amount": None}, "Ramesh 4521 udhaar"),
            ({"amount": -5}, "Ramesh 4521 udhaar -5"),
            ({"amount": 0}, "Ramesh 4521 udhaar 0"),
            ({"amount": 4521}, "Ramesh 4521 udhaar"),
            ({}, "Ramesh udhaar 50"),
        ]:
            with self.subTest(command=command):
                entry = dict(customer_name="Ramesh", last4_digits="4521", amount=50, mode="UDHAAR")
                decision = ai.Decision(intent="KHATA_UPDATE", khata={**entry, **changes})
                ai.execute_decision(decision, command)
                self.assertEqual(decision.intent, "INCOMPLETE_INPUT")
        self.assertEqual(db.get_khata_ledger(), initial)
        self.assertEqual([r["action_type"] for r in db.get_recent_logs()], ["WELCOME"])

    def test_failed_ledger_reads_return_lists(self):
        with patch.object(db, "DB_PATH", db.DB_PATH.parent / "absent" / "shop.db"), self.assertLogs("voxmetrics"):
            self.assertEqual(db.get_khata_ledger(), [])
            self.assertEqual(db.get_recent_logs(), [])

    def test_registration_lock_and_store_isolation(self):
        with TestClient(main.app) as client:
            for path in ("process-command", "voice-command"):
                self.assertEqual(client.post("/api/" + path, json={}).status_code, 401)
            self.assertEqual(client.get("/api/inventory").status_code, 401)
            credentials = {"phone": "9000000001", "password": "secret"}
            issued = client.post("/api/auth/send-otp", json={"phone": credentials["phone"]}).json()
            self.assertFalse(issued["is_registered"])
            registration = {**credentials, "shop_name": "New Shop", "otp": issued["otp_demo"]}
            wrong = "0000" if issued["otp_demo"] != "0000" else "1111"
            self.assertEqual(client.post("/api/auth/register", json={**registration, "otp": wrong}).status_code, 400)
            token = client.post("/api/auth/register", json=registration).json()["token"]
            self.assertEqual(client.post("/api/auth/register", json=registration).status_code, 400)
            client.headers["Authorization"] = "Bearer " + token
            snapshot = client.get("/api/inventory").json()
            self.assertEqual(len(snapshot["inventory"]), 8)
            self.assertEqual(snapshot["khata"], [])
            shop_id = db.find_shop(credentials["phone"])["id"]
            self.assertNotEqual(db.find_shop(credentials["phone"])["password_hash"], credentials["password"])
            db.update_inventory_batch([("rice", 3, "SELL"), ("rice", 67, "UPDATE_PRICE")], shop_id=shop_id)
            query = "SELECT item_name, quantity FROM inventory WHERE item_name='rice'"
            self.assertEqual(db.execute_analytics_query(query, shop_id)[0]["value"], 77)
            self.assertEqual(db.execute_analytics_query(query, 1)[0]["value"], 37)
            db.update_khata("Ramesh", "4521", 9, "UDHAAR", shop_id=shop_id)
            db.customer_details(1, "Ramesh", "4521", details="Demo note")
            for note in ("Lane 4", "Deliver at noon"):
                decision = ai.Decision(intent="CUSTOMER_SAVE", customer_name="Ramesh", last4_digits="4521",
                                       details=note, detected_lang="en")
                ai.execute_decision(decision, "Ramesh 4521 save " + note, shop_id=shop_id)
            lookup = ai.Decision(intent="CUSTOMER_LOOKUP", customer_name="Ramesh", last4_digits="4521",
                                 detected_lang="en")
            with (patch.object(ai, "client") as groq, patch.object(ai, "call_groq_chat") as chat,
                  patch.object(ai, "generate_tts_base64", return_value="audio") as speech):
                groq.audio.transcriptions.create.return_value = {"text": "Ramesh 4521 details"}
                chat.return_value.choices = [SimpleNamespace(message=SimpleNamespace(content=lookup.model_dump_json()))]
                result = client.post("/api/voice-command", files={"audio": ("voice.wav", b"mock", "audio/wav")}).json()
                self.assertIn("Lane 4", result["reply_local"])
                self.assertIn("Deliver at noon", result["reply_local"])
                self.assertIn("₹9.00", result["reply_local"])
                speech.assert_called_once_with(result["reply_local"], "en")
                self.assertEqual(result["logs"][0]["action_type"], "CUSTOMER_LOOKUP")
            demo = db.customer_details(1, "Ramesh", "4521")
            self.assertEqual((demo["notes"], demo["balance"]), ("Demo note", 450))
            before = db.get_recent_logs(shop_id=shop_id)
            for intent in ("CUSTOMER_SAVE", "CUSTOMER_LOOKUP"):
                missing = ai.Decision(intent=intent, customer_name="Ramesh", details="Lane 4")
                ai.execute_decision(missing, "Ramesh Lane 4", shop_id=shop_id)
                self.assertEqual(missing.intent, "INCOMPLETE_INPUT")
            self.assertEqual(before, db.get_recent_logs(shop_id=shop_id))
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/inventory").status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json=credentials).status_code, 200)

    def test_native_script_name_and_spoken_digits_match_existing_account(self):
        decision = ai.Decision(intent="KHATA_UPDATE", detected_lang="hi", khata=dict(
            mode="UDHAAR", customer_name="Ramesh", source_name="रमेश", last4_digits="4521",
            last4_words="चार पाँच दो एक", amount=35,
        ))
        result = ai.execute_decision(decision, "रमेश चार पाँच दो एक में ३५ रुपये उधार जोड़ो")
        self.assertIn("485.00", result.grounded_reply)
        self.assertEqual(len(db.get_khata_ledger()), 3)

    def test_text_and_voice_endpoints_use_grounded_replies_and_audio(self):
        update = ai.Decision(intent="KHATA_UPDATE", detected_lang="en", khata=dict(
            mode="JAMA", customer_name="Ramesh", last4_digits="4521", amount=125))
        query = ai.Decision(intent="KHATA_QUERY", detected_lang="en", customer_name="Ramesh", last4_digits="4521")
        def completion(decision):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=decision.model_dump_json()))])

        with (TestClient(main.app) as client, patch.object(ai, "client") as groq,
              patch.object(ai, "call_groq_chat", side_effect=[completion(update), completion(query)]),
              patch.object(main, "generate_tts_base64", return_value="audio") as text_audio,
              patch.object(ai, "generate_tts_base64", return_value="voice") as voice_audio):
            session = client.post("/api/auth/login", json={"phone": "9876543210", "password": "1234"}).json()
            client.headers["Authorization"] = "Bearer " + session["token"]
            initial = client.get("/api/inventory").json()
            self.assertEqual(set(initial), {"inventory", "khata", "logs", "shop_name"})
            self.assertEqual(len(initial["khata"]), 3)
            self.assertEqual([r["action_type"] for r in initial["logs"]], ["WELCOME"])
            for endpoint in ("process-command", "voice-command"):
                invalid = client.post(f"/api/{endpoint}", json={})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["inventory"], initial["inventory"])
                self.assertEqual(invalid.json()["khata"], initial["khata"])
                self.assertEqual(invalid.json()["logs"], initial["logs"])
            command = "Ramesh 4521 paid 125 rupees"
            result = client.post("/api/process-command", json={"transcript": command}).json()
            self.assertEqual(result["logs"][0]["raw_command"], command)
            self.assertEqual(result["audio_base64"], "audio")
            self.assertIn("₹325.00", result["reply_local"])
            text_audio.assert_called_once_with(result["reply_local"], "en")
            groq.audio.transcriptions.create.return_value = {"text": "What does Ramesh 4521 owe?"}
            result = client.post("/api/voice-command", files={"audio": ("voice.wav", b"mock", "audio/wav")}).json()
            self.assertEqual(result["intent"], "KHATA_QUERY")
            self.assertIn("₹325.00", result["reply_local"])
            voice_audio.assert_called_once_with(result["reply_local"], "en")
            self.assertEqual(result["audio_base64"], "voice")
            self.assertEqual([r["action_type"] for r in result["logs"]], ["KHATA_QUERY", "JAMA", "WELCOME"])
            self.assertEqual(len(result["khata"]), 3)
