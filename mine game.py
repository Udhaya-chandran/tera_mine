import subprocess
import shutil
import time
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from PIL import Image, ImageDraw, ImageOps

import uiautomator2 as u2


# ==========================================
# CONFIG
# ==========================================

SCREENSHOT = "screen.png"
CROP_DIR = "card_crops"

CROP_SIZE = 120
MATCH_THRESHOLD = 18

PLAY_STORE_PACKAGE = "com.android.vending"

MAX_ROUNDS = None

NEW_ROUND_DELAY = 2.0
UI_DUMP_INTERVAL = 1.0
AD_DUMP_INTERVAL = 2.0

PAIR_TAP_GAP = 0.2
PAIR_SETTLE_DELAY = 0.6
CAPTURE_FLIP_DELAY = 0.5

AD_RETRY_DELAY = 10

CLOSE_FRAC = (0.955, 0.025)

BLIND_CLOSE_ENABLED = False
BLIND_CLOSE_AFTER = 3

LAST_RESORT_ENABLED = False
LAST_RESORT_AFTER = 30
LAST_RESORT_WAIT = 10

WAIT_FOR_REWARD = True

UNSAFE_TEXTS = (
    "open website", "use app", "shop now", "install", "learn more",
    "download", "open app", "play now", "sign up", "get offer",
    "order now", "book now", "apply now", "visit site",
)

MINE_SCROLL_MAX_TRIES = 20
MINE_SCROLL_FRACTION = 0.25
MINE_FLIP_AFTER = 6

COLLECT_TIMEOUT = 30

USE_CLI_FALLBACK = True      # now ON: CLI dump is used if u2 dump fails

# ---- Card detection (relaxed) ----
CARD_MIN_FRAC = 0.08
CARD_MAX_FRAC = 0.35
CARD_SQUARE_TOLERANCE = 0.25   # fraction of width (was fixed 15 px)
CARD_LABEL = "?"
CARD_DETECT_RETRIES = 6
CARD_DETECT_DELAY = 0.7

GAME_MARKERS = ("matches", "end game", "conveyor belt", "play again",
                "level up", "play level", "time expired", "collect")


os.makedirs(CROP_DIR, exist_ok=True)

def _find_adb():
    found = shutil.which("adb")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "adb"

ADB = _find_adb()
_adb_dir = os.path.dirname(ADB)
if _adb_dir and os.path.isdir(_adb_dir) and _adb_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _adb_dir + os.pathsep + os.environ.get("PATH", "")

LAST_CARD_SIZE = None


# ==========================================
# UIAUTOMATOR2 CONNECTION
# ==========================================

try:
    d = u2.connect()
    d.settings["wait_timeout"] = 5
except Exception as e:
    print(f"u2 connect failed, using CLI only: {e}")
    d = None


# ==========================================
# ADB HELPERS
# ==========================================

def _adb(*args):
    try:
        proc = subprocess.run([ADB, *args], capture_output=True, text=True)
        if proc.returncode != 0 and ("SecurityException" in proc.stderr or "INJECT_EVENTS" in proc.stderr):
            print("\n[!] PERMISSION ERROR: On Xiaomi/MIUI devices, enable 'USB debugging (Security settings)' in Developer Options to allow touch events.\n")
    except FileNotFoundError:
        print("ERROR: adb not found. Add platform-tools to PATH.")


def tap_xy(x, y):
    if d is not None:
        try:
            d.click(int(x), int(y))
            return
        except Exception as e:
            if "SecurityException" in str(e) or "INJECT_EVENTS" in str(e):
                print("\n[!] PERMISSION ERROR: On Xiaomi/MIUI devices, enable 'USB debugging (Security settings)' in Developer Options to allow touch events.\n")
            pass
    _adb("shell", "input", "tap", str(x), str(y))


def tap_pair(x1, y1, x2, y2, gap=PAIR_TAP_GAP):
    cmd = f"input tap {x1} {y1}; sleep {gap}; input tap {x2} {y2}"
    if d is not None:
        try:
            d.shell(cmd)
            return
        except Exception:
            pass
    _adb("shell", cmd)


def swipe(x1, y1, x2, y2, duration=400):
    if d is not None:
        try:
            d.swipe(x1, y1, x2, y2, duration / 1000)
            return
        except Exception:
            pass
    _adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2),
         str(duration))


