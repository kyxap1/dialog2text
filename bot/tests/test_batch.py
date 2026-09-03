from src.batch import Batch


def test_media_keeps_forward_order_and_1_based_index():
    b = Batch()
    b.add_media("a/videos/1.mp4")
    b.add_media("a/videos/2.mp4")
    assert [(m.index, m.filename) for m in b.media] == [
        (1, "a/videos/1.mp4"),
        (2, "a/videos/2.mp4"),
    ]


def test_add_media_carries_an_optional_title_kept_on_re_add():
    b = Batch()
    b.add_media("https://www.youtube.com/watch?v=a", title="Hello")
    assert b.media[0].title == "Hello"
    b.add_media("https://www.youtube.com/watch?v=a", title="Different")
    assert len(b.media) == 1 and b.media[0].title == "Hello"


def test_text_is_extra_prompt_before_output_and_correction_after():
    b = Batch()
    assert b.add_text("use bullet points") == "prompt"
    b.has_output = True
    assert b.add_text("fix the second one") == "correction"
    assert b.extra_prompt == ["use bullet points"]
    assert b.corrections == ["fix the second one"]


def test_corrections_accumulate():
    b = Batch()
    b.has_output = True
    b.add_text("fix A")
    b.add_text("fix B")
    assert b.corrections == ["fix A", "fix B"]


def test_untranscribed_lists_only_items_without_a_path():
    b = Batch()
    b.add_media("1.mp4")
    b.add_media("2.mp4")
    b.media[0].transcript_path = "1/1.txt"
    assert [m.index for m in b.untranscribed()] == [2]


def test_merge_results_applies_paths_and_keeps_failures_pending():
    b = Batch()
    b.add_media("1.mp4")
    b.add_media("2.mp4")
    b.merge_results(
        [
            {"index": 1, "name": "1.mp4", "transcript_path": "1/1.txt", "error": None},
            {"index": 2, "name": "2.mp4", "transcript_path": None, "error": "boom"},
        ]
    )
    assert b.media[0].transcript_path == "1/1.txt"
    assert b.media[1].transcript_path is None
    assert [m.index for m in b.untranscribed()] == [2]


def test_failed_item_is_the_only_one_retried_on_next_go():
    b = Batch()
    b.add_media("1.mp4")
    b.add_media("2.mp4")
    b.merge_results(
        [
            {"index": 1, "transcript_path": "1/1.txt", "error": None},
            {"index": 2, "transcript_path": None, "error": "boom"},
        ]
    )
    retry = [m.filename for m in b.untranscribed()]
    assert retry == ["2.mp4"]
    b.merge_results([{"index": 2, "transcript_path": "2/2.txt", "error": None}])
    assert b.untranscribed() == []


def test_reset_clears_everything():
    b = Batch()
    b.add_media("1.mp4")
    b.add_text("x")
    b.has_output = True
    b.reset()
    assert b.media == [] and b.extra_prompt == [] and b.corrections == []
    assert b.has_output is False
