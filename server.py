#!/usr/bin/env python3
"""Schematic Hub: Konten, Upload, Vorschau und Download fuer .litematic-Dateien. Nur Python-Standardbibliothek."""
import array, gzip, hashlib, hmac, io, json, mimetypes, os, re, secrets, sqlite3, struct, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(ROOT, "VERSION")) as _vf:
        BUILD = _vf.read().strip()
except OSError:
    BUILD = "dev"
DATA = os.environ.get("HUB_DATA", os.path.join(ROOT, "data"))
FILES = os.path.join(DATA, "files")
PUBLIC = os.environ.get("HUB_PUBLIC") == "1"          # oeffentliche Version: nur eigene Texturen, Einladungscode
INVITE = os.environ.get("HUB_INVITE", "").strip()
PREVIEWS = os.path.join(DATA, "previews_pub" if PUBLIC else "previews")
GRIDS = os.path.join(DATA, "grids")
AVATARS = os.path.join(DATA, "avatars")
COVERS = os.path.join(DATA, "covers")
DB_PATH = os.path.join(DATA, "hub.db")
STATIC = os.path.join(ROOT, "static")
MAX_UPLOAD = 25 * 1024 * 1024
MAX_PREVIEW_BLOCKS = 4_000_000
MAX_PREVIEW_VOXELS = 150_000
SESSION_DAYS = 30
os.makedirs(FILES, exist_ok=True)
os.makedirs(PREVIEWS, exist_ok=True)
os.makedirs(GRIDS, exist_ok=True)
os.makedirs(AVATARS, exist_ok=True)
os.makedirs(COVERS, exist_ok=True)

