from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from database import get_db
from line_api import build_message, push_message

_scheduler = BackgroundScheduler(timezone="Asia/Tokyo")


def schedule_steps_for_follower(follower_id: int, scenario_id: int, follow_at: str):
    conn = get_db()
    try:
        steps = conn.execute(
            "SELECT * FROM scenario_steps WHERE scenario_id=? ORDER BY step_order",
            (scenario_id,)
        ).fetchall()

        fs = conn.execute(
            "SELECT id FROM follower_scenarios WHERE follower_id=? AND scenario_id=?",
            (follower_id, scenario_id)
        ).fetchone()
        if not fs:
            return

        base_time = datetime.fromisoformat(follow_at)
        cumulative_hours = 0

        for step in steps:
            cumulative_hours += step["delay_hours"]
            send_at = base_time + timedelta(hours=cumulative_hours)

            existing = conn.execute(
                "SELECT id FROM scheduled_messages WHERE follower_scenario_id=? AND scenario_step_id=?",
                (fs["id"], step["id"])
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO scheduled_messages
                       (follower_id, follower_scenario_id, scenario_step_id, scheduled_at)
                       VALUES (?, ?, ?, ?)""",
                    (follower_id, fs["id"], step["id"], send_at.strftime("%Y-%m-%d %H:%M:%S"))
                )

        conn.commit()
    finally:
        conn.close()


def _process_due_messages():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        due = conn.execute(
            """SELECT sm.id, sm.follower_id, sm.scenario_step_id,
                      f.line_user_id, f.company_id, f.status as follower_status,
                      ss.message_type, ss.message_content,
                      c.line_channel_token
               FROM scheduled_messages sm
               JOIN followers f ON f.id = sm.follower_id
               JOIN scenario_steps ss ON ss.id = sm.scenario_step_id
               JOIN companies c ON c.id = f.company_id
               WHERE sm.status = 'pending'
                 AND sm.scheduled_at <= ?
                 AND f.status = 'active'
                 AND f.delivery_paused = 0
               ORDER BY sm.scheduled_at""",
            (now,)
        ).fetchall()

        for msg in due:
            try:
                message = build_message(msg["message_type"], msg["message_content"])
                ok = push_message(
                    msg["line_user_id"],
                    [message],
                    msg["line_channel_token"]
                )
                status = "sent" if ok else "failed"
                conn.execute(
                    "UPDATE scheduled_messages SET status=?, sent_at=? WHERE id=?",
                    (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg["id"])
                )
            except Exception as e:
                conn.execute(
                    "UPDATE scheduled_messages SET status='failed', error_message=? WHERE id=?",
                    (str(e), msg["id"])
                )

        conn.commit()
    finally:
        conn.close()


def start_scheduler():
    _scheduler.add_job(_process_due_messages, "interval", minutes=1, id="send_steps")
    _scheduler.start()


def stop_scheduler():
    _scheduler.shutdown()
