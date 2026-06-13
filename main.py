import json
import secrets
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_db, init_db, hash_password
from line_api import verify_signature, get_profile, reply_message, push_text, build_message
from scheduler import start_scheduler, schedule_steps_for_follower

app = FastAPI()
sessions: Dict[str, dict] = {}


@app.on_event("startup")
async def startup():
    init_db()
    start_scheduler()


# ── 認証 ──────────────────────────────────────────────────────────────────────

def get_session(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "")
    s = sessions.get(token)
    if not s:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return s


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody):
    conn = get_db()
    admin = conn.execute(
        "SELECT * FROM admins WHERE username=? AND password_hash=?",
        (body.username, hash_password(body.password))
    ).fetchone()
    conn.close()
    if not admin:
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    token = secrets.token_hex(32)
    sessions[token] = {"admin_id": admin["id"], "username": admin["username"]}
    return {"token": token, "username": admin["username"]}


@app.post("/api/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "")
    sessions.pop(token, None)
    return {"ok": True}


# ── テナント（会社）管理 ───────────────────────────────────────────────────────

class CompanyBody(BaseModel):
    name: str
    line_channel_token: str
    line_channel_secret: str


@app.get("/api/companies")
def list_companies(s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        """SELECT c.*,
              (SELECT COUNT(*) FROM followers f WHERE f.company_id=c.id AND f.status='active') AS follower_count,
              (SELECT COUNT(*) FROM followers f WHERE f.company_id=c.id AND f.status='blocked') AS blocked_count,
              (SELECT COUNT(*) FROM scenarios sc WHERE sc.company_id=c.id) AS scenario_count
           FROM companies c ORDER BY c.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/companies")
def create_company(body: CompanyBody, s=Depends(get_session)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO companies (name, line_channel_token, line_channel_secret) VALUES (?,?,?)",
        (body.name, body.line_channel_token, body.line_channel_secret)
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return {"id": cid}


@app.put("/api/companies/{cid}")
def update_company(cid: int, body: CompanyBody, s=Depends(get_session)):
    conn = get_db()
    conn.execute(
        "UPDATE companies SET name=?, line_channel_token=?, line_channel_secret=? WHERE id=?",
        (body.name, body.line_channel_token, body.line_channel_secret, cid)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/companies/{cid}")
def delete_company(cid: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("DELETE FROM companies WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── ダッシュボード ─────────────────────────────────────────────────────────────

@app.get("/api/companies/{cid}/dashboard")
def dashboard(cid: int, s=Depends(get_session)):
    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=? AND status='active'", (cid,)
    ).fetchone()[0]
    blocked = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=? AND status='blocked'", (cid,)
    ).fetchone()[0]
    today = datetime.now().strftime("%Y-%m-%d")
    new_today = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=? AND date(follow_at)=?", (cid, today)
    ).fetchone()[0]

    # 直近30日の友達追加数
    daily = conn.execute(
        """SELECT date(follow_at) as d, COUNT(*) as cnt
           FROM followers WHERE company_id=? AND follow_at >= date('now','-30 days')
           GROUP BY d ORDER BY d""",
        (cid,)
    ).fetchall()

    # 流入経路別
    sources = conn.execute(
        """SELECT COALESCE(ca.name,'直接') as name, COUNT(*) as cnt
           FROM followers f
           LEFT JOIN campaigns ca ON ca.id = f.campaign_id
           WHERE f.company_id=?
           GROUP BY ca.id ORDER BY cnt DESC LIMIT 10""",
        (cid,)
    ).fetchall()

    # 配信状況
    pending = conn.execute(
        """SELECT COUNT(*) FROM scheduled_messages sm
           JOIN followers f ON f.id=sm.follower_id
           WHERE f.company_id=? AND sm.status='pending'""", (cid,)
    ).fetchone()[0]
    sent = conn.execute(
        """SELECT COUNT(*) FROM scheduled_messages sm
           JOIN followers f ON f.id=sm.follower_id
           WHERE f.company_id=? AND sm.status='sent'""", (cid,)
    ).fetchone()[0]

    conn.close()
    return {
        "total_followers": total,
        "blocked_followers": blocked,
        "new_today": new_today,
        "pending_messages": pending,
        "sent_messages": sent,
        "daily_follows": [dict(r) for r in daily],
        "inflow_sources": [dict(r) for r in sources],
    }


# ── 顧客管理 ──────────────────────────────────────────────────────────────────

@app.get("/api/companies/{cid}/followers")
def list_followers(cid: int, q: str = "", status: str = "", s=Depends(get_session)):
    conn = get_db()
    sql = """SELECT f.*, COALESCE(ca.name,'直接') as campaign_name
             FROM followers f
             LEFT JOIN campaigns ca ON ca.id=f.campaign_id
             WHERE f.company_id=?"""
    params = [cid]
    if q:
        sql += " AND (f.display_name LIKE ? OR f.tags LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND f.status=?"
        params.append(status)
    sql += " ORDER BY f.follow_at DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class FollowerUpdateBody(BaseModel):
    tags: Optional[list] = None
    memo: Optional[str] = None


@app.patch("/api/followers/{fid}")
def update_follower(fid: int, body: FollowerUpdateBody, s=Depends(get_session)):
    conn = get_db()
    if body.tags is not None:
        conn.execute("UPDATE followers SET tags=? WHERE id=?", (json.dumps(body.tags), fid))
    if body.memo is not None:
        conn.execute("UPDATE followers SET memo=? WHERE id=?", (body.memo, fid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/followers/{fid}/steps")
def follower_steps(fid: int, s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        """SELECT sm.*, ss.message_type, ss.message_content,
                  sc.name as scenario_name, ss.step_order
           FROM scheduled_messages sm
           JOIN scenario_steps ss ON ss.id=sm.scenario_step_id
           JOIN follower_scenarios fs ON fs.id=sm.follower_scenario_id
           JOIN scenarios sc ON sc.id=fs.scenario_id
           WHERE sm.follower_id=?
           ORDER BY sm.scheduled_at""",
        (fid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── シナリオ管理 ──────────────────────────────────────────────────────────────

class ScenarioBody(BaseModel):
    name: str
    description: str = ""
    is_active: int = 1


@app.get("/api/companies/{cid}/scenarios")
def list_scenarios(cid: int, s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        """SELECT sc.*,
              (SELECT COUNT(*) FROM scenario_steps st WHERE st.scenario_id=sc.id) AS step_count
           FROM scenarios sc WHERE sc.company_id=? ORDER BY sc.id DESC""",
        (cid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/companies/{cid}/scenarios")
def create_scenario(cid: int, body: ScenarioBody, s=Depends(get_session)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO scenarios (company_id, name, description, is_active) VALUES (?,?,?,?)",
        (cid, body.name, body.description, body.is_active)
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return {"id": sid}


@app.put("/api/scenarios/{sid}")
def update_scenario(sid: int, body: ScenarioBody, s=Depends(get_session)):
    conn = get_db()
    conn.execute(
        "UPDATE scenarios SET name=?, description=?, is_active=? WHERE id=?",
        (body.name, body.description, body.is_active, sid)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/scenarios/{sid}")
def delete_scenario(sid: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("DELETE FROM scenario_steps WHERE scenario_id=?", (sid,))
    conn.execute("DELETE FROM scenarios WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── ステップ管理 ──────────────────────────────────────────────────────────────

class StepBody(BaseModel):
    step_order: int
    delay_hours: int
    message_type: str = "text"
    message_content: str


@app.get("/api/scenarios/{sid}/steps")
def list_steps(sid: int, s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM scenario_steps WHERE scenario_id=? ORDER BY step_order",
        (sid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/scenarios/{sid}/steps")
def create_step(sid: int, body: StepBody, s=Depends(get_session)):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO scenario_steps (scenario_id, step_order, delay_hours, message_type, message_content)
           VALUES (?,?,?,?,?)""",
        (sid, body.step_order, body.delay_hours, body.message_type, body.message_content)
    )
    conn.commit()
    step_id = cur.lastrowid
    conn.close()
    return {"id": step_id}


@app.put("/api/steps/{step_id}")
def update_step(step_id: int, body: StepBody, s=Depends(get_session)):
    conn = get_db()
    conn.execute(
        """UPDATE scenario_steps SET step_order=?, delay_hours=?, message_type=?, message_content=?
           WHERE id=?""",
        (body.step_order, body.delay_hours, body.message_type, body.message_content, step_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/steps/{step_id}")
def delete_step(step_id: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("DELETE FROM scenario_steps WHERE id=?", (step_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── デフォルトシナリオ設定 ────────────────────────────────────────────────────

@app.post("/api/companies/{cid}/default-scenario/{sid}")
def set_default_scenario(cid: int, sid: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("UPDATE companies SET default_scenario_id=? WHERE id=?", (sid, cid))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 流入経路（キャンペーン）管理 ─────────────────────────────────────────────

class CampaignBody(BaseModel):
    name: str
    description: str = ""


@app.get("/api/companies/{cid}/campaigns")
def list_campaigns(cid: int, s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM campaigns WHERE company_id=? ORDER BY id DESC", (cid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/companies/{cid}/campaigns")
def create_campaign(cid: int, body: CampaignBody, s=Depends(get_session)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO campaigns (company_id, name, description) VALUES (?,?,?)",
        (cid, body.name, body.description)
    )
    conn.commit()
    cid_new = cur.lastrowid
    conn.close()
    return {"id": cid_new}


@app.delete("/api/campaigns/{camp_id}")
def delete_campaign(camp_id: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("DELETE FROM campaigns WHERE id=?", (camp_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 流入経路トラッキング（公開エンドポイント）─────────────────────────────────

@app.get("/join/{cid}/{camp_id}")
def track_and_redirect(cid: int, camp_id: int):
    conn = get_db()
    camp = conn.execute(
        "SELECT * FROM campaigns WHERE id=? AND company_id=?", (camp_id, cid)
    ).fetchone()
    if not camp:
        conn.close()
        raise HTTPException(status_code=404)

    conn.execute(
        "INSERT INTO campaign_clicks (campaign_id) VALUES (?)", (camp_id,)
    )
    conn.execute(
        "UPDATE campaigns SET click_count=click_count+1 WHERE id=?", (camp_id,)
    )
    conn.commit()

    company = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    conn.close()

    # LINE友達追加URLへリダイレクト（実際はLINEのチャンネルURLを会社ごとに設定すべきだが、
    # ここでは汎用的にLINE公式アカウントのURLを使う）
    line_add_url = f"https://line.me/R/ti/p/"
    return RedirectResponse(url=line_add_url, status_code=302)


# ── LINEウェブフック ──────────────────────────────────────────────────────────

@app.post("/webhook/{cid}")
async def webhook(cid: int, request: Request):
    body = await request.body()
    sig = request.headers.get("x-line-signature", "")

    conn = get_db()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    conn.close()

    if not company:
        raise HTTPException(status_code=404)

    if not verify_signature(body, sig, company["line_channel_secret"]):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400)

    for event in payload.get("events", []):
        _handle_event(event, dict(company))

    return {"ok": True}


def _handle_event(event: dict, company: dict):
    etype = event.get("type")
    source = event.get("source", {})
    user_id = source.get("userId")
    if not user_id:
        return

    if etype == "follow":
        _on_follow(user_id, company)
    elif etype == "unfollow":
        _on_unfollow(user_id, company["id"])


def _on_follow(user_id: str, company: dict):
    cid = company["id"]
    token = company["line_channel_token"]

    profile = get_profile(user_id, token) or {}
    display_name = profile.get("displayName", "")
    picture_url = profile.get("pictureUrl", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 直近5分以内のキャンペーンクリックと照合
    conn = get_db()
    unmatched_click = conn.execute(
        """SELECT cc.id, cc.campaign_id FROM campaign_clicks cc
           JOIN campaigns ca ON ca.id=cc.campaign_id
           WHERE ca.company_id=? AND cc.line_user_id IS NULL
             AND cc.clicked_at >= datetime('now','-5 minutes','localtime')
           ORDER BY cc.clicked_at DESC LIMIT 1""",
        (cid,)
    ).fetchone()

    campaign_id = None
    if unmatched_click:
        campaign_id = unmatched_click["campaign_id"]
        conn.execute(
            "UPDATE campaign_clicks SET line_user_id=?, matched_at=? WHERE id=?",
            (user_id, now, unmatched_click["id"])
        )

    # フォロワー登録（再フォローの場合はstatus更新）
    existing = conn.execute(
        "SELECT id FROM followers WHERE company_id=? AND line_user_id=?", (cid, user_id)
    ).fetchone()

    if existing:
        fid = existing["id"]
        conn.execute(
            "UPDATE followers SET status='active', display_name=?, picture_url=?, follow_at=?, unfollow_at=NULL WHERE id=?",
            (display_name, picture_url, now, fid)
        )
    else:
        cur = conn.execute(
            """INSERT INTO followers (company_id, line_user_id, display_name, picture_url, follow_at, campaign_id)
               VALUES (?,?,?,?,?,?)""",
            (cid, user_id, display_name, picture_url, now, campaign_id)
        )
        fid = cur.lastrowid

    conn.commit()

    # デフォルトシナリオがあればステップ配信を開始
    scenario_id = company.get("default_scenario_id")
    if scenario_id:
        existing_fs = conn.execute(
            "SELECT id FROM follower_scenarios WHERE follower_id=? AND scenario_id=?",
            (fid, scenario_id)
        ).fetchone()
        if not existing_fs:
            conn.execute(
                "INSERT INTO follower_scenarios (follower_id, scenario_id) VALUES (?,?)",
                (fid, scenario_id)
            )
            conn.commit()
            schedule_steps_for_follower(fid, scenario_id, now)

    conn.close()


def _on_unfollow(user_id: str, cid: int):
    conn = get_db()
    conn.execute(
        "UPDATE followers SET status='blocked', unfollow_at=datetime('now','localtime') WHERE company_id=? AND line_user_id=?",
        (cid, user_id)
    )
    conn.commit()
    conn.close()


# ── 手動でシナリオをフォロワーに割り当て ───────────────────────────────────

class AssignBody(BaseModel):
    follower_ids: list
    scenario_id: int


@app.post("/api/companies/{cid}/assign-scenario")
def assign_scenario(cid: int, body: AssignBody, s=Depends(get_session)):
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assigned = 0
    for fid in body.follower_ids:
        f = conn.execute(
            "SELECT * FROM followers WHERE id=? AND company_id=?", (fid, cid)
        ).fetchone()
        if not f:
            continue
        existing = conn.execute(
            "SELECT id FROM follower_scenarios WHERE follower_id=? AND scenario_id=?",
            (fid, body.scenario_id)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO follower_scenarios (follower_id, scenario_id) VALUES (?,?)",
                (fid, body.scenario_id)
            )
            conn.commit()
            schedule_steps_for_follower(fid, body.scenario_id, now)
            assigned += 1
    conn.close()
    return {"assigned": assigned}


# ── 配信停止・再開 ────────────────────────────────────────────────────────────

@app.post("/api/followers/{fid}/pause")
def pause_delivery(fid: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("UPDATE followers SET delivery_paused=1 WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/followers/{fid}/resume")
def resume_delivery(fid: int, s=Depends(get_session)):
    conn = get_db()
    conn.execute("UPDATE followers SET delivery_paused=0 WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── セグメント配信 ─────────────────────────────────────────────────────────────

class BroadcastBody(BaseModel):
    title: str
    tag_filter: str = ""
    message_type: str = "text"
    message_content: str


@app.get("/api/companies/{cid}/broadcasts")
def list_broadcasts(cid: int, s=Depends(get_session)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM broadcasts WHERE company_id=? ORDER BY id DESC LIMIT 100", (cid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/companies/{cid}/broadcast-preview")
def broadcast_preview(cid: int, tag: str = "", s=Depends(get_session)):
    conn = get_db()
    sql = "SELECT COUNT(*) FROM followers WHERE company_id=? AND status='active' AND delivery_paused=0"
    params = [cid]
    if tag:
        sql += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    count = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return {"count": count}


@app.post("/api/companies/{cid}/broadcasts")
def create_broadcast(cid: int, body: BroadcastBody, s=Depends(get_session)):
    conn = get_db()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not company:
        conn.close()
        raise HTTPException(status_code=404)

    sql = "SELECT * FROM followers WHERE company_id=? AND status='active' AND delivery_paused=0"
    params = [cid]
    if body.tag_filter:
        sql += " AND tags LIKE ?"
        params.append(f'%"{body.tag_filter}"%')
    targets = conn.execute(sql, params).fetchall()

    cur = conn.execute(
        """INSERT INTO broadcasts (company_id, title, tag_filter, message_type, message_content, target_count, status)
           VALUES (?,?,?,?,?,?,'sending')""",
        (cid, body.title, body.tag_filter, body.message_type, body.message_content, len(targets))
    )
    bid = cur.lastrowid
    conn.commit()

    from line_api import build_message, push_message as _push
    token = company["line_channel_token"]
    sent = 0
    failed = 0
    for f in targets:
        try:
            msg = build_message(body.message_type, body.message_content)
            ok = _push(f["line_user_id"], [msg], token)
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE broadcasts SET sent_count=?, failed_count=?, status='done', sent_at=? WHERE id=?",
        (sent, failed, now, bid)
    )
    conn.commit()
    conn.close()
    return {"id": bid, "sent": sent, "failed": failed}


# ── 詳細分析 ──────────────────────────────────────────────────────────────────

@app.get("/api/companies/{cid}/analytics")
def analytics(cid: int, s=Depends(get_session)):
    conn = get_db()

    # 月別フォロワー増減（12ヶ月）
    monthly = conn.execute(
        """SELECT strftime('%Y-%m', follow_at) as month, COUNT(*) as follows,
                  SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) as blocks
           FROM followers WHERE company_id=?
             AND follow_at >= date('now', '-12 months', 'localtime')
           GROUP BY month ORDER BY month""",
        (cid,)
    ).fetchall()

    # 全体のブロック率
    total_all = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=?", (cid,)
    ).fetchone()[0]
    total_blocked = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=? AND status='blocked'", (cid,)
    ).fetchone()[0]
    block_rate = round(total_blocked / total_all * 100, 1) if total_all > 0 else 0

    # シナリオ別進捗（各ステップに何人いるか）
    scenario_progress = conn.execute(
        """SELECT sc.name as scenario_name, ss.step_order,
                  COUNT(sm.id) as count, sm.status
           FROM scheduled_messages sm
           JOIN scenario_steps ss ON ss.id=sm.scenario_step_id
           JOIN follower_scenarios fs ON fs.id=sm.follower_scenario_id
           JOIN scenarios sc ON sc.id=fs.scenario_id
           JOIN followers f ON f.id=sm.follower_id
           WHERE f.company_id=?
           GROUP BY sc.id, ss.step_order, sm.status
           ORDER BY sc.id, ss.step_order""",
        (cid,)
    ).fetchall()

    # 配信成功率（直近30日）
    delivery_stats = conn.execute(
        """SELECT sm.status, COUNT(*) as cnt
           FROM scheduled_messages sm
           JOIN followers f ON f.id=sm.follower_id
           WHERE f.company_id=? AND sm.created_at >= date('now','-30 days','localtime')
           GROUP BY sm.status""",
        (cid,)
    ).fetchall()

    # タグ別友達数
    all_followers = conn.execute(
        "SELECT tags FROM followers WHERE company_id=? AND status='active'", (cid,)
    ).fetchall()
    tag_counts: dict = {}
    for row in all_followers:
        try:
            tags = json.loads(row["tags"] or "[]")
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        except Exception:
            pass

    # 配信停止中の人数
    paused_count = conn.execute(
        "SELECT COUNT(*) FROM followers WHERE company_id=? AND delivery_paused=1", (cid,)
    ).fetchone()[0]

    conn.close()
    return {
        "monthly_stats": [dict(r) for r in monthly],
        "block_rate": block_rate,
        "total_followers": total_all,
        "total_blocked": total_blocked,
        "paused_count": paused_count,
        "scenario_progress": [dict(r) for r in scenario_progress],
        "delivery_stats": [dict(r) for r in delivery_stats],
        "tag_counts": [{"tag": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])],
    }


# ── 静的ファイル ──────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="static", html=True), name="static")
