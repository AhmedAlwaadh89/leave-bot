---
name: video-storyboard-artist
description: Converts an approved script into a shot list and storyboard, generating one approved still frame per shot on Higgsfield (generate_image) before any video is made, and locking characters with a character sheet. Use for "ستوري بورد", "لقطات الفيديو", "shot list", "storyboard frames", "keyframes", or after a script is approved.
---

# Storyboard Artist (رسام الستوري بورد)

Rule zero: no video credits are spent in this stage. Stills only.

## Step 1 - Shot list
From the script table, build the shot list:
| Shot | Beat | Shot size | Angle | Camera move | Subject + action | Setting | Lighting | Duration |
Shot sizes: ECU, CU, MCU, MS, WS, EWS. Camera moves: static, push-in, pull-out, pan,
tilt, orbit, tracking, handheld. Vary size between consecutive shots; never cut between two
shots of the same size and angle.

## Step 2 - Lock consistency
- If a recurring person or mascot exists: load `get_workflow_instructions` with
  `character-sheet` and generate one sheet first. Reuse that image as a reference in every
  shot that includes the character.
- Define a **style string** once (realism level, palette, lens, grain, lighting) and append
  it verbatim to every frame prompt.
- Keep a **reference element** set via `manage_reference_elements` where available for
  product, logo, and character.

## Step 3 - Generate frames
- For each shot, write a prompt: `[shot size] of [subject doing action], [setting],
  [lighting], [camera/lens, e.g. 35mm, shallow depth], [style string]`.
- Use `generate_image_batch` for all shots at once, then `jobs_wait`, then one
  `show_generation_by_ids`. Match aspect ratio to the final video.
- Ask `models_explore` with action `recommend` for the image model if unsure; keep the
  same model for the whole board.

## Step 4 - Review with the user
Present frames in shot order with their prompts. For each rejected frame, change one
variable (angle, lighting, or subject pose) and regenerate only that frame. Record the
approved image job id per shot in `production.md`.

Stop at Gate 3: every shot has one approved still.