# ------------------------------------------------------------------ Datenbank

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, salt BLOB, hash BLOB, created INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, expires INTEGER);
    CREATE TABLE IF NOT EXISTS likes(
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        schematic_id INTEGER REFERENCES schematics(id) ON DELETE CASCADE,
        created INTEGER, PRIMARY KEY(user_id, schematic_id));
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY, schematic_id INTEGER REFERENCES schematics(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, text TEXT, created INTEGER);
    CREATE TABLE IF NOT EXISTS ratings(
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        schematic_id INTEGER REFERENCES schematics(id) ON DELETE CASCADE,
        stars INTEGER, created INTEGER, PRIMARY KEY(user_id, schematic_id));
    CREATE TABLE IF NOT EXISTS follows(
        follower_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        followee_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        created INTEGER, PRIMARY KEY(follower_id, followee_id));
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        type TEXT, text TEXT, link TEXT, created INTEGER, is_read INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS collections(
        id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        name TEXT, descr TEXT, public INTEGER, created INTEGER);
    CREATE TABLE IF NOT EXISTS collection_items(
        collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
        schematic_id INTEGER REFERENCES schematics(id) ON DELETE CASCADE,
        added INTEGER, PRIMARY KEY(collection_id, schematic_id));
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY, reporter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT, target_id INTEGER, reason TEXT, created INTEGER, resolved INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS schematics(
        id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        title TEXT, descr TEXT, tags TEXT, public INTEGER, size INTEGER,
        sx INTEGER, sy INTEGER, sz INTEGER, blocks INTEGER, downloads INTEGER DEFAULT 0,
        created INTEGER, materials TEXT, has_preview INTEGER);
    """)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(schematics)")}
    for name, ddl in (("views", "INTEGER DEFAULT 0"), ("file_hash", "TEXT"), ("updated", "INTEGER"), ("has_grid", "INTEGER DEFAULT 0"), ("cover_ver", "INTEGER DEFAULT 0")):
        if name not in cols:
            c.execute("ALTER TABLE schematics ADD COLUMN %s %s" % (name, ddl))
    ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    for name, ddl in (("avatar_ver", "INTEGER DEFAULT 0"), ("is_admin", "INTEGER DEFAULT 0")):
        if name not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, ddl))
    if not c.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        c.execute("UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)")
    c.commit()
    c.close()

# ------------------------------------------------------------------ Passwoerter und Sitzungen

def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 300_000)

def new_session(c, user_id):
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?)",
              (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + SESSION_DAYS * 86400))
    c.commit()
    return token

def user_from_token(c, token):
    if not token:
        return None
    row = c.execute("SELECT u.id, u.name, u.is_admin, u.avatar_ver, s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                    (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or row["expires"] < time.time():
        return None
    unread = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "name": row["name"], "admin": bool(row["is_admin"]), "av": row["avatar_ver"] or 0, "unread": unread}

def notify(c, user_id, kind, text, link):
    c.execute("INSERT INTO notifications(user_id,type,text,link,created) VALUES(?,?,?,?,?)", (user_id, kind, text[:300], link, int(time.time())))

_attempts = {}
_attempts_lock = threading.Lock()

def too_many(ip, limit=10, window=300):
    now = time.time()
    with _attempts_lock:
        lst = [t for t in _attempts.get(ip, []) if now - t < window]
        lst.append(now)
        _attempts[ip] = lst
        return len(lst) > limit

# ------------------------------------------------------------------ NBT und Litematic

class NBTError(Exception):
    pass

def parse_nbt(data):
    f = io.BytesIO(data)
    def rd(fmt):
        n = struct.calcsize(fmt)
        b = f.read(n)
        if len(b) < n:
            raise NBTError("zu kurz")
        return struct.unpack(fmt, b)[0]
    def rstr():
        return f.read(rd(">H")).decode("utf8", "replace")
    def payload(t, depth=0):
        if depth > 40:
            raise NBTError("zu tief")
        if t in (1, 2, 3, 4, 5, 6):
            return rd({1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}[t])
        if t == 7:
            n = rd(">i")
            if n < 0 or n > 200_000_000:
                raise NBTError("Array")
            b = f.read(n)
            if len(b) < n:
                raise NBTError("zu kurz")
            return b
        if t == 8:
            return rstr()
        if t == 9:
            it = rd(">b"); n = rd(">i")
            if n < 0 or n > 5_000_000:
                raise NBTError("Liste")
            return [payload(it, depth + 1) for _ in range(n)]
        if t == 10:
            d = {}
            while True:
                tt = rd(">b")
                if tt == 0:
                    return d
                k = rstr(); d[k] = payload(tt, depth + 1)
        if t == 11:
            n = rd(">i")
            if n < 0 or n > 50_000_000:
                raise NBTError("Array")
            b = f.read(4 * n)
            if len(b) < 4 * n:
                raise NBTError("zu kurz")
            return list(struct.unpack(">%di" % n, b))
        if t == 12:
            n = rd(">i")
            if n < 0 or n > 50_000_000:
                raise NBTError("Array")
            b = f.read(8 * n)
            if len(b) < 8 * n:
                raise NBTError("zu kurz")
            return struct.unpack(">%dQ" % n, b)
        raise NBTError("Tag %d" % t)
    if rd(">b") != 10:
        raise NBTError("kein Compound")
    rstr()
    return payload(10)

AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}

COLOR_RULES = [
    ("grass_block", "5d9b3a"), ("short_grass", "6fae45"), ("tall_grass", "6fae45"), ("fern", "5a9a3a"),
    ("leaves", "3f7a2c"), ("moss", "4a6b2a"), ("dirt", "86603f"), ("podzol", "6b4a2a"), ("mud", "4a3b32"),
    ("farmland", "6b4a2a"), ("sand", "dbcf9b"), ("gravel", "8a8481"), ("clay", "9ea4b0"),
    ("deepslate", "4c4c52"), ("blackstone", "2e2a30"), ("basalt", "4a4a50"), ("netherrack", "6f3636"),
    ("nether_brick", "3a1d22"), ("end_stone", "dfe0a6"), ("purpur", "a77ba7"), ("obsidian", "1b1030"),
    ("cobblestone", "7d7d7d"), ("stone_brick", "7b7b7b"), ("andesite", "888888"), ("diorite", "bdbdbd"),
    ("granite", "9a6b59"), ("tuff", "6b6e62"), ("calcite", "dcdcd6"), ("stone", "8a8a8a"),
    ("sandstone", "d8cb8d"), ("terracotta", "9a5d43"), ("copper", "c26a46"), ("iron_block", "d8d8d8"),
    ("gold_block", "f5d33f"), ("diamond_block", "5fe0d6"), ("emerald_block", "2bc35a"), ("lapis", "2a4fa8"),
    ("redstone", "b01a1a"), ("coal_block", "1a1a1a"), ("quartz", "ece6df"), ("prismarine", "4fa19a"),
    ("glowstone", "f0c96a"), ("sea_lantern", "bfe8dc"), ("shroomlight", "f0a45a"), ("lantern", "f0b55a"),
    ("torch", "f0b030"), ("glass", "9fd8e8"), ("ice", "9ec4f0"), ("snow", "f4f8fa"), ("water", "3a6fe0"),
    ("lava", "e0601a"), ("oak", "b08a52"), ("spruce", "6e4f2e"), ("birch", "d4c38a"), ("jungle", "a1744a"),
    ("acacia", "b3603a"), ("dark_oak", "4a3320"), ("mangrove", "7a3a3a"), ("cherry", "e8b4c0"),
    ("bamboo", "c9c05a"), ("crimson", "7a2a4a"), ("warped", "2a7a76"), ("planks", "b08a52"),
    ("log", "6e5230"), ("wood", "6e5230"), ("stairs", "9a8a70"), ("slab", "9a8a70"), ("fence", "9a7a4a"),
    ("door", "9a7a4a"), ("bookshelf", "8a6a3a"), ("bed", "c23a3a"), ("chest", "9a6a2a"), ("barrel", "8a5a2a"),
    ("furnace", "6a6a6a"), ("anvil", "3a3a3a"), ("cobweb", "dddddd"), ("bedrock", "2a2a2a"), ("ore", "7a7a7a"),
]
DYES = {"white": "e8eaea", "orange": "e07a1f", "magenta": "c24fc2", "light_blue": "4fa8e0", "yellow": "e8cf2a",
        "lime": "7ac21f", "pink": "e88aae", "gray": "4a4e52", "light_gray": "9a9a96", "cyan": "168a96",
        "purple": "7a3aa8", "blue": "3a46a8", "brown": "6a4426", "green": "4a6a22", "red": "a82a26", "black": "181a1e"}

def block_color(name):
    n = name.replace("minecraft:", "")
    for dye, col in DYES.items():
        if n.startswith(dye + "_") and any(k in n for k in ("wool", "concrete", "terracotta", "glass", "carpet", "bed", "banner", "shulker", "candle")):
            return col
    for key, col in COLOR_RULES:
        if key in n:
            return col
    h = int(hashlib.md5(n.encode()).hexdigest()[:6], 16)
    return "%02x%02x%02x" % (90 + (h >> 16) % 120, 90 + (h >> 8) % 120, 90 + h % 120)

import mcassets, own_models

ASSETS = None
if os.environ.get("HUB_TEXTURES", "1") != "0" and not PUBLIC:
    try:
        _jar = mcassets.find_jar()
        if _jar:
            ASSETS = mcassets.Assets(_jar)
            print("Texturen aus:", _jar)
        else:
            print("Hinweis: Keine Minecraft-Installation gefunden, es werden eigene Texturen benutzt.")
    except Exception as e:
        print("Hinweis: Minecraft-Texturen nicht verfuegbar, es werden eigene benutzt:", e)
SRC = "mc" if ASSETS is not None else "gen"

def block_color_name(name):
    return block_color("minecraft:" + name)

def analyze(raw):
    """Prueft die Datei und liefert Metadaten und Vorschau. Wirft ValueError bei ungueltigen Dateien."""
    try:
        nbt = parse_nbt(gzip.decompress(raw))
    except Exception:
        raise ValueError("Das ist keine gueltige .litematic-Datei.")
    regions = nbt.get("Regions")
    if not isinstance(regions, dict) or not regions:
        raise ValueError("Die Datei enthaelt keine Regionen.")
    infos = []
    mins = [10**9] * 3; maxs = [-10**9] * 3
    for r in regions.values():
        try:
            pos = r["Position"]; size = r["Size"]
            sx, sy, sz = size["x"], size["y"], size["z"]
            p = (pos["x"], pos["y"], pos["z"])
            lo = (p[0] + min(0, sx + 1), p[1] + min(0, sy + 1), p[2] + min(0, sz + 1))
            hi = (lo[0] + abs(sx), lo[1] + abs(sy), lo[2] + abs(sz))
            infos.append((r, lo, (abs(sx), abs(sy), abs(sz))))
            for i in range(3):
                mins[i] = min(mins[i], lo[i]); maxs[i] = max(maxs[i], hi[i])
            r["BlockStatePalette"][0]["Name"]; r["BlockStates"]
        except Exception:
            raise ValueError("Die Datei ist beschaedigt.")
    dims = [maxs[i] - mins[i] for i in range(3)]
    if min(dims) <= 0 or dims[0] * dims[1] * dims[2] > 200_000_000:
        raise ValueError("Die Groesse des Schematics ist ungueltig oder zu gross.")

    # Globale Zustandsliste: (name, props) -> index
    state_key = {}
    states = []   # dict(name, props, entry, hidden, opaque)
    def state_index(pe):
        name = pe["Name"].replace("minecraft:", "")
        props = {k: str(v) for k, v in (pe.get("Properties") or {}).items()}
        k = (name, tuple(sorted(props.items())))
        if k not in state_key:
            entry = None
            try:
                if ASSETS is not None:
                    entry = mcassets.build_state(ASSETS, name, props)
                else:
                    entry = own_models.build(name, props, block_color_name)
            except Exception:
                entry = None
            hidden = name in mcassets.HIDDEN or name in ("air", "cave_air", "void_air")
            opaque = entry["opaque"] if entry else (not hidden)
            state_key[k] = len(states)
            states.append({"name": name, "props": props, "entry": entry, "hidden": hidden, "opaque": opaque})
        return state_key[k]

    blocks = {}
    counts = {}
    total = 0
    sy_ = dims[1]; sz_ = dims[2]
    cells = dims[0] * dims[1] * dims[2]
    grid = array.array("H", b"\xff\xff" * cells) if cells <= 8_000_000 else None
    for r, lo, (ax, ay, az) in infos:
        pal = r["BlockStatePalette"]
        longs = r["BlockStates"]
        bits = max(2, (len(pal) - 1).bit_length())
        mask = (1 << bits) - 1
        ox, oy, oz = lo[0] - mins[0], lo[1] - mins[1], lo[2] - mins[2]
        gid = [state_index(pe) for pe in pal]
        isair = [pe["Name"] in AIR for pe in pal]
        i = 0
        nl = len(longs)
        for y in range(ay):
            for z in range(az):
                for x in range(ax):
                    bit = i * bits
                    idx = bit >> 6
                    if idx >= nl:
                        break
                    off = bit & 63
                    v = longs[idx] >> off
                    if off + bits > 64 and idx + 1 < nl:
                        v |= longs[idx + 1] << (64 - off)
                    p = v & mask
                    i += 1
                    if p < len(pal) and not isair[p]:
                        g = gid[p]
                        counts[g] = counts.get(g, 0) + 1
                        total += 1
                        if grid is not None:
                            grid[((oy + y) * sz_ + (oz + z)) * dims[0] + (ox + x)] = g
                        if total <= MAX_PREVIEW_BLOCKS and not states[g]["hidden"]:
                            blocks[((ox + x) * sy_ + (oy + y)) * sz_ + (oz + z)] = g
    by_name = {}
    for g, c in counts.items():
        by_name[states[g]["name"]] = by_name.get(states[g]["name"], 0) + c
    materials = sorted(by_name.items(), key=lambda t: -t[1])

    preview = None
    if total and total <= MAX_PREVIEW_BLOCKS and blocks:
        opq = [st["opaque"] for st in states]
        keys = blocks
        surface = []
        for k, g in blocks.items():
            z = k % sz_; t = k // sz_; y = t % sy_; x = t // sy_
            vis = False
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                nx, ny, nz = x + dx, y + dy, z + dz
                if nx < 0 or ny < 0 or nz < 0 or nx >= dims[0] or ny >= sy_ or nz >= sz_:
                    vis = True; break
                ng = keys.get((nx * sy_ + ny) * sz_ + nz)
                if ng is None or not opq[ng]:
                    vis = True; break
            if vis:
                surface.append((x, y, z, g))
        step = 1
        if len(surface) > MAX_PREVIEW_VOXELS:
            step = -(-len(surface) // MAX_PREVIEW_VOXELS)
            surface = surface[::step]
        flat = []
        for x, y, z, g in surface:
            flat += [x, y, z, g]
        # Texturliste und Palette
        tex = []; tex_idx = {}
        def ti(spec):
            if spec not in tex_idx:
                tex_idx[spec] = len(tex); tex.append(spec)
            return tex_idx[spec]
        pal_json = []
        textured = True
        for st in states:
            item = {"n": st["name"], "c": block_color("minecraft:" + st["name"]), "o": 1 if st["opaque"] else 0}
            if st["entry"]:
                item["tr"] = 1 if st["entry"]["tr"] else 0
                ms = []
                for mdl in st["entry"]["m"]:
                    els = []
                    for e in mdl["e"]:
                        e2 = dict(e)
                        e2["fc"] = {d: dict(f, t=ti(f["t"])) for d, f in e["fc"].items()}
                        els.append(e2)
                    ms.append({"x": mdl["x"], "y": mdl["y"], "e": els})
                item["m"] = ms
            pal_json.append(item)
        icons = {}
        if textured:
            seen = set()
            for st in states:
                if st["name"] in seen or not st["entry"]:
                    continue
                seen.add(st["name"])
                sp = (mcassets.icon_spec if ASSETS is not None else own_models.icon_spec)(st["entry"])
                if sp:
                    icons[st["name"]] = ti(sp)
        preview = {"ver": 4, "src": SRC, "size": dims, "step": step, "textured": textured, "tex": tex, "icons": icons,
                   "palette": pal_json, "v": flat}
    grid_gz = None
    if preview is not None and grid is not None and len(states) < 65535:
        grid_gz = gzip.compress(struct.pack("<III", dims[0], dims[1], dims[2]) + grid.tobytes(), 6)
    return {"dims": dims, "blocks": total, "materials": materials[:60], "preview": preview, "grid": grid_gz}

_grid_cache = {}
_grid_lock = threading.Lock()

def read_layer(sid, y):
    fp = os.path.join(GRIDS, "%d.gz" % sid)
    with _grid_lock:
        entry = _grid_cache.get(sid)
        if entry is None:
            if not os.path.isfile(fp):
                return None
            with open(fp, "rb") as fh:
                data = gzip.decompress(fh.read())
            entry = (struct.unpack("<III", data[:12]), data[12:])
            if len(_grid_cache) >= 4:
                _grid_cache.pop(next(iter(_grid_cache)))
            _grid_cache[sid] = entry
    (dx, dy, dz), raw = entry
    if y < 0 or y >= dy:
        return None
    off = y * dz * dx * 2
    cells = struct.unpack("<%dH" % (dx * dz), raw[off:off + dx * dz * 2])
    return {"w": dx, "d": dz, "h": dy, "c": list(cells)}


# ------------------------------------------------------------------ Import: .schem / .nbt -> .litematic
def _w_str(s):
    b = s.encode("utf8")
    return struct.pack(">H", len(b)) + b

def _nbt_payload(kind, v):
    if kind == "int": return struct.pack(">i", v)
    if kind == "long": return struct.pack(">q", v)
    if kind == "string": return _w_str(v)
    if kind == "compound":
        out = b""
        for k, (kk, vv) in v.items():
            out += struct.pack(">b", _TAGIDS[kk]) + _w_str(k) + _nbt_payload(kk, vv)
        return out + b"\x00"
    if kind == "list":
        sub, items = v
        out = struct.pack(">b", _TAGIDS[sub] if items else 0) + struct.pack(">i", len(items))
        for it in items:
            out += _nbt_payload(sub, it)
        return out
    if kind == "longarray":
        return struct.pack(">i", len(v)) + struct.pack(">%dq" % len(v), *v)
    raise ValueError(kind)

_TAGIDS = {"int": 3, "long": 4, "string": 8, "list": 9, "compound": 10, "longarray": 12}

def _state_parts(key):
    """'minecraft:oak_stairs[facing=east,half=bottom]' -> (name, {props})"""
    if "[" in key and key.endswith("]"):
        name, rest = key[:-1].split("[", 1)
        props = {}
        for part in rest.split(","):
            if "=" in part:
                k, v = part.split("=", 1); props[k] = v
        return name, props
    return key, {}

def _to_litematic(title, W, H, L, palette, idx_iter_len, get_index, data_version=3955):
    """palette: Liste (name, props); get_index: Funktion i -> Palettenindex (Reihenfolge y,z,x)."""
    total_cells = W * H * L
    if total_cells <= 0 or total_cells > 60_000_000:
        raise ValueError("Die Groesse des Schematics ist ungueltig oder zu gross.")
    # Palette: Luft immer an Index 0
    order = {}
    new_pal = [("minecraft:air", {})]
    order[("minecraft:air", ())] = 0
    remap = []
    for name, props in palette:
        if not name.startswith("minecraft:") and ":" not in name:
            name = "minecraft:" + name
        k = (name, tuple(sorted(props.items())))
        if name in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air"):
            remap.append(0); continue
        if k not in order:
            order[k] = len(new_pal); new_pal.append((name, props))
        remap.append(order[k])
    bits = max(2, (len(new_pal) - 1).bit_length())
    mask = (1 << bits) - 1
    nlongs = (total_cells * bits + 63) // 64
    arr = [0] * nlongs
    blocks = 0
    for i in range(total_cells):
        p = remap[get_index(i)]
        if p:
            blocks += 1
            bit = i * bits; w = bit >> 6; off = bit & 63
            arr[w] |= (p << off) & 0xFFFFFFFFFFFFFFFF
            if off + bits > 64:
                arr[w + 1] |= p >> (64 - off)
    signed = [v - (1 << 64) if v >= (1 << 63) else v for v in arr]
    now = int(time.time() * 1000)
    pal_nbt = []
    for name, props in new_pal:
        e = {"Name": ("string", name)}
        if props:
            e["Properties"] = ("compound", {k: ("string", str(v)) for k, v in props.items()})
        pal_nbt.append(e)
    region = {
        "BlockStatePalette": ("list", ("compound", pal_nbt)),
        "BlockStates": ("longarray", signed),
        "Entities": ("list", ("compound", [])),
        "PendingBlockTicks": ("list", ("compound", [])),
        "PendingFluidTicks": ("list", ("compound", [])),
        "Position": ("compound", {"x": ("int", 0), "y": ("int", 0), "z": ("int", 0)}),
        "Size": ("compound", {"x": ("int", W), "y": ("int", H), "z": ("int", L)}),
        "TileEntities": ("list", ("compound", [])),
    }
    root = {
        "MinecraftDataVersion": ("int", data_version), "Version": ("int", 6),
        "Metadata": ("compound", {
            "Author": ("string", "Import"), "Description": ("string", ""), "Name": ("string", title[:60]),
            "EnclosingSize": ("compound", {"x": ("int", W), "y": ("int", H), "z": ("int", L)}),
            "RegionCount": ("int", 1), "TimeCreated": ("long", now), "TimeModified": ("long", now),
            "TotalBlocks": ("int", blocks), "TotalVolume": ("int", total_cells)}),
        "Regions": ("compound", {"main": ("compound", region)}),
    }
    raw = struct.pack(">b", 10) + _w_str("") + _nbt_payload("compound", root)
    return gzip.compress(raw, 6)

def convert_to_litematic(raw, fmt, title):
    """.schem (Sponge) oder .nbt (Struktur-Block) in .litematic umwandeln. Wirft ValueError."""
    try:
        data = gzip.decompress(raw)
    except Exception:
        data = raw
    try:
        root = parse_nbt(data)
    except Exception:
        raise ValueError("Diese Datei konnte nicht gelesen werden.")
    if fmt == "schem":
        sch = root.get("Schematic", root)
        W, H, L = sch.get("Width"), sch.get("Height"), sch.get("Length")
        blocks = sch.get("Blocks")
        if blocks:       # Sponge v3
            pal_map, bd = blocks.get("Palette"), blocks.get("Data")
        else:            # Sponge v2
            pal_map, bd = sch.get("Palette"), sch.get("BlockData")
        if not (isinstance(pal_map, dict) and isinstance(bd, (bytes, bytearray)) and W and H and L):
            raise ValueError("Das ist kein unterstuetztes .schem (Sponge v2/v3). Alte .schematic-Dateien gehen nicht.")
        n = max(pal_map.values()) + 1 if pal_map else 0
        palette = [("minecraft:air", {})] * n
        for key, i in pal_map.items():
            palette[i] = _state_parts(key)
        total = W * H * L
        ids = []
        pos = 0; ln = len(bd)
        while pos < ln and len(ids) < total:
            v = 0; shift = 0
            while True:
                b = bd[pos]; pos += 1
                v |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            ids.append(v)
        if len(ids) < total:
            ids += [0] * (total - len(ids))
        if any(i >= n for i in ids[:1]) and n == 0:
            raise ValueError("Die Palette ist leer.")
        return _to_litematic(title, W, H, L, palette, total, lambda i: ids[i] if ids[i] < n else 0, root.get("DataVersion") or 3955)
    # .nbt (Struktur-Block)
    size = root.get("size")
    pal = root.get("palette") or (root.get("palettes") or [None])[0]
    blks = root.get("blocks")
    if not (isinstance(size, list) and len(size) == 3 and isinstance(pal, list) and isinstance(blks, list)):
        raise ValueError("Das ist keine gueltige .nbt-Struktur-Datei.")
    W, H, L = size
    palette = [(p.get("Name", "minecraft:air"), {k: str(v) for k, v in (p.get("Properties") or {}).items()}) for p in pal]
    cells = {}
    for b in blks:
        x, y, z = b["pos"]
        if 0 <= x < W and 0 <= y < H and 0 <= z < L and 0 <= b["state"] < len(palette):
            cells[(y * L + z) * W + x] = b["state"]
    air = _AIR_IDX(palette)
    return _to_litematic(title, W, H, L, palette, W * H * L, lambda i: cells.get(i, air), root.get("DataVersion") or 3955)

def _AIR_IDX(palette):
    for i, (n, _) in enumerate(palette):
        if n == "minecraft:air":
            return i
    palette.append(("minecraft:air", {}))
    return len(palette) - 1

# ------------------------------------------------------------------ HTTP

NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

def slug(s):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    return s[:60] or "schematic"

class Handler(BaseHTTPRequestHandler):
    server_version = "SchematicHub/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- Hilfen
    def ip(self):
        if PUBLIC:
            fwd = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd[:64]
        return self.client_address[0]

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")

    def err(self, status, msg):
        self.send_json({"error": msg}, status)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            raise ValueError("zu gross")
        return json.loads(self.rfile.read(n) or b"{}")

    def csrf_ok(self):
        return self.headers.get("X-Requested-With") == "hub"

    def me(self, c):
        return user_from_token(c, self.cookie("sid"))

    # ---- Routing
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)
        if path.startswith("/api/"):
            return self.api_get(path, qs)
        if path == "/api/info":
            return self.send_json({"textures": True, "source": SRC, "invite": bool(INVITE), "public": PUBLIC})
        if path.startswith("/tex/"):
            name = path[5:]
            if not name.endswith(".png") or not re.match(r"^[a-z0-9_/]+\.png$", name) or ".." in name:
                self.send_response(404); self.end_headers(); return
            m = re.match(r"^gen/([a-z]+)/([0-9a-f]{6})\.png$", name)
            if m and m.group(1) in ("planks", "log", "logtop", "bricks", "stone", "ore", "glass", "leaves", "plant", "grassside", "water", "plain", "noise"):
                data = own_models.texture(m.group(1), m.group(2))
            elif ASSETS is not None and not name.startswith("gen/"):
                data = ASSETS.texture_png(name[:-4])
            else:
                data = None
            if data is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.security_headers()
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/mods/blueprint.jar":
            fp = os.path.join(ROOT, "downloads", "blueprint-1.0.0.jar")
            try:
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/java-archive")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="blueprint-1.0.0.jar"')
            self.security_headers()
            self.end_headers()
            self.wfile.write(data)
            return
        return self.static(path)

    def do_POST(self):
        self.api_write("POST")

    def do_DELETE(self):
        self.api_write("DELETE")

    def static(self, path):
        if path == "/" or not path.startswith("/static/") and not path.startswith("/favicon"):
            file = os.path.join(ROOT, "index.html")
        else:
            rel = os.path.normpath(path.lstrip("/"))
            file = os.path.join(ROOT, rel)
            if not file.startswith(STATIC + os.sep) or not os.path.isfile(file):
                self.send_response(404); self.end_headers(); return
        try:
            with open(file, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_response(404); self.end_headers(); return
        ctype = mimetypes.guess_type(file)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    SELECT_S = """SELECT s.*, u.name AS author, u.avatar_ver AS aav,
        (SELECT AVG(stars) FROM ratings rt WHERE rt.schematic_id=s.id) AS rating_avg,
        (SELECT COUNT(*) FROM ratings rt WHERE rt.schematic_id=s.id) AS rating_n,
        (SELECT stars FROM ratings rt WHERE rt.schematic_id=s.id AND rt.user_id=?) AS my_rating,
        (SELECT COUNT(*) FROM likes l WHERE l.schematic_id=s.id) AS likes,
        (SELECT COUNT(*) FROM likes l WHERE l.schematic_id=s.id AND l.user_id=?) AS liked,
        (SELECT COUNT(*) FROM comments cm WHERE cm.schematic_id=s.id) AS ncomments
        FROM schematics s JOIN users u ON u.id=s.user_id"""

    def row_dict(self, r, me_id):
        return {"id": r["id"], "title": r["title"], "descr": r["descr"], "tags": [t for t in (r["tags"] or "").split(",") if t],
                "public": bool(r["public"]), "size": r["size"], "dims": [r["sx"], r["sy"], r["sz"]], "blocks": r["blocks"],
                "downloads": r["downloads"], "created": r["created"], "author": r["author"], "mine": r["user_id"] == me_id,
                "has_preview": bool(r["has_preview"]), "likes": r["likes"], "liked": bool(r["liked"]),
                "views": r["views"] or 0, "ncomments": r["ncomments"], "has_grid": bool(r["has_grid"]), "updated": r["updated"],
                "aav": r["aav"] or 0, "cover": r["cover_ver"] or 0, "rating": round(r["rating_avg"], 2) if r["rating_avg"] is not None else None,
                "rating_n": r["rating_n"], "my_rating": r["my_rating"]}


    # ------------------------------------------------------------------ Erweiterungen
    def send_image(self, path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return self.err(404, "Kein Bild.")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def read_jpeg(self, limit=700_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return None
        data = self.rfile.read(n)
        return data if data[:3] == b"\xff\xd8\xff" else None

    def visible_row(self, c, sid, me_id):
        r = c.execute(self.SELECT_S + " WHERE s.id=?", (me_id, me_id, sid)).fetchone()
        if not r or (not r["public"] and r["user_id"] != me_id):
            return None
        return r

    def collection_dict(self, c, r, me_id):
        n = c.execute("SELECT COUNT(*) FROM collection_items ci JOIN schematics s ON s.id=ci.schematic_id WHERE ci.collection_id=? AND (s.public=1 OR s.user_id=?)", (r["id"], me_id)).fetchone()[0]
        covers = [x["schematic_id"] for x in c.execute("SELECT ci.schematic_id FROM collection_items ci JOIN schematics s ON s.id=ci.schematic_id WHERE ci.collection_id=? AND (s.public=1 OR s.user_id=?) AND s.has_preview=1 ORDER BY ci.added DESC LIMIT 4", (r["id"], me_id))]
        return {"id": r["id"], "name": r["name"], "descr": r["descr"] or "", "public": bool(r["public"]), "owner": r["owner"],
                "mine": r["user_id"] == me_id, "count": n, "covers": covers}

    def extra_get(self, c, me, me_id, path, qs):
        if path == "/api/health":
            self.send_json({"ok": True, "build": BUILD}); return True
        if path == "/api/info":
            self.send_json({"textures": True, "source": SRC, "invite": bool(INVITE), "public": PUBLIC}); return True
        m = re.match(r"^/api/users/([A-Za-z0-9_]{3,20})/avatar$", path)
        if m:
            u = c.execute("SELECT id FROM users WHERE name=?", (m.group(1),)).fetchone()
            if not u:
                self.err(404, "Kein Bild."); return True
            self.send_image(os.path.join(AVATARS, "%d.jpg" % u["id"])); return True
        m = re.match(r"^/api/schematics/(\d+)/cover$", path)
        if m:
            sid = int(m.group(1))
            r = c.execute("SELECT user_id, public, cover_ver FROM schematics WHERE id=?", (sid,)).fetchone()
            if not r or (not r["public"] and r["user_id"] != me_id) or not r["cover_ver"]:
                self.err(404, "Kein Bild."); return True
            self.send_image(os.path.join(COVERS, "%d.jpg" % sid)); return True
        if path == "/api/ranking":
            now = int(time.time()); out = {}
            def pack(rows, extra=None):
                res = []
                for r in rows:
                    d = self.row_dict(r, me_id)
                    if extra: d["extra"] = r[extra]
                    res.append(d)
                return res
            out["liked_week"] = pack(c.execute(self.SELECT_S + " JOIN (SELECT schematic_id AS sid, COUNT(*) AS wk FROM likes WHERE created>? GROUP BY schematic_id) w ON w.sid=s.id WHERE s.public=1 ORDER BY w.wk DESC, s.created DESC LIMIT 10", (me_id, me_id, now - 7 * 86400)).fetchall())
            out["best_rated"] = pack(c.execute(self.SELECT_S + " WHERE s.public=1 AND rating_n>0 ORDER BY rating_avg DESC, rating_n DESC LIMIT 10", (me_id, me_id)).fetchall())
            out["downloads"] = pack(c.execute(self.SELECT_S + " WHERE s.public=1 AND s.downloads>0 ORDER BY s.downloads DESC LIMIT 10", (me_id, me_id)).fetchall())
            out["builders"] = [{"name": r["name"], "av": r["avatar_ver"] or 0, "likes": r["lk"], "uploads": r["n"], "downloads": r["dl"]} for r in c.execute(
                "SELECT * FROM (SELECT u.name, u.avatar_ver, "
                "(SELECT COUNT(*) FROM schematics s WHERE s.user_id=u.id AND s.public=1) AS n, "
                "(SELECT COALESCE(SUM(s.downloads),0) FROM schematics s WHERE s.user_id=u.id AND s.public=1) AS dl, "
                "(SELECT COUNT(*) FROM likes l JOIN schematics s ON s.id=l.schematic_id WHERE s.user_id=u.id AND s.public=1) AS lk FROM users u) "
                "WHERE n>0 ORDER BY lk DESC, n DESC LIMIT 10")]
            self.send_json(out); return True
        if path == "/api/notifications":
            if not me:
                self.err(401, "Bitte einloggen."); return True
            rows = c.execute("SELECT id, type, text, link, created, is_read FROM notifications WHERE user_id=? ORDER BY created DESC, id DESC LIMIT 40", (me_id,)).fetchall()
            self.send_json({"items": [{"id": r["id"], "type": r["type"], "text": r["text"], "link": r["link"], "created": r["created"], "read": bool(r["is_read"])} for r in rows],
                            "unread": sum(1 for r in rows if not r["is_read"])}); return True
        if path == "/api/feed":
            if not me:
                self.err(401, "Bitte einloggen."); return True
            rows = c.execute(self.SELECT_S + " WHERE s.public=1 AND s.user_id IN (SELECT followee_id FROM follows WHERE follower_id=?) ORDER BY s.created DESC LIMIT 36", (me_id, me_id, me_id)).fetchall()
            self.send_json({"items": [self.row_dict(r, me_id) for r in rows]}); return True
        if path == "/api/collections":
            sel = "SELECT c.*, u.name AS owner FROM collections c JOIN users u ON u.id=c.user_id WHERE "
            if qs.get("mine", ["0"])[0] == "1":
                if not me:
                    self.err(401, "Bitte einloggen."); return True
                rows = c.execute(sel + "c.user_id=? ORDER BY c.created DESC", (me_id,)).fetchall()
                sid = qs.get("sid", [""])[0]
                items = []
                for r in rows:
                    d = self.collection_dict(c, r, me_id)
                    if sid.isdigit():
                        d["has"] = bool(c.execute("SELECT 1 FROM collection_items WHERE collection_id=? AND schematic_id=?", (r["id"], int(sid))).fetchone())
                    items.append(d)
                self.send_json({"items": items}); return True
            user = (qs.get("user", [""])[0] or "").strip()
            if user:
                rows = c.execute(sel + "lower(u.name)=lower(?) AND (c.public=1 OR c.user_id=?) ORDER BY c.created DESC", (user, me_id)).fetchall()
            else:
                rows = c.execute(sel + "(c.public=1 OR c.user_id=?) ORDER BY c.created DESC LIMIT 60", (me_id,)).fetchall()
            self.send_json({"items": [self.collection_dict(c, r, me_id) for r in rows]}); return True
        m = re.match(r"^/api/collections/(\d+)$", path)
        if m:
            r = c.execute("SELECT c.*, u.name AS owner FROM collections c JOIN users u ON u.id=c.user_id WHERE c.id=?", (int(m.group(1)),)).fetchone()
            if not r or (not r["public"] and r["user_id"] != me_id):
                self.err(404, "Sammlung nicht gefunden."); return True
            rows = c.execute(self.SELECT_S + " JOIN collection_items ci ON ci.schematic_id=s.id WHERE ci.collection_id=? AND (s.public=1 OR s.user_id=?) ORDER BY ci.added DESC", (me_id, me_id, r["id"], me_id)).fetchall()
            d = self.collection_dict(c, r, me_id); d["items"] = [self.row_dict(x, me_id) for x in rows]
            self.send_json(d); return True
        if path == "/api/admin/reports":
            if not me or not me["admin"]:
                self.err(403, "Nur fuer Admins."); return True
            out = []
            for r in c.execute("SELECT rp.*, u.name AS reporter FROM reports rp JOIN users u ON u.id=rp.reporter_id WHERE rp.resolved=0 ORDER BY rp.created DESC LIMIT 100"):
                item = {"id": r["id"], "kind": r["kind"], "target": r["target_id"], "reason": r["reason"], "created": r["created"], "reporter": r["reporter"]}
                if r["kind"] == "schematic":
                    t = c.execute("SELECT s.title, u.name AS author FROM schematics s JOIN users u ON u.id=s.user_id WHERE s.id=?", (r["target_id"],)).fetchone()
                    item["title"] = t["title"] if t else None; item["author"] = t["author"] if t else None; item["link"] = "#/s/%d" % r["target_id"]
                else:
                    t = c.execute("SELECT cm.text, cm.schematic_id, u.name AS author FROM comments cm JOIN users u ON u.id=cm.user_id WHERE cm.id=?", (r["target_id"],)).fetchone()
                    item["title"] = t["text"][:200] if t else None; item["author"] = t["author"] if t else None; item["link"] = ("#/s/%d" % t["schematic_id"]) if t else None
                out.append(item)
            self.send_json({"items": out}); return True
        return False

    def extra_write(self, c, me, method, path, qs):
        uid = me["id"]
        if path == "/api/account/avatar":
            if method == "DELETE":
                try: os.remove(os.path.join(AVATARS, "%d.jpg" % uid))
                except OSError: pass
                c.execute("UPDATE users SET avatar_ver=0 WHERE id=?", (uid,)); c.commit(); self.send_json({"ok": True}); return True
            data = self.read_jpeg(400_000)
            if not data:
                self.err(400, "Bitte ein JPEG-Bild (bis 400 KB)."); return True
            with open(os.path.join(AVATARS, "%d.jpg" % uid), "wb") as fh: fh.write(data)
            ver = int(time.time()); c.execute("UPDATE users SET avatar_ver=? WHERE id=?", (ver, uid)); c.commit()
            self.send_json({"av": ver}); return True
        m = re.match(r"^/api/schematics/(\d+)/cover$", path)
        if m:
            sid = int(m.group(1)); r = c.execute("SELECT user_id FROM schematics WHERE id=?", (sid,)).fetchone()
            if not r or (r["user_id"] != uid and not me["admin"]):
                self.err(403, "Das ist nicht dein Schematic."); return True
            if method == "DELETE":
                try: os.remove(os.path.join(COVERS, "%d.jpg" % sid))
                except OSError: pass
                c.execute("UPDATE schematics SET cover_ver=0 WHERE id=?", (sid,)); c.commit(); self.send_json({"ok": True}); return True
            data = self.read_jpeg(700_000)
            if not data:
                self.err(400, "Bitte ein JPEG-Bild (bis 700 KB)."); return True
            with open(os.path.join(COVERS, "%d.jpg" % sid), "wb") as fh: fh.write(data)
            ver = int(time.time()); c.execute("UPDATE schematics SET cover_ver=? WHERE id=?", (ver, sid)); c.commit()
            self.send_json({"cover": ver}); return True
        m = re.match(r"^/api/schematics/(\d+)/rate$", path)
        if m and method == "POST":
            sid = int(m.group(1)); r = self.visible_row(c, sid, uid)
            if not r:
                self.err(404, "Nicht gefunden."); return True
            if r["user_id"] == uid:
                self.err(400, "Den eigenen Bau kannst du nicht bewerten."); return True
            d = self.body_json(); stars = int(d.get("stars", 0))
            if stars == 0:
                c.execute("DELETE FROM ratings WHERE user_id=? AND schematic_id=?", (uid, sid))
            elif 1 <= stars <= 5:
                c.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?)", (uid, sid, stars, int(time.time())))
            else:
                self.err(400, "1 bis 5 Sterne."); return True
            c.commit()
            row = c.execute("SELECT AVG(stars) a, COUNT(*) n FROM ratings WHERE schematic_id=?", (sid,)).fetchone()
            self.send_json({"rating": round(row["a"], 2) if row["a"] is not None else None, "rating_n": row["n"], "my_rating": stars or None}); return True
        m = re.match(r"^/api/users/([A-Za-z0-9_]{3,20})/follow$", path)
        if m and method == "POST":
            u = c.execute("SELECT id, name FROM users WHERE name=?", (m.group(1),)).fetchone()
            if not u or u["id"] == uid:
                self.err(400, "Das geht nicht."); return True
            if c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (uid, u["id"])).fetchone():
                c.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (uid, u["id"])); on = False
            else:
                c.execute("INSERT INTO follows VALUES(?,?,?)", (uid, u["id"], int(time.time()))); on = True
                notify(c, u["id"], "follow", "%s folgt dir jetzt" % me["name"], "#/u/%s" % me["name"])
            c.commit()
            n = c.execute("SELECT COUNT(*) FROM follows WHERE followee_id=?", (u["id"],)).fetchone()[0]
            self.send_json({"following": on, "followers": n}); return True
        if path == "/api/notifications/read" and method == "POST":
            c.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,)); c.commit(); self.send_json({"ok": True}); return True
        if path == "/api/collections" and method == "POST":
            d = self.body_json(); name = str(d.get("name", "")).strip()[:60]
            if len(name) < 2:
                self.err(400, "Bitte einen Namen angeben."); return True
            if c.execute("SELECT COUNT(*) FROM collections WHERE user_id=?", (uid,)).fetchone()[0] >= 50:
                self.err(400, "Maximal 50 Sammlungen."); return True
            cur = c.execute("INSERT INTO collections(user_id,name,descr,public,created) VALUES(?,?,?,?,?)", (uid, name, str(d.get("descr", "")).strip()[:500], 1 if d.get("public", True) else 0, int(time.time())))
            c.commit(); self.send_json({"id": cur.lastrowid}); return True
        m = re.match(r"^/api/collections/(\d+)(?:/(edit|add|remove))?$", path)
        if m:
            cid = int(m.group(1)); r = c.execute("SELECT user_id FROM collections WHERE id=?", (cid,)).fetchone()
            if not r or (r["user_id"] != uid and not me["admin"]):
                self.err(404, "Sammlung nicht gefunden."); return True
            if method == "DELETE" and not m.group(2):
                c.execute("DELETE FROM collections WHERE id=?", (cid,)); c.commit(); self.send_json({"ok": True}); return True
            if method == "POST":
                d = self.body_json()
                if m.group(2) == "edit":
                    name = str(d.get("name", "")).strip()[:60]
                    if len(name) < 2:
                        self.err(400, "Bitte einen Namen angeben."); return True
                    c.execute("UPDATE collections SET name=?, descr=?, public=? WHERE id=?", (name, str(d.get("descr", "")).strip()[:500], 1 if d.get("public") else 0, cid))
                elif m.group(2) in ("add", "remove"):
                    sid = int(d.get("schematic_id", 0))
                    if m.group(2) == "add":
                        if not self.visible_row(c, sid, uid):
                            self.err(404, "Schematic nicht gefunden."); return True
                        c.execute("INSERT OR IGNORE INTO collection_items VALUES(?,?,?)", (cid, sid, int(time.time())))
                    else:
                        c.execute("DELETE FROM collection_items WHERE collection_id=? AND schematic_id=?", (cid, sid))
                else:
                    return False
                c.commit(); self.send_json({"ok": True}); return True
        if path == "/api/report" and method == "POST":
            d = self.body_json(); kind = d.get("kind"); tid = int(d.get("id", 0)); reason = str(d.get("reason", "")).strip()[:500]
            if kind not in ("schematic", "comment") or not reason:
                self.err(400, "Bitte einen Grund angeben."); return True
            if too_many(self.ip() + "r", limit=10, window=3600):
                self.err(429, "Zu viele Meldungen, bitte warte."); return True
            c.execute("INSERT INTO reports(reporter_id,kind,target_id,reason,created) VALUES(?,?,?,?,?)", (uid, kind, tid, reason, int(time.time())))
            for a in c.execute("SELECT id FROM users WHERE is_admin=1").fetchall():
                notify(c, a["id"], "report", "Neue Meldung von %s" % me["name"], "#/admin")
            c.commit(); self.send_json({"ok": True}); return True
        m = re.match(r"^/api/admin/reports/(\d+)/resolve$", path)
        if m and method == "POST":
            if not me["admin"]:
                self.err(403, "Nur fuer Admins."); return True
            rp = c.execute("SELECT * FROM reports WHERE id=?", (int(m.group(1)),)).fetchone()
            if not rp:
                self.err(404, "Nicht gefunden."); return True
            d = self.body_json()
            if d.get("action") == "delete":
                if rp["kind"] == "schematic":
                    c.execute("DELETE FROM schematics WHERE id=?", (rp["target_id"],))
                    for p in (os.path.join(FILES, "%d.litematic" % rp["target_id"]), os.path.join(DATA, "previews", "%d.json" % rp["target_id"]), os.path.join(DATA, "previews_pub", "%d.json" % rp["target_id"]), os.path.join(GRIDS, "%d.gz" % rp["target_id"]), os.path.join(COVERS, "%d.jpg" % rp["target_id"])):
                        try: os.remove(p)
                        except OSError: pass
                else:
                    c.execute("DELETE FROM comments WHERE id=?", (rp["target_id"],))
            c.execute("UPDATE reports SET resolved=1 WHERE target_id=? AND kind=?", (rp["target_id"], rp["kind"])); c.commit()
            self.send_json({"ok": True}); return True
        return False

    def api_get(self, path, qs):
        c = db()
        try:
            me = self.me(c)
            me_id = me["id"] if me else -1
            if path == "/api/me":
                return self.send_json({"user": me})
            if self.extra_get(c, me, me_id, path, qs):
                return
            if path == "/api/stats":
                row = c.execute("SELECT COUNT(*) n, COALESCE(SUM(downloads),0) d, COALESCE(SUM(blocks),0) b FROM schematics WHERE public=1").fetchone()
                builders = c.execute("SELECT COUNT(DISTINCT user_id) FROM schematics WHERE public=1").fetchone()[0]
                return self.send_json({"schematics": row["n"], "downloads": row["d"], "blocks": row["b"], "builders": builders})
            if path == "/api/featured":
                r = c.execute(self.SELECT_S + " WHERE s.public=1 AND s.has_preview=1 ORDER BY (likes*3 + s.downloads + COALESCE(s.views,0)/10.0) DESC, s.created DESC LIMIT 1", (me_id, me_id)).fetchone()
                return self.send_json({"item": self.row_dict(r, me_id) if r else None})
            m = re.match(r"^/api/users/([A-Za-z0-9_]{3,20})$", path)
            if m:
                u = c.execute("SELECT id, name, created, avatar_ver FROM users WHERE name=?", (m.group(1),)).fetchone()
                if not u:
                    return self.err(404, "Nutzer nicht gefunden.")
                st = c.execute("SELECT COUNT(*) n, COALESCE(SUM(downloads),0) d, COALESCE(SUM(blocks),0) b FROM schematics WHERE user_id=? AND (public=1 OR user_id=?)", (u["id"], me_id)).fetchone()
                lk = c.execute("SELECT COUNT(*) FROM likes l JOIN schematics s ON s.id=l.schematic_id WHERE s.user_id=? AND s.public=1", (u["id"],)).fetchone()[0]
                fr = c.execute("SELECT COUNT(*) FROM follows WHERE followee_id=?", (u["id"],)).fetchone()[0]
                fg = c.execute("SELECT COUNT(*) FROM follows WHERE follower_id=?", (u["id"],)).fetchone()[0]
                ifollow = bool(c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (me_id, u["id"])).fetchone())
                return self.send_json({"name": u["name"], "joined": u["created"], "uploads": st["n"], "downloads": st["d"], "blocks": st["b"], "likes": lk,
                                       "av": u["avatar_ver"] or 0, "followers": fr, "following": fg, "i_follow": ifollow, "self": u["id"] == me_id})
            if path == "/api/schematics":
                q = (qs.get("q", [""])[0] or "").strip().lower()
                sort = qs.get("sort", ["new"])[0]
                mine = qs.get("mine", ["0"])[0] == "1"
                fav = qs.get("fav", ["0"])[0] == "1"
                tag = (qs.get("tag", [""])[0] or "").strip().lower()
                user = (qs.get("user", [""])[0] or "").strip()
                try:
                    offset = max(0, int(qs.get("offset", ["0"])[0])); limit = min(60, max(1, int(qs.get("limit", ["24"])[0])))
                except ValueError:
                    offset, limit = 0, 24
                frm = " WHERE "
                wargs = []
                if mine:
                    if not me:
                        return self.err(401, "Bitte einloggen.")
                    frm += "s.user_id=?"; wargs.append(me_id)
                elif fav:
                    if not me:
                        return self.err(401, "Bitte einloggen.")
                    frm += "(s.public=1 OR s.user_id=?) AND s.id IN (SELECT schematic_id FROM likes WHERE user_id=?)"; wargs += [me_id, me_id]
                else:
                    frm += "(s.public=1 OR s.user_id=?)"; wargs.append(me_id)
                if user:
                    frm += " AND lower(u.name)=lower(?)"; wargs.append(user)
                if q:
                    frm += " AND (lower(s.title) LIKE ? OR lower(s.descr) LIKE ? OR lower(s.tags) LIKE ? OR lower(u.name) LIKE ?)"
                    wargs += ["%" + q.replace("%", "") + "%"] * 4
                if tag:
                    frm += " AND (',' || lower(s.tags) || ',') LIKE ?"; wargs.append("%," + tag.replace("%", "") + ",%")
                order = {"downloads": "s.downloads DESC", "size": "s.blocks DESC", "likes": "likes DESC, s.created DESC",
                         "views": "COALESCE(s.views,0) DESC", "old": "s.created ASC",
                         "rating": "COALESCE(rating_avg,0) DESC, rating_n DESC"}.get(sort, "s.created DESC")
                total = c.execute("SELECT COUNT(*) FROM schematics s JOIN users u ON u.id=s.user_id" + frm, wargs).fetchone()[0]
                rows = c.execute(self.SELECT_S + frm + " ORDER BY " + order + " LIMIT ? OFFSET ?", [me_id, me_id] + wargs + [limit, offset]).fetchall()
                return self.send_json({"items": [self.row_dict(r, me_id) for r in rows], "total": total})
            m = re.match(r"^/api/schematics/(\d+)/comments$", path)
            if m:
                sid = int(m.group(1))
                r = c.execute("SELECT user_id, public FROM schematics WHERE id=?", (sid,)).fetchone()
                if not r or (not r["public"] and r["user_id"] != me_id):
                    return self.err(404, "Nicht gefunden.")
                rows = c.execute("SELECT cm.id, cm.text, cm.created, cm.user_id, u.name, u.avatar_ver FROM comments cm JOIN users u ON u.id=cm.user_id WHERE cm.schematic_id=? ORDER BY cm.created ASC LIMIT 300", (sid,)).fetchall()
                return self.send_json({"items": [{"id": x["id"], "text": x["text"], "created": x["created"], "author": x["name"], "aav": x["avatar_ver"] or 0,
                                                  "can_delete": bool(me) and (x["user_id"] == me_id or r["user_id"] == me_id or me["admin"])} for x in rows]})
            m = re.match(r"^/api/schematics/(\d+)/layer/(\d+)$", path)
            if m:
                sid, y = int(m.group(1)), int(m.group(2))
                r = c.execute("SELECT user_id, public, has_grid FROM schematics WHERE id=?", (sid,)).fetchone()
                if not r or (not r["public"] and r["user_id"] != me_id) or not r["has_grid"]:
                    return self.err(404, "Keine Ebenen-Daten.")
                layer = read_layer(sid, y)
                if layer is None:
                    return self.err(404, "Ebene nicht gefunden.")
                return self.send_json(layer, headers={"Cache-Control": "private, max-age=600"})
            m = re.match(r"^/api/schematics/(\d+)(/preview|/download)?$", path)
            if m:
                sid = int(m.group(1))
                r = c.execute(self.SELECT_S + " WHERE s.id=?", (me_id, me_id, sid)).fetchone()
                if not r or (not r["public"] and r["user_id"] != me_id):
                    return self.err(404, "Nicht gefunden.")
                if m.group(2) == "/preview":
                    fp = os.path.join(PREVIEWS, "%d.json" % sid)
                    if not os.path.isfile(fp) and r["has_preview"]:
                        try:
                            with open(os.path.join(FILES, "%d.litematic" % sid), "rb") as fh:
                                info = analyze(fh.read())
                            if info["preview"]:
                                with open(fp, "w") as fh:
                                    json.dump(info["preview"], fh, separators=(",", ":"))
                        except Exception:
                            pass
                    if not os.path.isfile(fp):
                        return self.err(404, "Keine Vorschau.")
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, max-age=600")
                    self.security_headers()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if m.group(2) == "/download":
                    fp = os.path.join(FILES, "%d.litematic" % sid)
                    if not os.path.isfile(fp):
                        return self.err(404, "Datei fehlt.")
                    c.execute("UPDATE schematics SET downloads=downloads+1 WHERE id=?", (sid,))
                    c.commit()
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="%s.litematic"' % slug(r["title"]))
                    self.security_headers()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if r["user_id"] != me_id:
                    c.execute("UPDATE schematics SET views=COALESCE(views,0)+1 WHERE id=?", (sid,))
                    c.commit()
                d = self.row_dict(r, me_id)
                d["materials"] = json.loads(r["materials"] or "[]")
                return self.send_json(d)
            return self.err(404, "Unbekannt.")
        finally:
            c.close()

    def api_write(self, method):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)
        if not self.csrf_ok():
            return self.err(403, "Ungueltige Anfrage.")
        c = db()
        try:
            me = self.me(c)
            if method == "POST" and path == "/api/register":
                if too_many(self.ip()):
                    return self.err(429, "Zu viele Versuche, bitte warte kurz.")
                d = self.body_json()
                name = str(d.get("name", "")).strip(); pw = str(d.get("password", ""))
                if INVITE and not hmac.compare_digest(str(d.get("invite", "")).strip().encode(), INVITE.encode()):
                    return self.err(403, "Der Einladungscode ist falsch.")
                if not NAME_RE.match(name):
                    return self.err(400, "Name: 3-20 Zeichen, nur Buchstaben, Zahlen und _.")
                if len(pw) < 8 or len(pw) > 200:
                    return self.err(400, "Passwort: mindestens 8 Zeichen.")
                if c.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                    return self.err(409, "Der Name ist schon vergeben.")
                salt = secrets.token_bytes(16)
                first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                cur = c.execute("INSERT INTO users(name,salt,hash,created,is_admin) VALUES(?,?,?,?,?)", (name, salt, hash_pw(pw, salt), int(time.time()), 1 if first else 0))
                c.commit()
                tok = new_session(c, cur.lastrowid)
                return self.send_json({"user": user_from_token(c, tok)}, headers={"Set-Cookie": self.set_cookie(tok)})
            if method == "POST" and path == "/api/login":
                if too_many(self.ip()):
                    return self.err(429, "Zu viele Versuche, bitte warte kurz.")
                d = self.body_json()
                name = str(d.get("name", "")).strip(); pw = str(d.get("password", ""))
                r = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
                if not r or not hmac.compare_digest(hash_pw(pw, r["salt"]), r["hash"]):
                    return self.err(401, "Name oder Passwort falsch.")
                tok = new_session(c, r["id"])
                return self.send_json({"user": user_from_token(c, tok)}, headers={"Set-Cookie": self.set_cookie(tok)})
            if method == "POST" and path == "/api/logout":
                tok = self.cookie("sid")
                if tok:
                    c.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(tok.encode()).hexdigest(),))
                    c.commit()
                return self.send_json({"ok": True}, headers={"Set-Cookie": "sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            if not me:
                return self.err(401, "Bitte einloggen.")
            if self.extra_write(c, me, method, path, qs):
                return
            m = re.match(r"^/api/schematics/(\d+)/(like|comments|edit)$", path)
            if method == "POST" and m:
                sid = int(m.group(1)); what = m.group(2)
                r = c.execute("SELECT user_id, public FROM schematics WHERE id=?", (sid,)).fetchone()
                if not r or (not r["public"] and r["user_id"] != me["id"]):
                    return self.err(404, "Nicht gefunden.")
                if what == "like":
                    if c.execute("SELECT 1 FROM likes WHERE user_id=? AND schematic_id=?", (me["id"], sid)).fetchone():
                        c.execute("DELETE FROM likes WHERE user_id=? AND schematic_id=?", (me["id"], sid)); liked = False
                    else:
                        c.execute("INSERT INTO likes VALUES(?,?,?)", (me["id"], sid, int(time.time()))); liked = True
                        if r["user_id"] != me["id"]:
                            t = c.execute("SELECT title FROM schematics WHERE id=?", (sid,)).fetchone()["title"]
                            notify(c, r["user_id"], "like", "%s gefaellt dein Bau \"%s\"" % (me["name"], t), "#/s/%d" % sid)
                    c.commit()
                    n = c.execute("SELECT COUNT(*) FROM likes WHERE schematic_id=?", (sid,)).fetchone()[0]
                    return self.send_json({"liked": liked, "likes": n})
                d = self.body_json()
                if what == "comments":
                    text = str(d.get("text", "")).strip()[:1000]
                    if not text:
                        return self.err(400, "Der Kommentar ist leer.")
                    if too_many(self.ip() + "c", limit=20, window=300):
                        return self.err(429, "Zu viele Kommentare, bitte warte kurz.")
                    cur = c.execute("INSERT INTO comments(schematic_id,user_id,text,created) VALUES(?,?,?,?)", (sid, me["id"], text, int(time.time())))
                    if r["user_id"] != me["id"]:
                        t = c.execute("SELECT title FROM schematics WHERE id=?", (sid,)).fetchone()["title"]
                        notify(c, r["user_id"], "comment", "%s hat \"%s\" kommentiert" % (me["name"], t), "#/s/%d" % sid)
                    c.commit()
                    return self.send_json({"id": cur.lastrowid})
                if r["user_id"] != me["id"]:
                    return self.err(403, "Das ist nicht dein Schematic.")
                title = str(d.get("title", "")).strip()[:80]
                if len(title) < 2:
                    return self.err(400, "Bitte einen Titel angeben.")
                descr = str(d.get("descr", "")).strip()[:2000]
                tags = ",".join(sorted({re.sub(r"[^a-z0-9_-]", "", t.lower())[:20] for t in str(d.get("tags", "")).split(",")} - {""}))[:200]
                c.execute("UPDATE schematics SET title=?, descr=?, tags=?, public=?, updated=? WHERE id=?",
                          (title, descr, tags, 1 if d.get("public") else 0, int(time.time()), sid))
                c.commit()
                return self.send_json({"ok": True})
            m = re.match(r"^/api/comments/(\d+)$", path)
            if method == "DELETE" and m:
                cm = c.execute("SELECT cm.id, cm.user_id, s.user_id AS owner FROM comments cm JOIN schematics s ON s.id=cm.schematic_id WHERE cm.id=?", (int(m.group(1)),)).fetchone()
                if not cm or (me["id"] not in (cm["user_id"], cm["owner"]) and not me["admin"]):
                    return self.err(404, "Nicht gefunden.")
                c.execute("DELETE FROM comments WHERE id=?", (cm["id"],)); c.commit()
                return self.send_json({"ok": True})
            if method == "POST" and path == "/api/account/password":
                d = self.body_json()
                row = c.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
                if not hmac.compare_digest(hash_pw(str(d.get("old", "")), row["salt"]), row["hash"]):
                    return self.err(401, "Das alte Passwort ist falsch.")
                new = str(d.get("new", ""))
                if len(new) < 8 or len(new) > 200:
                    return self.err(400, "Neues Passwort: mindestens 8 Zeichen.")
                salt = secrets.token_bytes(16)
                c.execute("UPDATE users SET salt=?, hash=? WHERE id=?", (salt, hash_pw(new, salt), me["id"]))
                # alle anderen Sitzungen beenden, diese behalten
                keep = hashlib.sha256((self.cookie("sid") or "").encode()).hexdigest()
                c.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (me["id"], keep))
                c.commit()
                return self.send_json({"ok": True})
            if method == "POST" and path == "/api/account/delete":
                d = self.body_json()
                row = c.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
                if not hmac.compare_digest(hash_pw(str(d.get("password", "")), row["salt"]), row["hash"]):
                    return self.err(401, "Das Passwort ist falsch.")
                ids = [x["id"] for x in c.execute("SELECT id FROM schematics WHERE user_id=?", (me["id"],))]
                c.execute("DELETE FROM users WHERE id=?", (me["id"],))
                c.commit()
                for sid in ids:
                    for p in (os.path.join(FILES, "%d.litematic" % sid), os.path.join(DATA, "previews", "%d.json" % sid), os.path.join(DATA, "previews_pub", "%d.json" % sid), os.path.join(GRIDS, "%d.gz" % sid), os.path.join(COVERS, "%d.jpg" % sid)):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                try:
                    os.remove(os.path.join(AVATARS, "%d.jpg" % me["id"]))
                except OSError:
                    pass
                return self.send_json({"ok": True}, headers={"Set-Cookie": "sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            if method == "POST" and path == "/api/upload":
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_UPLOAD:
                    return self.err(413, "Datei zu gross (maximal 25 MB).")
                raw = self.rfile.read(n)
                title = (qs.get("title", [""])[0] or "").strip()[:80]
                descr = (qs.get("descr", [""])[0] or "").strip()[:2000]
                tags = ",".join(sorted({re.sub(r"[^a-z0-9_-]", "", t.lower())[:20] for t in (qs.get("tags", [""])[0] or "").split(",")} - {""}))[:200]
                public = 1 if qs.get("public", ["1"])[0] == "1" else 0
                if len(title) < 2:
                    return self.err(400, "Bitte einen Titel angeben.")
                n_uploads = c.execute("SELECT COUNT(*) FROM schematics WHERE user_id=?", (me["id"],)).fetchone()[0]
                if n_uploads >= 200:
                    return self.err(400, "Maximal 200 Schematics pro Konto.")
                fmt = (qs.get("format", ["litematic"])[0] or "litematic").lower()
                if fmt in ("schem", "nbt"):
                    try:
                        raw = convert_to_litematic(raw, fmt, title)
                    except ValueError as e:
                        return self.err(400, str(e))
                    except Exception:
                        return self.err(400, "Die Datei konnte nicht umgewandelt werden.")
                fhash = hashlib.sha256(raw).hexdigest()
                dup = c.execute("SELECT title FROM schematics WHERE user_id=? AND file_hash=?", (me["id"], fhash)).fetchone()
                if dup:
                    return self.err(409, "Diese Datei hast du schon hochgeladen (\"%s\")." % dup["title"])
                try:
                    info = analyze(raw)
                except ValueError as e:
                    return self.err(400, str(e))
                cur = c.execute("INSERT INTO schematics(user_id,title,descr,tags,public,size,sx,sy,sz,blocks,created,materials,has_preview,file_hash,has_grid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (me["id"], title, descr, tags, public, len(raw), *info["dims"], info["blocks"], int(time.time()),
                                 json.dumps(info["materials"]), 1 if info["preview"] else 0, fhash, 1 if info["grid"] else 0))
                sid = cur.lastrowid
                with open(os.path.join(FILES, "%d.litematic" % sid), "wb") as fh:
                    fh.write(raw)
                if info["preview"]:
                    with open(os.path.join(PREVIEWS, "%d.json" % sid), "w") as fh:
                        json.dump(info["preview"], fh, separators=(",", ":"))
                if info["grid"]:
                    with open(os.path.join(GRIDS, "%d.gz" % sid), "wb") as fh:
                        fh.write(info["grid"])
                if public:
                    for f in c.execute("SELECT follower_id FROM follows WHERE followee_id=?", (me["id"],)).fetchall():
                        notify(c, f["follower_id"], "upload", "%s hat \"%s\" hochgeladen" % (me["name"], title), "#/s/%d" % sid)
                c.commit()
                return self.send_json({"id": sid})
            m = re.match(r"^/api/schematics/(\d+)$", path)
            if method == "DELETE" and m:
                sid = int(m.group(1))
                r = c.execute("SELECT user_id FROM schematics WHERE id=?", (sid,)).fetchone()
                if not r or (r["user_id"] != me["id"] and not me["admin"]):
                    return self.err(404, "Nicht gefunden.")
                c.execute("DELETE FROM schematics WHERE id=?", (sid,))
                c.commit()
                for p in (os.path.join(FILES, "%d.litematic" % sid), os.path.join(DATA, "previews", "%d.json" % sid), os.path.join(DATA, "previews_pub", "%d.json" % sid), os.path.join(GRIDS, "%d.gz" % sid), os.path.join(COVERS, "%d.jpg" % sid)):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                return self.send_json({"ok": True})
            return self.err(404, "Unbekannt.")
        except (ValueError, json.JSONDecodeError):
            return self.err(400, "Ungueltige Eingabe.")
        finally:
            c.close()

    def set_cookie(self, tok):
        secure = "; Secure" if os.environ.get("HUB_HTTPS") == "1" else ""
        return "sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (tok, SESSION_DAYS * 86400, secure)

def refresh_previews():
    """Baut Vorschauen im alten Format (oder ohne Texturen, obwohl jetzt welche da sind) aus den gespeicherten Dateien neu."""
    try:
        c = db()
        rows = c.execute("SELECT id FROM schematics").fetchall()
        n = 0
        for r in rows:
            sid = r["id"]
            fp = os.path.join(PREVIEWS, "%d.json" % sid)
            ok = False
            if os.path.isfile(fp):
                try:
                    with open(fp) as fh:
                        d = json.load(fh)
                    ok = (d.get("ver") == 4 and d.get("src") == SRC
                          and (os.path.isfile(os.path.join(GRIDS, "%d.gz" % sid)) or not d.get("v")))
                except Exception:
                    ok = False
            elif not c.execute("SELECT has_preview FROM schematics WHERE id=?", (sid,)).fetchone()["has_preview"]:
                ok = True  # hatte nie eine Vorschau (zu gross)
            if ok:
                continue
            src = os.path.join(FILES, "%d.litematic" % sid)
            if not os.path.isfile(src):
                continue
            try:
                with open(src, "rb") as fh:
                    info = analyze(fh.read())
            except Exception as e:
                print("Vorschau %d konnte nicht neu gebaut werden: %s" % (sid, e))
                continue
            if info["preview"]:
                tmp = fp + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(info["preview"], fh, separators=(",", ":"))
                os.replace(tmp, fp)
            if info["grid"]:
                with open(os.path.join(GRIDS, "%d.gz" % sid), "wb") as fh:
                    fh.write(info["grid"])
            _grid_cache.pop(sid, None)
            c.execute("UPDATE schematics SET has_preview=?, has_grid=? WHERE id=?", (1 if info["preview"] else 0, 1 if info["grid"] else 0, sid))
            c.commit()
            n += 1
        c.close()
        if n:
            print("%d Vorschau(en) neu berechnet." % n)
    except Exception as e:
        print("Vorschau-Aktualisierung fehlgeschlagen:", e)

def main():
    init_db()
    threading.Thread(target=refresh_previews, daemon=True).start()
    # Hoster setzen PORT; dann auf allen Adressen lauschen
    host = os.environ.get("HUB_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("HUB_PORT") or "8080")
    srv = ThreadingHTTPServer((host, port), Handler)
    print("Schematic Hub laeuft auf http://%s:%d" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
