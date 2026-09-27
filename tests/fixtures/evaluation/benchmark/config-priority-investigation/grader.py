import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

WORKSPACE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


class ConfigurationPriority(unittest.TestCase):
    def test_higher_priority_sources_win_and_unset_values_survive(self):
        from bootstrap import resolve_settings
        defaults = {"APP_MODE": "development", "APP_RETRIES": "3", "APP_TIMEOUT": "30"}
        project = {"APP_MODE": "staging", "APP_RETRIES": "5"}
        environment = {"APP_MODE": "production", "APP_TIMEOUT": "7"}
        self.assertEqual(
            resolve_settings(defaults, project, environment),
            {"APP_MODE": "production", "APP_RETRIES": "5", "APP_TIMEOUT": "7"},
        )

    def test_project_configuration_overrides_defaults(self):
        from bootstrap import resolve_settings
        self.assertEqual(
            resolve_settings({"LOG_LEVEL": "warning"}, {"LOG_LEVEL": "info"}, {}),
            {"LOG_LEVEL": "info"},
        )

    def test_environment_can_add_a_setting(self):
        from bootstrap import resolve_settings
        self.assertEqual(
            resolve_settings({}, {}, {"APP_TIMEOUT": "7"}),
            {"APP_TIMEOUT": "7"},
        )


stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ConfigurationPriority)
    )
print(json.dumps({"passed": result.wasSuccessful(), "detail": stream.getvalue()[-4000:]}, ensure_ascii=False))
