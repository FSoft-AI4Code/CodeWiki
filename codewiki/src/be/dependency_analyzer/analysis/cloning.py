import os
import shutil
import tempfile
import subprocess
import stat
import time
from typing import Optional

GIT_EXECUTABLE_PATH = shutil.which("git")


def sanitize_github_url(github_url: str) -> str:
    """
    Sanitize GitHub URL to ensure proper format and remove extra path components.

    Args:
        github_url: Raw GitHub URL or repository path

    Returns:
        str: Sanitized GitHub URL suitable for cloning
    """

    url = github_url.strip()

    protocol = "https://"
    if url.startswith("https://"):
        url = url[8:]
    elif url.startswith("http://"):
        url = url[7:]
        protocol = "http://"

    if url.startswith("www."):
        url = url[4:]

    parts = url.split("/")

    if url.startswith("github.com/"):
        url_parts = url.split("/")
        if len(url_parts) >= 3:
            owner = url_parts[1]
            repo = url_parts[2]
        else:
            return github_url
    elif "/" in url and not url.startswith("github.com"):
        url_parts = url.split("/")
        if len(url_parts) >= 2:
            owner = url_parts[0]
            repo = url_parts[1]
        else:
            return github_url
    else:
        return github_url

    if repo.endswith(".git"):
        repo = repo[:-4]

    return f"{protocol}github.com/{owner}/{repo}"


def clone_repository(github_url: str) -> str:
    """
    Clone a GitHub repository to a temporary directory.

    Args:
        github_url: GitHub repository URL (will be sanitized automatically)

    Returns:
        str: Path to the cloned repository directory

    Raises:
        RuntimeError: If cloning fails or git executable is not found.
    """
    if not GIT_EXECUTABLE_PATH:
        raise RuntimeError(
            "Git executable not found. Please install Git and ensure it is in the system's PATH."
        )

    sanitized_url = sanitize_github_url(github_url)

    temp_dir = tempfile.mkdtemp(prefix="gitprobe_")

    try:
        if os.name == "nt":
            try:
                subprocess.run(
                    [
                        GIT_EXECUTABLE_PATH,
                        "config",
                        "--global",
                        "core.longpaths",
                        "true",
                    ],
                    capture_output=True,
                    text=True,
                )
            except:
                pass

        subprocess.run(
            [
                GIT_EXECUTABLE_PATH,
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                sanitized_url,
                temp_dir,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if os.name == "nt":
            try:
                subprocess.run(
                    [
                        GIT_EXECUTABLE_PATH,
                        "-C",
                        temp_dir,
                        "config",
                        "core.sparseCheckout",
                        "true",
                    ],
                    capture_output=True,
                    text=True,
                )

                sparse_checkout_path = os.path.join(temp_dir, ".git", "info", "sparse-checkout")
                os.makedirs(os.path.dirname(sparse_checkout_path), exist_ok=True)
                with open(sparse_checkout_path, "w") as f:
                    f.write("*\n")
                    f.write("!**/tests/**/CvnF9nAXfESwhrtdkjGhX2wAkKHzwr8N2rjExPK8eZYS/**\n")
                    f.write(
                        "!**/0x0000000000000000000000000000000000000000000000000000000000000002/**\n"
                    )

                subprocess.run(
                    [
                        GIT_EXECUTABLE_PATH,
                        "-C",
                        temp_dir,
                        "read-tree",
                        "-m",
                        "-u",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                )
            except:
                pass
        return temp_dir
    except subprocess.TimeoutExpired:
        if os.path.exists(temp_dir):
            cleanup_repository_safe(temp_dir)
        raise RuntimeError(
            f"Repository cloning timed out after 5 minutes. The repository may be too large or network is slow."
        )
    except subprocess.CalledProcessError as e:
        if os.path.exists(temp_dir):
            cleanup_repository_safe(temp_dir)
        raise RuntimeError(f"Failed to clone repository: {e.stderr}")
    except FileNotFoundError:
        if os.path.exists(temp_dir):
            cleanup_repository_safe(temp_dir)
        raise RuntimeError(
            f"Git executable not found at '{GIT_EXECUTABLE_PATH}'. "
            "Please ensure Git is installed and the path is correct."
        )


def cleanup_repository_safe(repo_dir: str) -> bool:
    """
    Windows-safe removal of the cloned repository directory.
    Handles read-only files and permission issues common on Windows.

    Args:
        repo_dir: Path to the repository directory to remove

    Returns:
        bool: True if cleanup successful, False otherwise
    """

    def handle_remove_readonly(func, path, exc):
        """Error handler for Windows read-only files."""
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE)
            func(path)

    try:
        if os.path.exists(repo_dir):
            if os.name == "nt":
                shutil.rmtree(repo_dir, onerror=handle_remove_readonly)
            else:
                shutil.rmtree(repo_dir)
            return True
        return False
    except PermissionError as e:
        try:
            time.sleep(1)
            if os.path.exists(repo_dir):
                for root, dirs, files in os.walk(repo_dir):
                    for dir in dirs:
                        os.chmod(os.path.join(root, dir), stat.S_IWRITE)
                    for file in files:
                        file_path = os.path.join(root, file)
                        if os.path.exists(file_path):
                            os.chmod(file_path, stat.S_IWRITE)
                shutil.rmtree(repo_dir)
            return True
        except Exception as retry_e:
            print(f"⚠️ Warning: Failed to cleanup {repo_dir} after retry: {str(retry_e)}")
            return False
    except Exception as e:
        print(f"⚠️ Warning: Failed to cleanup {repo_dir}: {str(e)}")
        return False


def cleanup_repository(repo_dir: str) -> bool:
    """
    Remove the cloned repository directory (wrapper for backward compatibility).

    Args:
        repo_dir: Path to the repository directory to remove

    Returns:
        bool: True if cleanup successful, False otherwise
    """
    return cleanup_repository_safe(repo_dir)


def parse_github_url(github_url: str) -> dict:
    """
    Parse GitHub URL to extract owner and repository name.

    Args:
        github_url: GitHub repository URL

    Returns:
        dict: Repository information
    """
    parts = github_url.rstrip("/").split("/")
    if len(parts) >= 2:
        owner = parts[-2]
        name = parts[-1].replace(".git", "")
        return {
            "owner": owner,
            "name": name,
            "full_name": f"{owner}/{name}",
            "url": github_url,
        }
    return {
        "owner": "unknown",
        "name": "unknown",
        "full_name": "unknown",
        "url": github_url,
    }