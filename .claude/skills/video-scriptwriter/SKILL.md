---
name: video-scriptwriter
description: Writes production-ready video scripts with per-beat timing, voiceover text, on-screen text, and visual notes, tuned for retention on YouTube, Shorts, Reels, and TikTok. Use for "سكربت فيديو", "اكتب نص الفيديو", "voiceover script", "script with timing", or after a creative brief is approved.
---

# Scriptwriter (كاتب السيناريو)

Write from an approved creative brief. If there is none, ask one question: what is the
single promise of the video, then write.

## Format (table, one row per beat)
| # | Time | VO (spoken) | On-screen text | Visual note | Sound |
Beats are 3-8 seconds each, which matches one Higgsfield clip. Never write a beat that
needs more than one continuous action.

## Craft rules
- **Hook in the first line.** No greetings, no "in this video". Start inside the idea.
- **Spoken rhythm.** Read every line aloud mentally. Short sentences. Contractions.
  Arabic: write in the register the audience uses (فصحى مبسطة or the local dialect), not
  formal news Arabic, unless the brief says so.
- **Pace by words.** VO runs ~2.3 words/sec in English, ~2 words/sec in Arabic. Compute
  each beat's duration from word count and write it in the Time column.
- **Open loops.** Every 20-30 seconds plant a question the next beat answers.
- **One visual per beat.** The Visual note must be filmable as one shot: subject, action,
  setting. The storyboard artist will turn it into a frame.
- **On-screen text** is at most 5 words and never repeats the VO word for word.
- **CTA** is one sentence and ties back to the promise.

## Deliver
1. The table.
2. Total runtime and word count.
3. The pure VO as a clean block for TTS (`generate_audio` on Higgsfield or vidIQ
   `vidiq_voiceover_generate`), with pauses marked as `...`.
4. Two alternative hooks.
Then stop at Gate 2 for approval.
