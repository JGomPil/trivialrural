#!/usr/bin/env python3
"""
TrivialRural — lógica de movimiento limpia
Tablero 46 casillas: 30 anillo + 15 corredores (3×5) + 1 centro
Movimiento:
  Anillo: cw / ccw / (in si el dado alcanza un wedge y entra al corredor)
  Corredor: in (hacia centro) / out (hacia anillo) / cw (sale al anillo y gira) / ccw ídem
  Centro: elige uno de los 5 corredores de salida
"""
import json, csv, random, asyncio, os
from pathlib import Path
from aiohttp import web
import aiohttp

BASE_DIR      = Path(__file__).parent
import os
PORT          = int(os.environ.get("PORT", 8080))
DEFAULT_TIMER = 30

CATEGORIES = [
    {"id":"cultura",         "name":"Cultura General","icon":"📚","color":"#1A237E"},
    {"id":"nosotros",        "name":"Nosotros",       "icon":"🥂","color":"#880E4F"},
    {"id":"animacion",       "name":"Animación",      "icon":"🎭","color":"#4A148C"},
    {"id":"entretenimiento", "name":"Entretenimiento","icon":"🎬","color":"#1B5E20"},
    {"id":"gastronomia",     "name":"Gastronomía",    "icon":"🍴","color":"#BF360C"},
]
CAT_IDS = [c["id"] for c in CATEGORIES]
CAT_NAME_TO_ID = {
    "Cultura General":"cultura","Nosotros":"nosotros",
    "Animación":"animacion","Entretenimiento":"entretenimiento","Gastronomía":"gastronomia",
}

RING           = 30   # casillas 0-29
CORRIDOR_START = 30   # casillas 30-44
CORRIDOR_LEN   = 3
NUM_CORRIDORS  = 5
CENTER         = 45   # casilla 45
TOTAL          = 46

def build_board():
    board = []
    for side in range(5):
        board.append({"type":"wedge",  "cat":CAT_IDS[side]})
        board.append({"type":"normal", "cat":CAT_IDS[(side+1)%5]})
        board.append({"type":"normal", "cat":CAT_IDS[(side+2)%5]})
        board.append({"type":"roll",   "cat":CAT_IDS[(side+3)%5]})
        board.append({"type":"normal", "cat":CAT_IDS[(side+4)%5]})
        board.append({"type":"normal", "cat":CAT_IDS[side]})
    for i in range(5):
        for j in range(3):
            board.append({"type":"normal", "cat":CAT_IDS[i]})
    board.append({"type":"center", "cat":None})
    assert len(board) == TOTAL
    return board

BOARD = build_board()

# ── MOVIMIENTO ────────────────────────────────────────────────────────────
def wedge_of_corridor(corr_idx):
    return corr_idx * 6

