"""Messages class picker: which day/location chips light up.

Carollette picked "Oak Park" and the Friday chip lit too. The chip was drawn as
"all of its classes are picked", so when every Friday class happened to be at
Oak Park, Friday looked selected. A chip now lights only for what was tapped,
and a hand edit to a class inside it releases it.

The selection logic is a pure block in app/templates/messages/list.html
(<picker-state> markers). This test renders the page, pulls that block out of
the HTML the browser would get, and runs it under node, so it exercises the real
shipped code and not a copy.

Rules that have to hold:
  1. Tapping a location (or day) lights only that chip, even when its classes
     fully cover another group. THE REGRESSION.
  2. Tapping a chip again undoes it, leaving no stray classes behind.
  3. Turning a chip off keeps classes another lit chip still covers.
  4. Hand-ticking every class of a group does not light its chip.
  5. Unticking a class in a lit chip releases that chip; re-ticking does not
     relight it.
  6. Select all lights no chip; Clear/reset empties everything.
  7. Empty or unknown groups are no-ops.
  8. The page wires chips to keys and the send still reads the picked ids.

Needs `node` on PATH (preinstalled on GitHub's ubuntu runners).
Run:  RFID_ENABLED=false python3 tests/test_class_picker.py
Exit 0 = all green, 1 = failures.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import DanceClass, Location, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


if not shutil.which("node"):
    print("[FAIL] node is required to run the picker logic")
    sys.exit(1)

# Mon: Oak Park + Lathrup. Tue: Lathrup only. Wed: Oak Park + Lathrup.
# Thu: Oak Park only. Fri: Oak Park only (two classes) - the reported shape.
with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        db.session.add(admin)
    admin.set_password("pw12345")
    db.session.flush()
    op, lv = Location(name="Oak Park"), Location(name="Lathrup Village")
    db.session.add_all([op, lv])
    db.session.flush()
    spec = [("Mon A", 0, op), ("Mon B", 0, lv), ("Tue A", 1, lv), ("Wed A", 2, op),
            ("Wed B", 2, lv), ("Thu A", 3, op), ("Fri A", 4, op), ("Fri B", 4, op),
            ("Nowhere", 5, None)]
    for n, d, loc in spec:
        db.session.add(DanceClass(name=n, day_of_week=d, start_time=dtime(17, 0),
                                  end_time=dtime(18, 0), instructor_id=admin.id,
                                  location_id=loc.id if loc else None))
    db.session.commit()
    OP, LV = op.id, lv.id

client = app.test_client()
client.post("/auth/login", data={"username": "admin", "password": "pw12345"}, follow_redirects=True)
html = client.get("/messages").get_data(as_text=True)

m = re.search(r"// <picker-state>(.*?)// </picker-state>", html, re.S)
record("the page ships the picker-state block", bool(m))
classes_json = re.search(r"const classes = (\[.*?\]);\n", html, re.S)
classes = json.loads(classes_json.group(1))
by_name = {c["name"]: c["id"] for c in classes}
ids = lambda *names: sorted(by_name[n] for n in names)  # noqa: E731

HARNESS = r"""
%s
const classes = %s;
const out = {};
const names = Object.fromEntries(classes.map(c => [c.name, c.id]));
const id = (...n) => n.map(x => names[x]).sort((a, b) => a - b);
const snap = s => [...s.picked].sort((a, b) => a - b);
const on = (s, ...keys) => keys.filter(k => s.isOn(k)).sort();
const fresh = () => createPickerState(classes);
const OP = 'loc:%d', LV = 'loc:%d', FRI = 'day:4', MON = 'day:0', THU = 'day:3', WED = 'day:2', TUE = 'day:1';

let s = fresh();
s.toggleGroup(OP);
out.oak_only = [on(s, OP, LV, MON, TUE, WED, THU, FRI, 'day:5'), snap(s)];

s = fresh(); s.toggleGroup(LV);
out.lathrup_only = [on(s, OP, LV, MON, TUE, WED, THU, FRI), snap(s)];

s = fresh(); s.toggleGroup(OP); s.toggleGroup(OP);
out.oak_twice = [on(s, OP, FRI), snap(s)];

s = fresh(); s.toggleGroup(FRI);
out.fri_alone = [on(s, OP, FRI), snap(s)];

s = fresh(); s.toggleGroup(OP); s.toggleGroup(FRI);
out.oak_then_fri = [on(s, OP, FRI), snap(s)];
s.toggleGroup(FRI);
out.oak_then_fri_then_off = [on(s, OP, FRI), snap(s)];

s = fresh(); s.toggleGroup(MON); s.toggleGroup(OP); s.toggleGroup(MON);
out.mon_oak_mon_off = [on(s, MON, OP), snap(s)];

s = fresh(); s.toggleGroup(MON); s.toggleGroup(OP); s.toggleGroup(OP);
out.mon_oak_oak_off = [on(s, MON, OP), snap(s)];

