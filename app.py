"""LabEquip - Laboratory Resource Control (counting semaphore, role-based dashboard)."""
import threading, webbrowser, datetime as dt
from collections import deque
from functools import wraps
from flask import Flask, jsonify, request, session, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "labequip-change-this-secret"


class CountingSemaphore:
    """One semaphore per equipment type. S = free units.
    wait()   -> P(S): take a unit if S > 0, otherwise join the waiting queue.
    signal() -> V(S): hand the unit to the oldest waiting request, else S = S + 1."""
    def __init__(self, n):
        self.total, self.value, self.queue = n, n, deque()

    def wait(self, rid):
        if self.value > 0:
            self.value -= 1
            return True
        self.queue.append(rid)
        return False

    def signal(self):
        if self.queue:
            return self.queue.popleft()
        self.value += 1
        return None


USERS, SEM, REQ, LOG = {}, {}, {}, []
NEXT = 1


def add_user(uid, name, role, pw):
    USERS[uid.lower()] = {"id": uid, "name": name, "role": role, "pw": generate_password_hash(pw)}


def seed_users():
    add_user("admin", "System Administrator", "admin", "admin123")
    add_user("fac01", "Prof. Ramesh Rao", "faculty", "faculty123")
    add_user("2503A51524", "N. Pradeep Kumar", "student", "student123")
    add_user("2503A51525", "Ananya Reddy", "student", "student123")
    add_user("2503A51530", "Rahul Verma", "student", "student123")


def seed_lab():
    global NEXT
    SEM.clear(); REQ.clear(); LOG.clear(); NEXT = 1
    for n, c in [("Oscilloscope", 3), ("Multimeter", 5), ("Function Generator", 2), ("Power Supply", 4)]:
        SEM[n] = CountingSemaphore(c)
    log("Semaphore engine initialised", None, "sys")


def now():
    return dt.datetime.now().strftime("%d %b %H:%M:%S")


def log(msg, uid=None, kind="info"):
    LOG.insert(0, {"t": now(), "msg": msg, "uid": uid, "kind": kind})
    del LOG[300:]


def me():
    return USERS.get((session.get("uid") or "").lower())


