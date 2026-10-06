import unittest
from webui.emoji_codec import encode, preview, tokens

class EmojiTests(unittest.TestCase):
    def test_supported_ids_and_variation_normalization(self):
        self.assertEqual(encode("🍬 ㊗️，㊗🍬"),["127852","12951"])
        self.assertEqual(encode("㊗"),encode("㊗️"))
        self.assertEqual(preview(["10084"])[0]["label"], "❤️")
        self.assertEqual(preview(["127852"])[0]["label"],"🍬")

    def test_unsupported_clusters_rejected_whole(self):
        for value in ["", "127852", "hello", "123", "👍🏽", "🇨🇳", "👨‍👩‍👧‍👦", "🍬‍🍬", "1️⃣", "🍬a", "©️", "®️", "🏽", "🇨", "🦰", "❤️‍🔥", "🍬︎", "🍬️️", "🏴\U000e0067\U000e0062\U000e007f"]:
            with self.subTest(value=value),self.assertRaises(ValueError): encode(value)

    def test_unknown_qq_id_not_interpreted_as_codepoint(self):
        self.assertEqual(preview(["1"])[0]["label"],"旧版表情（保留）")
        self.assertEqual(tokens("1,1，2 127852"),["1","2","127852"])

    def test_general_single_codepoint_emoji_roundtrip(self):
        examples = {"😀": "128512", "😂": "128514", "👍": "128077",
                    "❤️": "10084", "🔥": "128293", "🎉": "127881",
                    "🐱": "128049", "🌹": "127801", "🚀": "128640",
                    "🍕": "127829", "💯": "128175", "✅": "9989", "🥳": "129395"}
        for emoji, expected in examples.items():
            with self.subTest(emoji=emoji):
                self.assertEqual(encode(emoji), [expected])
                item = preview([expected])[0]
                self.assertFalse(item["legacy"])
                self.assertEqual(encode(item["label"]), [expected])
        self.assertEqual(encode("❤️ ❤;👍，😀；🔥 👍"), ["10084", "128077", "128512", "128293"])

    def test_legacy_ids_are_not_arbitrary_unicode_or_qq_face_guesses(self):
        for value in ["1", "76", "169", "174", "99999", "65", "0128512", "1114112", "9" * 5000, "abc", "127995", "127464"]:
            with self.subTest(value=value[:20]):
                self.assertEqual(preview([value]), [{"id": value, "label": "旧版表情（保留）", "legacy": True}])
        self.assertEqual(tokens("1;1；2 128512"), ["1", "2", "128512"])
