from service.sandbox_run import _import_error


def test_import_error_reports_broken_agent(tmp_path):
    (tmp_path / "good.py").write_text("class Agent: pass\n")
    (tmp_path / "broken.py").write_text("class Agent(:\n")
    assert _import_error(tmp_path, "good:Agent") is None
    assert "AttributeError" in _import_error(tmp_path, "good:Missing")
    assert "SyntaxError" in _import_error(tmp_path, "broken:Agent")
