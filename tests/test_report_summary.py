"""The report summary table (the in-app Summary tab and the HTML and PNG
exports): values with colons of their own (a build fingerprint, a date, an
address) stay whole in the value column, and a kernel line stays text."""

import re

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui.report_dialog import report_html, summary_html  # noqa: E402

_PALETTE = dict.fromkeys(
    ("background", "surface", "header", "foreground", "muted", "border", "accent"), "#101010"
)
_FINGERPRINT = "google/raven/raven:13/TQ1A.230105.002/9325679:user/release-keys"
_KERNEL = "Linux localhost 5.10.43-android12-9 #1 SMP PREEMPT Mon Jan 2 12:00:00 UTC 2023 aarch64"


def _rows(html):
    return re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", html)


def _build_report():
    """What ADBHandler.build_report writes above its raw dump."""
    return "\n".join([
        "TurboADB build report",
        "Captured: 2023-01-02 12:00:00",
        "ADB serial: 192.168.1.5:5555",
        "",
        "Android build",
        "-------------",
        f"{'ro.build.fingerprint':34} {_FINGERPRINT}",
        f"{'ro.build.date':34} Mon Jan  2 12:00:00 UTC 2023",
        f"{'ro.build.version.release':34} 13",
        "",
        "Kernel",
        "------",
        _KERNEL,
    ])


def test_values_with_colons_stay_in_the_value_column():
    html = summary_html("Device build report", _build_report(), _PALETTE)
    assert _rows(html) == [
        ("Captured", "2023-01-02 12:00:00"),
        ("ADB serial", "192.168.1.5:5555"),
        ("ro.build.fingerprint", _FINGERPRINT),
        ("ro.build.date", "Mon Jan  2 12:00:00 UTC 2023"),
        ("ro.build.version.release", "13"),
    ]
    assert f"<p>{_KERNEL}</p>" in html
    assert "<h2>Kernel</h2>" in html


def test_a_kernel_line_with_a_double_space_stays_text():
    # A build date on the 1st to the 9th has two spaces before the day.
    kernel = "Linux localhost 4.14.190-perf #1 SMP PREEMPT Thu Jul  1 10:00:00 CST 2021 aarch64"
    html = summary_html("Device build report", f"Kernel\n------\n{kernel}", _PALETTE)
    assert _rows(html) == []
    assert f"<p>{kernel}</p>" in html


def test_label_rows_still_split_at_their_colon():
    summary = "\n".join([
        "Summary",
        "-------",
        "Battery: 80% (charging)",
        "CPU load: 12%",
        "Uptime: up 3 days, 4:05",
        "Current Battery Service state:",
        "  AC powered: false",
    ])
    assert _rows(summary_html("Device health report", summary, _PALETTE)) == [
        ("Battery", "80% (charging)"),
        ("CPU load", "12%"),
        ("Uptime", "up 3 days, 4:05"),
        ("Current Battery Service state", ""),
        ("AC powered", "false"),
    ]


def test_the_html_export_has_the_same_rows(monkeypatch):
    from turboadb.gui import report_dialog

    monkeypatch.setattr(report_dialog, "_report_palette", lambda: _PALETTE)
    page = report_html("Device build report", _build_report(), "[ro.build.date]: [x]")
    assert ("ro.build.fingerprint", _FINGERPRINT) in _rows(page)
