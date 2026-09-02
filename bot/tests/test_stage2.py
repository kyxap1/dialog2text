from src.batch import Batch
from src.stage2 import build_system_prompt, build_transcript_text


def test_transcript_text_has_index_and_name_headings(tmp_path):
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    (tmp_path / "1" / "1.txt").write_text("[SPEAKER_00]: hi\n")
    (tmp_path / "2" / "2.txt").write_text("[SPEAKER_01]: yo\n")
    b = Batch()
    b.add_media("call one.mp4")
    b.add_media("call two.mp4")
    b.media[0].transcript_path = "1/1.txt"
    b.media[1].transcript_path = "2/2.txt"
    text = build_transcript_text(b.media, tmp_path)
    assert text == (
        "=== [1] call one ===\n[SPEAKER_00]: hi\n\n"
        "=== [2] call two ===\n[SPEAKER_01]: yo"
    )


def test_transcript_text_skips_untranscribed_items(tmp_path):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "1.txt").write_text("ok")
    b = Batch()
    b.add_media("a.mp4")
    b.add_media("b.mp4")
    b.media[0].transcript_path = "1/1.txt"
    assert build_transcript_text(b.media, tmp_path) == "=== [1] a ===\nok"


def test_system_prompt_is_metaprompt_then_extra_then_corrections_in_order(tmp_path):
    mp = tmp_path / "default.md"
    mp.write_text("BASE\n")
    b = Batch()
    b.extra_prompt = ["keep it short"]
    b.corrections = ["fix A", "fix B"]
    assert build_system_prompt(mp, b) == "BASE\n\nkeep it short\n\nfix A\n\nfix B"
