import unittest

from read import format_tweet


class FormatTweetTests(unittest.TestCase):
    def _base_result(self, quoted_user_result: dict) -> dict:
        return {
            "rest_id": "100",
            "legacy": {"full_text": "outer tweet"},
            "core": {
                "user_results": {
                    "result": {
                        "core": {"screen_name": "outer_user", "name": "Outer User"},
                        "legacy": {},
                    }
                }
            },
            "quoted_status_result": {
                "result": {
                    "legacy": {
                        "user_id_str": "123456789",
                        "full_text": "quoted tweet",
                    },
                    "core": {"user_results": {"result": quoted_user_result}},
                }
            },
        }

    def test_quoted_tweet_uses_core_screen_name(self):
        result = self._base_result({"core": {"screen_name": "quoted_user"}})

        output = format_tweet(result)

        self.assertIn("[引用推文] @quoted_user", output)
        self.assertNotIn("[引用推文] @123456789", output)

    def test_quoted_tweet_falls_back_to_legacy_screen_name(self):
        result = self._base_result({"legacy": {"screen_name": "legacy_user"}})

        output = format_tweet(result)

        self.assertIn("[引用推文] @legacy_user", output)


if __name__ == "__main__":
    unittest.main()
