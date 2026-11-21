# The Healthy Years 404 — channel growth and quality plan

This plan is deliberately separate from the publishing workflow. It does not run GitHub Actions, upload videos, change privacy settings, or change `DRY_RUN`.

## Goals

Improve four outcomes together, using measured experiments rather than assuming that more uploads automatically mean more growth:

1. Reach: views and qualified impressions.
2. Audience: subscribers gained per video and returning viewers.
3. Quality: retention, average view duration, viewer satisfaction, and factual reliability.
4. Monetization readiness: original, useful, policy-compliant programming and a consistent returning audience.

## Editorial direction

Use the existing channel positioning — evidence-based healthy-aging education for adults 50+ — and test a balanced mix of:

- Healthy aging, longevity, and everyday habits.
- Brain health, memory, and sleep.
- Digestive health and nutrition.
- Heart, lung, bone, and mobility health.

These are editorial pillars, not performance claims. Do not give a topic extra weight merely because it sounds sensational. Reassess the mix using per-video results after a sufficiently large sample.

## Short-form creative standards

- The first sentence should create specific, truthful curiosity; no bait-and-switch.
- Deliver the central useful point early, ideally within the opening portion of the Short.
- Use clear spoken English, short sentences, and one main takeaway.
- Explain whether a finding is observational, preclinical, or from a clinical trial when that distinction matters.
- Do not turn association into causation or imply a treatment recommendation.
- End with a brief, non-pushy invitation to subscribe for evidence-based healthy-aging content.
- Keep the existing 85–120 spoken-word validation and verified-source requirements.

## Horizontal-video standards

- State the viewer benefit clearly in the opening.
- Use signposted sections and a coherent narrative rather than padding to meet a word count.
- Include practical context and limitations supported by the cited sources.
- End with a concise takeaway and a natural invitation to watch a related video or subscribe.
- Keep the existing 600–750 word and source validation requirements.

## Packaging

Titles should be specific and understandable, accurately reflect the evidence, and avoid unsupported certainty. For horizontal videos, review the title and thumbnail together; the thumbnail should communicate one idea with minimal text and must not imply a result the research does not establish.

## Measurement loop

For each video, collect the metrics available in YouTube Studio and compare like with like (Shorts against Shorts; horizontal videos against horizontal videos):

- Views and views over the same elapsed-time window.
- Shown-in-feed / impressions and viewed-versus-swiped-away where available.
- Average view duration and average percentage viewed.
- Subscribers gained.
- For horizontal videos: impressions, click-through rate, average view duration, and watch time.
- Traffic sources and returning viewers when available.

Do not compare a new upload's first few hours with an older video's lifetime total. Use a rolling cohort and require a reasonable sample before changing the content mix. Treat CTR as a horizontal-video packaging metric; Shorts-feed behavior is not represented by the same impressions CTR.

## Safety and reliability gates

- Keep source URLs restricted to the supplied research articles.
- Preserve word-count checks, duplicate checks, and the pending-publication queue.
- Never remove medical disclaimers or weaken medical-claim restrictions to improve retention.
- Do not change OAuth scopes, GitHub secrets, publication visibility, schedule, concurrency, or `DRY_RUN` as part of this plan.
- Any future analytics ingestion should be read-only and must not be able to trigger publishing.

## Current implementation in this branch

- Short and long-form prompts now ask for truthful hooks, earlier viewer value, clearer structure, and a non-pushy subscribe / related-video invitation.
- Long-form validation now rejects a title already used in history or in one of the Shorts, and rejects an exact previously published source-URL set. The original topic, word-count, source, and visual-term checks remain in place.

## Not included yet

This branch does not automatically ingest YouTube Studio CSV exports, calculate retention cohorts, generate thumbnail images, or adjust topic weights from channel analytics. Those require inspecting the actual export schemas and agreeing on a stable metric window before safely automating decisions.
