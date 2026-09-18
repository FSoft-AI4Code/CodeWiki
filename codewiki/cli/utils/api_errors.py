"""
LLM API error handling utilities with fail-fast behavior.
"""

from typing import Optional
import click

from codewiki.cli.utils.errors import APIError


class APIErrorHandler:
    """Handler for LLM API errors with fail-fast behavior."""

    @staticmethod
    def handle_api_error(
        error: Exception, context: Optional[str] = None, fail_fast: bool = True
    ) -> APIError:
        """
        Handle LLM API error and convert to APIError.

        Args:
            error: The original exception
            context: Additional context (e.g., module name)
            fail_fast: Whether to fail immediately (default: True)

        Returns:
            APIError instance
        """
        error_message = str(error)

        # Detect specific error types
        if "429" in error_message or "rate limit" in error_message.lower():
            message = (
                "LLM API rate limit exceeded.\n\n"
                "The API returned a 429 error, indicating too many requests.\n\n"
                "Troubleshooting:\n"
                "  1. Wait a few minutes before retrying\n"
                "  2. Check your API quota at your provider's dashboard\n"
                "  3. Consider upgrading your API plan\n"
                "  4. For large repositories, generate during off-peak hours"
            )
        elif "401" in error_message or "authentication" in error_message.lower():
            message = (
                "LLM API authentication failed.\n\n"
                "Your API key appears to be invalid or expired.\n\n"
                "Troubleshooting:\n"
                "  1. Verify your API key: codewiki config show\n"
                "  2. Update your API key: codewiki config set --api-key <new-key>\n"
                "  3. Check that your API key is active in your provider's dashboard"
            )
        elif "timeout" in error_message.lower():
            message = (
                "LLM API request timed out.\n\n"
                "The API did not respond within the expected time.\n\n"
                "Troubleshooting:\n"
                "  1. Check your internet connection\n"
                "  2. Verify the API service is operational\n"
                "  3. Try again in a few moments\n"
                "  4. If the issue persists, contact your API provider"
            )
        elif "network" in error_message.lower() or "connection" in error_message.lower():
            message = (
                "Network error while connecting to LLM API.\n\n"
                "Could not establish connection to the API.\n\n"
                "Troubleshooting:\n"
                "  1. Check your internet connection\n"
                "  2. Verify the base URL: codewiki config show\n"
                "  3. Check if you're behind a proxy or firewall\n"
                "  4. Try: curl -I <base-url> to test connectivity"
            )
        else:
            message = (
                f"LLM API error: {error_message}\n\n"
                "An unexpected error occurred while communicating with the LLM API.\n\n"
                "Troubleshooting:\n"
                "  1. Check your configuration: codewiki config validate\n"
                "  2. Verify API service status\n"
                "  3. Review the error message above for specific details"
            )

        if context:
            message = f"Context: {context}\n\n{message}"

        return APIError(message)

    @staticmethod
    def display_api_error(error: APIError, module_name: Optional[str] = None):
        """
        Display API error with formatting.

        Args:
            error: The API error
            module_name: Optional module name for context
        """
        click.echo()
        click.secho("✗ LLM API Error", fg="red", bold=True)
        click.echo()

        if module_name:
            click.echo(f"Module: {module_name}")
            click.echo()

        click.echo(error.message)
        click.echo()
        click.secho("Documentation generation stopped. No partial results saved.", fg="yellow")
        click.echo()


def wrap_api_call(func, *args, fail_fast: bool = True, context: Optional[str] = None, **kwargs):
    """
    Wrap an API call with error handling.

    Args:
        func: Function to call
        *args: Positional arguments
        fail_fast: Whether to raise on error (default: True)
        context: Optional context for error message
        **kwargs: Keyword arguments

    Returns:
        Function result

    Raises:
        APIError: If API call fails and fail_fast is True
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        api_error = APIErrorHandler.handle_api_error(e, context=context, fail_fast=fail_fast)
        if fail_fast:
            raise api_error
        else:
            APIErrorHandler.display_api_error(api_error)
            return None
