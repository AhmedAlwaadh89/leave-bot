---
name: video-editor
description: Assembles generated clips into a finished video on Higgsfield - cut order, pacing, transitions, music, sound design, voiceover sync, and burned captions - using the video-montage and video-editing workflows. Use for "مونتاج", "تجميع المقاطع", "اضف ترجمة", "موسيقى للفيديو", "edit the video", "add captions", "assemble clips", or after clips are approved.
---

# Editor (المونتير)

Load `get_workflow_instructions` with `video-montage` (clip assembly, soundtrack, captions)
or `video-editing` (native Higgsedit timeline, animated text, title cards) before touching
the editing tools. Follow that workflow's mechanics; this skill sets the editorial judgment.

## Edit decision list (EDL)
Build it from the script and the approved clip ids:
| Order | Shot | In | Out | Transition | VO segment | Music cue | Caption text |

## Pacing rules
- Shorts/Reels: cut every 1.5-3 seconds; change framing on every cut.
- YouTube long: 3-6 seconds average, breathe on emotional beats.
- Cut on action or on a VO stress word, not on silence.
- First cut must land before second 2. Last shot holds 1 second before the end card.
- Hard cuts by default. Use a transition only to mark a time or location jump.

## Sound
- VO on top, music ducked 12-18 dB under speech, SFX for key actions (whoosh on
  push-ins, impact on reveals). Generate music with `generate_audio` or vidIQ
  `vidiq_generate_music`; pick a tempo that matches cut rate (fast cuts = 120+ BPM).
- Align VO to picture: start each VO line within 200 ms of its shot's first frame.

## Captions
- Burned, large, centered in the safe zone (bottom 25% for 9:16, above platform UI).
- 1-4 words per caption on Shorts, one line of up to 6 words on long form.
- Arabic captions: right-to-left, a font with proper shaping, no justified text.
- Emphasize one keyword per caption with color, never more.

## Deliver
Render in the platform spec (9:16 1080x1920 or 16:9 1920x1080), export the EDL, note
the music and VO job ids. Stop at Gate 5 for review.
