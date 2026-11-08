import os, csv, io, sqlite3
from datetime import date, timedelta
from typing import Optional
import requests
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "arj_football.db")
API_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
API_BASE = "https://api.football-data.org/v4"

app = FastAPI(title="ARJ Football AI API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY, date TEXT, competition TEXT, home_team TEXT, away_team TEXT,
            home_goals INTEGER, away_goals INTEGER, status TEXT, source TEXT DEFAULT 'csv'
        )""")
init_db()
app.mount("/frontend", StaticFiles(directory=os.path.join(BASE, "..", "frontend")), name="frontend")

def normalize(row):
    return {
        "id": str(row.get("id") or f"{row.get('date')}-{row.get('home_team')}-{row.get('away_team')}"),
        "date": row.get("date"),
        "competition": row.get("competition") or "Unknown",
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "home_goals": row.get("home_goals"),
        "away_goals": row.get("away_goals"),
        "status": row.get("status") or "SCHEDULED",
        "source": row.get("source") or "csv",
    }

def save_rows(rows):
    inserted = 0
    with db() as con:
        for r in rows:
            x = normalize(r)
            if not x["home_team"] or not x["away_team"] or not x["date"]:
                continue
            cur = con.execute("""INSERT OR REPLACE INTO matches
                (id,date,competition,home_team,away_team,home_goals,away_goals,status,source)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (x["id"],x["date"],x["competition"],x["home_team"],x["away_team"],
                 x["home_goals"],x["away_goals"],x["status"],x["source"]))
            inserted += cur.rowcount
    return inserted

@app.get("/")
def home():
    return FileResponse(os.path.join(BASE, "..", "frontend", "index.html"))

@app.get("/api/health")
def health():
    return {"ok": True, "api_token_configured": bool(API_TOKEN), "database": "sqlite"}

