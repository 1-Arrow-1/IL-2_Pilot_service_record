import os
import sqlite3
import re
from flask import Flask, jsonify, send_from_directory, request
import base64
import hashlib
import shutil 
import string
import sys
import signal
import json
import webbrowser
import threading
import time

if getattr(sys, 'frozen', False):
    # PyInstaller: static files are in the temp _MEIPASS dir
    STATIC_ROOT = os.path.join(sys._MEIPASS, "static")
else:
    STATIC_ROOT = os.path.abspath("static")

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".il2_pilot_passport")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

last_ping = time.time()

def ping_monitor():
    global last_ping
    while True:
        time.sleep(5)
        # If no ping in last 30 seconds, shut down Flask process (forcefully)
        if time.time() - last_ping > 30:
            print("No activity detected for 30s. Exiting Flask app.")
            os._exit(0)  # More reliable than shutdown_func; kills the process immediately.
            
def ensure_charactersranks(game_path, STATIC_ROOT):
    """
    Ensures that static/charactersranks/ contains all subfolders from the mod.
    If not, copies from game_path. Otherwise, does nothing.
    """
    mod_src = os.path.join(game_path, "data", "swf", "il2", "charactersranks")
    static_dest = os.path.join(STATIC_ROOT, "charactersranks")
    standard_dest = os.path.join(STATIC_ROOT, "standard_charactersranks")

    def is_mod_installed():
        # Pick a representative file/subfolder, e.g., 101000/big.png
        # (You may want to improve this check for your use case)
        return os.path.exists(os.path.join(mod_src, "101000", "big.png"))

    def is_mod_copied():
        # If at least one known rank subfolder exists, assume it’s already copied
        return os.path.exists(os.path.join(static_dest, "101000", "big.png"))

    if is_mod_installed() and not is_mod_copied():
        # Copy mod files only if not already there
        if os.path.exists(static_dest):
            shutil.rmtree(static_dest)
        shutil.copytree(mod_src, static_dest)
        print("Copied modded charactersranks to static/charactersranks.")
    else:
        print("Mod already present or not installed. No copy performed.")

def save_config(game_path):
    """Save the game path to the config file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"game_path": game_path}, f)

def load_config():
    """Load the game path from the config file, if it exists."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("game_path")
        except Exception:
            pass
    return None
    
def find_il2_installation():
    search_paths = [
        r"Program Files (x86)\1C Game Studios\IL-2 Sturmovik Battle of Stalingrad",
        r"Program Files (x86)\Steam\steamapps\common\IL-2 Sturmovik Battle of Stalingrad",
        r"Games\IL-2 Sturmovik Battle of Stalingrad",
    ]
    for drive in string.ascii_uppercase:
        for rel_path in search_paths:
            abs_path = f"{drive}:\\{rel_path}"
            if os.path.isdir(abs_path):
                db_candidate = os.path.join(abs_path, "data", "Career", "cp.db")
                if os.path.isfile(db_candidate):
                    return abs_path, db_candidate
    return None, None
    
app = Flask(__name__, static_folder=STATIC_ROOT)
#DEFAULT_GAME_PATH = r"C:\Program Files (x86)\Steam\steamapps\common\IL-2 Sturmovik Battle of Stalingrad"
#DEFAULT_DB_PATH = os.path.join(DEFAULT_GAME_PATH, "data", "Career", "cp.db")

game_path = load_config()
if game_path:
    DB_PATH = os.path.join(game_path, "data", "Career", "cp.db")
    if not os.path.isfile(DB_PATH):
        game_path = None
        DB_PATH = None
else:
    game_path, DB_PATH = find_il2_installation()
    if not DB_PATH or not os.path.isfile(DB_PATH):
        game_path = None
        DB_PATH = None  # Require user to provide it in the frontend!
    else:
        # Optional: store it if found automatically
        save_config(game_path)

if game_path and DB_PATH:
    ensure_charactersranks(game_path, STATIC_ROOT)  
    
SQUADRONS_ROOT = os.path.join(STATIC_ROOT, "squadrons")

