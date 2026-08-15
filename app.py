from __future__ import annotations

import base64
import copy
import io
import os
import html
import json
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageFilter, ImageEnhance
from rlottie_python import LottieAnimation

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
INDEX_FILE = BASE_DIR / "index.html"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
RENDER_SIZE = 420
TARGET_FPS = 24
MAX_OUTPUT_FRAMES = 96
RENDER_LOCK = threading.Lock()

STYLE_VERSION = "v21"
INTER_FONT = Path(os.environ.get("INTER_FONT_PATH", str(BASE_DIR / "Inter.ttf")))
NUNITO_FONT = Path(os.environ.get("NUNITO_FONT_PATH", str(BASE_DIR / "Nunito.ttf")))
NUMBER_FONT = Path(os.environ.get("NUMBER_FONT_PATH", str(BASE_DIR / "SFMono-550.ttf")))
RIBBON_IMAGE = Path(os.environ.get("RIBBON_IMAGE_PATH", str(BASE_DIR / "GiftRibbon-928.png")))
RIBBON_BASE64 = BASE_DIR / "assets" / "gift-ribbon.png.b64"
PATTERN_IMAGE = Path(os.environ.get("PATTERN_IMAGE_PATH", str(BASE_DIR / "assets" / "pattern-symbol.png")))
COMPOSITE_SCALE = 4
FALLBACK_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Menlo.ttc"),
)
FALLBACK_FONT = next((path for path in FALLBACK_FONT_CANDIDATES if path.exists()), FALLBACK_FONT_CANDIDATES[0])

TELEGRAM_CARD_STYLE = {
    "canvas": {"color": (23, 23, 25, 255)},
    "card": {"inset": 10 / 420, "radius": 30 / 400},
    "background": {"layers": ("Background",)},
    "model": {"layers": ("Gift",), "root_scale": 76 / 63},
    "pattern": {
        "layers": ("Pattern", "Color Icon"),
        "asset_id": "P",
        "icon_layer_name": "Icon",
        "icon_scale": 1.76,
        "centers": (
            (217.5, 40.2), (417.2, 212.2), (77.2, 136.7),
            (331.7, 19.4), (336.7, 141.3), (413.13, 153.75),
            (75.8, 286.6), (342.1, 286.7), (-5.4, 215.8),
            (210.5, 354.6), (312.2, 390.1), (101.5, 391.0),
            (85.0, 472.5), (216.25, 472.5), (347.5, 472.5),
            (104.0, 29.6), (216.4, 201.1),
        ),
        "brightness": 0.50,
        "edge_fade_inset": 50.0,
        "edge_fade_blur": 28.0,
        "edge_fade_axes": "vertical",
    },
    "ribbon": {
        "source_size": 58.0,
        "component_size": 112.0,
        "profile_offset": (2.0, -2.0),
        "tint_offset": 5,
    },
    "number": {
        "font_size": 0.081,
        "tracking_em": 0.04,
        "angle": 45.0,
        "center_in_ribbon": (36.0, 22.0),
    },
}

CARD_INSET = round(RENDER_SIZE * TELEGRAM_CARD_STYLE["card"]["inset"])
CARD_SIZE = RENDER_SIZE - CARD_INSET * 2
CARD_RADIUS = round(CARD_SIZE * TELEGRAM_CARD_STYLE["card"]["radius"])
PREVIEW_BLACK = TELEGRAM_CARD_STYLE["canvas"]["color"]


