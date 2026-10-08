"""Liest Block-Modelle und Texturen aus der lokal installierten Minecraft-Version (client.jar).
Es werden keine Minecraft-Dateien mitgeliefert oder kopiert: alles wird zur Laufzeit aus deiner eigenen Installation gelesen."""
import glob, json, os, re, sys, threading, zipfile

TINT_FOLIAGE = ("leaves",)

def find_jar():
    env = os.environ.get("HUB_MC_JAR")
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    roots = [os.path.join(home, "Library", "Application Support", "minecraft", "versions"),
             os.path.join(os.environ.get("APPDATA", ""), ".minecraft", "versions"),
             os.path.join(home, ".minecraft", "versions")]
    cands = []
    for r in roots:
        for jar in glob.glob(os.path.join(r, "*", "*.jar")):
            try:
                with zipfile.ZipFile(jar) as z:
                    if "assets/minecraft/blockstates/stone.json" in z.namelist() or any(n.startswith("assets/minecraft/blockstates/") for n in z.namelist()[:5000]):
                        cands.append((os.path.getmtime(jar), jar))
            except Exception:
                pass
    def key(c):
        v = os.path.basename(os.path.dirname(c[1]))
        if re.match(r"^\d+(\.\d+)*$", v):
            return (1, tuple(int(x) for x in v.split(".")))
        return (0, (), c[0])
    cands.sort(key=key, reverse=True)
    return cands[0][1] if cands else None

def norm(path):
    return path.replace("minecraft:", "")

class Assets:
    def __init__(self, jar):
        self.jar = jar
        self.z = zipfile.ZipFile(jar)
        self.names = set(self.z.namelist())
        self.lock = threading.Lock()
        self._model = {}
        self._bs = {}
        self._state_cache = {}

    def read(self, name):
        with self.lock:
            return self.z.read(name)

    def texture_png(self, path):
        """path wie 'block/stone' (ohne .png)"""
        n = "assets/minecraft/textures/%s.png" % path
        return self.read(n) if n in self.names else None

    def blockstate(self, name):
        if name not in self._bs:
            n = "assets/minecraft/blockstates/%s.json" % name
            self._bs[name] = json.loads(self.read(n)) if n in self.names else None
        return self._bs[name]

    def model(self, path):
        path = norm(path)
        if path not in self._model:
            n = "assets/minecraft/models/%s.json" % path
            self._model[path] = json.loads(self.read(n)) if n in self.names else None
        return self._model[path]

    def resolve_model(self, path):
        """Gibt (texturen, elemente) zurueck; Kind ueberschreibt Eltern."""
        chain = []
        p = norm(path)
        for _ in range(12):
            m = self.model(p)
            if not m:
                break
            chain.append(m)
            if "parent" not in m:
                break
            p = norm(m["parent"])
        textures = {}
        for m in reversed(chain):
            textures.update(m.get("textures", {}))
        elements = None
        for m in chain:
            if "elements" in m:
                elements = m["elements"]
                break
        return textures, elements

# ---------------------------------------------------------------- Zustand -> Geometrie

def cond_ok(cond, props):
    if "OR" in cond:
        return any(cond_ok(c, props) for c in cond["OR"])
    if "AND" in cond:
        return all(cond_ok(c, props) for c in cond["AND"])
    for k, v in cond.items():
        if str(props.get(k, "")) not in str(v).split("|"):
            return False
    return True

def variant_ok(key, props):
    if key == "":
        return True
    for part in key.split(","):
        k, _, v = part.partition("=")
        if str(props.get(k, "")) not in v.split("|"):
            return False
    return True

def tint_for(block, texture):
    if block in ("water", "bubble_column"):
        return "water"
    if "leaves" in block:
        if "birch" in block: return "birch"
        if "spruce" in block: return "spruce"
        if any(x in block for x in ("cherry", "azalea")): return None
        return "foliage"
    if block == "redstone_wire": return "redstone"
    if block == "lily_pad": return "lily"
    if block in ("melon_stem", "pumpkin_stem", "attached_melon_stem", "attached_pumpkin_stem"): return "stem"
    return "grass"

TRANSLUCENT_WORDS = ("glass", "ice", "water", "slime", "honey")
HIDDEN = {"barrier", "light", "structure_void", "air", "cave_air", "void_air", "moving_piston"}

