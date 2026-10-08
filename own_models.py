"""Eigene (selbst erzeugte) Block-Formen und Texturen im Minecraft-Stil.
Keine Mojang-Dateien: alle Texturen werden hier aus Farbe + Muster berechnet. Dient der oeffentlichen Version."""
import hashlib, re, struct, zlib

HIDDEN = {"barrier", "light", "structure_void", "air", "cave_air", "void_air", "moving_piston"}
DIRS = ("up", "down", "north", "south", "east", "west")

# ---------------------------------------------------------------- Texturen (PNG, 16x16)
def _rng(seed):
    s = int.from_bytes(hashlib.md5(seed.encode()).digest()[:4], "little") or 1
    while True:
        s = (s * 1103515245 + 12345) & 0x7FFFFFFF
        yield s / 0x7FFFFFFF

def _clamp(v): return max(0, min(255, int(v)))

def png(width, height, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")

def _hex(h): return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def texture(pattern, hexcolor):
    """Liefert PNG-Bytes fuer 'gen/<pattern>/<hex>'."""
    r0, g0, b0 = _hex(hexcolor)
    _g = _rng(pattern + hexcolor)
    rnd = lambda: next(_g)
    px = [[None] * 16 for _ in range(16)]
    def shade(k): return (_clamp(r0 * k), _clamp(g0 * k), _clamp(b0 * k), 255)
    for y in range(16):
        for x in range(16):
            n = rnd()
            k = 0.9 + 0.2 * n
            px[y][x] = shade(k)
    if pattern == "planks":
        for y in range(16):
            for x in range(16):
                if y % 4 == 3: px[y][x] = shade(0.72)
                elif (x + (y // 4) * 7) % 16 == 0: px[y][x] = shade(0.8)
    elif pattern == "log":
        for y in range(16):
            for x in range(16):
                k = 0.78 + 0.28 * ((x * 5 + (x // 3) * 3) % 4) / 3
                px[y][x] = shade(k * (0.95 + 0.1 * rnd()))
    elif pattern == "logtop":
        for y in range(16):
            for x in range(16):
                d = max(abs(x - 7.5), abs(y - 7.5))
                ring = 0.85 + 0.25 * (int(d) % 2)
                px[y][x] = shade(ring * (0.97 + 0.06 * rnd())) if d < 7 else shade(0.7)
    elif pattern == "bricks":
        for y in range(16):
            for x in range(16):
                off = 4 if (y // 4) % 2 else 0
                if y % 4 == 3 or (x + off) % 8 == 7: px[y][x] = shade(0.6)
    elif pattern == "stone":
        for y in range(16):
            for x in range(16):
                if rnd() > 0.88: px[y][x] = shade(0.78)
    elif pattern == "ore":
        for y in range(16):
            for x in range(16): px[y][x] = (130, 130, 130, 255) if rnd() > 0.1 else (112, 112, 112, 255)
        for _ in range(7):
            cx, cy = int(rnd() * 14) + 1, int(rnd() * 14) + 1
            for dx, dy in ((0, 0), (1, 0), (0, 1)):
                px[(cy + dy) % 16][(cx + dx) % 16] = shade(1.1 + 0.2 * rnd())
    elif pattern == "glass":
        for y in range(16):
            for x in range(16):
                edge = x in (0, 15) or y in (0, 15)
                a = 230 if edge else (60 if (x + y) % 9 else 130)
                px[y][x] = (_clamp(r0 * (1.0 if edge else 1.1)), _clamp(g0 * (1.0 if edge else 1.1)), _clamp(b0 * (1.0 if edge else 1.1)), a)
    elif pattern == "leaves":
        for y in range(16):
            for x in range(16):
                n = rnd()
                px[y][x] = shade(0.7 + 0.5 * n) if n > 0.18 else (0, 0, 0, 0)
    elif pattern == "plant":
        for y in range(16):
            for x in range(16): px[y][x] = (0, 0, 0, 0)
        for _ in range(7):
            x0 = int(rnd() * 14) + 1; h = 6 + int(rnd() * 9)
            for yy in range(16 - h, 16): px[yy][x0] = shade(0.8 + 0.4 * rnd())
            px[16 - h][x0] = shade(1.25)
    elif pattern == "grassside":
        for y in range(16):
            for x in range(16):
                n = rnd()
                if y < 3 + (1 if n > 0.5 else 0): px[y][x] = (_clamp(r0 * (0.9 + 0.2 * n)), _clamp(g0 * (0.9 + 0.2 * n)), _clamp(b0 * (0.9 + 0.2 * n)), 255)
                else:
                    k = 0.9 + 0.2 * n; px[y][x] = (_clamp(134 * k), _clamp(96 * k), _clamp(67 * k), 255)
    elif pattern == "water":
        for y in range(16):
            for x in range(16): px[y][x] = (_clamp(r0 * (0.95 + 0.1 * rnd())), _clamp(g0 * (0.95 + 0.1 * rnd())), _clamp(b0), 170)
    elif pattern == "plain":
        for y in range(16):
            for x in range(16): px[y][x] = shade(0.97 + 0.06 * rnd())
    return png(16, 16, [c for row in px for p in row for c in p])

# ---------------------------------------------------------------- Farben und Muster pro Block
COLORS = [
    ("grass_block", "5d9b3a"), ("short_grass", "6fae45"), ("tall_grass", "6fae45"), ("fern", "5a9a3a"), ("leaves", "3f7a2c"), ("moss", "4a6b2a"),
    ("podzol", "6b4a2a"), ("dirt", "86603f"), ("mud", "4a3b32"), ("farmland", "6b4a2a"), ("sand", "dbcf9b"), ("gravel", "8a8481"), ("clay", "9ea4b0"),
    ("deepslate", "4c4c52"), ("blackstone", "2e2a30"), ("basalt", "4a4a50"), ("netherrack", "6f3636"), ("nether_brick", "3a1d22"), ("end_stone", "dfe0a6"),
    ("purpur", "a77ba7"), ("obsidian", "1b1030"), ("cobblestone", "7d7d7d"), ("stone_brick", "7b7b7b"), ("andesite", "888888"), ("diorite", "bdbdbd"),
    ("granite", "9a6b59"), ("tuff", "6b6e62"), ("calcite", "dcdcd6"), ("stone", "8a8a8a"), ("sandstone", "d8cb8d"), ("terracotta", "9a5d43"),
    ("copper", "c26a46"), ("iron", "d8d8d8"), ("gold", "f5d33f"), ("diamond", "5fe0d6"), ("emerald", "2bc35a"), ("lapis", "2a4fa8"), ("redstone", "b01a1a"),
    ("coal", "1a1a1a"), ("quartz", "ece6df"), ("prismarine", "4fa19a"), ("glowstone", "f0c96a"), ("sea_lantern", "bfe8dc"), ("shroomlight", "f0a45a"),
    ("lantern", "f0b55a"), ("torch", "f0b030"), ("glass", "9fd8e8"), ("ice", "9ec4f0"), ("snow", "f4f8fa"), ("water", "3f76e4"), ("lava", "e0601a"),
    ("dark_oak", "4a3320"), ("oak", "b08a52"), ("spruce", "6e4f2e"), ("birch", "d4c38a"), ("jungle", "a1744a"), ("acacia", "b3603a"), ("mangrove", "7a3a3a"),
    ("cherry", "e8b4c0"), ("bamboo", "c9c05a"), ("crimson", "7a2a4a"), ("warped", "2a7a76"), ("planks", "b08a52"), ("log", "6e5230"), ("wood", "6e5230"),
    ("crafting_table", "9a6a3a"), ("furnace", "6a6a6a"), ("smoker", "6a5a4a"), ("anvil", "3a3a3a"), ("smithing_table", "4a3a3a"), ("grindstone", "8a8a8a"),
    ("bookshelf", "8a6a3a"), ("bed", "c23a3a"), ("chest", "9a6a2a"), ("barrel", "8a5a2a"), ("hopper", "4a4a4a"), ("cobweb", "dddddd"), ("bedrock", "2a2a2a"),
    ("stairs", "9a8a70"), ("slab", "9a8a70"), ("fence", "9a7a4a"), ("door", "9a7a4a"), ("ore", "7a7a7a"), ("brick", "9a4a3a"),
]
DYES = {"white": "e8eaea", "orange": "e07a1f", "magenta": "c24fc2", "light_blue": "4fa8e0", "yellow": "e8cf2a", "lime": "7ac21f", "pink": "e88aae",
        "gray": "4a4e52", "light_gray": "9a9a96", "cyan": "168a96", "purple": "7a3aa8", "blue": "3a46a8", "brown": "6a4426", "green": "4a6a22", "red": "a82a26", "black": "181a1e"}

def color_of(name):
    n = name
    for dye, col in DYES.items():
        if n.startswith(dye + "_") and any(k in n for k in ("wool", "concrete", "terracotta", "glass", "carpet", "bed", "banner", "shulker", "candle")):
            return col
    for key, col in COLORS:
        if key in n:
            return col
    h = int(hashlib.md5(n.encode()).hexdigest()[:6], 16)
    return "%02x%02x%02x" % (90 + (h >> 16) % 120, 90 + (h >> 8) % 120, 90 + h % 120)

def pattern_of(n):
    if "planks" in n or n.endswith(("_fence", "_fence_gate", "_door", "_trapdoor", "_sign", "_stairs", "_slab")) and any(w in n for w in ("oak", "spruce", "birch", "jungle", "acacia", "mangrove", "cherry", "bamboo", "crimson", "warped")): return "planks"
    if any(w in n for w in ("crafting_table", "bookshelf", "chest", "barrel")): return "planks"
    if n.endswith(("_log", "_wood", "_stem", "_hyphae", "_block")) and any(w in n for w in ("log", "wood", "stem", "hyphae")) and "plank" not in n: return "log"
    if "brick" in n: return "bricks"
    if n.endswith("_ore"): return "ore"
    if "glass" in n or n in ("ice", "packed_ice", "blue_ice") or "ice" == n: return "glass"
    if "leaves" in n: return "leaves"
    if any(w in n for w in ("wool", "concrete", "carpet")): return "plain"
    if any(w in n for w in ("stone", "deepslate", "andesite", "diorite", "granite", "tuff", "cobble", "basalt", "blackstone", "bedrock", "netherrack", "obsidian")): return "stone"
    return "noise"

# ---------------------------------------------------------------- Formen
def spec(pattern, hexcolor): return "gen/%s/%s" % (pattern, hexcolor)

def box(f, t, tex):
    """tex: Dict Richtung -> spec oder ein spec fuer alle. Setzt cullface, wenn die Flaeche die Blockgrenze ganz ausfuellt."""
    fc = {}
    for d in DIRS:
        s = tex.get(d) if isinstance(tex, dict) else tex
        if s is None:
            continue
        face = {"t": s}
        full = False
        if d == "up": full = t[1] == 16 and f[0] == 0 and t[0] == 16 and f[2] == 0 and t[2] == 16
        elif d == "down": full = f[1] == 0 and f[0] == 0 and t[0] == 16 and f[2] == 0 and t[2] == 16
        elif d == "north": full = f[2] == 0 and f[0] == 0 and t[0] == 16 and f[1] == 0 and t[1] == 16
        elif d == "south": full = t[2] == 16 and f[0] == 0 and t[0] == 16 and f[1] == 0 and t[1] == 16
        elif d == "west": full = f[0] == 0 and f[2] == 0 and t[2] == 16 and f[1] == 0 and t[1] == 16
        elif d == "east": full = t[0] == 16 and f[2] == 0 and t[2] == 16 and f[1] == 0 and t[1] == 16
        if full: face["c"] = d
        fc[d] = face
    return {"f": list(f), "t": list(t), "fc": fc}

def cross(s):
    fc1 = {d: {"t": s} for d in ("north", "south")}; fc2 = {d: {"t": s} for d in ("west", "east")}
    rot = {"o": [8, 8, 8], "a": "y", "g": 45, "s": 1}
    return [{"f": [0.8, 0, 8], "t": [15.2, 16, 8], "fc": fc1, "r": rot, "sh": 0}, {"f": [8, 0, 0.8], "t": [8, 16, 15.2], "fc": fc2, "r": rot, "sh": 0}]

def _model(els, y=0, x=0): return {"x": x, "y": y, "e": els}

PLANT_WORDS = ("flower", "sapling", "tulip", "orchid", "dandelion", "poppy", "rose_bush", "cornflower", "lily_of_the_valley", "bush", "allium", "azure_bluet", "oxeye",
               "fern", "grass", "seagrass", "kelp", "wheat", "carrots", "potatoes", "beetroots", "sugar_cane", "mushroom", "roots", "cobweb", "vine", "coral_fan", "bamboo", "sweet_berry",
               "pitcher", "torchflower", "peony", "lilac", "sunflower", "nether_wart", "dead_bush", "propagule")

def build(name, props, color_of_fn=None):
    n = name
    if n in HIDDEN: return None
    col = (color_of_fn or color_of)(n)
    pat = pattern_of(n)
    base = spec(pat, col)
    P = lambda k: props.get(k)
    def r(opaque, tr, els, y=0): return {"opaque": opaque, "tr": tr, "m": [_model(els, y)]}

    if n == "water": return r(False, True, [box((0, 0, 0), (16, 16, 16), spec("water", "3f76e4"))])
    if n == "lava": return r(False, False, [box((0, 0, 0), (16, 16, 16), spec("noise", "e0601a"))])
    # Pflanzen
    if any(w in n for w in PLANT_WORDS) and "block" not in n and not n.endswith(("_slab", "_stairs", "_wall", "_fence")):
        if n == "bamboo" or n == "sugar_cane" or n == "kelp":
            pass
        green = "5fae3c" if not any(w in n for w in ("flower", "tulip", "orchid", "dandelion", "poppy", "rose", "cornflower", "allium", "azure", "oxeye", "peony", "lilac", "sunflower", "torchflower")) else color_of("x_" + n) if False else col
        if any(w in n for w in ("flower", "tulip", "orchid", "dandelion", "poppy", "rose", "cornflower", "allium", "azure", "oxeye", "peony", "lilac", "sunflower", "torchflower")):
            hexc = {"dandelion": "f2d21b", "poppy": "d02a22", "cornflower": "4a6fe0", "azure_bluet": "e8eaea", "oxeye": "f4f4f4", "allium": "b86ad8", "peony": "e88aae", "lilac": "c78ad0", "sunflower": "f5c518"}
            hexc = next((v for k, v in hexc.items() if k in n), "e05a8a")
            return r(False, False, cross(spec("plant", hexc)))
        if "mushroom" in n and "block" not in n: return r(False, False, cross(spec("plant", "c0392b" if "red" in n else "a1744a")))
        if "cobweb" in n: return r(False, False, cross(spec("plant", "e8e8e8")))
        return r(False, False, cross(spec("plant", green)))
    # Platten
    if n.endswith("_slab"):
        t = P("type") or "bottom"
        if t == "double": return r(True, False, [box((0, 0, 0), (16, 16, 16), base)])
        return r(False, False, [box((0, 0, 0), (16, 8, 16), base) if t == "bottom" else box((0, 8, 0), (16, 16, 16), base)])
    # Treppen
    if n.endswith("_stairs"):
        face = P("facing") or "north"; top = P("half") == "top"
        b = box((0, 8, 0), (16, 16, 16), base) if top else box((0, 0, 0), (16, 8, 16), base)
        y0, y1 = (0, 8) if top else (8, 16)
        hi = {"east": ((8, y0, 0), (16, y1, 16)), "west": ((0, y0, 0), (8, y1, 16)), "south": ((0, y0, 8), (16, y1, 16)), "north": ((0, y0, 0), (16, y1, 8))}[face]
        return r(False, False, [b, box(hi[0], hi[1], base)])
    # Zaeune, Mauern
    if n.endswith("_fence") or n.endswith("_wall"):
        wall = n.endswith("_wall")
        w0, w1 = (4, 12) if wall else (6, 10)
        els = [box((w0, 0, w0), (w1, 16, w1), base)]
        a0, a1 = (5, 11) if wall else (7, 9)
        for d, (f, t) in {"north": ((a0, 0 if wall else 6, 0), (a1, 14 if wall else 15, w0)), "south": ((a0, 0 if wall else 6, w1), (a1, 14 if wall else 15, 16)),
                          "west": ((0, 0 if wall else 6, a0), (w0, 14 if wall else 15, a1)), "east": ((w1, 0 if wall else 6, a0), (16, 14 if wall else 15, a1))}.items():
            if P(d) in ("true", "low", "tall"): els.append(box(f, t, base))
        return r(False, False, els)
    if n.endswith("_fence_gate"): return r(False, False, [box((0, 5, 6), (16, 16, 10), base)], 90 if P("facing") in ("east", "west") else 0)
    # Scheiben, Gitter
    if n.endswith("_pane") or n == "iron_bars":
        s = spec("glass", col) if n.endswith("_pane") else spec("stone", "6a6a6a")
        els = [box((7, 0, 7), (9, 16, 9), s)]
        for d, (f, t) in {"north": ((7, 0, 0), (9, 16, 7)), "south": ((7, 0, 9), (9, 16, 16)), "west": ((0, 0, 7), (7, 16, 9)), "east": ((9, 0, 7), (16, 16, 9))}.items():
            if P(d) == "true": els.append(box(f, t, s))
        return r(False, n.endswith("_pane"), els)
    # Flaches
    if n.endswith("_carpet") or n.endswith("_pressure_plate") or n in ("rail", "powered_rail", "detector_rail", "activator_rail") or n.endswith("_button") and False:
        return r(False, False, [box((0, 0, 0), (16, 1 if n.endswith("_carpet") else 1, 16), base)])
    if n == "snow":
        return r(False, False, [box((0, 0, 0), (16, 2 * int(P("layers") or 1), 16), base)])
    # Falltueren, Tueren, Leitern, Schilder
    face_y = {"north": 0, "east": 90, "south": 180, "west": 270}
    if n.endswith("_trapdoor"):
        if P("open") == "true": return r(False, False, [box((0, 0, 13), (16, 16, 16), base)], face_y.get(P("facing") or "north", 0))
        return r(False, False, [box((0, 13, 0), (16, 16, 16), base) if P("half") == "top" else box((0, 0, 0), (16, 3, 16), base)])
    if n.endswith("_door"): return r(False, False, [box((0, 0, 13), (16, 16, 16), base)], face_y.get(P("facing") or "north", 0))
    if n == "ladder": return r(False, False, [box((0, 0, 15), (16, 16, 16), spec("planks", "9a7a4a"))], face_y.get(P("facing") or "north", 0))
    if n.endswith("_wall_sign") or n.endswith("_wall_hanging_sign"): return r(False, False, [box((0, 4.5, 14), (16, 12.5, 16), base)], face_y.get(P("facing") or "north", 0))
    if n.endswith("_sign") or n.endswith("_hanging_sign"):
        rot = int(P("rotation") or 0) * 22.5
        return r(False, False, [box((0, 8, 7), (16, 16, 9), base), box((7, 0, 7), (9, 8, 9), base)], rot)
    # Lichter
    if "torch" in n and "flower" not in n: return r(False, False, [box((7, 0, 7), (9, 10, 9), spec("plain", "f0b030"))])
    if n.endswith("lantern"): return r(False, False, [box((5, 0, 5), (11, 7, 11), spec("plain", "f0b55a"))])
    if n in ("lever",) or n.endswith("_button"): return r(False, False, [box((5, 0, 5), (11, 3, 11), spec("stone", "8a8a8a"))])
    # Truhen, Betten, Shulker
    if n in ("chest", "trapped_chest", "ender_chest") or n.endswith("_chest"):
        return r(False, False, [box((1, 0, 1), (15, 14, 15), spec("planks", "7a4a1c" if n != "ender_chest" else "1a2a30"))])
    if n.endswith("_bed"): return r(False, False, [box((0, 3, 0), (16, 9, 16), spec("plain", col)), box((0, 0, 0), (16, 3, 16), spec("planks", "7a5a3a"))])
    if n.endswith("shulker_box"): return r(False, False, [box((0, 0, 0), (16, 16, 16), spec("plain", col if n != "shulker_box" else "8a5a9a"))])
    # Bloecke mit Spezialtexturen
    tr = pat == "glass"
    if n == "grass_block":
        return r(True, False, [box((0, 0, 0), (16, 16, 16), {"up": spec("noise", "5d9b3a"), "down": spec("noise", "86603f"), "north": spec("grassside", "5d9b3a"), "south": spec("grassside", "5d9b3a"),
                                                              "east": spec("grassside", "5d9b3a"), "west": spec("grassside", "5d9b3a")})])
    if pat == "log":
        s = spec("log", col); top = spec("logtop", col)
        ax = P("axis") or "y"
        faces = {"up": top, "down": top, "north": s, "south": s, "east": s, "west": s}
        if ax == "x": faces = {"east": top, "west": top, "up": s, "down": s, "north": s, "south": s}
        if ax == "z": faces = {"north": top, "south": top, "up": s, "down": s, "east": s, "west": s}
        return r(True, False, [box((0, 0, 0), (16, 16, 16), faces)])
    if n.endswith("_ore"):
        return r(True, False, [box((0, 0, 0), (16, 16, 16), spec("ore", color_of(n.replace("deepslate_", "").replace("_ore", ""))))])
    if pat == "leaves": return r(False, False, [box((0, 0, 0), (16, 16, 16), spec("leaves", "3f7a2c" if "birch" not in n else "6f9a45"))])
    return r(not tr, tr, [box((0, 0, 0), (16, 16, 16), base)])

def icon_spec(entry):
    for m in entry["m"]:
        for e in m["e"]:
            if e["f"] == [0, 0, 0] and e["t"] == [16, 16, 16]:
                return (e["fc"].get("north") or e["fc"].get("up"))["t"]
    e = entry["m"][0]["e"][0]
    return (e["fc"].get("north") or next(iter(e["fc"].values())))["t"]