def _asset_positions_from_final(data: dict, asset_id: str, positions: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    width = float(data.get("w", 512))
    height = float(data.get("h", 512))
    for asset in data.get("assets", []):
        if asset.get("id") == asset_id:
            width = float(asset.get("w") or width)
            height = float(asset.get("h") or height)
            break
    return tuple((round((x - CARD_INSET) * width / CARD_SIZE, 4), round((y - CARD_INSET) * height / CARD_SIZE, 4)) for x, y in positions)


def _scale_root_layer(data: dict, layer_name: str, factor: float) -> None:
    for layer in data.get("layers", []):
        if layer.get("nm") != layer_name:
            continue
        transform = layer.setdefault("ks", {})
        scale = transform.get("s")
        if not scale:
            transform["s"] = {"a": 0, "k": [100 * factor, 100 * factor, 100]}
            return
        values = scale.get("k") if scale.get("a", 0) == 0 else None
        if isinstance(values, list) and len(values) >= 2:
            values[0] = round(float(values[0]) * factor, 4)
            values[1] = round(float(values[1]) * factor, 4)
        return


def _scale_asset_layers(data: dict, asset_id: str, layer_name: str, factor: float) -> None:
    for asset in data.get("assets", []):
        if asset.get("id") != asset_id:
            continue
        for layer in asset.get("layers", []):
            if layer.get("nm") != layer_name:
                continue
            scale = layer.setdefault("ks", {}).get("s")
            values = scale.get("k") if isinstance(scale, dict) and scale.get("a", 0) == 0 else None
            if isinstance(values, list) and len(values) >= 2:
                values[0] = round(float(values[0]) * factor, 4)
                values[1] = round(float(values[1]) * factor, 4)
        return


def _layout_asset_layers(data: dict, asset_id: str, layer_name: str, positions: tuple[tuple[float, float], ...]) -> None:
    for asset in data.get("assets", []):
        if asset.get("id") != asset_id:
            continue
        matches = [layer for layer in asset.get("layers", []) if layer.get("nm") == layer_name]
        for index, layer in enumerate(matches):
            if index >= len(positions):
                layer["hd"] = True
                continue
            layer["hd"] = False
            layer.setdefault("ks", {})["p"] = {"a": 0, "k": [positions[index][0], positions[index][1], 0]}
        return


def _offset_root_layer(data: dict, layer_name: str, offset: tuple[float, float]) -> None:
    if not offset or offset == (0.0, 0.0):
        return
    width, height = float(data.get("w", 512)), float(data.get("h", 512))
    for layer in data.get("layers", []):
        if layer.get("nm") != layer_name:
            continue
        position = layer.get("ks", {}).get("p", {})
        values = position.get("k") if position.get("a", 0) == 0 else None
        if isinstance(values, list) and len(values) >= 2:
            values[0] = round(float(values[0]) + offset[0] * width, 4)
            values[1] = round(float(values[1]) + offset[1] * height, 4)
        return


def build_component_animation(source: dict, visible_layers: tuple[str, ...], root_scales: dict[str, float] | None = None, root_offsets: dict[str, tuple[float, float]] | None = None, asset_layer_scale: tuple[str, str, float] | None = None, asset_layer_layout: tuple[str, str, tuple[tuple[float, float], ...]] | None = None) -> LottieAnimation:
    data = copy.deepcopy(source)
    visible = set(visible_layers)
    for layer in data.get("layers", []):
        layer["hd"] = layer.get("nm") not in visible
    for name, factor in (root_scales or {}).items():
        _scale_root_layer(data, name, factor)
    for name, offset in (root_offsets or {}).items():
        _offset_root_layer(data, name, offset)
    if asset_layer_scale:
        _scale_asset_layers(data, *asset_layer_scale)
    if asset_layer_layout:
        _layout_asset_layers(data, *asset_layer_layout)
    return LottieAnimation.from_data(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def render_component(animation: LottieAnimation, frame_num: int) -> Image.Image:
    return animation.render_pillow_frame(frame_num=frame_num, width=RENDER_SIZE, height=RENDER_SIZE).convert("RGBA")


def apply_opacity(image: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return image
    result = image.copy()
    result.putalpha(result.getchannel("A").point(lambda value: round(value * opacity)))
    return result


def apply_pattern_edge_fade(image: Image.Image) -> Image.Image:
    style = TELEGRAM_CARD_STYLE["pattern"]
    scene_scale = RENDER_SIZE / CARD_SIZE
    inset = style["edge_fade_inset"] * scene_scale
    blur = style["edge_fade_blur"] * scene_scale
    radius = style["edge_fade_core_radius"] * scene_scale
    mask = Image.new("L", (RENDER_SIZE, RENDER_SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((inset, inset, RENDER_SIZE - inset - 1, RENDER_SIZE - inset - 1), radius=radius, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    result = image.copy()
    result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
    return result



def load_pattern_source() -> Image.Image:
    if not PATTERN_IMAGE.exists():
        raise FileNotFoundError("Pattern symbol missing: provide PATTERN_IMAGE_PATH or assets/pattern-symbol.png")
    return Image.open(PATTERN_IMAGE).convert("RGBA")


def build_pattern_layer() -> Image.Image:
    """Build the approved 17-symbol pattern at 4x with vertical-only edge fading."""
    style = TELEGRAM_CARD_STYLE["pattern"]
    ss = COMPOSITE_SCALE
    size = RENDER_SIZE * ss
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    symbol_size = 112 * (CARD_SIZE / RENDER_SIZE) * (style["icon_scale"] / 1.55)
    symbol = load_pattern_source().resize((round(symbol_size * ss), round(symbol_size * ss)), Image.Resampling.LANCZOS)
    symbol = ImageEnhance.Brightness(symbol).enhance(style["brightness"])
    for x, y in style["centers"]:
        layer.alpha_composite(symbol, (round((x - symbol_size / 2) * ss), round((y - symbol_size / 2) * ss)))
    card_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle((CARD_INSET * ss, CARD_INSET * ss, (CARD_INSET + CARD_SIZE) * ss - 1, (CARD_INSET + CARD_SIZE) * ss - 1), radius=CARD_RADIUS * ss, fill=255)
    fade = Image.new("L", (size, size), 0)
    core = (CARD_INSET + style["edge_fade_inset"]) * ss
    ImageDraw.Draw(fade).rectangle((0, core, size - 1, size - core - 1), fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(style["edge_fade_blur"] * ss))
    fade = ImageChops.multiply(fade, card_mask)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), fade))
    return layer

def backdrop_color(background: Image.Image) -> tuple[int, int, int, int]:
    rgb = background.convert("RGB")
    width, height = rgb.size
    points = ((int(width * 0.04), int(height * 0.04)), (int(width * 0.96), int(height * 0.04)), (int(width * 0.04), int(height * 0.96)), (int(width * 0.96), int(height * 0.96)))
    samples = [rgb.getpixel(point) for point in points]
    color = tuple(sorted(sample[channel] for sample in samples)[len(samples) // 2] for channel in range(3))
    return color[0], color[1], color[2], 255


def load_font(path: Path, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    selected = path if path.exists() else FALLBACK_FONT
    font = ImageFont.truetype(str(selected), size)
    if variation and hasattr(font, "set_variation_by_name"):
        try:
            font.set_variation_by_name(variation)
        except (OSError, ValueError):
            pass
    return font


def _ribbon_geometry() -> tuple[float, float, float, float]:
    style = TELEGRAM_CARD_STYLE["ribbon"]
    scale = CARD_SIZE / style["component_size"]
    size = style["source_size"] * scale
    x = CARD_INSET + CARD_SIZE - size + style["profile_offset"][0] * scale
    y = CARD_INSET + style["profile_offset"][1] * scale
    return scale, size, x, y


def _ribbon_tint(background: Image.Image) -> tuple[int, int, int, int]:
    base = backdrop_color(background)
    offset = TELEGRAM_CARD_STYLE["ribbon"]["tint_offset"]
    return tuple(max(0, min(255, value + offset)) for value in base[:3]) + (255,)


def load_ribbon_source() -> Image.Image:
    if RIBBON_IMAGE.exists():
        return Image.open(RIBBON_IMAGE).convert("RGBA")
    if not RIBBON_BASE64.exists():
        raise FileNotFoundError("Ribbon asset missing: provide RIBBON_IMAGE_PATH or assets/gift-ribbon.png.b64")
    payload = base64.b64decode(RIBBON_BASE64.read_text(encoding="ascii"))
    return Image.open(io.BytesIO(payload)).convert("RGBA")


def build_ribbon_shape(background: Image.Image) -> Image.Image:
    style = TELEGRAM_CARD_STYLE["ribbon"]
    ss = COMPOSITE_SCALE
    scale, size, x, y = _ribbon_geometry()
    source = load_ribbon_source().resize((round(size * ss), round(size * ss)), Image.Resampling.LANCZOS)
    tinted = Image.new("RGBA", source.size, _ribbon_tint(background))
    tinted.putalpha(source.getchannel("A"))
    layer = Image.new("RGBA", (RENDER_SIZE * ss, RENDER_SIZE * ss), (0, 0, 0, 0))
    layer.alpha_composite(tinted, (round(x * ss), round(y * ss)))
    protrude_x = max(0.0, style["profile_offset"][0] * scale)
    protrude_y = max(0.0, -style["profile_offset"][1] * scale)
    outer_mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle((CARD_INSET * ss, (CARD_INSET - protrude_y) * ss, (CARD_INSET + CARD_SIZE + protrude_x) * ss, (CARD_INSET + CARD_SIZE) * ss), radius=(CARD_RADIUS + max(protrude_x, protrude_y)) * ss, fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), outer_mask))
    return layer.resize((RENDER_SIZE, RENDER_SIZE), Image.Resampling.LANCZOS)


def build_number_layer(number: int) -> Image.Image:
    style = TELEGRAM_CARD_STYLE["number"]
    ss = COMPOSITE_SCALE
    ribbon_scale, _, ribbon_x, ribbon_y = _ribbon_geometry()
    center_x = ribbon_x + style["center_in_ribbon"][0] * ribbon_scale
    center_y = ribbon_y + style["center_in_ribbon"][1] * ribbon_scale
    font_size = CARD_SIZE * style["font_size"]
    font = load_font(NUMBER_FONT, round(font_size * ss))
    text = f"#{number}"
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [probe.textlength(character, font=font) for character in text]
    gap = style["tracking_em"] * font_size * ss
    total_width = sum(widths) + gap * max(0, len(text) - 1)
    box = font.getbbox(text)
    pad = 48 * ss
    strip = Image.new("RGBA", (int(round(total_width + 2 * pad)), int(round(box[3] - box[1] + 2 * pad))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)
    cursor = pad
    baseline = strip.height / 2 - (box[1] + box[3]) / 2
    for character, width in zip(text, widths):
        draw.text((cursor, baseline), character, font=font, fill=(255, 255, 255, 255))
        cursor += width + gap
    rotated = strip.rotate(-style["angle"], expand=True, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    layer = Image.new("RGBA", (RENDER_SIZE * ss, RENDER_SIZE * ss), (0, 0, 0, 0))
    layer.alpha_composite(rotated, (round(center_x * ss - rotated.width / 2), round(center_y * ss - rotated.height / 2)))
    return layer.resize((RENDER_SIZE, RENDER_SIZE), Image.Resampling.LANCZOS)


def card_canvas(scene: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (RENDER_SIZE, RENDER_SIZE), PREVIEW_BLACK)
    card = scene.resize((CARD_SIZE, CARD_SIZE), Image.Resampling.LANCZOS)
    mask = Image.new("L", (CARD_SIZE, CARD_SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CARD_SIZE - 1, CARD_SIZE - 1), radius=CARD_RADIUS, fill=255)
    card.putalpha(mask)
    canvas.alpha_composite(card, (CARD_INSET, CARD_INSET))
    return canvas


def compose_elements(background: Image.Image, pattern: Image.Image, model: Image.Image, number: int, ribbon: Image.Image | None = None, number_layer: Image.Image | None = None) -> tuple[Image.Image, dict[str, Image.Image]]:
    """Compose the card at 4x and downsample once, matching the approved pre-production path."""
    ss = COMPOSITE_SCALE
    size = RENDER_SIZE * ss
    card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    card.alpha_composite(background.resize((CARD_SIZE * ss, CARD_SIZE * ss), Image.Resampling.LANCZOS), (CARD_INSET * ss, CARD_INSET * ss))
    card.alpha_composite(pattern)
    card.alpha_composite(model.resize((CARD_SIZE * ss, CARD_SIZE * ss), Image.Resampling.LANCZOS), (CARD_INSET * ss, CARD_INSET * ss))
    card_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle((CARD_INSET * ss, CARD_INSET * ss, (CARD_INSET + CARD_SIZE) * ss - 1, (CARD_INSET + CARD_SIZE) * ss - 1), radius=CARD_RADIUS * ss, fill=255)
    card.putalpha(ImageChops.multiply(card.getchannel("A"), card_mask))
    large = Image.new("RGBA", (size, size), PREVIEW_BLACK)
    large.alpha_composite(card)
    final = large.resize((RENDER_SIZE, RENDER_SIZE), Image.Resampling.LANCZOS)
    ribbon = ribbon or build_ribbon_shape(background)
    number_layer = number_layer or build_number_layer(number)
    final.alpha_composite(ribbon)
    final.alpha_composite(number_layer)
    pattern_preview = pattern.resize((RENDER_SIZE, RENDER_SIZE), Image.Resampling.LANCZOS)
    return final, {"background": background, "pattern": pattern_preview, "model": model, "ribbon": ribbon, "number": number_layer, "scene": final}


def save_debug_sheet(path: Path, components: dict[str, Image.Image], final: Image.Image) -> None:
    entries = [("1  BACKGROUND", card_canvas(components["background"])), ("2  PATTERN", card_canvas(components["pattern"])), ("3  MODEL", card_canvas(components["model"])), ("4  RIBBON SHAPE", components["ribbon"]), ("5  NUMBER", components["number"]), ("6  FINAL COMPOSITE", final)]
    tile_h = RENDER_SIZE + 32
    sheet = Image.new("RGB", (RENDER_SIZE * 4, tile_h * 2), (18, 18, 20))
    font = load_font(INTER_FONT, 16, "Medium")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(entries):
        x = (index % 4) * RENDER_SIZE
        y = (index // 4) * tile_h
        preview = Image.new("RGBA", (RENDER_SIZE, RENDER_SIZE), PREVIEW_BLACK)
        preview.alpha_composite(image)
        sheet.paste(preview.convert("RGB"), (x, y))
        draw.text((x + 12, y + RENDER_SIZE + 7), label, font=font, fill=(235, 235, 238))
    path.parent.mkdir(exist_ok=True)
    sheet.save(path, format="PNG", optimize=True)


app = FastAPI(title="Gift to GIF", docs_url=None, redoc_url=None)


class RenderRequest(BaseModel):
    url: str = Field(min_length=3, max_length=300)


def parse_gift_ref(value: str) -> tuple[str, int]:
    raw = unquote(value.strip()).split("?", 1)[0].split("#", 1)[0].rstrip("/")
    token = urlparse(raw).path.rstrip("/").split("/")[-1] if "://" in raw else raw
    token = re.sub(r"\.(?:lottie\.json|large\.jpg|medium\.jpg|jpg|jpeg|png|webp)$", "", token, flags=re.IGNORECASE)
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*?)-(\d{1,12})", token)
    if not match:
        raise ValueError("Нужна публичная ссылка вида t.me/nft/GiftName-1234")
    return match.group(1), int(match.group(2))


def http_get(url: str, accept: str, max_bytes: int) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Referer": "https://t.me/"}
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=25) as response:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("Ответ источника слишком большой")
                return data
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except URLError as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Источник временно недоступен: {last_error}")


def meta_tag(page: str, property_name: str) -> str | None:
    for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.IGNORECASE | re.DOTALL):
        attrs: dict[str, str] = {}
        for key, _, value in re.findall(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", tag, flags=re.DOTALL):
            attrs[key.lower()] = html.unescape(value)
        if attrs.get("property", "").lower() == property_name.lower():
            return attrs.get("content")
    return None


def title_from_slug(slug: str) -> str:
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", slug)
    return value.replace("_", " ").replace("-", " ").strip().title()


def fetch_metadata(slug: str, number: int) -> dict[str, str | int | None]:
    source_url = "https://t.me/nft/" + slug + "-" + str(number)
    page = http_get(source_url, "text/html", 700_000).decode("utf-8", "replace")
    og_title = meta_tag(page, "og:title") or ""
    if og_title.lower().startswith("telegram "):
        og_title = ""
    title = re.sub(rf"\s*#{number}\s*$", "", og_title).strip()
    description = meta_tag(page, "og:description") or ""
    attrs: dict[str, str] = {}
    for line in description.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            attrs[key.strip().casefold()] = value.strip()
    return {"title": title or title_from_slug(slug), "number": number, "model": attrs.get("model"), "backdrop": attrs.get("backdrop"), "symbol": attrs.get("symbol"), "source_url": source_url}


def render_gift(slug: str, number: int) -> dict[str, str | int | None]:
    safe_stem = re.sub(r"[^a-z0-9_-]", "", slug.lower()) + "-" + str(number)
    gif_path = OUTPUT_DIR / f"{safe_stem}-{STYLE_VERSION}.gif"
    meta_path = OUTPUT_DIR / f"{safe_stem}-{STYLE_VERSION}.json"
    debug_path = OUTPUT_DIR / f"{safe_stem}-{STYLE_VERSION}-debug.png"
    if gif_path.exists() and meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    with RENDER_LOCK:
        if gif_path.exists() and meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
        metadata = fetch_metadata(slug, number)
        lottie_url = "https://nft.fragment.com/gift/" + slug.lower() + "-" + str(number) + ".lottie.json"
        data = json.loads(http_get(lottie_url, "application/json", 8_000_000).decode("utf-8"))
        if not data.get("layers") or not data.get("w") or not data.get("h"):
            raise ValueError("Для этой ссылки не найдена корректная анимация")
        background_animation = build_component_animation(data, TELEGRAM_CARD_STYLE["background"]["layers"])
        model_style = TELEGRAM_CARD_STYLE["model"]
        model_animation = build_component_animation(data, model_style["layers"], root_scales={"Gift": model_style["root_scale"]})
        source_frames = max(1, int(model_animation.lottie_animation_get_totalframe()))
        source_fps = max(1.0, float(model_animation.lottie_animation_get_framerate()))
        background = render_component(background_animation, 0)
        pattern = build_pattern_layer()
        ribbon = build_ribbon_shape(background)
        number_layer = build_number_layer(number)
        duration_seconds = source_frames / source_fps
        output_count = max(1, min(MAX_OUTPUT_FRAMES, int(round(duration_seconds * TARGET_FPS))))
        frame_dir = OUTPUT_DIR / f".{safe_stem}-{STYLE_VERSION}-png-frames"
        shutil.rmtree(frame_dir, ignore_errors=True)
        frame_dir.mkdir(parents=True, exist_ok=True)
        png_paths: list[Path] = []
        for index in range(output_count):
            seconds = index / TARGET_FPS
            source_index = min(source_frames - 1, int(round(seconds * source_fps)))
            model = render_component(model_animation, source_index)
            final, components = compose_elements(background, pattern, model, number, ribbon=ribbon, number_layer=number_layer)
            png_path = frame_dir / f"frame-{index:04d}.png"
            final.convert("RGB").save(png_path, format="PNG", optimize=False)
            png_paths.append(png_path)
            if index == 0:
                save_debug_sheet(debug_path, components, final)
        rgb_frames: list[Image.Image] = []
        for png_path in png_paths:
            with Image.open(png_path) as png_frame:
                if png_frame.mode != "RGB":
                    raise RuntimeError(f"PNG frame is not opaque RGB: {png_path.name}")
                rgb_frames.append(png_frame.copy())
        sample_size = 105
        columns = 9
        rows = (len(rgb_frames) + columns - 1) // columns
        palette_source = Image.new("RGB", (columns * sample_size, rows * sample_size))
        for index, frame in enumerate(rgb_frames):
            palette_source.paste(frame.resize((sample_size, sample_size), Image.Resampling.LANCZOS), ((index % columns) * sample_size, (index // columns) * sample_size))
        palette = palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        quantized = [frame.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for frame in rgb_frames]
        delay_ms = max(20, round((1000 / TARGET_FPS) / 10) * 10)
        temp_path = gif_path.with_suffix(".tmp.gif")
        quantized[0].save(temp_path, save_all=True, append_images=quantized[1:], duration=delay_ms, loop=0, optimize=True, disposal=2)
        temp_path.replace(gif_path)
        (frame_dir / "manifest.json").write_text(json.dumps({"version": STYLE_VERSION, "format": "RGB PNG", "size": [RENDER_SIZE, RENDER_SIZE], "frame_count": len(png_paths), "fps": TARGET_FPS, "files": [path.name for path in png_paths]}, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {**metadata, "gif_url": "/output/" + gif_path.name, "download_url": "/output/" + gif_path.name, "size_kb": round(gif_path.stat().st_size / 1024), "frame_count": output_count, "layout_version": STYLE_VERSION}
        temp_meta = meta_path.with_suffix(".tmp.json")
        temp_meta.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temp_meta.replace(meta_path)
        return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/api/render")
async def api_render(body: RenderRequest):
    try:
        slug, number = parse_gift_ref(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return await run_in_threadpool(render_gift, slug, number)
    except HTTPError as exc:
        detail = "Подарок не найден. Проверьте публичную ссылку." if exc.code == 404 else "Telegram или Fragment временно не отвечает."
        raise HTTPException(status_code=422, detail=detail) from exc
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        print(f"render error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=500, detail="Не удалось собрать GIF") from exc


@app.get("/output/{filename}")
def output_file(filename: str) -> FileResponse:
    if not re.fullmatch(r"[a-z0-9_-]+\.gif", filename):
        raise HTTPException(status_code=404)
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/gif", headers={"Cache-Control": "public, max-age=86400"})
