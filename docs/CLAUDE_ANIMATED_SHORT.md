# Claude animated Short pipeline

This workflow adds one third Short per day alongside the two existing MoneyPrinterTurbo Shorts.
The existing daily workflow continues to create two vertical Shorts and one independent horizontal
long-form video (600-750 words, approximately five minutes).

## What Claude does, and what it does not do

The Anthropic Messages API generates the topic, evidence-grounded script, eight-scene storyboard,
captions and metadata. The API is a text-generation API; it does not return an MP4. The repository
therefore renders the storyboard locally as a consistent 2D human-cartoon animation using SVG,
CairoSVG and FFmpeg. Narration is generated separately with Edge TTS and muxed into the MP4.

This is a template-based cartoon renderer, not a full generative-video model. It provides a
repeatable first version in the visual direction approved for The Healthy Years. A later upgrade
to a dedicated video-generation provider can replace the renderer without changing the
Claude storyboard contract.

## Required GitHub Actions secrets

Already used by the current publishing workflow:
- YT_CLIENT_ID
- YT_CLIENT_SECRET
- YT_REFRESH_TOKEN

New:
- ANTHROPIC_API_KEY

Optional repository variables:
- CLAUDE_MODEL (default claude-sonnet-5-5)
- CLAUDE_TTS_VOICE (default en-US-GuyNeural)

Never commit API keys to the repository. Add them under Settings → Secrets and variables → Actions.

## Daily schedule

- 06:00 UTC: two regular Shorts plus the independent horizontal long video.
- 12:00 UTC: one Claude-authored animated Short.

GitHub scheduled workflows run from the repository's default branch. The new workflow is
deliberately skipped if its required secrets are absent. It does not publish a silent video:
FFprobe must find both video and audio streams, vertical dimensions, and a duration of 20-40
seconds before the YouTube upload step is reached.

## Manual validation

Before enabling the workflow on the default branch:
1. Add ANTHROPIC_API_KEY to Actions secrets.
2. Run python -m py_compile scripts/daily_shorts.py scripts/claude_animated_short.py.
3. Run the animated workflow with YouTube upload disabled in a future test mode (not yet wired).
4. Review the generated MP4, voice, captions, storyboard and source list.
5. Only then merge/enable automatic publishing.

The repository changes in this branch have not been run through GitHub Actions and no video has
been uploaded as part of this implementation.
