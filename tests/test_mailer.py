"""mailer.fetch_new_emails — new-mail detection by IMAP UID.

Regression this guards: the poller used to compare IMAP *sequence numbers*
against a stored "seen" list. A sequence number is a position in the mailbox,
so it shifts every time anything is deleted or archived; in a mailbox with
tens of thousands of messages the newest letters kept landing on numbers that
were already stored, and every poll reported "nothing new" forever.
"""

import email.message

import pytest

from src import mailer

ACCOUNT = {"email": "me@example.com", "password": "pw", "provider": "yandex"}


def _raw(uid: int) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Boss <boss@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = f"Письмо {uid}"
    msg["Date"] = "Mon, 14 Sep 2026 10:00:00 +0300"
    msg.set_content(f"тело письма {uid}")
    return msg.as_bytes()


class FakeIMAP:
    """Only what fetch_new_emails uses: UID SEARCH and UID FETCH."""

    def __init__(self, all_uids, unseen_uids):
        self.all = list(all_uids)
        self.unseen = list(unseen_uids)
        self.fetched = []
        self.readonly = None
        self.validity = b"1"

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.all)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [self.validity]

    def uid(self, command, *args):
        if command == "search" and args == ("ALL",):
            return "OK", [" ".join(str(u) for u in self.all).encode()]
        if command == "search" and args == ("UNSEEN",):
            return "OK", [" ".join(str(u) for u in self.unseen).encode()]
        if command == "fetch":
            uid = int(args[0])
            self.fetched.append(args[0])
            return "OK", [(("1 (UID %d BODY[] {1}" % uid).encode(), _raw(uid)), b")"]
        raise AssertionError(f"unexpected uid call: {command} {args}")

    def search(self, charset, *criteria):
        raise AssertionError("sequence numbers must not be used, only UIDs")

    def logout(self):
        return "BYE", b"ok"


@pytest.fixture()
def fake(monkeypatch):
    def _install(all_uids, unseen_uids):
        conn = FakeIMAP(all_uids, unseen_uids)
        monkeypatch.setattr(mailer, "_connect", lambda account: conn)
        return conn

    return _install


def test_first_run_only_sets_a_baseline(fake):
    # Never polled: history must not be triaged, the mailbox end becomes the
    # baseline. Otherwise a fresh install spends its first polls reading years
    # of old unread mail through the LLM.
    conn = fake(range(1, 101), [99, 100])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, None, limit=20)
    assert msgs == [] and high == 100
    assert conn.fetched == []


def test_uidvalidity_change_rebases_without_fetching_old_mail(fake):
    conn = fake([1, 2], [1, 2])
    conn.validity = b"2"
    messages, high, validity = mailer.fetch_new_emails(ACCOUNT, 50000, 20, "1")
    assert messages == [] and high == 2 and validity == "2"
    conn.all.append(3)
    conn.unseen.append(3)
    messages, high, validity = mailer.fetch_new_emails(ACCOUNT, high, 20, validity)
    assert [m["uid"] for m in messages] == ["3"] and high == 3


def test_unknown_uidvalidity_fails_instead_of_guessing_namespace(fake):
    conn = fake([1], [1])
    conn.validity = None
    with pytest.raises(ConnectionError, match="UIDVALIDITY"):
        mailer.fetch_new_emails(ACCOUNT, 0)


def test_only_unseen_uids_above_the_baseline_are_returned(fake):
    fake(range(1, 106), [101, 102, 105])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=20)
    assert [m["uid"] for m in msgs] == ["101", "102", "105"]
    assert msgs[0]["subject"] == "Письмо 101"
    assert msgs[0]["from_addr"] == "boss@example.com"
    assert high == 105


def test_server_receive_date_takes_precedence_over_sender_header(fake, monkeypatch):
    conn = fake([1], [1])
    original = conn.uid
    def uid(command, *args):
        if command == 'fetch':
            assert 'INTERNALDATE' in args[1] and 'BODY.PEEK' in args[1]
            return 'OK', [(b'1 (INTERNALDATE "21-Oct-2026 12:00:00 +0500" BODY[] {1}', _raw(1)), b')']
        return original(command, *args)
    monkeypatch.setattr(conn, 'uid', uid)
    from datetime import datetime
    messages, _, _ = mailer.fetch_new_emails(ACCOUNT, 0)
    assert messages[0]['ts'] == datetime.fromisoformat('2026-10-21T12:00:00+05:00').timestamp()
    assert messages[0]['date_ts'] < messages[0]['ts']


def test_limit_keeps_the_oldest_and_leaves_the_rest_for_next_poll(fake):
    fake(range(1, 106), [101, 102, 103, 104, 105])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=2)
    assert [m["uid"] for m in msgs] == ["101", "102"]
    assert high == 102          # 103..105 are still above the baseline


def test_letters_read_elsewhere_are_not_fetched(fake):
    # 101 and 102 were read on the phone before the poll ran.
    fake(range(1, 106), [103])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=20)
    assert [m["uid"] for m in msgs] == ["103"] and high == 103


