import os
import tempfile
import unittest
from pathlib import Path

from skills import env_config

SAMPLE_ENV = """# 顶部注释，别动
BGI_EXE=BetterGI.exe
# 敌人与魔物路线组
BGI_ENEMY_CONFIG_NAME=敌人与魔物.json
LLM_PROVIDER=openai

DEFAULT_UID=100000000
OPENAI_API_KEY=
"""


class ParseTests(unittest.TestCase):
    def test_values_are_parsed_and_comments_ignored(self):
        values = env_config.env_values(SAMPLE_ENV)

        self.assertEqual(values["BGI_EXE"], "BetterGI.exe")
        self.assertEqual(values["DEFAULT_UID"], "100000000")
        self.assertEqual(values["OPENAI_API_KEY"], "")
        self.assertNotIn("# 顶部注释，别动", values)

    def test_export_and_quotes_are_supported(self):
        text = 'export FOO="a b"\nBAR=\'c d\'\nBAZ=plain\n'

        values = env_config.env_values(text)

        self.assertEqual(values, {"FOO": "a b", "BAR": "c d", "BAZ": "plain"})

    def test_last_duplicate_wins(self):
        self.assertEqual(env_config.env_values("A=1\nA=2\n")["A"], "2")


class UpdateTests(unittest.TestCase):
    def test_existing_key_is_replaced_in_place(self):
        updated = env_config.update_env_text(SAMPLE_ENV, {"LLM_PROVIDER": "github"})

        self.assertIn("LLM_PROVIDER=github", updated)
        self.assertNotIn("LLM_PROVIDER=openai", updated)
        # 其它行一字不动（注释、空行、顺序全部保留）
        self.assertEqual(len(updated.splitlines()), len(SAMPLE_ENV.splitlines()))
        before = [line for line in SAMPLE_ENV.splitlines() if "LLM_PROVIDER" not in line]
        after = [line for line in updated.splitlines() if "LLM_PROVIDER" not in line]
        self.assertEqual(before, after)

    def test_missing_key_is_appended_under_a_section(self):
        updated = env_config.update_env_text(SAMPLE_ENV, {"BGI_HOE_CONFIG_NAME": "锄大地.json"})

        self.assertIn(env_config.APPEND_SECTION, updated)
        self.assertTrue(updated.rstrip().endswith("BGI_HOE_CONFIG_NAME=锄大地.json"))
        self.assertIn("DEFAULT_UID=100000000", updated)

    def test_export_prefix_is_kept(self):
        updated = env_config.update_env_text("export FOO=1\n", {"FOO": "2"})

        self.assertEqual(updated.strip(), "export FOO=2")

    def test_round_trip_keeps_every_original_line(self):
        updated = env_config.update_env_text(SAMPLE_ENV, {"BGI_EXE": "New.exe"})

        for line in SAMPLE_ENV.splitlines():
            if not line.startswith("BGI_EXE="):
                self.assertIn(line, updated.splitlines())


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / ".env"
        self.path.write_text(SAMPLE_ENV, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_writes_values_and_keeps_a_backup(self):
        backup = env_config.save_env(str(self.path), {"DEFAULT_UID": "123456789"})

        self.assertIsNotNone(backup)
        self.assertTrue(os.path.isfile(backup))
        self.assertIn("DEFAULT_UID=123456789", self.path.read_text(encoding="utf-8"))
        # 备份里必须还是老值，否则回不去
        self.assertIn("DEFAULT_UID=100000000", Path(backup).read_text(encoding="utf-8"))

    def test_save_creates_the_file_when_missing(self):
        missing = Path(self.temp_dir.name) / "sub" / ".env"

        backup = env_config.save_env(str(missing), {"DEFAULT_UID": "1"})

        self.assertIsNone(backup)
        self.assertEqual(env_config.load_env(str(missing))["DEFAULT_UID"], "1")

    def test_no_tmp_file_is_left_behind(self):
        env_config.save_env(str(self.path), {"DEFAULT_UID": "1"})

        leftovers = [name for name in os.listdir(self.temp_dir.name) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_load_env_of_missing_file_is_empty(self):
        self.assertEqual(env_config.load_env(str(Path(self.temp_dir.name) / "nope")), {})


class FieldSchemaTests(unittest.TestCase):
    def test_every_field_key_is_documented_in_env_example(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        text = example.read_text(encoding="utf-8")

        missing = [key for key in env_config.ALL_FIELDS if f"{key}=" not in text]

        self.assertEqual(missing, [], f"这些键在 .env.example 里没有：{missing}")

    def test_field_kinds_are_known(self):
        allowed = {"text", "secret", "int", "bool", "choice", "path"}

        for key, field in env_config.ALL_FIELDS.items():
            self.assertIn(field.kind, allowed, key)
            if field.kind == "choice":
                self.assertTrue(field.choices, key)
                self.assertIn(field.default, field.choices, key)


class ValidateTests(unittest.TestCase):
    def test_mys_cookie_must_look_complete(self):
        """缺键的 cookie 会一路"看起来配好了"，实际每次都报未登录/风控 —— 保存时就该提醒。"""
        field = env_config.ALL_FIELDS["MYS_COOKIE"]

        self.assertEqual(
            env_config.validate_value(field, "ltuid=123456789; ltoken=abc; account_id=123456789"), ""
        )
        self.assertIn("ltuid", env_config.validate_value(field, "ltoken=abc"))
        self.assertIn("ltoken", env_config.validate_value(field, "ltuid=123456789"))
        # 留空 = 不启用，不该报任何问题
        self.assertEqual(env_config.validate_value(field, ""), "")

    def test_mys_cookie_field_is_a_password_field(self):
        field = env_config.ALL_FIELDS["MYS_COOKIE"]

        self.assertEqual(field.kind, "secret")
        self.assertIn("MYS_COOKIE", env_config.ALL_FIELDS)

    def test_int_field_rejects_non_numbers(self):
        field = env_config.ALL_FIELDS["MAX_HISTORY_MESSAGES"]

        self.assertIn("整数", env_config.validate_value(field, "abc"))
        self.assertIn("正整数", env_config.validate_value(field, "0"))
        self.assertEqual(env_config.validate_value(field, "20"), "")

    def test_bool_field_only_accepts_zero_or_one(self):
        field = env_config.ALL_FIELDS["BGI_AUTO_CREATE_ROUTE_GROUP"]

        self.assertIn("0 或 1", env_config.validate_value(field, "yes"))
        self.assertEqual(env_config.validate_value(field, "1"), "")

    def test_choice_field_warns_about_unknown_values(self):
        field = env_config.ALL_FIELDS["BGI_ROUTE_GROUP_POLICY"]

        self.assertIn("不在推荐值里", env_config.validate_value(field, "delete-everything"))
        self.assertEqual(env_config.validate_value(field, "shrink"), "")

    def test_path_field_warns_when_missing(self):
        field = env_config.ALL_FIELDS["BGI_DIR"]

        self.assertIn("不存在", env_config.validate_value(field, r"Z:\nope\nope"))
        self.assertEqual(env_config.validate_value(field, ""), "")

    def test_validate_values_skips_unknown_keys(self):
        self.assertEqual(env_config.validate_values({"NOT_IN_SCHEMA": "x"}), [])


class MaskTests(unittest.TestCase):
    def test_secret_is_masked_but_recognisable(self):
        masked = env_config.mask_secret("sk-1234567890abcdef")

        self.assertIn(env_config.SECRET_MASK, masked)
        self.assertTrue(masked.startswith("sk-"))
        self.assertTrue(masked.endswith("def"))

    def test_short_and_empty_values(self):
        self.assertEqual(env_config.mask_secret(""), "")
        self.assertEqual(env_config.mask_secret("abc"), env_config.SECRET_MASK)


if __name__ == "__main__":
    unittest.main()
