"""
CodeWiki: Transform codebases into comprehensive documentation using AI-powered analysis.

This package provides a CLI tool for generating documentation from code repositories,
and an MCP server for IDE-driven documentation generation.
"""

__version__ = "2.0.2"
__author__ = "CodeWiki Contributors"
__license__ = "MIT"

__all__ = ["__version__"]


def _use_system_certificates() -> None:
    """Verify HTTPS against the OS certificate store instead of certifi's bundle.

    Corporate networks that inspect TLS re-sign traffic with a root CA that IT
    installs in the OS store (which browsers and curl trust) but that Python's
    bundled certifi list lacks, so LLM calls fail with CERTIFICATE_VERIFY_FAILED.
    truststore makes every ssl context (httpx/OpenAI, requests, urllib) consult
    the OS store. Set CODEWIKI_NO_SYSTEM_CERTS=1 to keep certifi's bundle.
    """
    import os

    if os.environ.get("CODEWIKI_NO_SYSTEM_CERTS", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 — fall back to the default certificate bundle
        pass


_use_system_certificates()
