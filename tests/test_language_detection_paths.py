from pathlib import Path

from codewiki.cli.utils.validation import detect_supported_languages


def test_repo_under_excluded_dir_name_is_still_scanned(tmp_path: Path):
    # e.g. a Rust project kept in a folder called "target", or a repo under ".../env/"
    repo = tmp_path / "env" / "target"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("pub fn f() {}\n")
    (repo / "target" / "debug").mkdir(parents=True)          # real build dir inside the repo
    (repo / "target" / "debug" / "gen.rs").write_text("fn g() {}\n")

    assert detect_supported_languages(repo) == [("Rust", 1)]