def press_back():
    if d is not None:
        try:
            d.press("back")
            return
        except Exception:
            pass
    _adb("shell", "input", "keyevent", "4")


def take_screenshot():
    if d is not None:
        try:
            d.screenshot(SCREENSHOT)
            return
        except Exception:
            pass
    with open(SCREENSHOT, "wb") as f:
        subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f,
                       stderr=subprocess.DEVNULL)


def device_screen_size(xml=None):
    if d is not None:
        try:
            w, h = d.window_size()
            if w and h:
                return int(w), int(h)
        except Exception:
            pass
    if xml:
        return screen_size(xml)
    return 1080, 2600


# ==========================================
# UI DUMP
# ==========================================

def dump_ui_u2():
    if d is None:
        return None
    try:
        return d.dump_hierarchy(compressed=False)
    except Exception:
        return None


def dump_ui_cli():
    result = subprocess.run(
        [ADB, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
        capture_output=True, text=True)
    output = (result.stdout + result.stderr).lower()
    if "could not get idle state" in output or "error" in output:
        return None
    result = subprocess.run(
        [ADB, "exec-out", "cat", "/sdcard/ui.xml"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    return result.stdout


def dump_ui():
    xml = dump_ui_u2()
    if xml:
        return xml
    if USE_CLI_FALLBACK:
        return dump_ui_cli()
    return None


def focused_package():
    if d is None:
        return None
    try:
        return d.app_current().get("package")
    except Exception:
        return None


def save_debug_xml(xml, name):
    try:
        with open(name, "w", encoding="utf-8") as f:
            f.write(xml or "")
        print(f"(saved UI dump to {name})")
    except OSError:
        pass


# ==========================================
# XML PARSING
# ==========================================

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_nodes(xml):
    """Parse all nodes. Each node also knows if an ancestor is clickable."""

    if not xml:
        return []

    start = xml.find("<")
    if start == -1:
        return []

    try:
        root = ET.fromstring(xml[start:].encode("utf-8"))
    except ET.ParseError:
        return []

    nodes = []

    def walk(el, parent_clickable):
        if el.tag == "node":
            clickable = el.get("clickable") == "true"
            m = BOUNDS_RE.search(el.get("bounds", ""))
            if m:
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                nodes.append({
                    "package": el.get("package", ""),
                    "text": el.get("text", ""),
                    "desc": el.get("content-desc", ""),
                    "rid": el.get("resource-id", ""),
                    "class": el.get("class", ""),
                    "clickable": clickable,
                    "parent_clickable": parent_clickable,
                    "enabled": el.get("enabled") == "true",
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                })
            parent_clickable = parent_clickable or clickable
        for child in el:
            walk(child, parent_clickable)

    walk(root, False)
    return nodes


def center(n):
    return (n["x1"] + n["x2"]) // 2, (n["y1"] + n["y2"]) // 2


def screen_size(xml):
    nodes = parse_nodes(xml)
    if not nodes:
        return 1080, 2400
    return max(n["x2"] for n in nodes), max(n["y2"] for n in nodes)


# ==========================================
# LEVEL
# ==========================================

def get_current_level(xml):
    if not xml:
        return None

    for n in parse_nodes(xml):
        for value in (n["text"].strip(), n["desc"].strip()):
            match = re.fullmatch(r"level\s*(\d+)", value, re.IGNORECASE)
            if match:
                return int(match.group(1))

    match = re.search(r"\blevel\s*(\d+)\b", xml, re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def expected_cards_for_level(level):
    """
    Level 1  - 10 -> 12 cards
    Level 11 - 25 -> 16 cards
    Level 26 - 50 -> 20 cards
    Level 51+     -> 30 cards
    """
    if level is None:
        return None
    if 1 <= level <= 10:
        return 12
    if 11 <= level <= 25:
        return 16
    if 26 <= level <= 50:
        return 20
    if level >= 51:
        return 30
    return None


def update_expected_cards(xml):
    level = get_current_level(xml)

    if level is None:
        print("Could not detect current level from UI.")
        return None

    expected = expected_cards_for_level(level)

    if expected is None:
        print(f"Unsupported level detected: {level}")
        return None

    print("\n==========================================")
    print("          LEVEL DETECTED")
    print("==========================================")
    print(f"Current Level  : {level}")
    print(f"Expected Cards : {expected}")
    print("==========================================\n")

    return expected


# ==========================================
# FIND CARDS FROM UI (robust)
# ==========================================

def _is_squareish(w, h):
    return abs(w - h) <= max(15, CARD_SQUARE_TOLERANCE * max(w, h))


def _order_cards(found):
    """found: list of (cx, cy, w). Return {n: (cx, cy)} top->bottom, left->right."""

    global LAST_CARD_SIZE

    LAST_CARD_SIZE = int(sum(f[2] for f in found) / len(found))

    found.sort(key=lambda f: f[1])
    row_tol = LAST_CARD_SIZE / 2

    rows = []
    for item in found:
        if rows and abs(item[1] - rows[-1][0][1]) < row_tol:
            rows[-1].append(item)
        else:
            rows.append([item])

    ordered = []
    for row in rows:
        row.sort(key=lambda f: f[0])
        ordered.extend(row)

    return {i: (cx, cy) for i, (cx, cy, _) in enumerate(ordered, start=1)}


def _dedupe(found):
    """Drop nodes whose centers are almost identical (parent/child duplicates)."""
    out = []
    for cx, cy, w in found:
        if any(abs(cx - ox) < w * 0.3 and abs(cy - oy) < w * 0.3
               for ox, oy, _ in out):
            continue
        out.append((cx, cy, w))
    return out


def find_card_coordinates(xml, expected=None):
    """
    Pass 1: nodes labelled '?' (in content-desc OR text), roughly square,
            sized like a card. Clickable on the node or any ancestor.
    Pass 2 (fallback): square, clickable nodes of the most common size
            (cards all share one size), without needing the '?' label.
    """

    if xml is None:
        return {}

    nodes = parse_nodes(xml)
    if not nodes:
        return {}

    screen_w = max(n["x2"] for n in nodes)
    lo, hi = CARD_MIN_FRAC * screen_w, CARD_MAX_FRAC * screen_w

    # ---- pass 1 ----
    found = []
    for n in nodes:
        if CARD_LABEL not in (n["desc"].strip(), n["text"].strip()):
            continue
        w, h = n["x2"] - n["x1"], n["y2"] - n["y1"]
        if not _is_squareish(w, h) or not (lo <= w <= hi):
            continue
        cx, cy = center(n)
        found.append((cx, cy, w))

    found = _dedupe(found)

    if found and (expected is None or len(found) >= expected):
        return _order_cards(found)

    # ---- pass 2 ----
    screen_h = max(n["y2"] for n in nodes)
    cand = []
    for n in nodes:
        w, h = n["x2"] - n["x1"], n["y2"] - n["y1"]
        if not (n["clickable"] or n["parent_clickable"]):
            continue
        if not _is_squareish(w, h) or not (lo <= w <= hi):
            continue
        if _is_unsafe_node(n):
            continue
        # Exclude bottom nav bar (tabs) and top status bar
        if n["y1"] > 0.85 * screen_h or n["y2"] < 0.10 * screen_h:
            continue
        cand.append(n)

    if len(cand) >= 4:
        common = Counter(round((n["x2"] - n["x1"]) / 10) for n in cand)
        best_bucket, _ = common.most_common(1)[0]
        group = [n for n in cand
                 if abs(round((n["x2"] - n["x1"]) / 10) - best_bucket) <= 1]
        found2 = _dedupe([(*center(n), n["x2"] - n["x1"]) for n in group])
        if len(found2) >= 4 and (not found or len(found2) > len(found)):
            return _order_cards(found2)

    if found:
        return _order_cards(found)

    return {}


def detect_cards_with_retry(expected):
    """Re-dump a few times until the card count matches expected."""

    best = {}
    last_xml = None

    for attempt in range(1, CARD_DETECT_RETRIES + 1):

        xml = dump_ui()
        last_xml = xml
        cards = find_card_coordinates(xml, expected)

        if len(cards) > len(best):
            best = cards

        if expected and len(cards) == expected:
            return cards, xml

        if not expected and cards:
            return cards, xml

        print(f"  card detect attempt {attempt}: "
              f"{len(cards)} found (expected {expected})")
        time.sleep(CARD_DETECT_DELAY)

    return best, last_xml


def print_cards(cards):
    print("\n==========================================")
    print("          DETECTED CARD POSITIONS")
    print("==========================================")
    for number in sorted(cards):
        x, y = cards[number]
        print(f"Card {number:2d} -> ({x}, {y})")
    print("==========================================\n")


# ==========================================
# IMAGE HELPERS
# ==========================================

def current_crop_size():
    if LAST_CARD_SIZE:
        return max(40, min(CROP_SIZE, int(LAST_CARD_SIZE * 0.6)))
    return CROP_SIZE


def crop_card(card_number, x, y, image=None):
    if image is None:
        image = Image.open(SCREENSHOT).convert("RGB")
    half = current_crop_size() // 2
    crop = image.crop((x - half, y - half, x + half, y + half))
    crop.save(os.path.join(CROP_DIR, f"card_{card_number}.png"))
    return crop


def fingerprint(image):
    return list(ImageOps.grayscale(image).resize((32, 32)).getdata())


def similarity(fp1, fp2):
    return sum(abs(a - b) for a, b in zip(fp1, fp2)) / len(fp1)


def find_matches(fingerprints):
    numbers = list(fingerprints.keys())
    scored = []
    for i, a in enumerate(numbers):
        for b in numbers[i + 1:]:
            scored.append((similarity(fingerprints[a], fingerprints[b]), a, b))
    scored.sort()

    matches, used = [], set()
    for score, a, b in scored:
        if score >= MATCH_THRESHOLD:
            break
        if a in used or b in used:
            continue
        matches.append((a, b, score))
        used.update((a, b))
    return matches


def create_contact_sheet(cards):
    if not cards:
        return

    total = len(cards)
    if total == 12:
        cols, rows = 4, 3
    elif total == 16:
        cols, rows = 4, 4
    elif total == 20:
        cols, rows = 4, 5
    elif total == 30:
        cols, rows = 5, 6
    else:
        cols = 5
        rows = (total + cols - 1) // cols

    cell = 240
    sheet = Image.new("RGB", (cell * cols, cell * rows), "black")
    draw = ImageDraw.Draw(sheet)

    for n in cards:
        path = os.path.join(CROP_DIR, f"card_{n}.png")
        if not os.path.exists(path):
            continue
        img = Image.open(path).convert("RGB").resize((200, 200))
        x = ((n - 1) % cols) * cell + 20
        y = ((n - 1) // cols) * cell + 20
        sheet.paste(img, (x, y))
        draw.text((x + 5, y + 5), str(n), fill="red")

    sheet.save("all_card_crops.png")


# ==========================================
# SCREEN STATE DETECTION
# ==========================================

def _has(xml, phrase):
    return xml is not None and phrase in xml.lower()


def is_game_screen(xml):
    return _has(xml, "matches")


def is_end_game(xml):
    return _has(xml, "end game")


def is_play_again(xml):
    return _has(xml, "play again")


def is_level_up(xml):
    return _has(xml, "level up")      # heading only, never tap


def is_conveyor_belt(xml):
    return _has(xml, "conveyor belt")


def is_timer_expired(xml):
    return _has(xml, "time expired")


def is_ad_not_ready(xml):
    return _has(xml, "ad not ready")


def is_game_ui(xml):
    if xml is None:
        return False
    low = xml.lower()
    return any(m in low for m in GAME_MARKERS)


def is_play_store(xml):
    return any(n["package"] == PLAY_STORE_PACKAGE for n in parse_nodes(xml))


# ==========================================
# FIND BUTTONS
# ==========================================

def find_button_coordinates(xml, phrase):
    if xml is None:
        return None

    best, best_score = None, -1

    for n in parse_nodes(xml):
        text = n["text"].strip().lower()
        desc = n["desc"].strip().lower()
        rid = n["rid"].strip().lower()

        if not (phrase in text or phrase in desc or phrase in rid):
            continue

        score = 0
        if text == phrase:
            score += 20
        if n["clickable"]:
            score += 15
        if n["enabled"]:
            score += 5
        if phrase in desc:
            score += 3
        if phrase in rid:
            score += 2

        if score > best_score:
            best_score = score
            best = center(n)

    return best


def find_play_again_coordinates(xml):
    return find_button_coordinates(xml, "play again")


def find_play_level_coordinates(xml):
    return find_button_coordinates(xml, "play level")


def find_end_game_coordinates(xml):
    return find_button_coordinates(xml, "end game")


def find_collect_coordinates(xml):
    return find_button_coordinates(xml, "collect")


def find_mine_entrance_coordinates(xml):
    return find_button_coordinates(xml, "mine entrance")


def find_ok_coordinates(xml):
    if xml is None:
        return None
    for n in parse_nodes(xml):
        if n["rid"] == "android:id/button1" or n["text"].strip().lower() == "ok":
            return center(n)
    return None


# ==========================================
# SAFETY: NEVER TAP AD CALL-TO-ACTION AREAS
# ==========================================

def _is_unsafe_node(n):
    label_text = n["text"].strip().lower()
    label_desc = n["desc"].strip().lower()
    return any(u in label_text or u in label_desc for u in UNSAFE_TEXTS)


def point_is_unsafe(nodes, x, y):
    for n in nodes:
        if not _is_unsafe_node(n):
            continue
        if n["x1"] <= x <= n["x2"] and n["y1"] <= y <= n["y2"]:
            return True
    return False


def reward_pending(xml):
    if not WAIT_FOR_REWARD or xml is None:
        return False
    for n in parse_nodes(xml):
        for value in (n["text"], n["desc"]):
            if re.search(r"reward\s+in\s+\d+\s*s", value, re.IGNORECASE):
                return True
    return False


def find_button_next_to_continue(xml):
    if xml is None:
        return None

    all_nodes = parse_nodes(xml)

    cont = next((n for n in all_nodes
                 if n["text"].strip().lower() == "continue"), None)
    if cont is None:
        return None

    candidates = [
        n for n in all_nodes
        if n["clickable"]
        and "Button" in n["class"]
        and not _is_unsafe_node(n)
        and n["x1"] >= cont["x2"] - 10
        and n["y1"] < cont["y2"]
        and n["y2"] > cont["y1"]
    ]
    if not candidates:
        return None

    btn = min(candidates, key=lambda n: n["x1"])
    return center(btn)


CLOSE_WORDS = ("close", "dismiss", "skip", "Reward granted")
CLOSE_GLYPHS = ("x", "×", "✕", "✖", "✗", "╳")


def close_tap_point(n, nodes):
    """Where to tap inside a close-type node (avoid 'Open Website' pills)."""

    w = n["x2"] - n["x1"]
    h = n["y2"] - n["y1"]

    if w <= h * 1.5:
        return center(n)

    icons = [
        m for m in nodes
        if m is not n
        and "ImageView" in m["class"]
        and not m["text"].strip()
        and m["x1"] >= n["x1"] and m["x2"] <= n["x2"]
        and m["y1"] >= n["y1"] and m["y2"] <= n["y2"]
    ]
    if icons:
        icon = max(icons, key=lambda m: m["x2"])
        return center(icon)

    return (n["x2"] - h // 2 - 40, (n["y1"] + n["y2"]) // 2)


def find_close_by_label(nodes):
    """Close-type control in content-desc / text / resource-id."""

    best, best_score = None, -1

    for n in nodes:
        desc = n["desc"].strip().lower()
        text = n["text"].strip().lower()
        rid = n["rid"].strip().lower()

        label = f"{desc} {text} {rid}"

        if "mute" in label or _is_unsafe_node(n):
            continue

        is_close = (
            any(w in label for w in CLOSE_WORDS)
            or desc in CLOSE_GLYPHS
            or text in CLOSE_GLYPHS
        )
        if not is_close:
            continue

        score = 0
        if n["clickable"] or n["parent_clickable"]:
            score += 10
        if n["enabled"]:
            score += 3
        if desc in CLOSE_GLYPHS or text in CLOSE_GLYPHS:
            score += 5
        if "close" in label:
            score += 4
        # prefer controls near the top of the screen
        score += max(0, 3 - n["y1"] // 400)

        if score > best_score:
            best_score = score
            best = n

    if best is None:
        return None

    pt = close_tap_point(best, nodes)

    if point_is_unsafe(nodes, *pt):
        return None

    return pt


# ==========================================
# AD HANDLING
# ==========================================

def try_close_ad(xml, ad_ticks):
    """One attempt to close an ad. Returns True if something was tapped."""

    nodes = parse_nodes(xml)

    if reward_pending(xml):
        print("  reward countdown running, waiting...")
        return False

    pt = find_close_by_label(nodes)
    if pt:
        print(f"  close button -> {pt}")
        tap_xy(*pt)
        return True

    pt = find_button_next_to_continue(xml)
    if pt:
        print(f"  continue button -> {pt}")
        tap_xy(*pt)
        return True

    if BLIND_CLOSE_ENABLED and ad_ticks >= BLIND_CLOSE_AFTER:
        w, h = device_screen_size(xml)
        x, y = int(w * CLOSE_FRAC[0]), int(h * CLOSE_FRAC[1])
        if not point_is_unsafe(nodes, x, y):
            print(f"  blind close tap -> ({x}, {y})")
            tap_xy(x, y)
            return True

    if LAST_RESORT_ENABLED and ad_ticks >= LAST_RESORT_AFTER:
        print("  last resort: BACK")
        press_back()
        time.sleep(LAST_RESORT_WAIT)
        return True

    return False


# ==========================================
# ROUND LOGIC
# ==========================================

def play_round(expected):
    """Detect cards, capture faces in pairs, match, then tap matches."""

    cards, xml = detect_cards_with_retry(expected)

    if not cards or (expected and len(cards) != expected):
        print(f"\nERROR: Expected {expected} cards, but detected {len(cards)}.")
        save_debug_xml(xml, "debug_cards.xml")
        return False

    print_cards(cards)

    # ---- capture: flip two cards at a time, screenshot, crop ----
    fingerprints = {}
    numbers = sorted(cards)

    for i in range(0, len(numbers) - 1, 2):
        a, b = numbers[i], numbers[i + 1]
        (xa, ya), (xb, yb) = cards[a], cards[b]

        tap_pair(xa, ya, xb, yb)
        time.sleep(CAPTURE_FLIP_DELAY)

        take_screenshot()
        img = Image.open(SCREENSHOT).convert("RGB")

        fingerprints[a] = fingerprint(crop_card(a, xa, ya, img))
        fingerprints[b] = fingerprint(crop_card(b, xb, yb, img))

        time.sleep(PAIR_SETTLE_DELAY)

    # odd card out (shouldn't happen: counts are even)
    if len(numbers) % 2:
        n = numbers[-1]
        x, y = cards[n]
        tap_xy(x, y)
        time.sleep(CAPTURE_FLIP_DELAY)
        take_screenshot()
        fingerprints[n] = fingerprint(crop_card(n, x, y))
        time.sleep(PAIR_SETTLE_DELAY)

    create_contact_sheet(cards)

    matches = find_matches(fingerprints)
    print(f"Found {len(matches)} matches")

    for a, b, score in matches:
        print(f"MATCH {a} <-> {b}  (score {score:.1f})")
        (xa, ya), (xb, yb) = cards[a], cards[b]
        tap_pair(xa, ya, xb, yb)
        time.sleep(PAIR_SETTLE_DELAY)

    return True


def handle_mine_search():
    """After 'conveyor belt' appears, scroll until 'mine entrance' is visible."""

    w, h = device_screen_size()
    delta = int(h * MINE_SCROLL_FRACTION)
    mid_x = w // 2
    direction_down = True

    for attempt in range(1, MINE_SCROLL_MAX_TRIES + 1):

        xml = dump_ui()
        pt = find_mine_entrance_coordinates(xml)

        if pt:
            print(f"Mine entrance -> {pt}")
            tap_xy(*pt)
            return True

        if attempt % MINE_FLIP_AFTER == 0:
            direction_down = not direction_down

        cy = h // 2
        if direction_down:
            swipe(mid_x, cy + delta, mid_x, cy - delta)
        else:
            swipe(mid_x, cy - delta, mid_x, cy + delta)

        time.sleep(0.6)

    print("Mine entrance not found.")
    return False


# ==========================================
# MAIN LOOP
# ==========================================

def wait_for_round_end(timeout=12):
    """After tapping matches, wait for either an end screen or leftover cards."""

    end = time.time() + timeout

    while time.time() < end:

        xml = dump_ui()

        if xml:
            if (find_play_again_coordinates(xml) or is_level_up(xml)
                    or is_timer_expired(xml) or is_conveyor_belt(xml)):
                return "ended"

            if len(find_card_coordinates(xml)) >= 2:
                return "cards_left"

        time.sleep(0.7)

    return "timeout"


def tap_first_of(xml, phrases):
    for phrase in phrases:
        pt = find_button_coordinates(xml, phrase)
        if pt:
            print(f"  tapping '{phrase}' -> {pt}")
            tap_xy(*pt)
            return True
    return False


def main():

    rounds = 0
    ad_ticks = 0
    fails = 0
    expected = None
    fresh = True          # True = a brand-new board (strict card count)

    while MAX_ROUNDS is None or rounds < MAX_ROUNDS:

        if focused_package() == PLAY_STORE_PACKAGE:
            print("Play Store opened by mistake -> BACK")
            press_back()
            time.sleep(1)
            continue

        xml = dump_ui()

        if xml is None:
            print("UI dump failed, retrying...")
            time.sleep(UI_DUMP_INTERVAL)
            continue

        if is_play_store(xml):
            press_back()
            time.sleep(1)
            continue

        # ---- dialogs ----
        if is_ad_not_ready(xml):
            pt = find_ok_coordinates(xml)
            if pt:
                tap_xy(*pt)
            print(f"Ad not ready, waiting {AD_RETRY_DELAY}s...")
            time.sleep(AD_RETRY_DELAY)
            xml = dump_ui()
            pt = find_play_level_coordinates(xml)
            if pt:
                tap_xy(*pt)
            fresh = True
            continue

        # ---- game screens ----
        if is_game_ui(xml):
            ad_ticks = 0

            # 1) END-OF-ROUND screens FIRST (they may still contain "matches")
            if is_timer_expired(xml):
                if tap_first_of(xml, ("collect",)):
                    fresh = True
                    time.sleep(1)
                    continue

            pt = find_play_again_coordinates(xml)
            if pt:
                print("Play Again")
                tap_xy(*pt)
                fresh = True
                time.sleep(NEW_ROUND_DELAY)
                continue

            if is_level_up(xml):
                print("Level up screen")
                if tap_first_of(xml, ("collect", "claim", "continue", "next",
                                      "play level")):
                    fresh = True
                time.sleep(1.5)
                continue

            pt_belt = find_button_coordinates(xml, "conveyor belt")
            if pt_belt and not is_game_screen(xml):
                print(f"Entering Conveyor Belt -> {pt_belt}")
                tap_xy(*pt_belt)
                fresh = True
                time.sleep(NEW_ROUND_DELAY)
                continue

            pt_mine = find_mine_entrance_coordinates(xml)
            if pt_mine and not is_game_screen(xml):
                print(f"Entering Mine -> {pt_mine}")
                tap_xy(*pt_mine)
                fresh = True
                time.sleep(NEW_ROUND_DELAY)
                continue

            # 2) Board with cards -> play (Only when in actual game screen with cards)
            cards_now = find_card_coordinates(xml)

            if is_game_screen(xml) and (len(cards_now) >= 2 or fresh):

                if fresh:
                    expected = update_expected_cards(xml) or expected

                ok = play_round(expected if fresh else None)

                if not ok:
                    fails += 1
                    print(f"Round attempt failed ({fails}/3)")
                    if fails >= 3:
                        print("Could not start/play the round.")
                        break
                    time.sleep(1)
                    continue

                fails = 0
                fresh = False
                rounds += 1
                print(f"--- round {rounds} tapped, waiting for result ---")

                state = wait_for_round_end()
                print(f"    result: {state}")
                continue

            # 3) Lobby / pre-round screen
            pt = find_play_level_coordinates(xml)
            if pt:
                expected = update_expected_cards(xml) or expected
                print("Play Level")
                tap_xy(*pt)
                fresh = True
                time.sleep(NEW_ROUND_DELAY)
                continue

            if tap_first_of(xml, ("collect", "end game")):
                time.sleep(1)
                continue

            print("Game screen, nothing to do yet...")
            time.sleep(UI_DUMP_INTERVAL)
            continue

        # ---- not the game: probably an ad ----
        ad_ticks += 1
        print(f"Ad screen (tick {ad_ticks})")
        try_close_ad(xml, ad_ticks)
        time.sleep(AD_DUMP_INTERVAL)

    print("\nDone.")


if __name__ == "__main__":
    main()