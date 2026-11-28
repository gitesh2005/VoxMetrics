import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import ai_pipeline as pipeline, database


class KiranaQuantityTests(unittest.TestCase):
    def test_spoken_measures_override_raw_or_already_converted_model_values(self):
        cases = [
            ("ek pipa tel", "oil", 10), ("2 tins", "milk", 20), ("१ डिब्बा", "oil", 10),
            ("2 katta", "rice", 100), ("ek bori", "atta", 50), ("3 bora", "salt", 150),
            ("do darjan", "egg", 24), ("half a dozen", "egg", 6),
            ("ek pao", "rice", .25), ("teen pav", "salt", .75), ("आधा किलो", "sugar", .5),
            ("sawa kilo", "rice", 1.25), ("dedh liter", "oil", 1.5), ("ढाई किलो", "lentils", 2.5),
            ("500 gram", "flour", .5), ("500ml", "milk", .5),
        ]
        for phrase, name, expected in cases:
            for model_value in (1, expected):
                with self.subTest(phrase=phrase, model_value=model_value):
                    self.assertEqual(pipeline.normalize_kirana_quantity(phrase, name, model_value), expected)

    def test_mixed_batch_and_confirmation_use_committed_base_units(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(database, "DATA_DIR", Path(folder)), \
                patch.object(database, "DB_PATH", Path(folder) / "inventory.db"):
            database.initialize_database()
            clauses = ["add 2 pipa oil", "sell ek bori rice", "create 2 dozen eggs at 5 per piece"]
            items = [
                pipeline.CommandItem(action="ADD", item_name="oil", quantity=2),
                pipeline.CommandItem(action="SELL", item_name="rice", quantity=50),
                pipeline.CommandItem(action="CREATE_ITEM", item_name="egg", source_name="eggs",
                                     quantity=2, unit="pieces", price_per_unit=5),
            ]
            for item, clause in zip(items, clauses):
                item.source_clause = clause
            decision = pipeline.Decision(intent="INVENTORY_UPDATE", detected_lang="en", items=items)
            units = {r["item_name"]: r["unit"] for r in database.read_inventory()}
            result = pipeline.execute_decision(decision, ", ".join(clauses), list(units), units)
            rows = {r["item_name"]: r for r in database.read_inventory()}
            self.assertEqual(rows["oil"]["quantity"], 45)
            self.assertEqual(rows["rice"]["quantity"], 30)
            self.assertEqual(rows["rice"]["units_sold"], 90)
            self.assertEqual((rows["egg"]["quantity"], rows["egg"]["price"]), (24, 5))
            for fragment in ("+20 L", "2 pipa", "total stock 45 L", "-50 kg", "+24 pieces"):
                self.assertIn(fragment, result.grounded_reply)

    def test_wrong_dimension_and_missing_price_do_not_write(self):
        for clause, action, name, unit in [
            ("add 2 tins sugar", "ADD", "sugar", "kg"),
            ("add 2 boris soap", "ADD", "soap", "kg"),
            ("create 2 dozen eggs", "CREATE_ITEM", "eggs", "pieces"),
        ]:
            with self.subTest(clause=clause), patch.object(pipeline, "update_inventory_batch") as write:
                item = pipeline.CommandItem(action=action, item_name=name, quantity=2, unit=unit,
                                            price_per_unit=2 if action == "CREATE_ITEM" else None)
                decision = pipeline.Decision(intent="INVENTORY_UPDATE", items=[item])
                pipeline.execute_decision(decision, clause, [name], {name: unit})
                write.assert_not_called()
                self.assertEqual(decision.intent, "INCOMPLETE_INPUT")
