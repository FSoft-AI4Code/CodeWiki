"""Windows compatibility for the subscription backend dependency."""

import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific compatibility test")
def test_caw_imports_without_posix_fcntl():
    from codewiki.src.be.caw_compat import CawAgent, ToolGroup

    assert CawAgent is not None
    assert ToolGroup is not None
