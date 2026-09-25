"""Canned Audible / Audnexus / Google Books / Open Library responses for tests (no network)."""
import io, json, re, urllib.parse
from PIL import Image
import enrich
CALLS = []
def _jpeg(w, h, color=(90, 60, 30)):
    b = io.BytesIO(); Image.new("RGB", (w, h), color).save(b, "JPEG"); return b.getvalue()
P = {  # asin -> Audible product
 "B00B5HZGUG": dict(asin="B00B5HZGUG", title="The Martian", authors=[{"name":"Andy Weir"}], narrators=[{"name":"R.C. Bray"}],
                    runtime_length_min=1, release_date="2013-03-22", publisher_name="Podium Publishing",
                    product_images={"500":"https://m.media-amazon.com/images/I/martian._SL500_.jpg"}, publisher_summary="<p>Six days ago...</p>"),
 "B08G9PRS1K": dict(asin="B08G9PRS1K", title="Project Hail Mary", authors=[{"name":"Andy Weir"}], narrators=[{"name":"Ray Porter"}],
                    runtime_length_min=1, release_date="2021-05-04", product_images={"500":"https://m.media-amazon.com/images/I/phm._SL500_.jpg"}),
 "B003ZWFO7E": dict(asin="B003ZWFO7E", title="The Way of Kings", authors=[{"name":"Brandon Sanderson"}],
                    narrators=[{"name":"Michael Kramer"},{"name":"Kate Reading"}], runtime_length_min=1,
                    series=[{"title":"The Stormlight Archive","sequence":"1"}], release_date="2010-08-31",
                    product_images={"500":"https://m.media-amazon.com/images/I/wok._SL500_.jpg"}),
 "B002V1BRQS": dict(asin="B002V1BRQS", title="Killing Floor", authors=[{"name":"Lee Child"}], narrators=[{"name":"Dick Hill"}],
                    runtime_length_min=1, series=[{"title":"Jack Reacher","sequence":"1"}], release_date="1997-01-01"),
 "HOMEASIN01": dict(asin="HOMEASIN01", title="Home", authors=[{"name":"Harlan Coben"}], narrators=[{"name":"Steven Weber"}],
                    runtime_length_min=600, release_date="2016-09-20"),
}
AUDNEX = {"B003ZWFO7E": dict(title="The Way of Kings", seriesPrimary={"name":"The Stormlight Archive","position":"1"},
          genres=[{"name":"Science Fiction & Fantasy","type":"genre"},{"name":"Epic","type":"tag"}],
          narrators=[{"name":"Michael Kramer"},{"name":"Kate Reading"}], authors=[{"name":"Brandon Sanderson"}],
          image="https://m.media-amazon.com/images/I/wok-audnexus.jpg", summary="<p>Roshar is a world of stone and storms.</p>",
          runtimeLengthMin=1, releaseDate="2010-08-31")}
def fake_get(url, timeout=12, raw=False, limit=0):
    CALLS.append(url)
    u = urllib.parse.urlparse(url); q = dict(urllib.parse.parse_qsl(u.query))
    if u.netloc.endswith(("media-amazon.com",)) or "books.google" in url and "fife" in url:
        return _jpeg(2400, 2400) if "amazon" in url else _jpeg(800, 1200)
    if u.netloc == "api.audnex.us":
        asin = u.path.rsplit("/", 1)[-1]
        if asin in AUDNEX: return AUDNEX[asin]
        raise OSError("404")
    if u.netloc.startswith("api.audible."):
        m = re.search(r"/products/([A-Z0-9]{10})$", u.path)
        if m: return {"product": P[m.group(1)]} if m.group(1) in P else {}
        t = q.get("title", "").lower(); a = q.get("author", "").lower()
        hits = [p for p in P.values() if p["title"].lower() == t or (t and t in p["title"].lower())]
        if a:   # Audible's author filter does not match narrators
            hits = [p for p in hits if any(a in x["name"].lower() for x in p["authors"])]
        return {"products": hits}
    if "googleapis.com" in u.netloc:
        if "isbn:9780553293357" in q.get("q", ""):
            return {"items":[{"volumeInfo":{"title":"Foundation","authors":["Isaac Asimov"],"publishedDate":"1991",
                    "publisher":"Spectra","industryIdentifiers":[{"type":"ISBN_13","identifier":"9780553293357"}],
                    "imageLinks":{"thumbnail":"http://books.google.com/books/content?id=x&zoom=1&edge=curl"},
                    "description":"The Galactic Empire is dying."}}]}
        return {"items": []}
    if "openlibrary.org" in u.netloc:
        if u.netloc.startswith("covers"): raise OSError("404")
        return {"docs": []}
    raise OSError("unmocked " + url)
def install():
    enrich._get = fake_get