if getattr(sys, 'frozen', False):
    # PyInstaller: Save user-generated files in a persistent directory, e.g., in user's home
    PILOT_PHOTO_DIR = os.path.join(os.path.expanduser("~"), ".il2_pilot_passport", "pilot_photos")
else:
    # For development, keep using static/pilot_photos
    PILOT_PHOTO_DIR = os.path.join(STATIC_ROOT, "pilot_photos")
os.makedirs(PILOT_PHOTO_DIR, exist_ok=True)  # Ensure the folder exists at startup



def extract_country_id(description):
    match = re.search(r'birthCountryInfo=(\d+)', description)
    return int(match.group(1)) if match else None

def extract_fullname(description):
    m = re.search(r'fullname=([^&]+)', description)
    return m.group(1).replace('%20', ' ') if m else "Unknown"

def extract_birthdate(description):
    m = re.search(r'birthDate=([\d\.]+)', description)
    if m:
        dt = m.group(1)
        parts = dt.split('.')
        if len(parts) >= 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
        else:
            return dt
    return ""

def get_latest_pilot(conn, desc):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, squadronId, insDate FROM pilot WHERE description=? ORDER BY insDate DESC LIMIT 1",
        (desc,)
    )
    row = cur.fetchone()
    return row  # (id, squadronId, insDate) or None
    
def collect_career_chain(conn, starting_career_id):
    """Traverse both up and down the chain, collecting all related career ids."""
    # Traverse up to find the root
    cur = conn.cursor()
    chain = set()
    current = starting_career_id
    while True:
        chain.add(current)
        cur.execute("SELECT extends FROM career WHERE id=?", (current,))
        row = cur.fetchone()
        if row and row[0] != -1:
            current = row[0]
        else:
            break

    # Traverse down (find all who extend any in the chain)
    added = True
    while added:
        added = False
        cur.execute("SELECT id, extends FROM career")
        for row in cur.fetchall():
            id_, ext = row
            if ext in chain and id_ not in chain:
                chain.add(id_)
                added = True
    return list(chain)

