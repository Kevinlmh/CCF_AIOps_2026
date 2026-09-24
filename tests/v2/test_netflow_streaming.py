import inspect
from pathlib import Path

from baseline.bian.preprocessing.multisource import _iter_netflow_file, _parse_netflow_file
from baseline.bian.preprocessing.observations import ParseStats


FIXTURE = Path("tests/fixtures/multisource/case/xian_window/processed/netflow_5tuple_minute_readable.csv")


def test_v2_netflow_iterator_is_lazy_and_matches_legacy_parser():
    streamed = _iter_netflow_file(FIXTURE, "xian", ("br-1",), ParseStats())
    assert inspect.isgenerator(streamed)
    expected = _parse_netflow_file(FIXTURE, "xian", ("br-1",), ParseStats())
    assert list(streamed) == expected