def corridor_of_wedge(ring_pos):
    """Returns first corridor square of this wedge, or None if not a wedge."""
    if ring_pos % 6 == 0:
        return CORRIDOR_START + (ring_pos // 6) * CORRIDOR_LEN
    return None

def move_ring(pos, steps, direction):
    """Move `steps` steps on ring (cw=+1, ccw=-1). Returns path list."""
    path = []
    d = 1 if direction == 'cw' else -1
    for _ in range(steps):
        pos = (pos + d) % RING
        path.append(pos)
    return path

def move_corridor_in(pos, steps):
    """Move inward along corridor from current pos. Handles ring→corr→center."""
    path = []
    for _ in range(steps):
        if pos < RING:
            if pos % 6 == 0:
                pos = CORRIDOR_START + (pos // 6) * CORRIDOR_LEN
            else:
                break  # not a wedge, can't enter corridor
        elif pos < CENTER:
            ci  = (pos - CORRIDOR_START) // CORRIDOR_LEN
            off = (pos - CORRIDOR_START) % CORRIDOR_LEN
            cf  = CORRIDOR_START + ci * CORRIDOR_LEN
            if off < CORRIDOR_LEN - 1:
                pos = cf + off + 1
            else:
                pos = CENTER
        else:
            break  # already at center, stop
        path.append(pos)
    return path

def move_corridor_out(pos, steps):
    """Move outward along corridor from current pos. Handles center→corr→wedge→ring."""
    path = []
    for _ in range(steps):
        if pos == CENTER:
            break  # can't move out from center without knowing which corridor
        elif pos < CENTER and pos >= CORRIDOR_START:
            ci  = (pos - CORRIDOR_START) // CORRIDOR_LEN
            off = (pos - CORRIDOR_START) % CORRIDOR_LEN
            cf  = CORRIDOR_START + ci * CORRIDOR_LEN
            if off > 0:
                pos = cf + off - 1
            else:
                pos = ci * 6  # back to wedge
        elif pos < RING:
            pos = (pos + 1) % RING  # continue cw on ring once on wedge
        else:
            break
        path.append(pos)
    return path

def move_from_center(steps, corridor_idx):
    """Exit center down corridor `corridor_idx`, then out along ring."""
    path = []
    pos  = CENTER
    for _ in range(steps):
        if pos == CENTER:
            pos = CORRIDOR_START + corridor_idx * CORRIDOR_LEN + (CORRIDOR_LEN - 1)
        elif pos >= CORRIDOR_START and pos < CENTER:
            ci  = (pos - CORRIDOR_START) // CORRIDOR_LEN
            off = (pos - CORRIDOR_START) % CORRIDOR_LEN
            cf  = CORRIDOR_START + ci * CORRIDOR_LEN
            if off > 0:
                pos = cf + off - 1
            else:
                pos = ci * 6  # wedge
        elif pos < RING:
            pos = (pos + 1) % RING
        else:
            break
        path.append(pos)
    return path

def move_from_center_to(steps, corridor_idx, ring_dir):
    """Exit center through corridor `corridor_idx`, then ring in `ring_dir`."""
    steps_to_wedge = CORRIDOR_LEN + 1  # center→(off2)→(off1)→(off0)→wedge = 4 steps
    wedge_pos = corridor_idx * 6
    if steps <= steps_to_wedge:
        return move_from_center(steps, corridor_idx)
    steps_on_ring = steps - steps_to_wedge
    path_to_wedge = move_from_center(steps_to_wedge, corridor_idx)
    path_ring     = move_ring(wedge_pos, steps_on_ring, ring_dir)
    return path_to_wedge + path_ring

def add_unique(dirs, key, path):
    """Add path to dirs only if destination is unique."""
    if not path: return
    dest = path[-1]
    for existing_path in dirs.values():
        if existing_path[-1] == dest:
            return  # same destination already exists
    dirs[key] = path

def compute_dirs(pos, dice):
    """
    Compute ALL reachable destinations from pos with dice steps.
    
    Rules:
    - Anillo: cw, ccw, plus entering any reachable corridor
    - Corredor: out (same corr cw/ccw on ring) + in toward center
      If dice reaches center: exit via each other corridor (cw/ccw each)
      If dice doesn't reach center: just in/out options
    - Centro: exit via each of 5 corridors (cw/ccw each on ring)
    
    Returns dict {key: path_list}
    """
    dirs = {}

    if pos == CENTER:
        # Exit via any of 5 corridors, then cw or ccw on ring
        for i in range(5):
            p_cw  = move_from_center_to(dice, i, 'cw')
            p_ccw = move_from_center_to(dice, i, 'ccw')
            add_unique(dirs, f'd{i}_cw',  p_cw)
            add_unique(dirs, f'd{i}_ccw', p_ccw)
        return dirs

    if pos >= CORRIDOR_START:
        ci  = (pos - CORRIDOR_START) // CORRIDOR_LEN   # corridor index 0-4
        off = (pos - CORRIDOR_START) % CORRIDOR_LEN    # offset: 0=inner, 1=mid, 2=outer
        wedge_pos      = ci * 6
        # steps to reach center going in
        steps_to_center = CORRIDOR_LEN - off           # 1, 2, or 3
        # steps to reach wedge going out
        steps_to_wedge  = off + 1                      # 1, 2, or 3

        # ── OPTION A: Go OUT via same corridor ──────────────────────────
        # Walk out to wedge, then cw or ccw on ring with remaining steps
        if dice <= steps_to_wedge:
            # Can't reach the wedge: stop somewhere in corridor going out
            p_out = move_corridor_out(pos, dice)
            add_unique(dirs, 'out', p_out)
        else:
            # Reach wedge, then continue on ring
            path_to_wedge = move_corridor_out(pos, steps_to_wedge)
            steps_on_ring = dice - steps_to_wedge
            p_cw  = path_to_wedge + move_ring(wedge_pos, steps_on_ring, 'cw')
            p_ccw = path_to_wedge + move_ring(wedge_pos, steps_on_ring, 'ccw')
            add_unique(dirs, 'out_cw',  p_cw)
            add_unique(dirs, 'out_ccw', p_ccw)

        # ── OPTION B: Go IN toward center ────────────────────────────────
        if dice < steps_to_center:
            # Stop within this corridor going inward
            p_in = move_corridor_in(pos, dice)
            add_unique(dirs, 'in', p_in)
        elif dice == steps_to_center:
            # Land exactly on center
            p_in = move_corridor_in(pos, dice)
            add_unique(dirs, 'in', p_in)
        else:
            # Reach center, then MUST exit via each OTHER corridor — can't stop at center
            steps_after    = dice - steps_to_center
            path_to_center = move_corridor_in(pos, steps_to_center)
            # Do NOT add center as a stop option (dice doesn't land exactly there)
            for i in range(5):
                if i == ci: continue  # can't re-enter same corridor
                p_cw  = move_from_center_to(steps_after, i, 'cw')
                p_ccw = move_from_center_to(steps_after, i, 'ccw')
                add_unique(dirs, f'd{i}_cw',  path_to_center + p_cw  if p_cw  else [])
                add_unique(dirs, f'd{i}_ccw', path_to_center + p_ccw if p_ccw else [])

        return dirs

    # ── RING ─────────────────────────────────────────────────────────────
    # Basic ring movement
    add_unique(dirs, 'cw',  move_ring(pos, dice, 'cw'))
    add_unique(dirs, 'ccw', move_ring(pos, dice, 'ccw'))

    # Entering a corridor:
    # If on wedge: go straight in
    if pos % 6 == 0:
        p_in = move_corridor_in(pos, dice)
        add_unique(dirs, 'in', p_in)

    # Find nearest wedge going cw and ccw, enter its corridor with remaining steps
    for ring_dir in ('cw', 'ccw'):
        d = 1 if ring_dir == 'cw' else -1
        for stw in range(1, dice + 1):
            cand = (pos + d * stw) % RING
            if cand % 6 == 0:
                steps_left = dice - stw
                if steps_left == 0:
                    break  # landing exactly on wedge, already covered by cw/ccw
                rp  = move_ring(pos, stw, ring_dir)
                inp = move_corridor_in(cand, steps_left)
                fp  = rp + inp
                add_unique(dirs, f'in_{ring_dir}', fp)
                break  # only nearest wedge in each direction

    return dirs

def has_all_wedges(player):
    return set(CAT_IDS).issubset(set(player["wedges"]))

# ── QUESTIONS ─────────────────────────────────────────────────────────────
def load_questions(path):
    qs = {c["id"]:[] for c in CATEGORIES}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            # Añadimos el delimitador punto y coma
            for row in csv.DictReader(f, delimiter=';'):
                cat_id = CAT_NAME_TO_ID.get(row.get("categoria","").strip())
                if not cat_id: continue
                answers = [row.get(k,"").strip() for k in
                           ("respuesta_correcta","respuesta2","respuesta3","respuesta4")]
                answers = [a for a in answers if a]
                if len(answers) < 2: continue
                qs[cat_id].append({
                    "question": row.get("pregunta","").strip(),
                    "correct":  row.get("respuesta_correcta","").strip(),
                    "answers":  answers,
                })
        print(f"  ✓ {sum(len(v) for v in qs.values())} preguntas cargadas")
    except FileNotFoundError:
        print(f"  ⚠  CSV no encontrado: {path}")
    return qs

def pick_question(cat_id):
    pool  = state["questions"].get(cat_id, [])
    used  = state["used_questions"].setdefault(cat_id, set())
    avail = [i for i in range(len(pool)) if i not in used]
    if not avail:
        state["used_questions"][cat_id] = set()
        avail = list(range(len(pool)))
    if not avail: return None
    idx = random.choice(avail); used.add(idx)
    q   = pool[idx].copy()
    sh  = q["answers"].copy(); random.shuffle(sh)
    q["shuffled_answers"] = sh
    q["correct_index"]    = sh.index(q["correct"])
    return q

PLAYER_COLORS = [
    "#E53935","#1E88E5","#43A047","#FB8C00",
    "#8E24AA","#00ACC1","#F4511E","#6D4C41","#C0CA33","#EC407A"
]
PLAYER_TOKENS = ["♦","★","●","▲","■","♠","♥","♣","✿","⬟"]

state = {
    "phase":"lobby","players":[],"questions":{},"used_questions":{},
    "current_player":0,"current_question":None,"current_category":None,
    "dice_value":None,"winner":None,"chat":[],"host_id":None,
    "timer_seconds":DEFAULT_TIMER,"timer_remaining":None,
    "board":BOARD,"turn_ended":False,"anim_steps":[],
    "taken_colors":[],"pending_direction":False,"pending_dice":None,
    "possible_dests":{},
    "center_challenge":False,"center_cats_remaining":[],"center_cats_correct":[],
}
clients={}; timer_task=None

def public_state():
    q = None
    if state["current_question"]:
        cq = state["current_question"]
        q  = {"question":cq["question"],"answers":cq["shuffled_answers"],
              "cat_id":state["current_category"]}
    return {
        "phase":state["phase"],"players":state["players"],
        "current_player":state["current_player"],
        "current_question":q,"current_category":state["current_category"],
        "dice_value":state["dice_value"],"winner":state["winner"],
        "chat":state["chat"][-30:],"categories":CATEGORIES,"board":state["board"],
        "timer_seconds":state["timer_seconds"],"timer_remaining":state["timer_remaining"],
        "turn_ended":state["turn_ended"],"anim_steps":state["anim_steps"],
        "taken_colors":state["taken_colors"],
        "pending_direction":state["pending_direction"],
        "pending_dice":state["pending_dice"],
        "possible_dests":state["possible_dests"],
        "center_challenge":state["center_challenge"],
        "center_cats_remaining":state["center_cats_remaining"],
        "center_cats_correct":state["center_cats_correct"],
        "ring_size":RING,"center_idx":CENTER,
    }

async def broadcast(t, extra=None):
    msg  = json.dumps({"type":t,"state":public_state(),**(extra or {})})
    dead = []
    for ws in list(clients):
        try: await ws.send_str(msg)
        except: dead.append(ws)
    for ws in dead: clients.pop(ws, None)

# ── TIMER ─────────────────────────────────────────────────────────────────
async def run_timer():
    for remaining in range(state["timer_seconds"], -1, -1):
        state["timer_remaining"] = remaining
        await broadcast("tick", {"timer":remaining})
        if remaining == 0: break
        await asyncio.sleep(1)
    if state["phase"] == "question":
        cq = state["current_question"]
        state["timer_remaining"] = 0
        if state["center_challenge"]:
            await _center_fail(cq)
        else:
            state["phase"] = "answer_reveal"; state["turn_ended"] = True
            await broadcast("answer_reveal", {
                "correct":False,"correct_index":cq["correct_index"],"chosen_index":-1,
                "message":f"⏱ ¡Tiempo! Era: {cq['correct'] if cq else '?'}",
                "turn_ended":True})

def cancel_timer():
    global timer_task
    if timer_task and not timer_task.done(): timer_task.cancel()
    timer_task = None; state["timer_remaining"] = None

def start_timer():
    global timer_task; cancel_timer()
    timer_task = asyncio.create_task(run_timer())

# ── CENTER CHALLENGE ──────────────────────────────────────────────────────
async def _start_center_challenge(cur):
    cats = CAT_IDS.copy(); random.shuffle(cats)
    state["center_challenge"]      = True
    state["center_cats_remaining"] = cats
    state["center_cats_correct"]   = []
    state["phase"]      = "question"
    state["turn_ended"] = False
    await _ask_next_center_question(cur)

async def _ask_next_center_question(cur):
    cat_id   = state["center_cats_remaining"][0]
    state["current_category"] = cat_id
    state["current_question"] = pick_question(cat_id)
    cat_name = next((c["name"] for c in CATEGORIES if c["id"]==cat_id), "?")
    done     = len(state["center_cats_correct"])
    await broadcast("center_question", {
        "message":f"🏆 Pregunta {done+1}/5 — {cat_name}",
        "cats_remaining":state["center_cats_remaining"],
        "cats_correct":state["center_cats_correct"],
    })
    start_timer()

async def _center_fail(cq):
    cur = state["players"][state["current_player"]]
    state["center_challenge"]      = False
    state["center_cats_remaining"] = []
    state["center_cats_correct"]   = []
    state["phase"]      = "answer_reveal"
    state["turn_ended"] = True
    lost_name = ""
    if cur["wedges"]:
        lost = random.choice(cur["wedges"])
        cur["wedges"].remove(lost)
        lost_name = next((c["name"] for c in CATEGORIES if c["id"]==lost), "?")
    msg = f"✗ Fallaste. Era: {cq['correct'] if cq else '?'}."
    if lost_name: msg += f" Pierdes el quesito de {lost_name}."
    await broadcast("answer_reveal", {
        "correct":False,"correct_index":cq["correct_index"],"chosen_index":-1,
        "message":msg,"turn_ended":True,"center_fail":True})

# ── HANDLERS ──────────────────────────────────────────────────────────────
async def handle_join(ws, data):
    pid   = data.get("player_id") or f"p{len(state['players'])+1}"
    name  = data.get("name","Jugador")[:20]
    color = data.get("color","")

    # Jugador ya existe (reconexión): devolverle su estado sin importar la fase
    existing = next((p for p in state["players"] if p["id"]==pid), None)
    if existing:
        # Actualizar nombre por si acaso
        existing["name"] = name
        clients[ws] = pid
        await ws.send_str(json.dumps({"type":"joined","player_id":pid,
            "is_host":pid==state["host_id"],"state":public_state()}))
        return

    # Jugador nuevo: solo se permite en lobby
    if state["phase"] != "lobby":
        # Servidor reinició y no conoce a este jugador — añadirle como nuevo
        # solo si el estado es lobby, si no, informar de que la partida está en curso
        await ws.send_str(json.dumps({"type":"error",
            "message":"La partida ya ha comenzado. Espera a la siguiente."}))
        return

    if color in state["taken_colors"] or color not in PLAYER_COLORS:
        avail = [c for c in PLAYER_COLORS if c not in state["taken_colors"]]
        color = avail[0] if avail else PLAYER_COLORS[0]
    state["taken_colors"].append(color)
    idx = len(state["players"])
    state["players"].append({"id":pid,"name":name,"color":color,
        "token":PLAYER_TOKENS[idx%len(PLAYER_TOKENS)],
        "pos":CENTER,"wedges":[],"score":0})
    clients[ws] = pid
    if state["host_id"] is None: state["host_id"] = pid
    await ws.send_str(json.dumps({"type":"joined","player_id":pid,
        "is_host":pid==state["host_id"],"state":public_state()}))
    await broadcast("player_joined", {"message":f"🎉 {name} se ha unido"})

async def handle_set_timer(ws, data):
    if clients.get(ws) != state["host_id"] or state["phase"] != "lobby": return
    secs = max(10, min(120, int(data.get("seconds", DEFAULT_TIMER))))
    state["timer_seconds"] = secs
    await broadcast("timer_set", {"message":f"⏱ Tiempo: {secs}s"})

async def handle_start(ws):
    if state["phase"] != "lobby" or not state["players"]: return
    random.shuffle(state["players"])
    state["phase"] = "rolling"; state["current_player"] = 0
    order = ", ".join(p["name"] for p in state["players"])
    await broadcast("game_started", {"message":f"¡Comenzamos! Orden: {order}"})

async def handle_roll(ws):
    pid = clients.get(ws)
    cur = state["players"][state["current_player"]]
    if cur["id"] != pid or state["phase"] != "rolling": return

    dice = random.randint(1, 6)
    state["dice_value"]       = dice
    state["pending_dice"]     = dice
    state["pending_direction"]= True
    state["phase"]            = "choosing_direction"

    dirs = compute_dirs(cur["pos"], dice)
    state["possible_dests"] = dirs

    await broadcast("choose_direction", {
        "message": f"🎲 {cur['name']} sacó un {dice}",
        "dice": dice, "dirs": dirs})

async def handle_choose_direction(ws, data):
    pid = clients.get(ws)
    cur = state["players"][state["current_player"]]
    if cur["id"] != pid or state["phase"] != "choosing_direction": return

    direction = data.get("direction", "cw")
    dirs      = state["possible_dests"]
    steps     = dirs.get(direction)

    # Fallback: first available option
    if not steps:
        steps = next(iter(dirs.values()), []) if dirs else []

    state["anim_steps"]        = steps
    state["pending_direction"] = False
    state["pending_dice"]      = None

    new_pos = steps[-1] if steps else cur["pos"]
    cur["pos"] = new_pos

    square  = BOARD[new_pos]
    sq_type = square["type"]
    cat_id  = square["cat"]
    state["current_category"] = cat_id
    msg = f"{cur['name']} avanza {state['dice_value']}"

    if sq_type == "roll":
        state["phase"] = "rolling"; state["turn_ended"] = False
        await broadcast("rolled_again", {
            "message":f"🎲 {msg} — ¡Casilla dado! Vuelve a tirar",
            "square_type":"roll","steps":steps})
        return

    if sq_type == "center":
        if has_all_wedges(cur):
            state["phase"] = "question"
            state["center_challenge_pending"] = True
            await broadcast("rolled", {
                "message":f"🏆 {msg} — ¡Cayó en el centro con todos los quesitos!",
                "square_type":"center","steps":steps,
                "center_warning":True})  # client shows warning overlay
        else:
            cat_id = random.choice(CAT_IDS)
            state["current_category"] = cat_id
            state["current_question"] = pick_question(cat_id)
            state["phase"] = "question"; state["turn_ended"] = False
            await broadcast("rolled", {
                "message":f"🏆 {msg} — ¡Centro! Pregunta aleatoria",
                "square_type":"center","steps":steps})
        return

    state["current_question"] = pick_question(cat_id)
    state["phase"] = "question"; state["turn_ended"] = False
    if sq_type == "wedge": msg += " — ¡Casilla quesito!"
    await broadcast("rolled", {"message":msg,"square_type":sq_type,"steps":steps})

async def handle_anim_done(ws):
    pid = clients.get(ws)
    cur = state["players"][state["current_player"]]
    if cur["id"] != pid: return
    if state["phase"] == "question":
        # Check if center challenge is pending
        if state.get("center_challenge_pending"):
            state["center_challenge_pending"] = False
            await _start_center_challenge(cur)
        else:
            start_timer()
            await broadcast("question_ready", {})

async def handle_answer(ws, data):
    pid = clients.get(ws)
    cur = state["players"][state["current_player"]]
    if cur["id"] != pid or state["phase"] != "question": return
    cancel_timer()

    idx     = data.get("answer_index", -1)
    cq      = state["current_question"]
    correct = (idx == cq["correct_index"])

    if state["center_challenge"]:
        if correct:
            done_cat = state["center_cats_remaining"].pop(0)
            state["center_cats_correct"].append(done_cat)
            if not state["center_cats_remaining"]:
                state["phase"]  = "finished"
                state["winner"] = cur["id"]
                state["center_challenge"] = False
                await broadcast("answer_reveal", {
                    "correct":True,"correct_index":cq["correct_index"],"chosen_index":idx,
                    "message":f"🏆 ¡{cur['name']} gana TrivialRural!",
                    "turn_ended":True,"is_final":True})
            else:
                cat_name = next((c["name"] for c in CATEGORIES if c["id"]==done_cat),"?")
                await broadcast("answer_reveal", {
                    "correct":True,"correct_index":cq["correct_index"],"chosen_index":idx,
                    "message":f"✓ {cat_name} ¡Correcto! Siguiente…",
                    "turn_ended":False,"center_next":True})
                await asyncio.sleep(1.8)
                if state["center_challenge"]:
                    await _ask_next_center_question(cur)
        else:
            await _center_fail(cq)
        return

    sq_type  = BOARD[cur["pos"]]["type"]
    cat_id   = state["current_category"]
    if correct:
        cur["score"] += 1
        if sq_type == "wedge":
            if cat_id not in cur["wedges"]: cur["wedges"].append(cat_id)
            cat_name = next((c["name"] for c in CATEGORIES if c["id"]==cat_id), cat_id)
            state["phase"] = "answer_reveal"; state["turn_ended"] = True
            if has_all_wedges(cur):
                await broadcast("answer_reveal", {
                    "correct":True,"correct_index":cq["correct_index"],"chosen_index":idx,
                    "message":f"🧀 ¡Tienes los 5 quesitos! Ahora dirígete al centro para ganar.",
                    "turn_ended":True,"all_wedges":True})
            else:
                await broadcast("answer_reveal", {
                    "correct":True,"correct_index":cq["correct_index"],"chosen_index":idx,
                    "message":f"🧀 ¡Quesito de {cat_name}! Turno siguiente.",
                    "turn_ended":True})
        else:
            state["phase"] = "answer_reveal"; state["turn_ended"] = True
            await broadcast("answer_reveal", {
                "correct":True,"correct_index":cq["correct_index"],"chosen_index":idx,
                "message":"✓ ¡Correcto! Turno siguiente.","turn_ended":True})
    else:
        state["phase"] = "answer_reveal"; state["turn_ended"] = True
        await broadcast("answer_reveal", {
            "correct":False,"correct_index":cq["correct_index"],"chosen_index":idx,
            "message":f"✗ Incorrecto. Era: {cq['correct']}","turn_ended":True})

async def handle_next_turn(ws):
    pid = clients.get(ws)
    cur = state["players"][state["current_player"]]
    if cur["id"] != pid or state["phase"] != "answer_reveal": return
    cancel_timer()
    state["anim_steps"] = []
    state["current_player"] = (state["current_player"]+1) % len(state["players"])
    nxt = state["players"][state["current_player"]]
    state["phase"] = "rolling"; state["dice_value"] = None; state["current_question"] = None
    await broadcast("next_turn", {"message":f"Turno de {nxt['name']}"})

async def handle_chat(ws, data):
    pid  = clients.get(ws)
    p    = next((x for x in state["players"] if x["id"]==pid), None)
    msg  = data.get("message","").strip()[:120]
    if msg:
        state["chat"].append({"name":p["name"] if p else "?","msg":msg})
        await broadcast("chat", {"message":f"💬 {(p['name'] if p else '?')}: {msg}"})

async def handle_restart(ws):
    cancel_timer()
    for p in state["players"]:
        p["pos"] = CENTER; p["wedges"] = []; p["score"] = 0
    state.update(phase="lobby",current_player=0,current_question=None,
                 dice_value=None,winner=None,chat=[],used_questions={},
                 timer_remaining=None,turn_ended=False,anim_steps=[],
                 taken_colors=[],pending_direction=False,pending_dice=None,
                 possible_dests={},center_challenge=False,
                 center_cats_remaining=[],center_cats_correct=[])
    state.pop("center_challenge_pending", None)
    await broadcast("restarted", {"message":"♻️ Reiniciada."})

# ── WS / HTTP ─────────────────────────────────────────────────────────────
async def ws_handler(request):
    ws = web.WebSocketResponse(); await ws.prepare(request)
    print(f"  + {request.remote}")
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try: data = json.loads(msg.data)
                except: continue
                a = data.get("action","")
                if   a=="join":             await handle_join(ws,data)
                elif a=="set_timer":        await handle_set_timer(ws,data)
                elif a=="start":            await handle_start(ws)
                elif a=="roll":             await handle_roll(ws)
                elif a=="choose_direction": await handle_choose_direction(ws,data)
                elif a=="anim_done":        await handle_anim_done(ws)
                elif a=="answer":           await handle_answer(ws,data)
                elif a=="next_turn":        await handle_next_turn(ws)
                elif a=="chat":             await handle_chat(ws,data)
                elif a=="restart":          await handle_restart(ws)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE): break
    finally: clients.pop(ws, None)
    return ws

async def index_handler(r): return web.FileResponse(BASE_DIR/"index.html")
async def static_handler(r):
    fp = (BASE_DIR/r.match_info["filename"]).resolve()
    if BASE_DIR in fp.parents and fp.exists() and fp.is_file(): return web.FileResponse(fp)
    raise web.HTTPNotFound()

async def main():
    state["questions"] = load_questions(BASE_DIR/"trivial_amigos.csv")
    app = web.Application()
    app.router.add_get("/",           index_handler)
    app.router.add_get("/ws",         ws_handler)
    app.router.add_get("/{filename}", static_handler)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()
    print(f"\n{'═'*48}\n   🎲  TRIVIALRURAL — puerto {PORT}\n{'═'*48}\n")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n👋")
