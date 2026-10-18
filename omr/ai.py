"""Read table rows with Claude vision.

The local pipeline still finds the table, straightens it and works out the rows and columns.
Claude then transcribes the text, which fixes what a local OCR engine gets wrong
(handwriting, Devanagari, similar-looking digits) while the grid keeps every value in the right row.
"""
import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
GEMINI_DEFAULT = "gemini-2.5-flash"
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]  # free-text ids are also accepted by the app
ROWS_PER_TILE = 14
MARGIN = 90  # white strip on the left that carries the red row labels

PROMPT = """You are transcribing a scanned or photographed table (a contact list). The image shows rows {first} to {last} of the table.

Red bracketed numbers like [{first}] in the white strip on the left are row labels added by the software. They are not part of the sheet; use them to say which row you are describing.

The table has {ncol} columns, left to right (usually First Name, Middle Name, Last Name, Contact Number).

For every row that has any content, return:
- row: the label number
- cells: the exact text of each column, {ncol} strings, "" for an empty cell
- mark: what was handwritten in pen on or beside that row: "D" or "L" for those letters, "\u2713" for a tick / check mark, "X" for a cross, "" if nothing. Ignore printed text and yellow highlighter.
- unsure: true if any text in the row was hard to read

Rules:
- Transcribe exactly what is written. Do not correct spelling, translate, expand or reformat. Keep non-English text (Devanagari etc.) and symbols as they appear; use ? for a character you cannot identify.
- If a cell holds several lines or several values separated by | or ::: or /, keep them in order, separated by " | ".
- A cell that spans several rows belongs to the row where its text is printed.
- Leave out rows that are completely empty."""

SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {"row": {"type": "integer"}, "cells": {"type": "array", "items": {"type": "string"}},
                       "mark": {"type": "string"}, "unsure": {"type": "boolean"}},
        "required": ["row", "cells", "mark", "unsure"], "additionalProperties": False}}},
    "required": ["rows"], "additionalProperties": False,
}


class AIError(Exception):
    pass


def _gemini_schema():
    """Gemini's response_schema is a JSON-schema subset: it has no additionalProperties."""
    sch = copy.deepcopy(SCHEMA)
    sch.pop("additionalProperties", None)
    sch["properties"]["rows"]["items"].pop("additionalProperties", None)
    return sch


def make_client(api_key: str | None, provider: str = "claude"):
    if provider == "gemini":
        from google import genai
        return genai.Client(api_key=api_key or None)  # None -> GEMINI_API_KEY / GOOGLE_API_KEY from the environment
    import anthropic
    # No key given -> the SDK falls back to ANTHROPIC_API_KEY / an `ant auth login` profile
    return anthropic.Anthropic(api_key=api_key or None, timeout=180.0, max_retries=2)


def check(api_key: str | None, model: str, provider: str = "claude") -> str:
    """Cheap call used by the Settings 'Test' button. Raises AIError with a readable message."""
    if provider == "gemini":
        try:
            client = make_client(api_key, "gemini")  # keep a reference: the SDK closes the client when it is garbage-collected
            r = client.models.generate_content(model=model, contents="Reply with the single word: ok")
            return r.text or "ok"
        except Exception as e:
            raise AIError(_explain(e))
    import anthropic
    try:
        r = make_client(api_key).messages.create(model=model, max_tokens=50,
                                                 messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                                                 output_config={"effort": "low"})
        return next((b.text for b in r.content if b.type == "text"), "ok")
    except anthropic.AuthenticationError:
        raise AIError("The API key was rejected.")
    except anthropic.NotFoundError:
        raise AIError(f"Model '{model}' was not found for this key.")
    except anthropic.APIConnectionError:
        raise AIError("Could not reach the Anthropic API. Check the internet connection.")
    except anthropic.APIStatusError as e:
        raise AIError(f"API error {e.status_code}: {e.message}")
    except Exception as e:  # missing credentials etc.
        raise AIError(str(e))


