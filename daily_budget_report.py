#!/usr/bin/env python3
"""
Мониторинг бюджетов Яндекс.Директ через Click.ru API.

Проверяет все активные кабинеты Директ у всех суб-пользователей Click.ru,
считает доступный остаток (баланс кабинета + баланс плательщиков) и
прогнозирует, на сколько дней хватит бюджета.

Расход считается за вчерашний день. Если вчера расхода не было —
по среднему за последние 7 дней.

По умолчанию показывает только кабинеты, где бюджет закончится менее чем
через 3 дня. С флагом --all выводит все кабинеты.

Зависимости: только стандартная библиотека Python 3.8+.
"""

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Глобальная переменная: устанавливается из конфига
BASE_URL = "https://api.click.ru/V0"


# ──────────────────────────────────────────────
#  Конфигурация
# ──────────────────────────────────────────────

def load_config(config_path: str = "config.json") -> Tuple[str, str]:
    """
    Загружает конфигурацию из JSON-файла и переменных окружения.

    Приоритет: переменная окружения CLICKRU_TOKEN переопределяет файл.
    Если токен не найден — скрипт завершается с ошибкой.

    Возвращает кортеж (token, base_url).
    """
    config: Dict[str, Any] = {}

    # 1. Пробуем загрузить из JSON-файла
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Не удалось прочитать {config_path}: {e}", file=sys.stderr)

    # 2. Переменные окружения (имеют приоритет)
    env_token = os.environ.get("CLICKRU_TOKEN")
    env_base = os.environ.get("CLICKRU_BASE_URL")

    token = env_token or config.get("clickru", {}).get("token", "")
    base_url = env_base or config.get("clickru", {}).get("base_url", "https://api.click.ru/V0")

    if not token:
        print("❌ Ошибка: не указан токен Click.ru API.", file=sys.stderr)
        print("   Скопируйте config.example.json → config.json и укажите токен", file=sys.stderr)
        print("   или задайте переменную окружения CLICKRU_TOKEN.", file=sys.stderr)
        sys.exit(1)

    return token, base_url


# ──────────────────────────────────────────────
#  API-клиент (только stdlib, без внешних библиотек)
# ──────────────────────────────────────────────

