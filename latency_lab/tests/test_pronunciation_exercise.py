import unittest
from latency_lab import directory_locations as loc, provider_directory as directory


class PronunciationExercise(unittest.TestCase):
    def test_observed_distinctive_variants(self):
        samples = {
            "eat ketchup": "Ikeja", "Then Icoi": "Ikoyi", "E-co-E area.": "Ikoyi",
            "I KOLODU I KOLODU": "Ikorodu", "I COULDU": "Ikorodu",
            "Leckie": "Lekki", "Licky": "Lekki", "Leaky...": "Lekki",
            "AČA": "Ajah", "AJA": "Ajah", "Oh, bah.": "Ogba", "OBBA": "Ogba",
            "Bagado.": "Gbagada", "Bagada": "Gbagada", "Suruliri": "Surulere",
            "Surinari": "Surulere", "Suruniri": "Surulere", "Suru Leary": "Surulere",
            "and Lopéduo.": "Ilupeju", "or on short key": "Oworonshoki",
            "Somonu.": "Shomolu", "SHOW MODULS": "Shomolu", "O DZUDO BEGA": "Ojodu Berger",
            "Magudu": "Magodo", "Magoodoo.": "Magodo", "i ya ni pa ja": "Iyana Ipaja",
            "Yanai Paja.": "Iyana Ipaja", "Yianni Pajang": "Iyana Ipaja",
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                body=loc.clarify({"intent":"lookup","network":"PROVIDER","plan":"Pro","state":"Lagos"},text)
                self.assertIsNotNone(body)
                self.assertEqual(body["status"],"needs_confirmation")
                self.assertEqual(body["data"]["suggested_filters"]["town"],expected)
                self.assertEqual(body["data"]["suggested_filters"]["plan"],"Pro")

    def test_common_phrases_are_not_forced_into_places(self):
        for text in ["I got to", "in the bedroom", "equal to", "I could", "Oh, wow.", "Show my news."]:
            body=loc.clarify({"intent":"lookup","town":"Ikeja","plan":"Pro","state":"Lagos"},text)
            self.assertEqual(body["status"],"needs_input")
            self.assertNotIn("suggested_filters",body["data"])
        for text in ["Treat everyone equally", "I would like to eat ketchup", "There is a leaky tap", "Show modules in the app"]:
            self.assertIsNone(loc.clarify({"intent":"lookup","town":"Ikeja","plan":"Pro"},text))

    def test_new_location_does_not_get_stuck_on_previous_question(self):
        self.assertTrue(loc.new_location("Magoodoo",{"town":"Lekki"}))
        self.assertTrue(loc.new_location("I'm in Ikoyi",{"town":"Lekki"}))
        self.assertFalse(loc.new_location("Yes",{"town":"Lekki"}))
        self.assertFalse(loc.new_location("Licky",{"town":"Lekki"}))

    def test_conflicting_names_ask_which_area(self):
        body=loc.clarify({"intent":"lookup","town":"Bagada","state":"Lagos"},"I am in Magoodoo")
        self.assertEqual(body["status"],"needs_input")
        self.assertNotIn("suggested_filters",body["data"])

    def test_legacy_sheet_spelling_equivalence(self):
        rows=[["CODE","ZONE","STATE","TOWN","PROVIDER","ADDRESS","CAT","PLAN","SPECIALTY"],
              ["1","","Lagos","SOMOLU","A","Road","D","PRO & HIGHER","GENERAL MEDICINE"],
              ["2","","Lagos","SHOMOLU","B","Road","D","PRO & HIGHER","GENERAL MEDICINE"],
              ["3","","Lagos","OWOROSHONKI","C","Road","D","PRO & HIGHER","GENERAL MEDICINE"]]
        self.assertEqual(len(directory.match_records("PROVIDER",rows,{"town":"Shomolu","plan":"Pro"})),2)
        self.assertEqual(len(directory.match_records("PROVIDER",rows,{"town":"Oworonshoki","plan":"Pro"})),1)
        self.assertIsNone(loc.validate_labels({"network":"PROVIDER","town":"Oworonshoki","plan":"Pro"},directory.records("PROVIDER",rows)))


if __name__=="__main__": unittest.main()
