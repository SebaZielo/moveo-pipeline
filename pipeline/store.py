"""MOVEO pipeline: read and write the app's documents in Supabase (table public.app_docs) and its picture storage.

Every document keeps the same shape it had in the Claude app database, so the scripts in pipeline/legacy work
unchanged: a collection is exported to a folder of <id>.json files, a script runs on the folder, and the changed
documents are written back.

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (GitHub Actions secrets). The key is never printed.
"""
import hashlib, json, os, time, urllib.parse
import requests

TABLE = "app_docs"


def _h(d):
    return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, url=None, key=None, dry_run=False):
        self.url = (url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.key = key or os.environ["SUPABASE_SERVICE_KEY"]
        self.dry = dry_run
        self.s = requests.Session()
        self.s.headers.update({"apikey": self.key, "Content-Type": "application/json"})
        if self.key.startswith("eyJ"):            # legacy service_role JWT; new sb_secret_ keys go in apikey only
            self.s.headers["Authorization"] = "Bearer " + self.key
        self.writes = 0
        self.deletes = 0

    # ------------------------------------------------------------------ low level
    def _req(self, method, path, tries=4, **kw):
        for i in range(tries):
            try:
                r = self.s.request(method, self.url + path, timeout=90, **kw)
            except requests.RequestException as ex:
                err = str(ex)
            else:
                if r.status_code < 400:
                    return r
                err = f"{r.status_code} {r.text[:300]}"
                if r.status_code < 500 and r.status_code != 429:
                    break
            time.sleep(3 * (i + 1))
        raise RuntimeError(f"Supabase {method} {path.split('?')[0]} failed: {err}")

    # ------------------------------------------------------------------ documents
    def list(self, collection, ids_only=False):
        """All docs of a collection: {id: data}."""
        out, start = {}, 0
        sel = "id" if ids_only else "id,data"
        while True:
            r = self._req("GET", f"/rest/v1/{TABLE}?select={sel}&collection=eq.{urllib.parse.quote(collection)}"
                                 f"&order=id&offset={start}&limit=1000")
            page = r.json()
            for row in page:
                out[row["id"]] = True if ids_only else row["data"]
            if len(page) < 1000:
                return out
            start += 1000

    def get(self, collection, doc_id):
        r = self._req("GET", f"/rest/v1/{TABLE}?select=data&collection=eq.{urllib.parse.quote(collection)}"
                             f"&id=eq.{urllib.parse.quote(doc_id)}")
        rows = r.json()
        return rows[0]["data"] if rows else None

    def put(self, collection, docs):
        """Upsert {id: data} (whole documents)."""
        rows = [{"collection": collection, "id": i, "data": d} for i, d in docs.items()]
        if self.dry:
            self.writes += len(rows)
            return
        for k in range(0, len(rows), 200):
            self._req("POST", f"/rest/v1/{TABLE}?on_conflict=collection,id", data=json.dumps(rows[k:k + 200], ensure_ascii=False).encode(),
                      headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        self.writes += len(rows)

    def delete(self, collection, ids):
        ids = list(ids)
        if self.dry:
            self.deletes += len(ids)
            return
        for k in range(0, len(ids), 150):
            chunk = ",".join('"' + i.replace('"', "") + '"' for i in ids[k:k + 150])
            self._req("DELETE", f"/rest/v1/{TABLE}?collection=eq.{urllib.parse.quote(collection)}&id=in.({urllib.parse.quote(chunk)})")
        self.deletes += len(ids)

    # ------------------------------------------------------------------ pictures
    def public_url(self, bucket, path):
        return f"{self.url}/storage/v1/object/public/{bucket}/{path}"

    def upload(self, bucket, path, data, content_type="image/jpeg"):
        if not self.dry:
            self._req("POST", f"/storage/v1/object/{bucket}/{path}", data=data,
                      headers={"Content-Type": content_type, "x-upsert": "true", "Cache-Control": "max-age=31536000"})
        return self.public_url(bucket, path)

    def remove(self, bucket, paths):
        if paths and not self.dry:
            self._req("DELETE", f"/storage/v1/object/{bucket}", data=json.dumps({"prefixes": list(paths)}))


class Collection:
    """A collection loaded in memory. Change docs freely, then save(): only changed/new docs are written,
    and docs removed from .docs are deleted."""

    def __init__(self, store, name, docs=None):
        self.store, self.name = store, name
        self.docs = store.list(name) if docs is None else docs
        self._orig = {i: _h(d) for i, d in self.docs.items()}

    def export(self, folder):
        os.makedirs(folder, exist_ok=True)
        for f in os.listdir(folder):
            if f.endswith(".json"):
                os.remove(os.path.join(folder, f))
        for i, d in self.docs.items():
            with open(os.path.join(folder, i + ".json"), "w") as fh:
                json.dump(d, fh, ensure_ascii=False)
        return folder

    def changed(self):
        return {i: d for i, d in self.docs.items() if self._orig.get(i) != _h(d)}

    def removed(self):
        return [i for i in self._orig if i not in self.docs]

    def save(self):
        ch, rm = self.changed(), self.removed()
        if ch:
            self.store.put(self.name, ch)
        if rm:
            self.store.delete(self.name, rm)
        self._orig = {i: _h(d) for i, d in self.docs.items()}
        return {"written": len(ch), "deleted": len(rm)}


def merge(doc, patch):
    """Apply an update the way ArtifactData 'update' did: fields replace, {"__delete__": true} removes a field."""
    for k, v in patch.items():
        if isinstance(v, dict) and v.get("__delete__") is True:
            doc.pop(k, None)
        else:
            doc[k] = v
    return doc
