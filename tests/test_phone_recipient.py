"""The Phone page never overwrites a recipient number the user typed.

A refresh of the messages list cleared its selection, and the next time the
list got the focus Qt made its first row current, which put that message's
number in the recipient box over the one typed there.  A refresh now keeps
the selected message, and a message picked in the list fills the box only
while the user has not typed in it; Reply and "Message…" still replace it.
"""
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest  # noqa: E402

NOW_MS = int(time.time() * 1000)
SMS = [
    {"type": "1", "date": str(NOW_MS - 60_000), "address": "+15550110", "body": "hello"},
    {"type": "2", "date": str(NOW_MS - 90_000), "address": "+15550111", "body": "on my way"},
]


@pytest.fixture
def panel(qapp):
    from turboadb.gui.phone_panel import PhonePanel

    page = PhonePanel(object())
    page._loaded = True  # no device queries: the lists are filled by the test
    page.resize(900, 700)
    page.show()
    page.show_page(page.PAGE_MESSAGES)
    page._fill_sms(SMS)
    qapp.processEvents()
    yield page
    page.close_panel()
    page.hide()
    page.deleteLater()
    qapp.processEvents()


def _type(qapp, box, text):
    box.setFocus()
    box.selectAll()
    QTest.keyClicks(box, text)
    qapp.processEvents()


def _focus(qapp, widget):
    widget.setFocus()
    qapp.processEvents()


def test_a_typed_number_survives_a_refresh_and_the_list_getting_the_focus(qapp, panel):
    panel.sms.setCurrentRow(1)  # a message picked first
    assert panel.sms_to.text() == "+15550111"
    _type(qapp, panel.sms_to, "+4912345")
    panel._fill_sms(SMS)  # a refresh lands
    _focus(qapp, panel.sms)
    panel.sms.setCurrentRow(0)  # even a click on another message
    assert panel.sms_to.text() == "+4912345"


def test_a_picked_message_stays_picked_across_a_refresh(qapp, panel):
    panel.sms.setCurrentRow(1)
    panel._fill_sms(list(SMS))
    _focus(qapp, panel.sms)
    assert panel.sms.currentRow() == 1
    assert panel.sms_to.text() == "+15550111"


def test_reply_and_an_emptied_box_take_a_messages_number_again(qapp, panel):
    _type(qapp, panel.sms_to, "+4912345")
    panel._reply_item(panel.sms.item(0))  # Reply: the user asked for this number
    assert panel.sms_to.text() == "+15550110"
    _type(qapp, panel.sms_to, "+4912345")
    panel.sms_to.selectAll()
    QTest.keyClick(panel.sms_to, "\b")  # emptied by the user
    qapp.processEvents()
    assert panel.sms_to.text() == ""
    panel.sms.setCurrentRow(1)
    assert panel.sms_to.text() == "+15550111"
