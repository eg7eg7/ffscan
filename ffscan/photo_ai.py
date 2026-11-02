"""Photo analysis with Claude: orientation, caption, tags, date, place, text on the back.

Providers:
  anthropic  the Claude API directly. Auth: ANTHROPIC_API_KEY (run.sh loads it from the macOS Keychain).
  vertex     Claude on Google Vertex AI. Auth: `gcloud auth application-default login`; the model needs
             enabling in Model Garden and a quota grant for the project.
"""
import base64
import io
import json

from anthropic import Anthropic, AnthropicVertex, BetaRefusalFallbackMiddleware
from PIL import Image

MODEL = "claude-opus-5-5"
VERTEX_FALLBACK_MODEL = "claude-opus-4-8"  # Vertex has no server-side fallbacks: retried client-side

SYSTEM = """You catalogue scanned family photo prints for an archive.

For each photo you get the scanned picture side, and sometimes the scanned back of the same print.
The picture is shown exactly as the scanner stored it, which may be sideways or upside down.

- rotation: degrees CLOCKWISE to turn the stored image so it is upright, one of 0, 90, 180, 270.
  Judge from people, horizon, sky, buildings, furniture, text, gravity (hanging objects, water).
  Answer 0 when the image already looks upright or when it is genuinely ambiguous (e.g. leaves, textures).
- caption: one or two plain sentences describing what is shown, as a family member would write it.
  Describe people by appearance and activity; never guess names or identities.
- tags: 3 to 8 short lowercase keywords (scene, objects, event type, season).
- date_estimate: when the photo was probably taken: a year ("1987"), a decade ("1980s"), or "" if no idea.
  Use clues like film colour cast, print border style, clothing, cars, technology.
- date_confidence: low, medium or high.
- place: the place if recognisable (landmark, city, country) or clearly indicated, otherwise "".
  When the photo's GPS position is given, use it: name the place there (landmark or area, city, country), and
  mention it in the caption too.
- back_text: handwriting, stamps or stickers on the back, transcribed as written (any language).
  Ignore paper-maker watermarks (Kodak, Fuji, DNP...) and machine-printed lab codes. "" if none or no back.
- back_date: a date written on the back as YYYY, YYYY-MM or YYYY-MM-DD, otherwise "".
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
        "caption": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "date_estimate": {"type": "string"},
        "date_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "place": {"type": "string"},
        "back_text": {"type": "string"},
        "back_date": {"type": "string"},
    },
    "required": ["rotation", "caption", "tags", "date_estimate", "date_confidence", "place", "back_text", "back_date"],
    "additionalProperties": False,
}

_clients = {}


def client(provider, project=None, region=None):
    key = (provider, project, region)
    if key not in _clients:
        if provider == "vertex":
            _clients[key] = AnthropicVertex(
                project_id=project, region=region,
                middleware=[BetaRefusalFallbackMiddleware([{"model": VERTEX_FALLBACK_MODEL}])],
            )
        else:
            _clients[key] = Anthropic()
    return _clients[key]


def _image_block(path, max_side=1568):
    """Downscale (1568 px long side is plenty to judge a print) and send raw stored orientation."""
    im = Image.open(path).convert("RGB")  # deliberately NOT exif_transpose: rotation is relative to stored pixels
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def analyze(picture, back=None, *, provider="anthropic", project=None, region="global", model=MODEL, gps=None):
    """Return the parsed analysis dict for one print (picture path, optional back path, optional (lat, lon))."""
    content = [{"type": "text", "text": "Picture side:"}, _image_block(picture)]
    if back:
        content += [{"type": "text", "text": "Back of the same print:"}, _image_block(back)]
    if gps:
        content.append({"type": "text", "text": f"GPS position stored with the photo: {gps[0]:.6f}, {gps[1]:.6f}"})
    content.append({"type": "text", "text": "Catalogue this photo."})
    # Claude API: server-side fallback if the model declines; Vertex uses the client middleware instead
    fallback = {} if provider == "vertex" else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    response = client(provider, project, region).beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
        **fallback,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(response.stop_details, 'category', None)}")
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    result["usage"] = {"input": response.usage.input_tokens, "output": response.usage.output_tokens, "model": response.model}
    return result


def to_metadata(result, existing):
    """Merge an analysis into metadata fields without overwriting what the user typed."""
    md = {}
    if not existing.get("description"):
        md["description"] = result["caption"] + (f"\nWritten on back: {result['back_text']}" if result["back_text"] else "")
    if not existing.get("place") and result["place"]:
        md["place"] = result["place"]
    if not existing.get("date"):
        if result["back_date"]:
            md["date"] = result["back_date"]
        elif result["date_estimate"].isdigit() and result["date_confidence"] != "low":
            md["date"] = result["date_estimate"]
    tags = list(result["tags"])
    if result["date_estimate"] and not md.get("date") and not existing.get("date"):
        tags.append(f"c. {result['date_estimate']}")  # decade guesses can't go in EXIF dates
    md["tags"] = ", ".join(tags)
    return md


if __name__ == "__main__":  # python photo_ai.py picture.jpg [back.jpg]   (Claude API, ANTHROPIC_API_KEY)
    import sys

    print(json.dumps(analyze(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), indent=2))
