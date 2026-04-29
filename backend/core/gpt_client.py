"""
ClipCut AI — OpenAI GPT-4o mini calls
  - select_moments   : choose viral clips with scores
  - generate_captions: batch TikTok captions + hashtags
  - generate_hook_text: 5-8 word hook sentence
  - translate_transcript: subtitle language translation
"""
import json, re
from openai import OpenAI


def select_moments(openai_key, transcript, max_clips, clip_duration,
                   video_start=None, video_end=None, hot_segments=None, log_fn=None):
    # Minimum 45 s so every clip is a self-contained idea, never a fragment.
    # Cap at (clip_duration - 5) so it cannot exceed the max.
    MIN_DURATION = min(max(45, clip_duration // 2), max(15, clip_duration - 5))
    MIN_GAP = max(30, clip_duration // 2)

    segments_info = [
        {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"].strip()}
        for s in transcript["segments"]
        if (video_start is None or s["end"] >= video_start)
        and (video_end   is None or s["start"] <= video_end)
    ]

    hot_hint = ""
    if hot_segments:
        ranges = ", ".join(
            f"{int(s)//60}:{int(s)%60:02d}-{int(e)//60}:{int(e)%60:02d}"
            for s, e in hot_segments
        )
        hot_hint = (
            f"\nHINT: The following time ranges are the MOST REPLAYED parts "
            f"(YouTube heatmap): {ranges}. Prefer moments near these when content quality "
            f"is equal, but NEVER sacrifice clip diversity or content completeness for this."
        )

    prompt = (
        f"You are a TikTok viral content expert.\n\n"
        f"Select exactly {max_clips} clips from this transcript.\n\n"
        f"DURATION RULES:\n"
        f"- Minimum: {MIN_DURATION}s — never produce a shorter clip\n"
        f"- Maximum: {clip_duration}s\n"
        f"- Ideal range: {MIN_DURATION}s to {clip_duration}s\n\n"
        f"DIVERSITY RULES (critical):\n"
        f"- Each clip must start at least {MIN_GAP}s after the previous clip start.\n"
        f"- Spread clips across the ENTIRE transcript, not clustered in one zone.\n"
        f"- No overlap between clips.\n\n"
        f"CONTENT RULES (most important):\n"
        f"- Each clip must cover ONE complete idea from A to Z.\n"
        f"- Start exactly where the idea/argument/story BEGINS, not in the middle.\n"
        f"- End exactly where the idea reaches its CONCLUSION or natural pause.\n"
        f"- NEVER cut mid-sentence or mid-explanation. The viewer must understand "
        f"the full point without needing context from outside the clip.\n"
        f"- Prefer moments with a hook (surprising fact, question, bold claim) "
        f"at the start, and a satisfying payoff or punchline at the end.\n"
        f"For each clip give a viral_score (1-10) based on standalone value.{hot_hint}\n\n"
        f"Transcript: {json.dumps(segments_info, ensure_ascii=False)}\n\n"
        f"Reply ONLY with JSON: "
        f'{{\"clips\": [{{\"start\": 12.5, \"end\": 78.3, '
        f'\"title\": \"Short punchy title\", \"viral_score\": 8}}]}}'
    )
    client   = OpenAI(api_key=openai_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, temperature=0.6,
    )
    raw_clips = json.loads(response.choices[0].message.content).get("clips", [])
    result     = []
    last_start = -MIN_GAP
    for c in sorted(raw_clips, key=lambda x: x["start"])[:max_clips * 2]:
        s, e, t = c["start"], c["end"], c["title"]
        score   = int(c.get("viral_score", 7))
        if s - last_start < MIN_GAP and result:
            if log_fn:
                log_fn(f"   skip (trop proche) : {t[:40]} [{s:.0f}s]")
            continue
        if e - s < MIN_DURATION:
            e = s + MIN_DURATION
        if e - s > clip_duration:
            e = s + clip_duration
        result.append((s, e, t, score))
        last_start = s
        if len(result) >= max_clips:
            break
    if log_fn:
        durations = [round(e - s, 1) for s, e, _, _ in result]
        starts    = [round(s, 1) for s, _, _, _ in result]
        log_fn(f"GPT selection: {len(result)} clips starts={starts}s durations={durations}s")
    return result


def generate_captions(openai_key, clips_meta, log_fn=None):
    client   = OpenAI(api_key=openai_key)
    captions = []
    titles   = [t for _, _, t, _ in clips_meta]
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"For each of these {len(titles)} TikTok clip titles, write a short punchy "
                f"caption (max 100 chars) followed by 5 trending hashtags on a new line.\n"
                f"Format each as: CAPTION\n#tag1 #tag2 #tag3 #tag4 #tag5\n"
                f"Separate clips with ---\n\n"
                + "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))}],
            temperature=0.8, max_tokens=800,
        )
        raw   = resp.choices[0].message.content.strip()
        parts = re.split(r"\n---\n|---", raw)
        for p in parts:
            p = p.strip()
            if p:
                lines        = p.split("\n")
                caption_line = re.sub(r"^\d+\.\s*", "", lines[0]).strip()
                hashtags     = lines[1].strip() if len(lines) > 1 else "#viral #fyp #trending"
                captions.append(f"{caption_line}\n\n{hashtags}")
    except Exception:
        captions = []

    while len(captions) < len(clips_meta):
        i = len(captions)
        t = clips_meta[i][2] if i < len(clips_meta) else "Clip"
        captions.append(f"{t}\n\n#viral #fyp #trending #tiktok #foryou")
    return captions


def generate_hook_text(openai_key, clip_title: str) -> str:
    try:
        client = OpenAI(api_key=openai_key)
        resp   = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"Write ONE very short hook sentence (5-8 words max) to open a TikTok clip "
                f"about: '{clip_title}'. Make it intriguing or create curiosity. "
                f"Return ONLY the sentence, no quotes, no punctuation at end."}],
            temperature=0.9, max_tokens=30,
        )
        return resp.choices[0].message.content.strip().strip('"\'.')
    except Exception:
        return clip_title[:60]


def translate_transcript(openai_key, transcript, target_lang, log_fn=None):
    client              = OpenAI(api_key=openai_key)
    translated_segments = []
    for seg in transcript.get("segments", []):
        orig_text = seg["text"].strip()
        if not orig_text:
            continue
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content":
                    f"Translate this text to {target_lang}. "
                    f"Return ONLY the translation, nothing else:\n{orig_text}"}],
                temperature=0.2, max_tokens=300,
            )
            translated_text = resp.choices[0].message.content.strip()
        except Exception:
            translated_text = orig_text
        trans_words = translated_text.split()
        seg_start, seg_end = seg["start"], seg["end"]
        duration  = seg_end - seg_start
        n         = max(len(trans_words), 1)
        new_words = [
            {"word":  w,
             "start": seg_start + (i / n) * duration,
             "end":   seg_start + ((i + 1) / n) * duration}
            for i, w in enumerate(trans_words)
        ]
        translated_segments.append({**seg, "text": translated_text, "words": new_words})
    if log_fn:
        log_fn(f"Translation done ({len(translated_segments)} segments)")
    return {**transcript, "segments": translated_segments}
