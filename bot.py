import discord
from discord.ext import commands, tasks
import json
import os
import random
import string
import requests
from datetime import datetime, timedelta

TOKEN = os.getenv("DISCORD_TOKEN")
FORM_URL = "https://docs.google.com/forms/d/e/YOUR_FORM_ID/formResponse"  # Replace with your formResponse URL

CONFIG_FILE = "user_config.json"
LOG_FILE = "hydration_logs.json"

intents = discord.Intents.default()
intents.messages = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ Helpers ------------------

def generate_account_number():
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=16))

def load_json(file):
    if not os.path.exists(file):
        return {}
    with open(file, 'r') as f:
        return json.load(f)

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=2)

def get_display_name(user):
    return user.global_name or user.name

def log_hydration_locally(user_id, data):
    logs = load_json(LOG_FILE)
    logs.setdefault(user_id, []).append(data)
    save_json(LOG_FILE, logs)

def convert_to_ml(oz):
    return round(oz * 29.5735, 2)

def convert_to_g(oz):
    return round(oz * 28.3495, 2)

def get_unit_label(metric, metric_unit, imperial_unit):
    return metric_unit if metric else imperial_unit

# ------------------ Bot Events ------------------

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} - Ready to hydrate!")
    hydration_check_loop.start()
    weekly_report_loop.start()

# ------------------ Commands ------------------

@bot.command()
async def setup(ctx):
    user = ctx.author
    config = load_json(CONFIG_FILE)
    uid = str(user.id)

    if uid in config:
        await ctx.send("You're already set up.")
        return

    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    await ctx.send("Would you like to use metric (ml/g) or imperial (oz)? Type `metric` or `imperial`.")
    try:
        msg = await bot.wait_for('message', check=check, timeout=60)
        unit_pref = msg.content.strip().lower()
        if unit_pref not in ["metric", "imperial"]:
            raise ValueError("Invalid unit")
    except Exception:
        await ctx.send("Setup failed: invalid or missing unit preference.")
        return

    account_number = generate_account_number()
    config[uid] = {
        "username": f"{user.name}#{user.discriminator}",
        "account_number": account_number,
        "checkin_time": "20:00",
        "form_type": "hydration",
        "goal_liters": 2.5,
        "unit": unit_pref,
        "last_checkin": None
    }
    save_json(CONFIG_FILE, config)
    await ctx.send(f"Setup complete, {get_display_name(user)}. Using `{unit_pref}` units. Your account number is `{account_number}`.")

@bot.command()
async def hydrate(ctx):
    await log_hydration(ctx.author)

# ------------------ Core Logging Logic ------------------

async def log_hydration(user):
    config = load_json(CONFIG_FILE)
    uid = str(user.id)

    if uid not in config:
        await user.send("Please run `!setup` first.")
        return

    unit_pref = config[uid].get("unit", "metric")
    is_metric = unit_pref == "metric"

    dm = await user.create_dm()
    await dm.send(f"Hi {get_display_name(user)}! Let's log your hydration for today. 💧")

    def check(m): return m.author == user and m.channel == dm

    try:
        await dm.send(f"How much water did you drink today (in {get_unit_label(is_metric, 'ml', 'oz')})?")
        water = await bot.wait_for('message', check=check, timeout=120)

        await dm.send(f"How much sugary drink ({get_unit_label(is_metric, 'ml', 'oz')})?")
        sugar = await bot.wait_for('message', check=check, timeout=120)

        await dm.send("Caffeine intake (mg)?")
        caffeine = await bot.wait_for('message', check=check, timeout=120)

        await dm.send(f"Any hydrating foods (in {get_unit_label(is_metric, 'grams', 'oz')})?")
        foods = await bot.wait_for('message', check=check, timeout=120)

        await dm.send("Notes or how you feel today?")
        notes = await bot.wait_for('message', check=check, timeout=180)

        water_ml = float(water.content) if is_metric else convert_to_ml(float(water.content))
        sugar_ml = float(sugar.content) if is_metric else convert_to_ml(float(sugar.content))
        foods_g = float(foods.content) if is_metric else convert_to_g(float(foods.content))

        form_data = {
            "entry.111111": uid,
            "entry.111112": config[uid]["username"],
            "entry.777777": config[uid]["account_number"],
            "entry.222222": str(water_ml),
            "entry.333333": str(sugar_ml),
            "entry.444444": caffeine.content,
            "entry.555555": str(foods_g),
            "entry.666666": notes.content
        }

        local_log = {
            "timestamp": datetime.utcnow().isoformat(),
            "water": water_ml,
            "sugar": sugar_ml,
            "caffeine": float(caffeine.content),
            "foods": foods_g,
            "notes": notes.content
        }

        requests.post(FORM_URL, data=form_data)
        config[uid]["last_checkin"] = datetime.utcnow().isoformat()
        save_json(CONFIG_FILE, config)
        log_hydration_locally(uid, local_log)

        await dm.send("✅ Your hydration log was submitted successfully!")

    except Exception as e:
        await dm.send(f"⚠️ Something went wrong: {e}")

# ------------------ Analytics / Reports ------------------

@tasks.loop(hours=24)
async def weekly_report_loop():
    now = datetime.utcnow()
    if now.weekday() != 6:
        return

    config = load_json(CONFIG_FILE)
    logs = load_json(LOG_FILE)

    for uid, entries in logs.items():
        user = await bot.fetch_user(int(uid))
        recent_logs = [entry for entry in entries if datetime.fromisoformat(entry['timestamp']) >= now - timedelta(days=7)]

        if not recent_logs:
            continue

        total_water = sum(e['water'] for e in recent_logs)
        avg_caffeine = sum(e['caffeine'] for e in recent_logs) / len(recent_logs)
        days_logged = len(recent_logs)

        goal = config[uid].get("goal_liters", 2.5) * 1000 * 7
        hydration_pct = min(100, round((total_water / goal) * 100, 1))

        summary = (
            f"📊 Weekly Hydration Summary for {get_display_name(user)}\n"
            f"Days Logged: {days_logged}/7\n"
            f"Total Water Intake: {int(total_water)} ml\n"
            f"Avg Caffeine: {round(avg_caffeine, 1)} mg\n"
            f"Goal Completion: {hydration_pct}%\n"
        )
        await user.send(summary)

# ------------------ Background Tasks ------------------

@tasks.loop(minutes=1)
async def hydration_check_loop():
    config = load_json(CONFIG_FILE)
    now = datetime.utcnow()

    for uid, user_data in config.items():
        checkin_time = user_data.get("checkin_time", "20:00")
        hour, minute = map(int, checkin_time.split(":"))

        try:
            user = await bot.fetch_user(int(uid))
            last_checkin = user_data.get("last_checkin")
            today_str = now.strftime('%Y-%m-%d')

            if (last_checkin is None or not last_checkin.startswith(today_str)) and \
               now.hour == hour and now.minute == minute:
                await log_hydration(user)
        except Exception as e:
            print(f"[ERROR] Could not checkin user {uid}: {e}")

# ------------------ Bot Entry ------------------

if __name__ == '__main__':
    bot.run(TOKEN)