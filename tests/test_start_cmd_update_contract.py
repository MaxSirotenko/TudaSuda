from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
START_CMD = ROOT / "start.cmd"
GITIGNORE = ROOT / ".gitignore"
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def read_start() -> str:
    return START_CMD.read_text(encoding="utf-8-sig")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold())


def pos(text: str, fragment: str) -> int:
    index = text.find(fragment.casefold())
    assert index != -1, fragment
    return index


def test_start_cmd_exists_and_is_not_empty():
    assert START_CMD.exists()
    assert read_start().strip()


def test_temp_bootstrap_protects_self_update_before_git_commands():
    text = norm(read_start())
    assert "%~dp0" in text
    assert "start_cmd_project_root" in text
    assert "%temp%" in text
    assert "copy /y \"%~f0\"" in text
    assert "start_cmd_bootstrapped" in text
    assert "call \"%start_cmd_temp%\"" in text
    assert "del \"%start_cmd_temp%\"" in text
    assert "exit /b %start_cmd_exit_code%" in text
    assert pos(text, "copy /y \"%~f0\"") < pos(text, "git status --porcelain")


def test_bootstrap_enters_real_project_root_before_update():
    text = norm(read_start())
    assert "cd /d \"%start_cmd_project_root%\"" in text
    assert pos(text, "cd /d \"%start_cmd_project_root%\"") < pos(text, "call :safe_git_update")


def test_dirty_tree_check_is_fatal_and_precedes_fetch():
    text = norm(read_start())
    dirty = pos(text, "git status --porcelain --untracked-files")
    fetch = pos(text, "git fetch --prune origin main")
    assert dirty < fetch
    assert "git status --short" in text
    assert "auto-update stopped because local changes were detected" in text
    dirty_block = text[pos(text, "if defined git_dirty"):fetch]
    assert "exit /b 1" in dirty_block
    assert "exit /b 0" not in dirty_block


def test_origin_fetch_switch_pull_and_sha_contract():
    text = norm(read_start())
    assert "git remote get-url origin" in text
    assert "git fetch --prune origin main" in text or "git fetch origin main --prune" in text
    assert "call :fail git fetch --prune origin main failed" in text
    assert "refs/remotes/origin/main" in text
    assert "git switch main" in text
    assert "git switch --create main --track origin/main" in text
    assert "git branch --set-upstream-to=origin/main main" in text
    assert "git pull --ff-only origin main" in text
    assert "call :fail git pull --ff-only origin main failed" in text
    assert "git rev-parse head" in text
    assert "git rev-parse refs/remotes/origin/main" in text
    assert "if not \"%local_head%\"==\"%origin_main%\"" in text
    assert "local head does not match origin/main" in text


def test_no_old_silent_fallback_or_forbidden_git_commands():
    text = norm(read_start())
    forbidden = [
        "continuing with local checkout",
        "skipping git pull",
        "git reset",
        "git clean",
        "git stash",
        "git restore",
        "git checkout -f",
        "git switch -f",
        "force push",
    ]
    for item in forbidden:
        assert item not in text


def test_update_happens_before_requirements_and_streamlit():
    text = norm(read_start())
    update = pos(text, "call :safe_git_update")
    requirements = pos(text, "requirements.txt")
    streamlit_run = pos(text, "python -m streamlit run")
    assert update < requirements < streamlit_run
    after_update_call = text[update:requirements]
    assert "if errorlevel 1 exit /b 1" in after_update_call


def test_streamlit_launch_contract_is_preserved():
    text = norm(read_start())
    assert "set \"streamlit_entrypoint=virtual_warehouse_app.py\"" in text
    assert "set \"streamlit_entrypoint=app.py\"" not in text
    assert "streamlit run \"app.py\"" not in text
    assert "--server.port 8501" in text
    assert "call :free_port 8501" in text
    assert "--server.filewatchertype poll" in text
    assert "data\\last_import\\start.log" in text


def test_gitignore_contract():
    lines = GITIGNORE.read_text(encoding="utf-8-sig").splitlines()
    assert "data/performance/" in lines
    assert "data/" not in lines
    assert "data/performance_benchmarks/" in lines
    assert "data/browser_performance_benchmarks/" in lines


def test_no_conflict_markers_in_changed_text_files():
    for path in (START_CMD, GITIGNORE):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            assert line.strip() not in CONFLICT_MARKERS
