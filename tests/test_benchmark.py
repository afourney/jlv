from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


@unittest.skipIf(sys.platform == "win32", "benchmark uses POSIX resource accounting")
class BenchmarkMemoryTests(unittest.TestCase):
    def test_peak_rss_units_match_platform(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "benchmark.py"
        spec = importlib.util.spec_from_file_location("jlv_benchmark", path)
        benchmark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(benchmark)
        for platform, reported_rss in (("darwin", 64 * 1024 * 1024), ("linux", 64 * 1024)):
            with (
                self.subTest(platform=platform),
                patch.object(benchmark.sys, "platform", platform),
                patch.object(
                    benchmark.resource, "getrusage", return_value=Mock(ru_maxrss=reported_rss)
                ) as usage,
            ):
                self.assertEqual(benchmark.peak_rss_mib(), 64.0)
                usage.assert_called_once_with(benchmark.resource.RUSAGE_SELF)