def build_state(assets, name, props):
    """name ohne 'minecraft:'. Liefert dict mit opaque, tr, models[{x,y,e:[...]}] und texturliste."""
    key = (name, tuple(sorted(props.items())))
    if key in assets._state_cache:
        return assets._state_cache[key]
    res = _build(assets, name, props)
    assets._state_cache[key] = res
    return res

def _model_entry(assets, name, mref):
    textures, elements = assets.resolve_model(mref["model"])
    def tex(ref):
        for _ in range(10):
            if ref is None:
                return None
            if ref.startswith("#"):
                ref = textures.get(ref[1:])
            else:
                return norm(ref)
        return None
    if not elements:
        t = tex("#particle") or tex("#all")
        if not t:
            return None
        elements = [{"from": [0, 0, 0], "to": [16, 16, 16], "faces": {d: {"texture": t, "cullface": d} for d in ("up", "down", "north", "south", "east", "west")}}]
        textures = {}
    out = []
    seen_boxes = {}
    for el in elements:
        faces = {}
        for d, f in el.get("faces", {}).items():
            t = tex(f.get("texture"))
            if not t:
                continue
            spec = t
            if "tintindex" in f:
                tt = tint_for(name, t)
                if tt:
                    spec = t + "@" + tt
            face = {"t": spec}
            if "uv" in f: face["uv"] = f["uv"]
            if f.get("cullface"): face["c"] = f["cullface"]
            if f.get("rotation"): face["r"] = f["rotation"]
            faces[d] = face
        if not faces:
            continue
        box = (tuple(el["from"]), tuple(el["to"]))
        e = {"f": el["from"], "t": el["to"], "fc": faces}
        if seen_boxes.get(box):  # deckungsgleiche Ebene (z.B. Gras-Overlay) minimal nach aussen schieben
            e["o"] = 1
        seen_boxes[box] = True
        if "rotation" in el:
            r = el["rotation"]
            e["r"] = {"o": r.get("origin", [8, 8, 8]), "a": r.get("axis", "y"), "g": r.get("angle", 0), "s": 1 if r.get("rescale") else 0}
        if el.get("shade") is False:
            e["sh"] = 0
        out.append(e)
    if not out:
        return None
    return {"x": mref.get("x", 0), "y": mref.get("y", 0), "e": out}


# ---------------------------------------------------------------- Spezialmodelle (werden im Spiel nicht aus Blockmodellen gezeichnet)
def _crop(path, x, y, w, h):
    return "%s#%d,%d,%d,%d" % (path, x, y, w, h)

def _cuboid_faces(path, u, v, w, h, d):
    """Texturbereiche eines Quaders nach Minecrafts ModelPart-Layout (u,v = texOffs)."""
    return {"up": _crop(path, u + d, v, w, d), "down": _crop(path, u + d + w, v, w, d),
            "west": _crop(path, u, v + d, d, h), "north": _crop(path, u + d, v + d, w, h),
            "east": _crop(path, u + d + w, v + d, d, h), "south": _crop(path, u + 2 * d + w, v + d, w, h)}

def _box(f, t, faces):
    return {"f": list(f), "t": list(t), "fc": {d: {"t": spec, "uv": [0, 0, 16, 16]} for d, spec in faces.items()}}

COPPER_CHESTS = {"copper_chest": "copper", "exposed_copper_chest": "copper_exposed",
                 "weathered_copper_chest": "copper_weathered", "oxidized_copper_chest": "copper_oxidized"}

