"""Offline checks (no network, no Supabase): python3 -m tests.test_offline [EVENTS_DIR]"""
import datetime as dt, glob, json, os, sys
from pipeline import store as S, run as R, datecheck as D

class LocalStore(S.Store):
    def __init__(self, data):
        self.data, self.dry, self.writes, self.deletes = data, False, 0, 0
        self.url = "https://example.supabase.co"
    def list(self, c, ids_only=False):
        return {i: (True if ids_only else json.loads(json.dumps(d))) for i, d in self.data.get(c, {}).items()}
    def get(self, c, i):
        return self.data.get(c, {}).get(i)
    def put(self, c, docs):
        self.data.setdefault(c, {}).update(json.loads(json.dumps(docs))); self.writes += len(docs)
    def delete(self, c, ids):
        for i in ids: self.data.get(c, {}).pop(i, None)
        self.deletes += len(list(ids))
    def upload(self, b, p, data, content_type=None):
        return self.public_url(b, p)
    def remove(self, b, p):
        pass

def main():
    global bad
    today = dt.date(2026, 10, 29)

    def check(name, got, exp):
        global bad
        ok = all((got or {}).get(k) == v for k, v in exp.items())
        bad += not ok
        print("OK  " if ok else "FAIL", name, "->", json.dumps(got, default=str)[:200])

    E = lambda **k: dict({"id": "x", "title": "La casa de Bernarda Alba", "kind": "event", "dates": ["2026-10-31"], "url": "https://www.alternativateatral.com/obra1"}, **k)
    ev = lambda name, d, t=None, end=None, status="": {"name": name, "start": dt.date.fromisoformat(d), "time": t, "end": dt.date.fromisoformat(end) if end else None, "status": status}
    P = lambda evs=(), text="", ok=True, status=200: {"status": status, "ok": ok, "events": list(evs), "text": text}

    check("same dates -> ok", D.decide(E(time="20:30"), P([ev("La casa de Bernarda Alba", "2026-10-31", "20:30")]), today)[0], {"verify": "ok"})
    check("new dates -> fixed", D.decide(E(), P([ev("Bernarda Alba", "2026-10-31", "20:30"), ev("Bernarda Alba", "2026-11-07", "20:30")]), today)[0],
          {"verify": "fixed", "dates": ["2026-10-31", "2026-11-07"], "time": "20:30"})
    check("past only -> flagged, not hidden", D.decide(E(), P([ev("La casa de Bernarda Alba", "2026-10-20")]), today)[0], {"verify": "unconfirmed"})
    check("cancelled", D.decide(E(), P([ev("La casa de Bernarda Alba", "2026-10-31", status="https://schema.org/EventCancelled")]), today)[0], {"verify": "cancelled"})
    check("exhibition window", D.decide(E(kind="ongoing", dates=[], title="Fundaremos Buenos Aires"), P([ev("Fundaremos Buenos Aires", "2026-09-01", end="2027-01-31")]), today)[0],
          {"verify": "fixed", "kind": "ongoing", "end": "2027-01-31"})
    check("404", D.decide(E(), P(ok=False, status=404), today)[0], {"verify": "unconfirmed", "verifyNote": "Alternativa Teatral: Page gone (404)"})
    check("other event on page -> not used", D.decide(E(), P([ev("Recital de Fito Páez", "2026-11-02"), ev("Stand up Night", "2026-11-03")], text="Recital ..."), today)[0], {"verify": "unconfirmed"})
    check("text confirms", D.decide(E(), P(text="La casa de Bernarda Alba\nFunciones: sábado 31 de octubre a las 20.30. Entradas $15.000"), today)[0], {"verify": "ok"})
    check("text cancel flag", D.decide(E(), P(text="La casa de Bernarda Alba: función suspendida por lluvia"), today)[0], {"verify": "unconfirmed"})
    check("place keeps place", D.decide(E(kind="ongoing", dates=[], genre="food", title="La Capitana", days=["tue"]), P([ev("Gnocchi Night at La Capitana", "2026-10-29")]), today)[0], {"verifyNote": "Alternativa Teatral: page lists dated events, kept as a place"})
    check("weekly text adds end", D.decide(E(kind="ongoing", dates=[], days=["fri", "sat"], title="Bernarda Alba Lorca"), P(text="Bernarda Alba de Lorca. Viernes y sábados a las 20.30, hasta el 29 de noviembre."), today)[0], {"verify": "fixed", "end": "2026-11-29"})
    check("no network -> no write", {"p": D.decide(E(), P(ok=False, status=0), today)[0]}, {"p": None})
    check("instagram", D.decide(E(url="https://instagram.com/x"), {"social": True, "ok": False, "events": [], "text": "", "status": 0}, today)[0], {"verify": "unconfirmed"})
    print("local_dt", D.local_dt("2026-10-31T00:30:00.000Z"), D.local_dt("2026-10-31T21:00:00-03:00"), D.local_dt("2026-10-31"))

    # cleanup + store round trip on a real snapshot
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if src:
        events = {}
        for f in glob.glob(os.path.join(src, "*.json")):
            d = json.load(open(f)); events[d["id"]] = d
        st = LocalStore({"events": events, "reactions": {list(events)[0]: {}}})
        c = R.Ctx(st, today)
        print("cleanup on snapshot:", R.step_cleanup(c), "events left", len(st.data["events"]))
        c2 = R.Ctx(st, today)
        c2.events.docs[list(c2.events.docs)[1]]["verify"] = "ok"
        print("save only changed:", c2.events.save())
        q = D.queue(st.data["events"], today, 400)
        print("datecheck queue", len(q), "first", [(e["title"][:30], e.get("verifiedAt")) for e in q[:3]])
    print("FAILS", bad)

bad = 0
if __name__ == '__main__':
    main()