def auth(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = me()
            if not u:
                return jsonify(error="Please sign in again."), 401
            if roles and u["role"] not in roles:
                return jsonify(error="Your role does not have access to this action."), 403
            return f(u, *a, **k)
        return w
    return deco


def promote(rid):
    r = REQ[rid]; r["status"] = "allocated"; r["since"] = now()
    log(f"Waiting request granted: {r['name']} now holds {r['equipment']}", r["uid"], "alloc")


def do_allocate(eq, student, purpose, by):
    global NEXT
    sem = SEM.get(eq)
    if not sem:
        return "Unknown equipment."
    if any(r["uid"] == student["id"] and r["equipment"] == eq for r in REQ.values()):
        return f"{student['name']} already has an active request for {eq}."
    rid = NEXT; NEXT += 1
    REQ[rid] = {"id": rid, "uid": student["id"], "name": student["name"], "equipment": eq,
                "purpose": purpose or "General lab work", "time": now(), "since": None,
                "by": by["name"], "status": "waiting"}
    if sem.wait(rid):
        REQ[rid]["status"] = "allocated"; REQ[rid]["since"] = now()
        log(f"P({eq}): {student['name']} allocated {eq} (S={sem.value})", student["id"], "alloc")
        return None
    log(f"P({eq}): S=0, {student['name']} placed in waiting queue", student["id"], "wait")
    return None


@app.get("/")
def home():
    return render_template("dashboard.html") if me() else redirect("/login")


@app.get("/login")
def login_page():
    return redirect("/") if me() else render_template("login.html")


@app.post("/api/login")
def login():
    d = request.get_json() or {}
    u = USERS.get((d.get("id") or "").strip().lower())
    if not u or u["role"] != d.get("role") or not check_password_hash(u["pw"], d.get("password") or ""):
        return jsonify(error="Invalid ID, password or role."), 401
    session["uid"] = u["id"]
    log(f"{u['name']} signed in as {u['role']}", u["id"], "auth")
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/state")
@auth()
def state(u):
    staff = u["role"] != "student"
    reqs = []
    for r in sorted(REQ.values(), key=lambda r: r["id"]):
        if staff or r["uid"] == u["id"]:
            pos = list(SEM[r["equipment"]].queue).index(r["id"]) + 1 if r["status"] == "waiting" else 0
            reqs.append({**r, "pos": pos})
    logs = [l for l in LOG if staff or l["uid"] == u["id"]][:80]
    return jsonify(
        user={"id": u["id"], "name": u["name"], "role": u["role"]},
        equipment=[{"name": n, "total": s.total, "free": s.value, "used": s.total - s.value,
                    "waiting": len(s.queue)} for n, s in SEM.items()],
        requests=reqs, log=logs,
        sems=[{"name": n, "S": s.value, "total": s.total, "queue": [REQ[i]["name"] for i in s.queue]}
              for n, s in SEM.items()])


@app.get("/api/people")
@auth("faculty", "admin")
def people(u):
    return jsonify([{"id": x["id"], "name": x["name"], "role": x["role"]} for x in USERS.values()
                    if x["role"] == "student"])


@app.post("/api/allocate")
@auth()
def allocate(u):
    d = request.get_json() or {}
    student = u
    if u["role"] != "student":
        student = USERS.get((d.get("student_id") or "").lower())
        if not student:
            return jsonify(error="Select a student."), 400
    err = do_allocate(d.get("equipment"), student, (d.get("purpose") or "").strip()[:80], u)
    return (jsonify(error=err), 400) if err else jsonify(message="Request processed.")


@app.post("/api/release")
@auth()
def release(u):
    rid = (request.get_json() or {}).get("id")
    r = REQ.get(rid)
    if not r:
        return jsonify(error="Request not found."), 404
    if u["role"] == "student" and r["uid"] != u["id"]:
        return jsonify(error="You can only release your own allocations."), 403
    sem = SEM[r["equipment"]]
    del REQ[rid]
    if r["status"] == "waiting":
        sem.queue.remove(rid)
        log(f"{r['name']} cancelled waiting request for {r['equipment']}", r["uid"], "release")
        return jsonify(message="Request cancelled.")
    nxt = sem.signal()
    log(f"V({r['equipment']}): {r['name']} released {r['equipment']} (S={sem.value})", r["uid"], "release")
    if nxt:
        promote(nxt)
    return jsonify(message="Equipment released.")


@app.post("/api/equipment")
@auth("admin")
def equipment(u):
    d = request.get_json() or {}
    name, delta = (d.get("name") or "").strip(), int(d.get("delta") or 0)
    if not name or delta == 0:
        return jsonify(error="Enter an equipment name and a non-zero unit change."), 400
    sem = SEM.get(name)
    if not sem:
        if delta < 0:
            return jsonify(error="Equipment does not exist."), 400
        SEM[name] = CountingSemaphore(delta)
    elif delta > 0:
        sem.total += delta
        for _ in range(delta):
            nxt = sem.signal()
            if nxt:
                promote(nxt)
    else:
        if sem.value < -delta or sem.total + delta < 1:
            return jsonify(error="Only free units can be removed, and at least 1 unit must remain."), 400
        sem.value += delta; sem.total += delta
    log(f"Admin changed {name} capacity by {delta:+d}", u["id"], "sys")
    return jsonify(message="Inventory updated.")


@app.get("/api/users")
@auth("admin")
def users(u):
    return jsonify([{"id": x["id"], "name": x["name"], "role": x["role"]} for x in USERS.values()])


@app.post("/api/users")
@auth("admin")
def new_user(u):
    d = request.get_json() or {}
    uid, name, role, pw = (d.get(k, "").strip() for k in ("id", "name", "role", "password"))
    if not (uid and name and pw) or role not in ("student", "faculty", "admin"):
        return jsonify(error="Fill every field."), 400
    if uid.lower() in USERS:
        return jsonify(error="That ID already exists."), 400
    add_user(uid, name, role, pw)
    log(f"Admin created {role} account for {name}", u["id"], "sys")
    return jsonify(message="Account created.")


@app.post("/api/reset")
@auth("admin")
def reset(u):
    seed_lab()
    return jsonify(message="Demo data reset.")


seed_users(); seed_lab()

if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, threaded=False)  # one request at a time keeps P/V operations atomic
