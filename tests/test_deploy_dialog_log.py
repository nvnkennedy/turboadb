"""The deploy dialog's status lines are read as the log panel reads them.

It stripped the level tag with a pattern of its own that knew four tags, so
"[WARN] …" or "[DEBUG] …" kept its tag in the dialog while the log panel, the
status bar and the toasts (log_panel.classify) read it.
"""
import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import deploy_dialog  # noqa: E402
from turboadb.gui import settings as settings_mod  # noqa: E402
from turboadb.gui.log_panel import classify  # noqa: E402


@pytest.fixture
def dialog(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings_mod, "_cache", None)
    monkeypatch.setattr(settings_mod, "deploy_password", lambda: "")
    monkeypatch.setattr(settings_mod, "set_deploy_password", lambda value: None)
    dlg = deploy_dialog.DeployDialog()
    if dlg._pw_load is not None:
        dlg._pw_load.wait(5000)
    yield dlg
    dlg.deleteLater()


@pytest.mark.parametrize("line, shown", [
    ("[OK] pc1: serve started", "pc1: serve started"),
    ("[ERROR] pc2: WinRM failed: timeout", "pc2: WinRM failed: timeout"),
    ("[WARN] pc3: pip upgrade of turboadb failed", "pc3: pip upgrade of turboadb failed"),
    ("[DEBUG] $ winrm run_ps", "$ winrm run_ps"),
    ("[CANCELLED] deploy to pc4", "Cancelled: deploy to pc4"),
    ("3 host(s), 3 at a time (600s budget).", "3 host(s), 3 at a time (600s budget)."),
])
def test_a_status_line_is_shown_without_its_tag(dialog, line, shown):
    dialog.status.clear()
    dialog._append(line)
    assert dialog.status.toPlainText() == shown == classify(line)[1]