def get_squadron_shortname(squadron_id, conn):
    if not squadron_id:
        return "Unknown"
    cur = conn.cursor()
    cur.execute("SELECT configId FROM squadron WHERE id=?", (squadron_id,))
    row = cur.fetchone()
    if not row:
        return "Unknown"
    configId = row[0]
    folder = os.path.join(STATIC_ROOT, "squadrons", str(configId))
    info_file = os.path.join(folder, "info.locale=eng.txt")
    if not os.path.isfile(info_file):
        return "Unknown"
    with open(info_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("*"):
                parts = line.strip('|').split(',')
                if len(parts) >= 3:
                    shortname = parts[-1].strip().strip('"')
                    return shortname
                if "|" in line:
                    last_field = line.split('|')[0]
                    fields = last_field.split(',')
                    if len(fields) >= 3:
                        return fields[-1].strip().strip('"')
    return "Unknown"

def get_award_name_static(tpar2):
    info_path = os.path.join(STATIC_ROOT, 'achievements', str(tpar2), 'info.locale=eng.txt')
    if not os.path.isfile(info_path):
        return tpar2  # fallback, just the code
    try:
        with open(info_path, encoding='utf-8') as f:
            for line in f:
                if '&name=' in line:
                    match = re.search(r'&name\s*=\s*"([^"]+)"', line)
                    if match:
                        return match.group(1)
        return tpar2
    except Exception:
        return tpar2

def get_rank_name(country, rank_id, game_path=None):
    folder = str(country * 1000 + rank_id)
    info_filename = "info.locale=eng.txt"
    paths_to_try = []
    if game_path:
        mod_path = os.path.join(game_path, "data", "swf", "il2", "charactersranks", folder, info_filename)
        paths_to_try.append(mod_path)
    # Try modded static folder first, then standard static folder
    mod_static_path = os.path.join(STATIC_ROOT, "charactersranks", folder, info_filename)
    standard_static_path = os.path.join(STATIC_ROOT, "standard_charactersranks", folder, info_filename)
    paths_to_try.append(mod_static_path)
    paths_to_try.append(standard_static_path)
    for info_path in paths_to_try:
        if os.path.isfile(info_path):
            with open(info_path, encoding="utf-8") as f:
                for line in f:
                    if "&name=" in line:
                        match = re.search(r'&name\s*=\s*"([^"]+)"', line)
                        if match:
                            return match.group(1)
    return f"Rank {rank_id}"


def get_photo_path_for_desc(desc):
    pilot_hash = hashlib.sha256(desc.encode('utf-8')).hexdigest()[:20]
    img_path_fs = os.path.join(PILOT_PHOTO_DIR, f"{pilot_hash}.png")
    if os.path.exists(img_path_fs):
        if getattr(sys, 'frozen', False):
            return f"/pilot_photos/{pilot_hash}.png"
        else:
            return f"/static/pilot_photos/{pilot_hash}.png"
    return "/static/sample_photo.jpg"  # fallback



    
def find_chain_tip(conn, chain_ids):
    cur = conn.cursor()
    cur.execute("SELECT extends FROM career WHERE extends != -1")
    all_extends = set(row[0] for row in cur.fetchall())
    # The tip is a chain id that is not extended by anyone else
    tips = [cid for cid in chain_ids if cid not in all_extends]
    # Normally only one, but if not, pick the highest (latest)
    return tips[-1] if tips else chain_ids[0]
    
from datetime import datetime

def ensure_charactersranks(game_path, STATIC_ROOT):
    """
    Ensures that static/charactersranks/ contains all subfolders from the mod.
    If not, copies from game_path. Otherwise, does nothing.
    """
    mod_src = os.path.join(game_path, "data", "swf", "il2", "charactersranks")
    static_dest = os.path.join(STATIC_ROOT, "charactersranks")
    standard_dest = os.path.join(STATIC_ROOT, "standard_charactersranks")

    def is_mod_installed():
        # Pick a representative file/subfolder, e.g., 101000/big.png
        # (You may want to improve this check for your use case)
        return os.path.exists(os.path.join(mod_src, "101000", "big.png"))

    def is_mod_copied():
        # If at least one known rank subfolder exists, assume it’s already copied
        return os.path.exists(os.path.join(static_dest, "101000", "big.png"))

    if is_mod_installed() and not is_mod_copied():
        # Copy mod files only if not already there
        if os.path.exists(static_dest):
            shutil.rmtree(static_dest)
        shutil.copytree(mod_src, static_dest)
        print("Copied modded charactersranks to static/charactersranks.")
    else:
        print("Mod already present or not installed. No copy performed.")

def get_rank_image_path(country, rank_id, date_str, STATIC_ROOT):
    """
    Returns the URL to the appropriate rank image for Flask/static use.
    Falls back to standard_charactersranks if modded does not exist.
    """
    folder = str(country * 1000 + rank_id)
    filename = "big.png"
    # Soviet 1943+ special logic
    if country == 101 and date_str:
        try:
            from datetime import datetime
            dt = datetime.strptime(date_str, "%Y.%m.%d")
            if dt >= datetime(1943, 1, 1):
                filename = "big.1943.png"
        except Exception:
            pass
    elif country == 103:
        filename = "medium.png"     

    # Preferred (modded) path
    mod_path = os.path.join(STATIC_ROOT, "charactersranks", folder, filename)
    # Fallback (standard vanilla) path
    vanilla_path = os.path.join(STATIC_ROOT, "standard_charactersranks", folder, filename)
    if os.path.exists(mod_path):
        return f"/static/charactersranks/{folder}/{filename}"
    elif os.path.exists(vanilla_path):
        return f"/static/standard_charactersranks/{folder}/{filename}"
    else:
        return "/static/award_placeholder.png"   # Or any suitable fallback

def clear_config():
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
    except Exception:
        pass


@app.route('/api/set_game_path', methods=['POST'])
def set_game_path():
    global game_path, DB_PATH
    user_path = request.json.get('game_path', '').strip()
    if not user_path:
        return {'error': 'No path provided'}, 400
    db_candidate = os.path.join(user_path, "data", "Career", "cp.db")
    if not os.path.isfile(db_candidate):
        return {'error': 'cp.db not found in the provided path'}, 404
    game_path = user_path
    DB_PATH = db_candidate
    save_config(game_path)  # <-- persist it!
    ensure_charactersranks(game_path, STATIC_ROOT)
    return {'ok': True, 'game_path': game_path, 'db_path': DB_PATH}

    
@app.route("/")
def index():
    return send_from_directory(STATIC_ROOT, 'index.html')

@app.route('/api/save_photo', methods=['POST'])
def save_photo():
    desc = request.form.get('desc')
    if not desc:
        return {"error": "No pilot description"}, 400
    img_data = request.form.get('img_data')
    if not img_data:
        return {"error": "No image data"}, 400
    img_str = re.sub(r"^data:image/\w+;base64,", "", img_data)
    img_bytes = base64.b64decode(img_str)
    pilot_hash = hashlib.sha256(desc.encode('utf-8')).hexdigest()[:20]
    img_path = os.path.join(PILOT_PHOTO_DIR, f"{pilot_hash}.png")
    with open(img_path, 'wb') as f:
        f.write(img_bytes)
    if getattr(sys, 'frozen', False):
        return {"path": f"/pilot_photos/{pilot_hash}.png"}
    else:
        return {"path": f"/static/pilot_photos/{pilot_hash}.png"}


@app.route("/api/pilots")
def api_pilots():
    if not DB_PATH or not os.path.isfile(DB_PATH):
        clear_config()
        return jsonify({"error": "IL-2 not found. Please provide the correct game path."}), 400

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, playerId FROM career WHERE extends = -1")
        roots = cur.fetchall()

        country_map = {
            101: "Soviet Union",
            102: "Great Britain",
            103: "United States of America",
            201: "Germany"
        }

        pilots = []
        for root_id, player_id in roots:
            # 1. Get all career ids in the chain (root to tip)
            career_chain = collect_career_chain(conn, root_id)
            tip_career_id = find_chain_tip(conn, career_chain)

            # 2. Get the pilotId for the tip
            cur.execute("SELECT playerId FROM career WHERE id = ?", (tip_career_id,))
            row = cur.fetchone()
            if not row:
                continue
            tip_pilot_id = row[0]

            # 3. Get the latest pilot details
            cur.execute("SELECT description, squadronId FROM pilot WHERE id = ?", (tip_pilot_id,))
            row = cur.fetchone()
            if not row:
                continue
            desc, squadronId = row
            name = extract_fullname(desc)
            name_parts = name.split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            country_id = extract_country_id(desc)
            country_name = country_map.get(country_id, "Unknown")
            squadron_short = get_squadron_shortname(squadronId, conn) if squadronId else "Unknown"
            pilots.append({
                "desc": desc,
                "display": name,
                "country": country_name,
                "squadron": squadron_short,
                "pilot_id": tip_pilot_id,      # Add latest pilot id here
                "root_career_id": root_id      # for grouping/lookup
            })
        return jsonify(pilots)
    finally:
        conn.close()



@app.route("/api/service_record")
def api_service_record():
    if not DB_PATH or not os.path.isfile(DB_PATH):
        return jsonify({"error": "IL-2 not found. Please provide the correct game path."}), 400

    desc = request.args.get('desc')
    if not desc:
        return jsonify({"error": "Missing desc"}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Find the pilot id for the description
    cur.execute("SELECT id FROM pilot WHERE description = ?", (desc,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Pilot not found"}), 404
    pilot_id = row[0]

    # Find the career id for this pilot
    cur.execute("SELECT id FROM career WHERE playerId = ?", (pilot_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Career not found"}), 404
    starting_career_id = row[0]

    # Find all linked careers (merge chain)
    career_chain = collect_career_chain(conn, starting_career_id)
    # Get all pilotIds for the career chain
    pilot_ids = []
    for cid in career_chain:
        cur.execute("SELECT playerId FROM career WHERE id=?", (cid,))
        prow = cur.fetchone()
        if prow:
            pilot_ids.append(prow[0])

    # Use only the latest pilot for display (for details, not events)
    tip_career_id = find_chain_tip(conn, career_chain)
    cur.execute("SELECT playerId FROM career WHERE id = ?", (tip_career_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Career not found"}), 404
    latest_pilot_id = row[0]
    cur.execute("SELECT description, squadronId FROM pilot WHERE id = ?", (latest_pilot_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Pilot not found"}), 404
    desc, squadron_id = row
    name = extract_fullname(desc)
    name_parts = name.split(' ', 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    birthdate = extract_birthdate(desc)
    country_id = extract_country_id(desc)
    country_map = {
        101: "Soviet Union",
        102: "Great Britain",
        103: "United States of America",
        201: "Germany"
    }
    country_name = country_map.get(country_id, "Unknown")
    squadron_short = get_squadron_shortname(squadron_id, conn) if squadron_id else "Unknown"
    
    cur.execute("SELECT rankId FROM pilot WHERE id = ?", (latest_pilot_id,))
    rank_row = cur.fetchone()
    if rank_row:
        current_rank_id = rank_row[0]
        rank_name = get_rank_name(country_id, current_rank_id, game_path)
    else:
        current_rank_id = None
        rank_name = "Unknown"

    # Get all events for merged pilots
    ids_qs = ",".join(str(i) for i in pilot_ids)
    query = f"""SELECT date, type, rankId, tpar2, squadronId FROM event WHERE type IN (6,8) AND pilotId IN ({ids_qs}) ORDER BY date ASC"""
    cur.execute(query)
    promotions, awards = [], []
    for date, etype, rank_id, tpar2, squadron_id in cur.fetchall():
        d = date.split()[0] if date else ""
        dt_parts = d.split('.')
        if len(dt_parts) == 3:
            dt_formatted = f"{dt_parts[2]}.{dt_parts[1]}.{dt_parts[0]}"
            date_for_check = f"{dt_parts[0]}.{dt_parts[1]}.{dt_parts[2]}"
        else:
            dt_formatted = d
            date_for_check = d
        if etype == 6:
            rank_name = get_rank_name(country_id, rank_id, game_path)
            rank_img = get_rank_image_path(country_id, rank_id, date_for_check, STATIC_ROOT)
            promotions.append({"desc": f"{rank_name}", "date": dt_formatted, "img": rank_img})
        elif etype == 8:
            award_name = get_award_name_static(tpar2)
            awards.append({"desc": award_name, "date": dt_formatted, "tpar2": tpar2})

    photo_url = get_photo_path_for_desc(desc)
    return jsonify({
        "pilot_info": {
            "full_name": name,
            "first_name": first_name,
            "last_name": last_name,
            "birth_date": birthdate,
            "birth_country": country_name,
            "pilot_id": latest_pilot_id,
            "squadron": squadron_short,
            "rank_name": rank_name,
            "photo_url": photo_url
        },
        "promotions": promotions,
        "awards": awards
    })

from collections import OrderedDict

@app.route("/api/pilot_stats")
def api_pilot_stats():
    if not DB_PATH or not os.path.isfile(DB_PATH):
        return jsonify({"error": "IL-2 not found. Please provide the correct game path."}), 400

    desc = request.args.get('desc')
    if not desc:
        return jsonify({"error": "Missing desc"}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Find the pilot id for the description
    cur.execute("SELECT id FROM pilot WHERE description = ?", (desc,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Pilot not found"}), 404
    pilot_id = row[0]

    # Find the career id for this pilot
    cur.execute("SELECT id FROM career WHERE playerId = ?", (pilot_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Career not found"}), 404
    starting_career_id = row[0]

    # Find all linked careers (merge chain)
    career_chain = collect_career_chain(conn, starting_career_id)
    tip_career_id = find_chain_tip(conn, career_chain)
    cur.execute("SELECT playerId FROM career WHERE id = ?", (tip_career_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Career not found"}), 404
    latest_pilot_id = row[0]

    # Get all relevant columns
    cur.execute("PRAGMA table_info(pilot)")
    columns = [row[1] for row in cur.fetchall()]
    cur.execute(f"SELECT * FROM pilot WHERE id = ?", (latest_pilot_id,))
    pilot_row = cur.fetchone()
    stats = dict(zip(columns, pilot_row))

    # Remove undesired fields
    skip_fields = {
        'killLightPlane', 'killMediumPlane', 'killHeavyPlane',
        'careerStartDate', 'insDate'
    }

    def friendly_label(field):
        import re
        # Remove "kill" prefix for labels, but don't display the word 'kill'
        if field.lower().startswith('kill'):
            field = field[4:]
        # Fix AAA, AA, ID, etc.
        field = re.sub(r'([A-Z]{2,})([A-Z][a-z])', r'\1 \2', field)
        field = re.sub(r'(?<=[a-z])([A-Z])', r' \1', field)
        words = field.split()
        new_words = [w if w.isupper() and len(w) > 1 else w.capitalize() for w in words]
        return ' '.join(new_words)

    sorties = stats.get('sorties') or 0
    good = stats.get('goodSorties') or 0
    # OrderedDict to maintain order: first block, then rest
    output = OrderedDict()
    output["Sorties"] = sorties
    output["Good Sorties"] = good
    output["Success Rate"] = f"{(good / sorties * 100):.1f}%" if sorties else "0.0%"

    # Now add all other fields as per table order, skip already-added and unwanted
    skip_fields = {
        'description', 'id', 'name', 'lastName', 'personageId', 'avatarPath', 'birthDay',
        'isDeleted', 'penalty', 'penaltyPot', 'pcp', 'squadronId', 'rankId', 'state',
        'stateDate', 'statePeriod', 'trainPot', 'vehPot', 'shipPot', 'buildingPot', 'score',
        'transferProb', 'wounded', 'nickname', 'deathDate', 'bioInfo', 'startDate',
        'playerCountryId', 'startSquadronInfo', 'virtualSquadronId', 'playerPremiumStatus',
        'startRankInfo', 'careerStartDate', 'insDate', 'killLightPlane', 'killMediumPlane', 
        'killHeavyPlane'
    }
    # ...plus, skip "sorties", "goodSorties", "success_rate" (already shown)
    skip_fields |= {'sorties', 'goodSorties', 'success_rate'}

    for col in columns:
        if col in skip_fields:
            continue
        val = stats.get(col)
        # Optionally, skip fields with only zero value
        if isinstance(val, (int, float)) and val == 0:
            continue
        # Optional: nicer label
        label = friendly_label(col)
        output[label] = val

    return jsonify(output)

@app.route('/shutdown', methods=['POST'])
def shutdown():
    shutdown_func = request.environ.get('werkzeug.server.shutdown')
    if shutdown_func:
        shutdown_func()
    else:
        # Force exit if shutdown not found
        os.kill(os.getpid(), signal.SIGTERM)
    return 'Server shutting down...'

@app.route('/api/ping', methods=['POST'])
def ping():
    global last_ping
    last_ping = time.time()
    print("Ping received at", time.strftime("%H:%M:%S"))
    return {'ok': True}

    
@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory(STATIC_ROOT, path)
    
if getattr(sys, 'frozen', False):
    @app.route('/pilot_photos/<path:filename>')
    def serve_pilot_photo(filename):
        return send_from_directory(PILOT_PHOTO_DIR, filename)




if __name__ == "__main__":
    def open_browser():
        # Wait for Flask to start up
        time.sleep(1)
        webbrowser.open("http://127.0.0.1:5000/")
    threading.Thread(target=open_browser).start()
    threading.Thread(target=ping_monitor, daemon=True).start()
    print("About to run app.run()")
    app.run(debug=False)
    print("Flask app has exited!")