def _special(assets, name, props):
    rot = {"north": 0, "east": 90, "south": 180, "west": 270}
    if name in ("chest", "trapped_chest", "ender_chest") or name in COPPER_CHESTS or (name.startswith("waxed_") and name[6:] in COPPER_CHESTS):
        tex = {"chest": "normal", "trapped_chest": "trapped", "ender_chest": "ender"}.get(name)
        if tex is None:
            tex = COPPER_CHESTS[name[6:] if name.startswith("waxed_") else name]
        path = "entity/chest/" + tex
        if ("assets/minecraft/textures/%s.png" % path) not in assets.names:
            return None
        els = [_box((1, 0, 1), (15, 10, 15), _cuboid_faces(path, 0, 19, 14, 10, 14)),
               _box((1, 10, 1), (15, 14, 15), _cuboid_faces(path, 0, 0, 14, 5, 14)),
               _box((7, 9, 0), (9, 13, 1), _cuboid_faces(path, 0, 0, 2, 4, 1))]
        return {"opaque": False, "tr": False, "m": [{"x": 0, "y": rot.get(props.get("facing", "north"), 0), "e": els}]}
    if name == "shulker_box" or name.endswith("_shulker_box"):
        color = name[:-len("_shulker_box")] if name != "shulker_box" else ""
        path = "entity/shulker/shulker_" + color if color else "entity/shulker/shulker"
        if ("assets/minecraft/textures/%s.png" % path) not in assets.names:
            return None
        els = [_box((0, 0, 0), (16, 8, 16), _cuboid_faces(path, 0, 28, 16, 8, 16)),
               _box((0, 8, 0), (16, 16, 16), _cuboid_faces(path, 0, 0, 16, 12, 16))]
        face = props.get("facing", "up")
        mx, my = {"up": (0, 0), "down": (180, 0), "north": (90, 0), "south": (270, 0), "east": (90, 270), "west": (90, 90)}.get(face, (0, 0))
        return {"opaque": False, "tr": False, "m": [{"x": mx, "y": my, "e": els}]}
    return None

def _build(assets, name, props):
    if name in HIDDEN:
        return None
    sp = _special(assets, name, props)
    if sp is not None:
        return sp
    if name in ("water", "lava"):
        t = "block/water_still@water" if name == "water" else "block/lava_still"
        faces = {d: {"t": t, "c": d} for d in ("up", "down", "north", "south", "east", "west")}
        return {"opaque": False, "tr": name == "water", "m": [{"x": 0, "y": 0, "e": [{"f": [0, 0, 0], "t": [16, 16, 16], "fc": faces, "sh": 0}]}]}
    bs = assets.blockstate(name)
    refs = []
    if bs:
        if "variants" in bs:
            chosen = None
            for k, v in bs["variants"].items():
                if variant_ok(k, props):
                    chosen = v
                    break
            if chosen is None:
                chosen = next(iter(bs["variants"].values()))
            refs = [chosen[0] if isinstance(chosen, list) else chosen]
        elif "multipart" in bs:
            for part in bs["multipart"]:
                if "when" not in part or cond_ok(part["when"], props):
                    a = part["apply"]
                    refs.append(a[0] if isinstance(a, list) else a)
    if not refs:
        # Kein Blockstate (z.B. Spezialbloecke): Würfel mit passender Textur, falls vorhanden
        t = "block/" + name
        if ("assets/minecraft/textures/%s.png" % t) in assets.names:
            faces = {d: {"t": t, "c": d} for d in ("up", "down", "north", "south", "east", "west")}
            return {"opaque": True, "tr": False, "m": [{"x": 0, "y": 0, "e": [{"f": [0, 0, 0], "t": [16, 16, 16], "fc": faces}]}]}
        return None
    models = []
    for r in refs:
        m = _model_entry(assets, name, r)
        if m:
            models.append(m)
    if not models:
        return None
    opaque = False
    tr = False
    if len(models) == 1:
        els = models[0]["e"]
        e = els[0]
        if (e["f"] == [0, 0, 0] and e["t"] == [16, 16, 16] and "r" not in e and len(e["fc"]) == 6
                and all(x["f"] == e["f"] and x["t"] == e["t"] for x in els)):
            opaque = True
    texnames = [f["t"] for m in models for e in m["e"] for f in e["fc"].values()]
    if any(w in t for t in texnames for w in TRANSLUCENT_WORDS):
        tr = True
        opaque = False
    if any("leaves" in t for t in texnames):
        opaque = False
    return {"opaque": opaque, "tr": tr, "m": models}

def icon_spec(entry):
    """Eine Textur fuer die Materialliste."""
    if not entry:
        return None
    for m in entry["m"]:
        for e in m["e"]:
            fc = e["fc"]
            if "up" in fc and e["f"] == [0, 0, 0] and e["t"] == [16, 16, 16]:
                return fc["north"]["t"] if "north" in fc else fc["up"]["t"]
    m = entry["m"][0]["e"][0]["fc"]
    first = m.get("north") or next(iter(m.values()))
    return first["t"]
