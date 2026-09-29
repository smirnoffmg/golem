import hashlib
import json

import pytest

from golem.proposal_payload import (
    MAX_PAGE_BODY,
    MAX_REPLY,
    ProposalError,
    canonical_json,
    payload_digest,
    payload_of,
    summary_of,
)

FILES = {
    "pages/home.xhtml": "<p>New body</p>",
    "replies/sd-12.txt": "Hello, the export works again.",
    "issues/desc.md": "Errors in export since 10:00.",
    "issues/comment.md": "Seen again at 12:00.",
}


def read(path: str) -> str:
    try:
        return FILES[path]
    except KeyError:
        raise FileNotFoundError(path) from None


WIKI = {
    "kind": "wiki_edit",
    "page_id": "123456",
    "title": "Home",
    "version": 7,
    "body_file": "pages/home.xhtml",
}
REPLY = {"kind": "desk_reply", "request": "SD-12", "public": True, "text_file": "replies/sd-12.txt"}
ISSUE = {
    "kind": "tracker_issue",
    "action": "create",
    "project": "CORSAR",
    "issue_type": "Bug",
    "summary": "Export fails",
    "description_file": "issues/desc.md",
}
COMMENT = {
    "kind": "tracker_issue",
    "action": "comment",
    "issue": "CORSAR-12",
    "comment_file": "issues/comment.md",
}


def test_a_wiki_edit_carries_the_page_it_read_and_the_new_body():
    assert payload_of("wiki_edit", WIKI, read) == {
        "page_id": "123456",
        "title": "Home",
        "version": 7,
        "body": "<p>New body</p>",
    }


def test_a_desk_reply_carries_the_request_the_text_and_its_visibility():
    assert payload_of("desk_reply", REPLY, read) == {
        "request": "SD-12",
        "public": True,
        "text": "Hello, the export works again.",
    }


def test_a_tracker_issue_is_a_new_issue_or_a_comment():
    assert payload_of("tracker_issue", ISSUE, read) == {
        "action": "create",
        "project": "CORSAR",
        "issue_type": "Bug",
        "summary": "Export fails",
        "description": "Errors in export since 10:00.",
    }
    assert payload_of("tracker_issue", COMMENT, read) == {
        "action": "comment",
        "issue": "CORSAR-12",
        "comment": "Seen again at 12:00.",
    }


def test_the_file_must_name_the_catalogs_kind():
    with pytest.raises(ProposalError, match="kind"):
        payload_of("desk_reply", WIKI, read)


@pytest.mark.parametrize(
    "change",
    [
        {"page_id": "12a"},
        {"page_id": 123},
        {"version": 0},
        {"version": True},
        {"version": 2**53},
        {"title": ""},
        {"title": "x" * 256},
        {"extra": "field"},
    ],
)
def test_a_malformed_wiki_edit_is_refused(change):
    with pytest.raises(ProposalError):
        payload_of("wiki_edit", WIKI | change, read)


@pytest.mark.parametrize(
    "change",
    [
        {"request": "sd-12"},
        {"request": "SD12"},
        {"public": "yes"},
    ],
)
def test_a_malformed_desk_reply_is_refused(change):
    with pytest.raises(ProposalError):
        payload_of("desk_reply", REPLY | change, read)


def test_a_desk_reply_must_say_whether_the_customer_reads_it():
    reply = dict(REPLY)
    del reply["public"]
    with pytest.raises(ProposalError, match="public"):
        payload_of("desk_reply", reply, read)


@pytest.mark.parametrize(
    "manifest",
    [
        ISSUE | {"action": "delete"},
        ISSUE | {"project": "corsar"},
        ISSUE | {"summary": "x" * 256},
        ISSUE | {"summary": "two\nlines"},
        COMMENT | {"issue": "CORSAR"},
        {**COMMENT, "project": "CORSAR"},
    ],
)
def test_a_malformed_tracker_issue_is_refused(manifest):
    with pytest.raises(ProposalError):
        payload_of("tracker_issue", manifest, read)


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "pages/../../x", ""])
def test_a_body_file_stays_inside_the_repository(path):
    with pytest.raises(ProposalError, match="file"):
        payload_of("wiki_edit", WIKI | {"body_file": path}, read)


def test_a_body_file_may_be_required_under_the_roles_directory():
    payload_of("wiki_edit", WIKI, read, under="pages")
    with pytest.raises(ProposalError, match="pages"):
        payload_of("desk_reply", REPLY, read, under="pages")


def test_a_missing_body_file_is_refused():
    with pytest.raises(ProposalError, match="missing"):
        payload_of("wiki_edit", WIKI | {"body_file": "pages/gone.xhtml"}, read)


def test_a_page_body_over_the_limit_is_refused_rather_than_cut():
    # A cut body proposed back would delete the rest of the page (ADR 0015).
    files = {"pages/home.xhtml": "x" * (MAX_PAGE_BODY + 1)}
    with pytest.raises(ProposalError, match="200000"):
        payload_of("wiki_edit", WIKI, files.__getitem__)


def test_a_reply_over_the_limit_is_refused():
    files = {"replies/sd-12.txt": "x" * (MAX_REPLY + 1)}
    with pytest.raises(ProposalError, match="30000"):
        payload_of("desk_reply", REPLY, files.__getitem__)


def test_an_empty_body_is_refused():
    with pytest.raises(ProposalError, match="empty"):
        payload_of("desk_reply", REPLY, {"replies/sd-12.txt": "  \n"}.__getitem__)


def test_a_file_that_is_not_an_object_is_refused():
    with pytest.raises(ProposalError):
        payload_of("wiki_edit", ["wiki_edit"], read)


def test_the_canonical_form_sorts_keys_and_escapes_like_rfc_8785():
    # RFC 8785 3.2.2.2: only '"', '\\' and control characters are escaped, the latter as
    # lowercase \u00xx except the short forms; everything else is literal UTF-8.
    value = {"b": "é\n\x1f/\u2028", "a": [1, True, None], "B": False}
    assert canonical_json(value) == '{"B":false,"a":[1,true,null],"b":"é\\n\\u001f/\u2028"}'


def test_the_canonical_form_orders_keys_by_utf16_code_units():
    # RFC 8785 3.2.3: U+E000 sorts after U+1F600, whose first UTF-16 unit is a surrogate.
    assert canonical_json({"": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"":1}'


@pytest.mark.parametrize("value", [1.5, 2**53, {1: "a"}, "\ud800"])
def test_the_canonical_form_refuses_what_a_payload_never_holds(value):
    with pytest.raises(ProposalError):
        canonical_json(value)


def test_the_digest_is_sha256_of_the_canonical_form():
    payload = {"request": "SD-12", "public": True, "text": "Hi"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert payload_digest(payload) == expected
    assert payload_digest(dict(reversed(payload.items()))) == expected


def test_a_summary_names_what_a_person_decides():
    assert summary_of("wiki_edit", payload_of("wiki_edit", WIKI, read)) == "Edit page Home"
    assert summary_of("desk_reply", payload_of("desk_reply", REPLY, read)) == "Reply to SD-12"
    assert summary_of("tracker_issue", payload_of("tracker_issue", ISSUE, read)) == (
        "New CORSAR issue: Export fails"
    )
    assert summary_of("tracker_issue", payload_of("tracker_issue", COMMENT, read)) == (
        "Comment on CORSAR-12"
    )