def test_nothing_new_leaves_the_baseline_untouched(fake):
    fake(range(1, 106), [101, 102])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 102, limit=20)
    assert msgs == [] and high == 102


def test_empty_mailbox_baseline_is_zero(fake):
    fake([], [])
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, None, limit=20)
    assert msgs == [] and high == 0


def test_inbox_is_selected_readonly(fake):
    # BODY.PEEK + readonly select: the plugin must never mark anything read.
    conn = fake([1], [1])
    mailer.fetch_new_emails(ACCOUNT, None)
    assert conn.readonly is True


def test_rewind_is_bounded_by_how_many_letters_were_asked(fake):
    # 20k unread letters must never become 20k LLM calls, whatever `back` says.
    fake(range(1, 20_001), list(range(1, 20_001)))
    assert mailer.rewind_baseline(ACCOUNT, 20) == (19_980, "1")
    assert mailer.rewind_baseline(ACCOUNT, 1000) == (19_900, "1")   # clamped to 100


def test_rewind_with_fewer_unread_than_asked_takes_them_all(fake):
    fake(range(1, 101), [10, 11, 12])
    assert mailer.rewind_baseline(ACCOUNT, 20) == (9, "1")


def test_rewind_on_a_mailbox_without_unread_returns_none(fake):
    fake(range(1, 101), [])
    assert mailer.rewind_baseline(ACCOUNT, 20) == (None, "1")


def test_a_letter_that_fails_to_fetch_is_retried_then_skipped(monkeypatch):
    # 101 ok, 102 fails on every attempt, 103 ok: the poller must neither
    # stall on the poison letter (the "no new mail" bug again) nor lose the
    # letters after it.
    class FlakyIMAP(FakeIMAP):
        def __init__(self):
            super().__init__([101, 102, 103], [101, 102, 103])
            self.fetch_attempts = {}

        def uid(self, command, *args):
            if command == "fetch":
                uid = int(args[0])
                self.fetch_attempts[uid] = self.fetch_attempts.get(uid, 0) + 1
                if uid == 102:
                    raise OSError("fetch glitch")
                return super().uid(command, *args)
            return super().uid(command, *args)

    conn = FlakyIMAP()
    monkeypatch.setattr(mailer, "_connect", lambda account: conn)
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=10)
    assert [m["uid"] for m in msgs] == ["101", "103"]   # 102 skipped...
    assert conn.fetch_attempts[102] == 2                # ...after a retry
    assert high == 103                                   # and the queue flows


def test_a_transient_fetch_glitch_is_covered_by_the_retry(monkeypatch):
    # Fails once, succeeds on the retry: the letter must NOT be lost.
    class OnceIMAP(FakeIMAP):
        def __init__(self):
            super().__init__([101, 102], [101, 102])
            self.failed_once = False

        def uid(self, command, *args):
            if command == "fetch" and int(args[0]) == 102 and not self.failed_once:
                self.failed_once = True
                raise OSError("transient")
            return super().uid(command, *args)

    conn = OnceIMAP()
    monkeypatch.setattr(mailer, "_connect", lambda account: conn)
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=10)
    assert [m["uid"] for m in msgs] == ["101", "102"]
    assert high == 102


def test_html_style_and_script_do_not_leak_into_the_body(monkeypatch):
    msg = email.message.EmailMessage()
    msg["From"] = "Boss <boss@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = "s"
    msg.set_content(
        "<html><head><style>p{color:red}</style></head>"
        "<body><!-- hidden --><p>Настоящий текст письма</p></body></html>",
        subtype="html")
    raw = msg.as_bytes()

    class HtmlIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == "fetch":
                return "OK", [(b"1 (UID 5 BODY[] {1}", raw), b")"]
            return super().uid(command, *args)

    conn = HtmlIMAP([5], [5])
    monkeypatch.setattr(mailer, "_connect", lambda account: conn)
    msgs, _, _ = mailer.fetch_new_emails(ACCOUNT, 0, limit=5)
    assert msgs[0]["body"] == "Настоящий текст письма"


def test_poison_letter_at_limit_one_does_not_block_later_mail(monkeypatch):
    class PoisonIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == "fetch" and args[0] == "101":
                raise OSError("unreadable letter")
            return super().uid(command, *args)

    conn = PoisonIMAP([101, 102], [101, 102])
    monkeypatch.setattr(mailer, "_connect", lambda account: conn)
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, 100, limit=1)
    assert msgs == [] and high == 101
    msgs, high, _ = mailer.fetch_new_emails(ACCOUNT, high, limit=1)
    assert [m["uid"] for m in msgs] == ["102"] and high == 102


def test_failed_search_is_not_treated_as_an_empty_mailbox(monkeypatch):
    class BrokenIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == "search":
                return "NO", [b"server failure"]
            return super().uid(command, *args)

    monkeypatch.setattr(mailer, "_connect", lambda account: BrokenIMAP([], []))
    with pytest.raises(ConnectionError):
        mailer.fetch_new_emails(ACCOUNT, None)
