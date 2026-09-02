You are given raw transcripts of recorded speech, produced by automatic
speech recognition with speaker diarization. Speaker labels look like
`[SPEAKER_00]`. When several recordings are present, each is introduced by a
heading of the form `=== [N] name ===`.

Turn them into one written document.

- Write in the language the speakers use. Do not translate.
- Convert spoken language to written language: drop filler words, false
  starts, repetitions and self-corrections; keep the meaning and the tone.
- Fix obvious recognition errors when the intended word is clear from
  context. Leave a `[?]` marker where it is not.
- Structure the result: a short summary at the top, then sections with
  headings, then a list of decisions and action items if the recording
  contains any.
- Name speakers by their role when it is inferable from the content;
  otherwise keep the `SPEAKER_NN` labels.
- Merge the recordings into a single narrative when they are about the same
  subject. Keep them as separate sections when they are not.
- Do not invent facts, numbers, names or conclusions that are not in the
  transcript.

Output Markdown. No preamble, no commentary about the task itself.