def _tile_image(warped: np.ndarray, master: list[float], r0: int, r1: int) -> np.ndarray:
    """Rows r0..r1-1 of the rectified table with a red label per row in a left margin."""
    y0 = int(max(0, master[r0] - 4)); y1 = int(min(warped.shape[0], master[r1] + 4))
    body = warped[y0:y1]
    canvas = np.full((body.shape[0], body.shape[1] + MARGIN, 3), 255, np.uint8)
    canvas[:, MARGIN:] = body
    for r in range(r0, r1):
        cy = int((master[r] + master[r + 1]) / 2) - y0
        cv2.putText(canvas, f"[{r}]", (6, cy + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 220), 2, cv2.LINE_AA)
    return canvas


def _parse(text: str, r0: int, r1: int, ncol: int) -> dict[int, dict]:
    out = {}
    for item in json.loads(text)["rows"]:
        r = item["row"]
        if not (r0 <= r < r1):
            continue
        cells = [str(c) for c in item["cells"]][:ncol]
        cells += [""] * (ncol - len(cells))
        out[r] = {"cells": cells, "mark": item["mark"].strip().upper()[:1] if item["mark"].strip() else "",
                  "unsure": bool(item["unsure"])}
    return out


def _call_gemini(client, model: str, img: np.ndarray, r0: int, r1: int, ncol: int) -> dict[int, dict]:
    from google.genai import types
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=buf.tobytes(), mime_type="image/jpeg"),
                  PROMPT.format(first=r0, last=r1 - 1, ncol=ncol)],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_gemini_schema(),
                                           temperature=0.0))
    if not resp.text:
        raise AIError("Gemini returned no text (the image may have been blocked).")
    return _parse(resp.text, r0, r1, ncol)


def _call(client, model: str, img: np.ndarray, r0: int, r1: int, ncol: int, provider: str = "claude") -> dict[int, dict]:
    if provider == "gemini":
        return _call_gemini(client, model, img, r0, r1, ncol)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    resp = client.messages.create(
        model=model, max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.tobytes()).decode()}},
            {"type": "text", "text": PROMPT.format(first=r0, last=r1 - 1, ncol=ncol)}]}])
    if resp.stop_reason == "refusal":
        raise AIError("The model declined this image.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _parse(text, r0, r1, ncol)


def read_rows(warped: np.ndarray, master: list[float], ncol: int, api_key: str | None, model: str, provider: str = "claude"):
    """-> (rows, failed_ranges). rows = {row_index: {cells, mark, unsure}} for every tile that worked."""
    client = make_client(api_key, provider)
    nrows = len(master) - 1
    tiles = [(a, min(a + ROWS_PER_TILE, nrows)) for a in range(0, nrows, ROWS_PER_TILE)]
    if len(tiles) > 1 and tiles[-1][1] - tiles[-1][0] < 4:  # don't send a sliver tile on its own
        tiles[-2:] = [(tiles[-2][0], tiles[-1][1])]

    def work(t):
        a, b = t
        try:
            return t, _call(client, model, _tile_image(warped, master, a, b), a, b, ncol, provider), None
        except Exception as e:  # one bad tile must not lose the page
            return t, None, e

    rows, failed, first_err = {}, [], None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (a, b), res, err in pool.map(work, tiles):
            if res is None:
                failed.append((a, b)); first_err = first_err or err
            else:
                rows.update(res)
    if failed and not rows and first_err is not None:  # nothing worked: surface the real reason
        raise AIError(_explain(first_err))
    return rows, failed


def _explain(e: Exception) -> str:
    try:
        from google.genai import errors as gerr
        if isinstance(e, gerr.APIError):
            code = getattr(e, "code", 0)
            if code in (400, 401, 403):
                return "Google rejected the request: check the Gemini API key (and that the Generative Language API is enabled for it). " + str(getattr(e, "message", ""))[:160]
            if code == 404:
                return "That Gemini model name was not found for this key."
            if code == 429:
                return "Gemini rate limit or quota reached. Try again in a minute, or use a paid key."
            return f"Gemini error {code}: {str(getattr(e, 'message', ''))[:160]}"
    except ImportError:
        pass
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "The API key was rejected."
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the internet connection."
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {e.message}"
    return str(e)
