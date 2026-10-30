"""The review frame strip uses current submitted state, never historical receipts."""
import pytest

from app import db, review_workflow as review
from .test_review_workflow import task, edit, moved, done
from .test_confirmation_workflow import confirmation, claim
from .test_workflow_transitions import send_back


def assert_indexes(sid, submitted, drafts, *, response=None):
    session = review.get_session(sid, 2)
    assert session["resume"]["submittedFrameIndexes"] == submitted
    assert session["resume"]["draftFrameIndexes"] == drafts
    assert session["progress"]["submittedFrames"] == len(submitted)
    assert session["progress"]["draftFrames"] == len(drafts)
    assert not set(submitted) & set(drafts)
    if response is not None:
        assert response["session"]["resume"]["submittedFrameIndexes"] == submitted
        assert response["session"]["resume"]["draftFrameIndexes"] == drafts
    # List and detail consumers must receive the same frame identities.
    listed = next(item for item in review.list_sessions(2) if item["id"] == sid)
    assert listed["resume"]["submittedFrameIndexes"] == submitted
    assert listed["resume"]["draftFrameIndexes"] == drafts


def test_indexes_follow_current_state_and_empty_frame_submission(task):
    assert_indexes(task, [], [])
    review.write("cursor", task, 2, "strip-cursor", {"expectedCursorRevision": 0}, 2)
    assert_indexes(task, [], [])
    assert_indexes(task, [], [0], response=edit(task, "draft", moved()))
    assert_indexes(task, [], [], response=edit(task, "discard"))
    assert_indexes(task, [2], [], response=edit(task, "submit", fi=2))
    assert_indexes(task, [0, 2], [], response=edit(task, "submit", moved()))
    old_submission = review.get_frame(task, 0, 2)["lastSubmission"]["id"]
    assert_indexes(task, [2], [0], response=edit(task, "draft", moved(16)))
    assert review.get_frame(task, 0, 2)["lastSubmission"]["id"] == old_submission
    assert_indexes(task, [0, 2], [], response=edit(task, "discard"))
    edit(task, "draft", moved(16))
    assert_indexes(task, [0, 2], [], response=edit(task, "draft", moved()))


def test_failed_submit_does_not_turn_frame_green_and_retry_recovers(task, caplog):
    edit(task, "draft", moved())
    with db.connect() as connection:
        connection.execute("CREATE TRIGGER strip_fail BEFORE INSERT ON review_submission_payloads BEGIN SELECT RAISE(ABORT, 'strip test disk failure'); END")
    with pytest.raises(review.ReviewError, match="保存失败"):
        edit(task, "submit", moved(), key="strip-retry")
    assert "review.storage_failed" in caplog.text
    assert_indexes(task, [], [0])
    with db.connect() as connection:
        connection.execute("DROP TRIGGER strip_fail")
    assert_indexes(task, [0], [], response=edit(task, "submit", moved(), key="strip-retry"))


def test_returned_frame_is_not_submitted_until_explicit_resubmission(confirmation, task):
    assert_indexes(task, [0, 1, 2], [])
    claim(confirmation)
    send_back(confirmation)
    assert_indexes(task, [1, 2], [])
    assert [item["frameIndex"] for item in review.get_session(task, 2)["returnRequests"]] == [0]
    # Keeping the original B patch or discarding edits must not restore green.
    assert_indexes(task, [1, 2], [], response=edit(task, "draft", moved()))
    assert_indexes(task, [1, 2], [0], response=edit(task, "draft", moved(18)))
    assert_indexes(task, [1, 2], [], response=edit(task, "discard"))
    assert_indexes(task, [0, 1, 2], [], response=edit(task, "submit", moved()))
    done(task)
    assert_indexes(task, [0, 1, 2], [])
    assert review.get_session(task, 2)["returnRequests"] == []