def api_get(path: str, token: str, user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    GET-запрос к Click.ru API. Возвращает распарсенный JSON.

    При ошибке возвращает словарь с ключом "error".
    """
    url = f"{BASE_URL}{path}"
    headers = {
        "X-Auth-Token": token,
        "Accept": "application/json",
    }
    if user_id is not None:
        headers["X-Auth-UserId"] = str(user_id)

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {e.code}", "body": body[:500]}
    except (urllib.error.URLError, OSError) as e:
        return {"error": str(e)}


def fetch_csv(url: str, token: str, user_id: Optional[int] = None) -> str:
    """Загружает CSV со статистикой и возвращает как строку."""
    headers = {
        "X-Auth-Token": token,
        "Accept": "text/csv",
    }
    if user_id is not None:
        headers["X-Auth-UserId"] = str(user_id)

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return ""


# ──────────────────────────────────────────────
#  Логика расчёта
# ──────────────────────────────────────────────

def parse_daily_spend(csv_data: str) -> Dict[int, float]:
    """Парсит CSV со статистикой в словарь {account_id: сумма_расхода}."""
    spend: Dict[int, float] = defaultdict(float)
    try:
        reader = csv.DictReader(io.StringIO(csv_data))
        for row in reader:
            aid = int(row.get("accountId", 0))
            cost = float(row.get("cost", 0))
            spend[aid] += cost
    except Exception:
        pass
    return spend


def parse_weekly_spend(csv_data: str) -> Dict[int, Dict[str, Any]]:
    """
    Парсит CSV с недельной статистикой.
    Возвращает {account_id: {"total": float, "active_days": int}}.
    """
    result: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {"total": 0.0, "active_days": 0}
    )
    try:
        reader = csv.DictReader(io.StringIO(csv_data))
        for row in reader:
            aid = int(row.get("accountId", 0))
            cost = float(row.get("cost", 0))
            result[aid]["total"] += cost
            if cost > 0:
                result[aid]["active_days"] += 1
    except Exception:
        pass
    return result


def classify_alert(runway: float) -> Tuple[str, str]:
    """Классифицирует запас дней: возвращает (эмодзи, текст_уровня)."""
    if runway <= 0:
        return "🔴", "ЗАКОНЧИЛИСЬ"
    elif runway < 1:
        return "🟠", "МЕНЕЕ 1 ДНЯ"
    elif runway < 3:
        return "🟡", "МЕНЕЕ 3 ДНЕЙ"
    else:
        return "🟢", "НОРМА"


# ──────────────────────────────────────────────
#  Главная логика
# ──────────────────────────────────────────────

def run_report(token: str, show_all: bool = False) -> List[Dict[str, Any]]:
    """
    Основной цикл: обходит всех USER-пользователей, собирает балансы
    и статистику, возвращает список алертов.
    """
    # 1. Определяем мастер-пользователя
    master = api_get("/user", token)
    if "error" in master:
        print(f"❌ Ошибка авторизации: {master.get('error')}", file=sys.stderr)
        print(f"   Проверьте токен в config.json", file=sys.stderr)
        sys.exit(1)

    master_id = master.get("response", {}).get("id")
    if not master_id:
        print("❌ Не удалось получить ID мастер-пользователя.", file=sys.stderr)
        sys.exit(1)

    # 2. Получаем список суб-пользователей
    users_resp = api_get("/users", token, master_id)
    users = users_resp.get("response", {}).get("users", [])

    if not users:
        print("❌ Список пользователей пуст или недоступен.", file=sys.stderr)
        sys.exit(1)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%d.%m.%Y")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    all_alerts: List[Dict[str, Any]] = []

    for user in users:
        uid = user.get("id")
        utype = user.get("type", "")
        desc = user.get("description") or user.get("login") or f"user_{uid}"

        if utype != "USER":
            continue  # Пропускаем MASTER-пользователей

        # 3. Аккаунты пользователя
        acc_resp = api_get("/accounts", token, uid)
        if "error" in acc_resp:
            continue
        accounts = acc_resp.get("response", {}).get("accounts", [])

        # 4. Плательщики
        payers_resp = api_get("/users/payers", token, uid)
        payers: List[Dict] = []
        if isinstance(payers_resp, list):
            payers = payers_resp
        elif "response" in payers_resp:
            rp = payers_resp["response"]
            payers = rp.get("payers", []) if isinstance(rp, dict) else (rp if isinstance(rp, list) else [])

        # Суммируем балансы всех плательщиков
        total_payer = sum(
            float(p.get("balance", 0)) for p in payers if isinstance(p, dict)
        )

        # Фильтруем: только активные кабинеты Яндекс.Директ
        direct_accounts = [
            a for a in accounts
            if a.get("service") == "DIRECT" and a.get("state") == "ACTIVE"
        ]
        if not direct_accounts:
            continue

        account_ids = [str(a["id"]) for a in direct_accounts]
        account_map = {a["id"]: a for a in direct_accounts}
        ids_str = ",".join(account_ids)

        # 5. Статистика: вчерашний день + неделя
        stats_yesterday = fetch_csv(
            f"{BASE_URL}/stat/v2?fields=date,accountId,cost&dateFrom={yesterday}&dateTo={yesterday}&accountIds={ids_str}",
            token, uid,
        )
        stats_week = fetch_csv(
            f"{BASE_URL}/stat/v2?fields=date,accountId,cost&dateFrom={week_ago}&dateTo={yesterday}&accountIds={ids_str}",
            token, uid,
        )

        yesterday_spend = parse_daily_spend(stats_yesterday)
        week_spend = parse_weekly_spend(stats_week)

        # 6. Рассчитываем прогноз для каждого кабинета
        for aid in account_map:
            acc = account_map[aid]
            acc_name = acc.get("name", f"Кабинет {aid}")
            acc_balance = float(acc.get("balance", 0))

            daily_spend = yesterday_spend.get(aid, 0)
            spend_source = "вчера"

            if daily_spend <= 0:
                ws = week_spend.get(aid, {"total": 0.0, "active_days": 0})
                if ws["active_days"] > 0:
                    daily_spend = ws["total"] / ws["active_days"]
                    spend_source = f"ср. за {ws['active_days']} дн."
                else:
                    daily_spend = 0

            total_available = acc_balance + total_payer
            runway = total_available / daily_spend if daily_spend > 0 else float("inf")

            level, label = classify_alert(runway)

            # Если не show_all — пропускаем зелёную зону
            if not show_all and level == "🟢":
                continue

            all_alerts.append({
                "user_desc": desc,
                "account_name": acc_name,
                "account_balance": acc_balance,
                "payer_balance": total_payer,
                "daily_spend": daily_spend,
                "total_available": total_available,
                "runway_days": runway,
                "level": level,
                "label": label,
                "spend_source": spend_source,
            })

    return all_alerts


def print_report(alerts: List[Dict[str, Any]], show_all: bool = False) -> None:
    """Выводит отчёт в читаемом текстовом формате."""
    today_str = datetime.now().strftime("%d.%m.%Y")

    if not alerts:
        print(f"✅ {today_str} — все бюджеты в порядке, запас 3+ дня по всем активным кабинетам Директ.")
        return

    # Сортируем: красные → оранжевые → жёлтые → зелёные
    level_order = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}
    alerts.sort(key=lambda a: (level_order.get(a["level"], 9), a["runway_days"]))

    print(f"📊 Бюджеты Директ — {today_str}")
    print("=" * 50)

    for a in alerts:
        print(f"\n{a['level']} {a['user_desc']} — {a['account_name']}")
        print(f"   Баланс кабинета:  {a['account_balance']:>12,.0f} ₽")
        print(f"   Баланс плательщика:{a['payer_balance']:>12,.0f} ₽")
        print(f"   Всего доступно:    {a['total_available']:>12,.0f} ₽")
        print(f"   Расход в день:     {a['daily_spend']:>12,.0f} ₽ ({a['spend_source']})")
        days_str = f"{a['runway_days']:.1f}" if a['runway_days'] >= 0.1 else "<0.1"
        print(f"   Прогноз:           {days_str:>12} дн. — {a['label']}")

    print("\n" + "=" * 50)

    # Подсчёт по уровням
    red = sum(1 for a in alerts if a["level"] == "🔴")
    orange = sum(1 for a in alerts if a["level"] == "🟠")
    yellow = sum(1 for a in alerts if a["level"] == "🟡")
    green = sum(1 for a in alerts if a["level"] == "🟢")

    parts = []
    if red:
        parts.append(f"🔴 {red} — закончились")
    if orange:
        parts.append(f"🟠 {orange} — менее 1 дня")
    if yellow:
        parts.append(f"🟡 {yellow} — менее 3 дней")
    if green:
        parts.append(f"🟢 {green} — норма")

    print(f"⚠️  Требуют пополнения: {red + orange + yellow} каб. ({', '.join(parts)})")


# ──────────────────────────────────────────────
#  Точка входа
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Мониторинг бюджетов Яндекс.Директ через Click.ru API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python daily_budget_report.py                    # только проблемные кабинеты
  python daily_budget_report.py --all              # все кабинеты
  python daily_budget_report.py -c /path/to/config.json
  CLICKRU_TOKEN=abc123 python daily_budget_report.py  # токен из переменной окружения
        """,
    )
    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="Путь к файлу конфигурации (по умолчанию: config.json)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Показать все кабинеты, включая с достаточным бюджетом (🟢)",
    )
    args = parser.parse_args()

    global BASE_URL
    token, BASE_URL = load_config(args.config)

    try:
        alerts = run_report(token, show_all=args.all)
        print_report(alerts, show_all=args.all)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
