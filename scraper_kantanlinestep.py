#!/usr/bin/env python3
"""
かんたんLINEステップ データ抽出スクレイパー
使い方: python3 scraper.py
"""
import asyncio, csv, json, os, sys
from pathlib import Path
from datetime import datetime

async def run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright が見つかりません")
        sys.exit(1)

    print("=" * 50)
    print("  かんたんLINEステップ データ抽出ツール")
    print("=" * 50)
    print()
    print("ログイン情報を入力してください（このPCには保存されません）")
    print()

    login_url = input("ログインURL (例: https://system.kantanlinestep.com/login): ").strip()
    if not login_url:
        login_url = "https://system.kantanlinestep.com/login"

    email = input("メールアドレス: ").strip()
    import getpass
    password = getpass.getpass("パスワード: ")

    out_dir = Path("/home/ubuntu/apps/line-step/data")
    out_file = out_dir / f"kantan_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print()
    print("⏳ ブラウザを起動しています...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await ctx.new_page()

        # ── ログイン ──
        print(f"🌐 {login_url} にアクセス中...")
        await page.goto(login_url, timeout=30000)
        await page.wait_for_load_state("networkidle")

        # スクリーンショットでページ確認
        ss_path = "/tmp/kantan_login.png"
        await page.screenshot(path=ss_path)
        print(f"📸 ログインページ確認: {ss_path}")

        # メール/パスワード入力（一般的なセレクタを試す）
        selectors_email = ['input[type="email"]', 'input[name="email"]',
                           'input[name="login_id"]', 'input[id*="mail"]', 'input[placeholder*="メール"]']
        selectors_pass  = ['input[type="password"]']

        filled_email = False
        for sel in selectors_email:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.fill(email)
                    filled_email = True
                    print(f"✅ メール入力: {sel}")
                    break
            except Exception:
                continue

        if not filled_email:
            print("⚠️  メール欄が見つかりません。ページHTMLを確認します...")
            html = await page.content()
            print(html[:2000])
            await browser.close()
            return

        for sel in selectors_pass:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.fill(password)
                    break
            except Exception:
                continue

        # サブミット
        try:
            await page.locator('button[type="submit"]').first.click(timeout=5000)
        except Exception:
            await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=15000)
        print("✅ ログイン完了")

        await page.screenshot(path="/tmp/kantan_after_login.png")
        print("📸 ログイン後確認: /tmp/kantan_after_login.png")

        # ── 友達一覧ページを探す ──
        current_url = page.url
        base = "/".join(current_url.split("/")[:3])

        friend_url_candidates = [
            f"{base}/friends", f"{base}/contacts", f"{base}/users",
            f"{base}/friend", f"{base}/members", f"{base}/admin/friends",
            f"{base}/admin/contacts", f"{base}/friend/list",
        ]

        # ナビゲーションリンクからも探す
        nav_links = await page.locator("a").all()
        for link in nav_links:
            try:
                text = (await link.inner_text()).strip()
                href = await link.get_attribute("href")
                if href and any(kw in text for kw in ["友達", "フォロワー", "顧客", "会員", "連絡先"]):
                    full = href if href.startswith("http") else base + href
                    if full not in friend_url_candidates:
                        friend_url_candidates.insert(0, full)
                        print(f"🔗 友達ページ候補: {text} → {full}")
            except Exception:
                pass

        friends_page = None
        for url in friend_url_candidates:
            try:
                resp = await page.goto(url, timeout=8000)
                if resp and resp.status < 400:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                    # 友達っぽいデータがあるか確認
                    rows = await page.locator("table tr, .friend-item, .contact-item, [class*='friend'], [class*='user-row']").count()
                    if rows > 1:
                        friends_page = url
                        print(f"✅ 友達ページ発見: {url} ({rows}行)")
                        break
            except Exception:
                pass

        if not friends_page:
            print("⚠️  友達一覧ページが自動検出できませんでした")
            print("現在のURL:", page.url)
            manual_url = input("友達一覧ページのURLを貼り付けてください: ").strip()
            if manual_url:
                await page.goto(manual_url, timeout=15000)
                await page.wait_for_load_state("networkidle")
                friends_page = manual_url

        await page.screenshot(path="/tmp/kantan_friends.png")
        print("📸 友達一覧確認: /tmp/kantan_friends.png")

        # ── データ抽出 ──
        print()
        print("📋 データを抽出しています...")
        all_rows = []

        page_num = 1
        while True:
            # テーブル行を取得
            rows = await page.locator("table tbody tr").all()
            if not rows:
                # カード形式の場合
                rows = await page.locator("[class*='friend'], [class*='contact'], [class*='user-item']").all()

            for row in rows:
                try:
                    cells = await row.locator("td").all()
                    if len(cells) < 2:
                        continue

                    texts = []
                    for cell in cells:
                        texts.append((await cell.inner_text()).strip())

                    # タグを探す（badgeやtagクラスを持つ要素）
                    tag_els = await row.locator("[class*='tag'], [class*='badge'], [class*='label']").all()
                    tags = []
                    for t in tag_els:
                        txt = (await t.inner_text()).strip()
                        if txt:
                            tags.append(txt)

                    # LINE User IDを探す（Uから始まる文字列）
                    full_text = " ".join(texts)
                    import re
                    uid_match = re.search(r'U[a-f0-9]{32}', full_text)
                    line_uid = uid_match.group(0) if uid_match else ""

                    all_rows.append({
                        "raw_cells": texts,
                        "tags": ",".join(tags),
                        "line_user_id": line_uid,
                    })
                except Exception:
                    pass

            print(f"  ページ {page_num}: {len(rows)}件")

            # 次ページ
            next_btn = page.locator("a:has-text('次'), a:has-text('next'), [class*='next']:not([disabled]), [aria-label*='next']").first
            try:
                if await next_btn.is_visible(timeout=2000) and await next_btn.is_enabled(timeout=2000):
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    page_num += 1
                else:
                    break
            except Exception:
                break

        # ── CSV出力 ──
        print()
        if all_rows:
            # ヘッダーを確認して書き出し
            print(f"📊 {len(all_rows)}件 抽出完了")
            print()

            # 列数を確認
            max_cols = max(len(r["raw_cells"]) for r in all_rows) if all_rows else 0
            print(f"検出された列数: {max_cols}")
            if all_rows:
                print("1行目サンプル:", all_rows[0]["raw_cells"])
                print("タグサンプル:", all_rows[0]["tags"])

            print()
            print("列の意味を確認してください（番号で指定）:")
            for i in range(max_cols):
                sample = all_rows[0]["raw_cells"][i] if i < len(all_rows[0]["raw_cells"]) else ""
                print(f"  [{i}] {sample[:40]}")

            try:
                uid_col  = int(input("LINE User ID の列番号 (なければEnter): ").strip() or "-1")
                name_col = int(input("表示名の列番号 (なければEnter): ").strip() or "-1")
                tag_col  = int(input("タグの列番号 (タグを別途検出済みならEnter): ").strip() or "-1")
                date_col = int(input("友達追加日の列番号 (なければEnter): ").strip() or "-1")
            except ValueError:
                uid_col = name_col = tag_col = date_col = -1

            with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["line_user_id", "display_name", "tags", "follow_at"])
                writer.writeheader()
                for row in all_rows:
                    cells = row["raw_cells"]
                    uid = cells[uid_col] if uid_col >= 0 and uid_col < len(cells) else row["line_user_id"]
                    name = cells[name_col] if name_col >= 0 and name_col < len(cells) else ""
                    tags = cells[tag_col] if tag_col >= 0 and tag_col < len(cells) else row["tags"]
                    date = cells[date_col] if date_col >= 0 and date_col < len(cells) else ""
                    writer.writerow({"line_user_id": uid, "display_name": name, "tags": tags, "follow_at": date})

            print()
            print(f"✅ 保存完了: {out_file}")
            print(f"   → 管理画面の「データ移行」→「CSVを取り込み」でインポートできます")
        else:
            print("⚠️  データが抽出できませんでした")
            html = await page.content()
            print("ページソース（最初の3000文字）:")
            print(html[:3000])

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