@app.get("/api/matches")
def matches(competition: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None):
    clauses, params = [], []
    if competition:
        clauses.append("competition = ?"); params.append(competition)
    if date_from:
        clauses.append("date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("date <= ?"); params.append(date_to + "T23:59:59")
    sql = "SELECT * FROM matches"
    if clauses: sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY date DESC LIMIT 500"
    with db() as con:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    return {"count": len(rows), "matches": [{
        "id":r["id"],"date":r["date"],"competition":r["competition"],"homeTeam":r["home_team"],
        "awayTeam":r["away_team"],"homeGoals":r["home_goals"],"awayGoals":r["away_goals"],
        "status":r["status"],"source":r["source"]} for r in rows]}

@app.post("/api/sync")
def sync(competition: str = "PL", date_from: Optional[str] = None, date_to: Optional[str] = None):
    if not API_TOKEN:
        raise HTTPException(400, "توکن API روی سرور تنظیم نشده است.")
    date_from = date_from or date.today().isoformat()
    date_to = date_to or (date.today() + timedelta(days=7)).isoformat()
    try:
        response = requests.get(
            f"{API_BASE}/competitions/{competition}/matches",
            headers={"X-Auth-Token": API_TOKEN},
            params={"dateFrom": date_from, "dateTo": date_to},
            timeout=20,
        )
        if response.status_code == 429:
            raise HTTPException(429, "محدودیت درخواست API؛ کمی بعد دوباره تلاش کن.")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise HTTPException(502, f"خطا در دریافت از سرویس فوتبال: {exc}")
    rows = []
    for m in payload.get("matches", []):
        score = m.get("score", {}).get("fullTime", {})
        rows.append({
            "id": f"fd-{m.get('id')}", "date": m.get("utcDate"),
            "competition": competition,
            "home_team": m.get("homeTeam", {}).get("name"),
            "away_team": m.get("awayTeam", {}).get("name"),
            "home_goals": score.get("home"), "away_goals": score.get("away"),
            "status": m.get("status"), "source": "football-data.org"
        })
    count = save_rows(rows)
    return {"inserted": count, "received": len(rows), "source": "football-data.org"}

@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "فقط فایل CSV پذیرفته می‌شود.")
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        required = {"date","home_team","away_team","home_goals","away_goals","status"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise HTTPException(400, "ستون‌های الزامی: date, home_team, away_team, home_goals, away_goals, status")
        rows=[]
        for r in reader:
            for k in ("home_goals","away_goals"):
                r[k] = int(r[k]) if r.get(k,"").strip().isdigit() else None
            rows.append(r)
        n=save_rows(rows)
        return {"inserted": n}
    except UnicodeDecodeError:
        raise HTTPException(400, "فایل باید با UTF-8 ذخیره شده باشد.")

@app.get("/api/teams")
def teams():
    with db() as con:
        rows=con.execute("SELECT home_team AS team FROM matches UNION SELECT away_team AS team FROM matches ORDER BY team").fetchall()
    return {"teams":[r["team"] for r in rows]}

@app.get("/api/teams/{team}/form")
def team_form(team: str):
    with db() as con:
        rows=con.execute("""SELECT date,home_team,away_team,home_goals,away_goals
        FROM matches WHERE status='FINISHED' AND (home_team=? OR away_team=?)
        AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY date DESC LIMIT 10""",(team,team)).fetchall()
    games=[]
    for r in reversed(rows):
        home=r["home_team"]==team
        gf=r["home_goals"] if home else r["away_goals"]
        ga=r["away_goals"] if home else r["home_goals"]
        games.append({"date":r["date"],"goals_for":gf,"goals_against":ga,"points":3 if gf>ga else 1 if gf==ga else 0})
    return {"team":team,"matches":games,"recent_five":games[-5:],"previous_five":games[:5]}

@app.get("/api/teams/{team}/trend")
def team_trend(team: str):
    # ML classifies historical form direction only; it does not predict match results.
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        raise HTTPException(501, "برای تحلیل ML، وابستگی scikit-learn را نصب کن.")

    with db() as con:
        names = [r["team"] for r in con.execute(
            "SELECT home_team AS team FROM matches UNION SELECT away_team AS team FROM matches"
        ).fetchall()]

    def history_for(name):
        with db() as con:
            rows = con.execute("""SELECT date,home_team,away_team,home_goals,away_goals
                FROM matches WHERE status='FINISHED' AND (home_team=? OR away_team=?)
                AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY date""",
                (name,name)).fetchall()
        out=[]
        for r in rows:
            home = r["home_team"] == name
            gf = r["home_goals"] if home else r["away_goals"]
            ga = r["away_goals"] if home else r["home_goals"]
            out.append((gf,ga,3 if gf>ga else 1 if gf==ga else 0))
        return out

    def features(window):
        points=[x[2] for x in window]
        gf=sum(x[0] for x in window); ga=sum(x[1] for x in window)
        return [sum(points), gf/5, ga/5, (gf-ga)/5, sum(1 for p in points if p==3), sum(1 for p in points if p==0)]

    X, y = [], []
    current = None
    for name in names:
        games = history_for(name)
        for i in range(10, len(games)+1):
            previous, recent = games[i-10:i-5], games[i-5:i]
            delta = sum(x[2] for x in recent) - sum(x[2] for x in previous)
            label = "improving" if delta >= 3 else "declining" if delta <= -3 else "stable"
            X.append(features(recent)); y.append(label)
        if name == team:
            current = games

    if not current or len(current) < 10:
        return {"team":team,"label":"insufficient_data","message":"برای تحلیل روند به حداقل ۱۰ مسابقه پایان‌یافته نیاز است."}
    if len(set(y)) < 2 or len(y) < 12:
        return {"team":team,"label":"insufficient_training_data",
                "message":"برای آموزش مدل، تاریخچه چند تیم و نمونه‌های متنوع‌تری لازم است.",
                "training_samples":len(y)}

    model = RandomForestClassifier(n_estimators=120, max_depth=4, random_state=42,
                                   class_weight="balanced")
    model.fit(X, y)
    prediction = model.predict([features(current[-5:])])[0]
    confidence = float(max(model.predict_proba([features(current[-5:])])[0]))
    return {"team":team,"label":prediction,"confidence":round(confidence,3),
            "training_samples":len(y),"method":"RandomForest historical-form classifier",
            "note":"Historical performance trend classification only; not a future match prediction."}
