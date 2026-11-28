import base64
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx
from groq import NotFoundError

from backend import ai_pipeline as pipeline


class RegionalPipelineTests(unittest.TestCase):
    def test_groq_fallback_preserves_khata_prompt_and_json_contract(self):
        messages = [{"role": "system", "content": pipeline.SYSTEM_PROMPT},
                    {"role": "user", "content": "Check the ledger"}]
        error = NotFoundError("model_not_found", body=None, response=httpx.Response(
            404, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")))
        with patch.object(pipeline, "client") as client, self.assertLogs("voxmetrics"):
            create = client.chat.completions.create
            create.side_effect = [error, "completed"]
            self.assertEqual(pipeline.call_groq_chat(messages), "completed")
            for call, model in zip(create.call_args_list, ["openai/gpt-oss-120b", "openai/gpt-oss-20b"], strict=True):
                self.assertEqual(call.kwargs, dict(model=model, messages=messages, temperature=0.0,
                                                   response_format={"type": "json_object"}))
        for intent in ("KHATA_UPDATE", "KHATA_QUERY"):
            self.assertIn(intent, messages[0]["content"])

    def test_voice_routes_and_english_retry(self):
        for code, voice in {**pipeline.GTTS_LANG_MAP, "unlisted": "hi"}.items():
            with self.subTest(code=code), patch("gtts.gTTS") as tts:
                tts.return_value.write_to_fp.side_effect = lambda buf: buf.write(b"mp3")
                self.assertEqual(pipeline.generate_tts_base64("reply", code),
                                 base64.b64encode(b"mp3").decode())
                tts.assert_called_once_with(text="reply", lang=voice)

        with patch("gtts.gTTS") as tts, patch.object(pipeline.logger, "warning"):
            english = Mock()
            english.write_to_fp.side_effect = lambda buf: buf.write(b"ok")
            tts.side_effect = [ValueError("unsupported"), english]
            self.assertTrue(pipeline.generate_tts_base64("reply", "or"))
            self.assertEqual([c.kwargs["lang"] for c in tts.call_args_list], ["bn", "en"])
            tts.side_effect = OSError("offline")
            self.assertEqual(pipeline.generate_tts_base64("reply", "mai"), "")

    def test_whisper_autodetection_and_noise_gate(self):
        with (patch.object(pipeline, "client") as groq,
              patch.object(pipeline, "run_ai_pipeline") as interpret,
              patch.object(pipeline, "read_inventory", return_value=[]),
              patch.object(pipeline, "generate_tts_base64", return_value="")):
            transcribe = groq.audio.transcriptions.create
            transcribe.return_value = {"text": "unclear", "segments": [{"no_speech_prob": .9}]}
            result = pipeline.process_voice_command(b"audio", "test.wav", "audio/wav")
            self.assertEqual(result["intent"], "NOISE_ERROR")
            interpret.assert_not_called()
            transcribe.assert_called_once_with(
                model="whisper-large-v3", file=("test.wav", b"audio", "audio/wav"),
                response_format="verbose_json", temperature=0.0,
            )

    def test_gujarati_sale_uses_quoted_verb_and_localized_database_result(self):
        transcript = "૨ કિલો ખાંડ વેચી"
        decision = pipeline.Decision(
            detected_lang="gu", intent="INVENTORY_UPDATE",
            items=[pipeline.CommandItem(action="SELL", item_name="khand", quantity=2,
                                        source_name="ખાંડ", action_words="વેચી", source_clause=transcript)],
        )
        translated = "ખાંડનો સ્ટોક બદલાયો (-2 કિલો)."
        def response(content):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        with (patch.object(pipeline, "client"), patch.object(pipeline, "read_inventory", return_value=[]),
              patch.object(pipeline, "get_existing_items", return_value=["sugar"]),
              patch.object(pipeline, "call_groq_chat", side_effect=[
                  response(decision.model_dump_json()), response('{"reply_local":"' + translated + '"}')
              ]), patch.object(pipeline, "update_inventory_batch", return_value=[
                  SimpleNamespace(item_name="sugar", before=9, after=7, unit="kg")
              ]) as write):
            result = pipeline.run_ai_pipeline(transcript)
            self.assertEqual(result["reply_local"], translated)
            self.assertEqual(result["detected_lang"], "gu")
            self.assertEqual(write.call_args.args[0], [pipeline.InventoryWrite("sugar", 2, "SELL")])

    def test_regional_creation_requires_quoted_action_and_distinct_amounts(self):
        clause = "૫ કિલો હળદર ૮૦ રૂપિયે ઉમેરો"
        item = pipeline.CommandItem(
            action="CREATE_ITEM", item_name="turmeric", quantity=5, price_per_unit=80,
            unit="kg", source_name="હળદર", action_words="ઉમેરો", source_clause=clause,
        )
        self.assertTrue(pipeline._creation_stated(item, clause))
        item.action_words = "missing verb"
        self.assertFalse(pipeline._creation_stated(item, clause))
        item.action_words = "ઉમેરો"
        item.price_per_unit = 5
        self.assertFalse(pipeline._creation_stated(item, clause))

    def test_two_letter_routing_for_dialects(self):
        for code, route in {"kok": "mr", "raj": "hi", "mai": "hi", "tanglish": "ta",
                            "kanglish": "kn", "or": "or", "ks": "ks"}.items():
            with self.subTest(code=code):
                decision = pipeline.Decision(detected_lang=code, intent="NOISE_ERROR")
                self.assertEqual(decision.detected_lang, route)
