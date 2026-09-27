"""Worker capabilities: auto-detection merged with capabilities.yaml."""

from worker.app.capabilities import Capabilities


def test_missing_file_uses_detection(tmp_path):
    caps = Capabilities(tmp_path / "absent.yaml").get()
    assert caps["os"] in {"windows", "linux", "macos", "unknown"}
    assert caps["arch"]
    assert caps["tags"] == []
    assert caps["labels"] == {}
    assert "ephemeral" in caps["providers"]


def test_file_extends_detection(tmp_path):
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        "tags: [legacy, mssql]\n"
        "labels:\n  site: office-mtl\n"
        "providers: [ephemeral, git]\n"
        "tools: [my-tool]\n"
        "path_roots:\n  - 'D:\\src'\n",
        encoding="utf-8",
    )
    caps = Capabilities(path).get()
    assert caps["tags"] == ["legacy", "mssql"]
    assert caps["labels"] == {"site": "office-mtl"}
    assert caps["providers"] == ["ephemeral", "git"]
    assert "my-tool" in caps["tools"]
    assert caps["path_roots"] == ["D:\\src"]


def test_file_is_hot_reloaded(tmp_path):
    path = tmp_path / "capabilities.yaml"
    path.write_text("tags: [a]\n", encoding="utf-8")
    caps = Capabilities(path)
    assert caps.get()["tags"] == ["a"]
    # A new mtime/size must be picked up without recreating the object.
    path.write_text("tags: [a, b]\n", encoding="utf-8")
    assert caps.get()["tags"] == ["a", "b"]


def test_invalid_yaml_surfaces_error(tmp_path):
    path = tmp_path / "capabilities.yaml"
    path.write_text("tags: [unterminated\n", encoding="utf-8")
    result = Capabilities(path).get()
    # The worker keeps serving with detected capabilities and reports the error.
    assert "error" in result
    assert "ephemeral" in result["providers"]


def test_detect_resources_reports_cpu_and_memory():
    from worker.app.capabilities import detect_resources

    resources = detect_resources()
    # CPU count is always available from the stdlib; total RAM and speed are
    # best-effort but must never raise (None is an accepted answer).
    assert resources["cpu_count"] is None or resources["cpu_count"] >= 1
    assert resources["ram_total_bytes"] is None or resources["ram_total_bytes"] > 0
    assert resources["cpu_speed_mhz"] is None or resources["cpu_speed_mhz"] > 0
    assert "ram_available_bytes" in resources
    assert "cpu_count_physical" in resources