s = fresh(); ['Fri A', 'Fri B'].forEach(n => s.toggleClass(names[n]));
out.hand_fri = [on(s, FRI, OP), snap(s)];

s = fresh(); s.toggleGroup(OP); s.toggleClass(names['Thu A']);
out.untick_in_oak = [on(s, OP, THU, FRI), snap(s)];
s.toggleClass(names['Thu A']);
out.reticked = [on(s, OP, THU, FRI), snap(s)];

s = fresh(); s.toggleGroup(OP); s.toggleClass(names['Mon B']);
out.tick_outside_group = [on(s, OP), snap(s)];

s = fresh(); s.toggleGroup(OP); s.pickAll(true);
out.select_all = [on(s, OP, LV, MON, FRI), snap(s).length];
s.pickAll(false);
out.clear = [on(s, OP, FRI), snap(s)];

s = fresh(); s.toggleGroup(OP); s.reset();
out.reset = [on(s, OP), snap(s)];
s.toggleGroup(OP);
out.after_reset = [on(s, OP), snap(s)];

s = fresh(); s.toggleGroup('loc:999'); s.toggleGroup('day:6'); s.toggleGroup('bogus:1'); s.toggleGroup('');
out.noop = [on(s, 'loc:999', 'day:6'), snap(s)];

s = fresh(); s.toggleGroup(OP); s.toggleGroup(LV);
out.both_locations = [on(s, OP, LV, MON, WED), snap(s).length];
s.toggleGroup(OP);
out.both_then_oak_off = [on(s, OP, LV), snap(s)];

console.log(JSON.stringify(out));
""" % (m.group(1) if m else "", json.dumps(classes), OP, LV)

proc = subprocess.run(["node", "-e", HARNESS], capture_output=True, text=True, timeout=30)
if proc.returncode != 0:
    print(proc.stderr)
    record("the picker logic runs under node", False, proc.stderr[-300:])
    sys.exit(1)
o = json.loads(proc.stdout)

def check(name, key, expect):
    record(name, o[key] == expect, f"got {o[key]} expected {expect}")

oak = ids("Mon A", "Wed A", "Thu A", "Fri A", "Fri B")
lath = ids("Mon B", "Tue A", "Wed B")

check("REGRESSION: tapping Oak Park lights only Oak Park, not Friday or Thursday",
      "oak_only", [["loc:%d" % OP], oak])
check("tapping Lathrup lights only Lathrup", "lathrup_only", [["loc:%d" % LV], lath])
check("tapping Oak Park twice leaves nothing lit or picked", "oak_twice", [[], []])
check("tapping Friday lights Friday only", "fri_alone", [["day:4"], ids("Fri A", "Fri B")])
check("Oak Park then Friday lights both", "oak_then_fri", [sorted(["day:4", "loc:%d" % OP]), oak])
check("turning Friday off keeps Oak Park's Friday classes (Oak Park still lit)",
      "oak_then_fri_then_off", [["loc:%d" % OP], oak])
check("Monday off while Oak Park is lit keeps Monday's Oak Park class, drops the Lathrup one",
      "mon_oak_mon_off", [["loc:%d" % OP], oak])
check("Oak Park off while Monday is lit keeps all Monday classes",
      "mon_oak_oak_off", [["day:0"], ids("Mon A", "Mon B")])
check("hand-ticking every Friday class does not light Friday", "hand_fri", [[], ids("Fri A", "Fri B")])
check("unticking a class releases the Oak Park chip but keeps the rest picked",
      "untick_in_oak", [[], sorted(set(oak) - set(ids("Thu A")))])
check("re-ticking the class does not relight Oak Park", "reticked", [[], oak])
check("ticking a class outside the lit chip leaves the chip lit",
      "tick_outside_group", [["loc:%d" % OP], sorted(oak + ids("Mon B"))])
check("Select all picks every class and lights no chip", "select_all", [[], len(classes)])
check("Clear empties everything", "clear", [[], []])
check("reset empties everything", "reset", [[], []])
check("a chip works again after reset", "after_reset", [["loc:%d" % OP], oak])
check("empty, unknown, and malformed groups are no-ops", "noop", [[], []])
check("both locations lit shows both and picks every located class",
      "both_locations", [sorted(["loc:%d" % OP, "loc:%d" % LV]), len(classes) - 1])
check("turning one location off leaves the other lit and exact",
      "both_then_oak_off", [["loc:%d" % LV], lath])

# Page wiring
record("chips pass a key to toggleGroup, not a derived id list",
       "onclick=\"toggleGroup('${key}')\"" in html and "classesWhere" not in html
       and "groupFullyPicked" not in html)
record("the send reads the picker's picked ids", "[...classPicker.picked]" in html)
record("day and location chips are keyed day:N and loc:N",
       "chip(DAYS[d], 'day:' + d)" in html and "chip(l.name, 'loc:' + l.id)" in html)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
