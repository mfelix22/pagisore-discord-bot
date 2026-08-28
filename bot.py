import asyncio
import os
from datetime import date, datetime, time, timedelta, timezone

import discord
import publish_api
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
ATTENDANCE_CHANNEL_ID = os.getenv("ATTENDANCE_CHANNEL_ID")
EO_PARTY_CHANNEL_ID = os.getenv("EO_PARTY_CHANNEL_ID")
PAGISORE_BOT_API_SECRET = os.getenv("PAGISORE_BOT_API_SECRET")

if not TOKEN:
    raise ValueError("DISCORD_TOKEN is missing from .env")
if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL is missing from .env")
if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY is missing from .env")
if not GUILD_ID:
    raise ValueError("DISCORD_GUILD_ID is missing from .env")
if not EO_PARTY_CHANNEL_ID:
    print("Warning: EO_PARTY_CHANNEL_ID not set. Discord publishing will not work.")
if not PAGISORE_BOT_API_SECRET:
    print("Warning: PAGISORE_BOT_API_SECRET not set. Discord publishing will not work.")

guild = discord.Object(id=int(GUILD_ID))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
# Required to fetch all guild members for /syncdiscord.
# Also enable "Server Members Intent" in the Discord bot developer portal.
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

publish_api.init(client, supabase, EO_PARTY_CHANNEL_ID, PAGISORE_BOT_API_SECRET)

WIB = timezone(timedelta(hours=7))
EVENT_TIME = time(23, 59, tzinfo=WIB)  # temporary test time, set back to time(23, 59) later
# EVENT_TIME = time(19, 55, tzinfo=WIB)
CLOSE_OFFSET = timedelta(minutes=1)  # temporary test, set back to timedelta(hours=3)(minutes=1) later
CRON_UTC = time(23, 0, tzinfo=timezone.utc)  # 06:00 WIB


def is_officer(user):
    return user.guild_permissions.administrator or user.guild_permissions.manage_guild


def to_utc(dt_wib):
    return dt_wib.astimezone(timezone.utc)


_supabase_lock = asyncio.Lock()


async def aexecute(query):
    async with _supabase_lock:
        return await asyncio.to_thread(query.execute)


def parse_iso_to_wib(value):
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(WIB)


def format_wib_date(d):
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%d %B %Y")


def format_wib_time(t):
    if isinstance(t, str):
        t = datetime.strptime(t, "%H:%M:%S").time()
    return t.strftime("%H:%M")


def format_wib_datetime(dt):
    return dt.strftime("%d %B %Y %H:%M")


def build_attendance_embed(event, attendance_rows):
    event_name = event.get("name", "Event")
    event_date = event.get("event_date")
    event_time = event.get("event_time")
    close_at = event.get("attendance_close_at")

    title_map = {
        "Guild League": "⚔️ Guild League ⚔️",
        "Emperium Overrun": "⚔️ Emperium Overrun ⚔️",
    }
    title = title_map.get(event_name, f"⚔️ {event_name} ⚔️")

    close_wib = parse_iso_to_wib(close_at) if close_at else None
    close_text = format_wib_time(close_wib.time()) if close_wib else "?"

    hadir = [r for r in attendance_rows if r["status"] == "hadir"]
    tidak = [r for r in attendance_rows if r["status"] == "tidak_hadir"]
    tentative = [r for r in attendance_rows if r["status"] == "tentative"]

    embed = discord.Embed(
        title=title,
        description=(
            f"📅 {format_wib_date(event_date)}\n"
            f"⏰ {format_wib_time(event_time)} WIB\n"
            f"⏳ Attendance closes: {close_text} WIB\n\n"
            "Pilih status kehadiran di bawah ini."
        ),
        color=discord.Color.gold(),
    )

    COL_WIDTH = 22

    def names(rows, show_reason=False):
        if not rows:
            return f"{'—': <{COL_WIDTH}}"
        lines = []
        for r in rows:
            name = r["discord_username"] or "Unknown"
            reason = r.get("reason")
            if show_reason and reason:
                line = f"> {name} — *{reason}*"
            else:
                line = f"> {name}"
            lines.append(line.ljust(COL_WIDTH))
        return "\n".join(lines)

    embed.add_field(name=f"✅ Hadir ({len(hadir)})", value=names(hadir), inline=True)
    embed.add_field(name=f"🤔 Tentative ({len(tentative)})", value=names(tentative), inline=True)
    embed.add_field(name=f"❌ Tidak Hadir ({len(tidak)})", value=names(tidak, show_reason=True), inline=True)

    return embed


def event_name_for_today():
    weekday = datetime.now(WIB).weekday()
    if weekday in (1, 3):  # Tuesday and Thursday
        return "Guild League"
    if weekday == 6:  # Sunday
        return "Emperium Overrun"
    return None


class AttendanceButton(discord.ui.Button):
    def __init__(self, event_id: int, status: str, label: str, style: discord.ButtonStyle):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"pagisore_attendance_{status}_{event_id}"
        )
        self.event_id = event_id
        self.status = status

    async def callback(self, interaction: discord.Interaction):
        if self.status == "tidak_hadir":
            await interaction.response.send_modal(
                DeclineReasonModal(
                    event_id=self.event_id,
                    message=interaction.message,
                )
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            now_wib = datetime.now(WIB)

            event_response = await aexecute(
                supabase
                .table("events")
                .select("attendance_close_at")
                .eq("id", self.event_id)
            )

            if not event_response.data:
                await interaction.followup.send(
                    "❌ Could not find this event.",
                    ephemeral=True
                )
                return

            close_wib = parse_iso_to_wib(event_response.data[0]["attendance_close_at"])

            if close_wib and now_wib >= close_wib:
                await interaction.followup.send(
                    "⏰ Attendance for this event is already closed.",
                    ephemeral=True
                )
                return

            discord_id = str(interaction.user.id)
            username = interaction.user.display_name

            await aexecute(
                supabase.table("event_attendance").upsert({
                    "event_id": self.event_id,
                    "discord_user_id": discord_id,
                    "discord_username": username,
                    "member_id": None,
                    "status": self.status,
                    "responded_at": to_utc(datetime.now(WIB)).isoformat(),
                }, on_conflict="event_id,discord_user_id")
            )

            try:
                event_response = await aexecute(
                    supabase
                    .table("events")
                    .select("name,event_date,event_time,attendance_close_at")
                    .eq("id", self.event_id)
                    .limit(1)
                )
                if event_response.data:
                    event = event_response.data[0]
                    attendance_response = await aexecute(
                        supabase
                        .table("event_attendance")
                        .select("discord_username,status,reason")
                        .eq("event_id", self.event_id)
                    )
                    embed = build_attendance_embed(event, attendance_response.data or [])
                    if interaction.message:
                        await interaction.message.edit(embed=embed)
            except Exception as error:
                print(f"Error updating attendance embed: {error}")

            status_display = {
                "hadir": "Hadir",
                "tidak_hadir": "Tidak Hadir",
                "tentative": "Tentative",
            }.get(self.status, self.status)
            await interaction.followup.send(
                f"✅ Your attendance has been recorded as {status_display}.",
                ephemeral=True
            )

        except Exception as error:
            print(f"Supabase error in attendance button: {error}")
            await interaction.followup.send(
                "❌ Could not save your attendance. Please try again.",
                ephemeral=True
            )


class AttendanceView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.add_item(AttendanceButton(event_id, "hadir", "✅ Hadir", discord.ButtonStyle.green))
        self.add_item(AttendanceButton(event_id, "tentative", "🤔 Tentative", discord.ButtonStyle.gray))
        self.add_item(AttendanceButton(event_id, "tidak_hadir", "❌ Tidak Hadir", discord.ButtonStyle.red))


class DeclineReasonModal(discord.ui.Modal, title="Alasan tidak hadir"):
    reason = discord.ui.TextInput(
        label="Kenapa kamu tidak bisa hadir?",
        style=discord.TextStyle.short,
        max_length=100,
        required=False,
        placeholder="contoh: kerja/berhalangan",
    )

    def __init__(self, event_id: int, message: discord.Message):
        super().__init__()
        self.event_id = event_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            now_wib = datetime.now(WIB)

            event_response = await aexecute(
                supabase
                .table("events")
                .select("attendance_close_at")
                .eq("id", self.event_id)
            )

            if not event_response.data:
                await interaction.followup.send(
                    "❌ Could not find this event.",
                    ephemeral=True
                )
                return

            close_wib = parse_iso_to_wib(event_response.data[0]["attendance_close_at"])

            if close_wib and now_wib >= close_wib:
                await interaction.followup.send(
                    "⏰ Attendance for this event is already closed.",
                    ephemeral=True
                )
                return

            discord_id = str(interaction.user.id)
            username = interaction.user.display_name
            reason = self.reason.value or None

            await aexecute(
                supabase.table("event_attendance").upsert({
                    "event_id": self.event_id,
                    "discord_user_id": discord_id,
                    "discord_username": username,
                    "member_id": None,
                    "status": "tidak_hadir",
                    "reason": reason,
                    "responded_at": to_utc(datetime.now(WIB)).isoformat(),
                }, on_conflict="event_id,discord_user_id")
            )

            event_response = await aexecute(
                supabase
                .table("events")
                .select("name,event_date,event_time,attendance_close_at")
                .eq("id", self.event_id)
                .limit(1)
            )
            if event_response.data:
                event = event_response.data[0]
                attendance_response = await aexecute(
                    supabase
                    .table("event_attendance")
                    .select("discord_username,status,reason")
                    .eq("event_id", self.event_id)
                )
                embed = build_attendance_embed(event, attendance_response.data or [])
                if self.message:
                    await self.message.edit(embed=embed)

            await interaction.followup.send(
                "✅ Kamu tercatat tidak hadir.",
                ephemeral=True
            )
        except Exception as error:
            print(f"Supabase error in decline modal: {error}")
            await interaction.followup.send(
                "❌ Could not save your attendance. Please try again.",
                ephemeral=True
            )


async def create_attendance_post(channel, event_name):
    now_wib = datetime.now(WIB)
    event_date_wib = now_wib.date()
    event_dt_wib = datetime.combine(event_date_wib, EVENT_TIME, tzinfo=WIB)
    close_wib = event_dt_wib - CLOSE_OFFSET

    if now_wib >= close_wib:
        return "closed"

    existing = await aexecute(
        supabase
        .table("events")
        .select("id")
        .eq("name", event_name)
        .eq("event_date", event_date_wib.isoformat())
    )

    if existing.data:
        return "exists"

    event_data = {
        "name": event_name,
        "event_type": "attendance",
        "event_date": event_date_wib.isoformat(),
        "event_time": EVENT_TIME.replace(tzinfo=None).isoformat(),
        "attendance_open_at": to_utc(now_wib).isoformat(),
        "attendance_close_at": to_utc(close_wib).isoformat(),
        "created_at": to_utc(now_wib).isoformat(),
    }

    insert_response = await aexecute(supabase.table("events").insert(event_data))
    event_row = insert_response.data[0]
    event_id = event_row["id"]

    view = AttendanceView(event_id)
    client.add_view(view)

    embed = build_attendance_embed(event_row, [])

    attendance_channel = f"<#{ATTENDANCE_CHANNEL_ID}>" if ATTENDANCE_CHANNEL_ID else "absensi-guild-event"
    if event_name == "Emperium Overrun":
        content = (
            "hi ges (IZIIIIIN @everyone ) ini list party untuk EO malam ini yak,\n"
            f"kalau tidak bisa ikut bisa kabarin di {attendance_channel}\n"
            "NOTE: masih ada kemungkinan berubah ya please tetep cek ingame sebelum mulai EO nanti\n\n"
            "terimakasih banyak"
        )
    else:
        content = (
            "hi ges (IZIIIIIN @everyone ) ini list party untuk GL malam ini yak,\n"
            f"kalau tidak bisa ikut bisa kabarin di {attendance_channel}\n"
            "NOTE: masih ada kemungkinan berubah ya please tetep cek ingame sebelum mulai GL nanti\n\n"
            "terimakasih banyak"
        )

    await channel.send(
        content,
        allowed_mentions=discord.AllowedMentions(everyone=True),
        embed=embed,
        view=view
    )

    close_time_text = format_wib_time(close_wib.time())
    event_time_text = format_wib_time(EVENT_TIME)

    return {
        "event_id": event_id,
        "event_date_text": format_wib_date(event_date_wib),
        "event_time_text": event_time_text,
        "close_time_text": close_time_text,
    }


@tree.command(
    name="ping",
    description="Check if the PAGISORE bot is online"
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏓 Pong! PAGISORE Bot is online."
    )


@tree.command(
    name="members",
    description="Show PAGISORE guild members"
)
async def members(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        response = (
            supabase
            .table("members")
            .select("ign")
            .order("ign")
            .execute()
        )
        rows = response.data

        if not rows:
            await interaction.followup.send("No members found.")
            return

        lines = [
            f"**PAGISORE Members ({len(rows)})**",
            ""
        ]

        for index, row in enumerate(rows, start=1):
            lines.append(f"{index}. {row['ign']}")

        await interaction.followup.send("\n".join(lines))

    except Exception as error:
        print(f"Supabase error in /members: {error}")
        await interaction.followup.send(
            "❌ Could not load members from Supabase."
        )


@tree.command(
    name="attendance",
    description="Create a PAGISORE attendance post"
)
@app_commands.choices(event_name=[
    app_commands.Choice(name="Guild League", value="Guild League"),
    app_commands.Choice(name="Emperium Overrun", value="Emperium Overrun"),
])
@app_commands.describe(event_name="Name of the event")
async def attendance(interaction: discord.Interaction, event_name: str):
    if not is_officer(interaction.user):
        await interaction.response.send_message(
            "❌ You need Manage Server or Administrator permission.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        result = await create_attendance_post(interaction.channel, event_name)

        if result == "closed":
            await interaction.followup.send(
                "❌ Attendance for this event would already be closed.",
                ephemeral=True
            )
            return

        if result == "exists":
            await interaction.followup.send(
                "❌ This event has already been posted today.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Attendance post created.\n\n"
            # f"Event ID: {result['event_id']}\n"
            f"Event: {event_name}\n"
            f"Date: {result['event_date_text']}\n"
            f"Time: {result['event_time_text']} WIB\n"
            f"Closes: {result['close_time_text']} WIB",
            ephemeral=True
        )

    except Exception as error:
        print(f"Supabase error in /attendance: {error}")
        await interaction.followup.send(
            "❌ Could not create the attendance post.",
            ephemeral=True
        )


@tree.command(
    name="attendance_list",
    description="Show attendance for the latest event"
)
@app_commands.choices(event_name=[
    app_commands.Choice(name="Guild League", value="Guild League"),
    app_commands.Choice(name="Emperium Overrun", value="Emperium Overrun"),
])
@app_commands.describe(event_name="Name of the event")
async def attendance_list(interaction: discord.Interaction, event_name: str):
    if not is_officer(interaction.user):
        await interaction.response.send_message(
            "❌ You need Manage Server or Administrator permission.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        event_response = (
            supabase
            .table("events")
            .select("id,name,event_date,event_time,attendance_close_at")
            .eq("name", event_name)
            .order("event_date", desc=True)
            .limit(1)
            .execute()
        )

        if not event_response.data:
            await interaction.followup.send(
                "❌ Event not found.",
                ephemeral=True
            )
            return

        event = event_response.data[0]
        event_id = event["id"]
        now_wib = datetime.now(WIB)
        close_wib = parse_iso_to_wib(event["attendance_close_at"])

        if close_wib and now_wib >= close_wib:
            status_text = "Status: Closed"
        else:
            close_time_text = format_wib_time(close_wib.time()) if close_wib else "?"
            status_text = f"Status: Open until {close_time_text} WIB"

        attendance_response = (
            supabase
            .table("event_attendance")
            .select("discord_username,status")
            .eq("event_id", event_id)
            .execute()
        )

        rows = attendance_response.data

        hadir = [row for row in rows if row["status"] == "hadir"]
        tidak = [row for row in rows if row["status"] == "tidak_hadir"]
        tentative = [row for row in rows if row["status"] == "tentative"]

        lines = [
            f"📋 {event['name']} Attendance",
            f"Event ID: {event_id}",
            f"Date: {format_wib_date(event['event_date'])}",
            f"Time: {format_wib_time(event['event_time'])} WIB",
            status_text,
            ""
        ]

        lines.append(f"✅ Hadir ({len(hadir)})")
        for row in hadir:
            username = row["discord_username"] or "Unknown"
            lines.append(f"- {username}")

        lines.append("")
        lines.append(f"🤔 Tentative ({len(tentative)})")
        for row in tentative:
            username = row["discord_username"] or "Unknown"
            lines.append(f"- {username}")

        lines.append("")
        lines.append(f"❌ Tidak Hadir ({len(tidak)})")
        for row in tidak:
            username = row["discord_username"] or "Unknown"
            lines.append(f"- {username}")

        lines.append("")
        lines.append(f"Total responses: {len(rows)}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    except Exception as error:
        print(f"Supabase error in /attendance_list: {error}")
        await interaction.followup.send(
            "❌ Could not load attendance list.",
            ephemeral=True
        )


@tree.command(
    name="syncdiscord",
    description="Sync Discord user IDs to member IGNs (officer only)"
)
@app_commands.describe(dry_run="Show matches without updating")
async def syncdiscord(interaction: discord.Interaction, dry_run: bool = False):
    if not is_officer(interaction.user):
        await interaction.response.send_message(
            "❌ You need Manage Server or Administrator permission.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        guild_obj = client.get_guild(int(GUILD_ID))
        if not guild_obj:
            guild_obj = await client.fetch_guild(int(GUILD_ID))
    except Exception as error:
        await interaction.followup.send(
            f"❌ Could not find Discord guild: {error}",
            ephemeral=True
        )
        return

    discord_members = []
    try:
        async for m in guild_obj.fetch_members(limit=None):
            discord_members.append(m)
    except Exception as error:
        await interaction.followup.send(
            f"❌ Could not fetch Discord members. Make sure Server Members Intent is enabled: {error}",
            ephemeral=True
        )
        return

    try:
        response = await aexecute(
            supabase.table("members").select("id, ign, discord_id")
        )
        db_members = response.data or []
    except Exception as error:
        await interaction.followup.send(
            f"❌ Could not load members from Supabase: {error}",
            ephemeral=True
        )
        return

    db_by_ign = {m["ign"].strip().lower(): m for m in db_members}

    matched = []
    not_found = []
    skipped = []
    failed = []

    for d in discord_members:
        name = (d.nick or d.display_name or "").strip()
        if not name:
            continue

        dbm = db_by_ign.get(name.lower())
        if not dbm:
            not_found.append(name)
            continue

        if dbm.get("discord_id") == str(d.id):
            skipped.append(name)
            continue

        matched.append({"ign": dbm["ign"], "discord_id": str(d.id)})

        if dry_run:
            continue

        try:
            await aexecute(
                supabase
                .table("members")
                .update({"discord_id": str(d.id)})
                .eq("id", dbm["id"])
            )
        except Exception as error:
            print(f"Failed to update {dbm['ign']}: {error}")
            failed.append(name)

    summary = (
        f"{'🔍 Dry run — no changes made' if dry_run else '✅ Discord IDs synced'}.\n"
        f"Matched/updated: {len(matched) - (0 if dry_run else len(failed))}\n"
        f"Not found in dashboard: {len(not_found)}\n"
        f"Already up to date: {len(skipped)}"
    )
    if not dry_run and failed:
        summary += f"\nFailed to update: {len(failed)}"

    # Limit output to avoid Discord message cap
    if not_found:
        preview = ", ".join(not_found[:10])
        if len(not_found) > 10:
            preview += f", +{len(not_found) - 10} more"
        summary += f"\nNot found examples: {preview}"

    await interaction.followup.send(summary, ephemeral=True)


@tasks.loop(time=CRON_UTC)
async def auto_attendance():
    if not ATTENDANCE_CHANNEL_ID:
        print("ATTENDANCE_CHANNEL_ID not set. Skipping auto attendance.")
        return

    event_name = event_name_for_today()
    if not event_name:
        return

    channel = client.get_channel(int(ATTENDANCE_CHANNEL_ID))
    if not channel:
        print("Auto attendance channel not found")
        return

    try:
        result = await create_attendance_post(channel, event_name)

        if result == "closed":
            print(f"Auto attendance skipped for {event_name}: already closed")
        elif result == "exists":
            print(f"Auto attendance skipped for {event_name}: already posted")
        else:
            print(f"Auto attendance posted: {event_name} (ID {result['event_id']})")

    except Exception as error:
        print(f"Auto attendance error: {error}")


@auto_attendance.before_loop
async def before_auto_attendance():
    await client.wait_until_ready()


@client.event
async def on_ready():
    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)

    try:
        events_response = await aexecute(
            supabase
            .table("events")
            .select("id")
            .limit(200)
        )
        for event in events_response.data:
            client.add_view(AttendanceView(event["id"]))
    except Exception as error:
        print(f"Error loading persistent attendance views: {error}")

    if not auto_attendance.is_running():
        auto_attendance.start()

    print(f"PAGISORE bot logged in as {client.user}")
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")




async def main():
    try:
        await asyncio.gather(client.start(TOKEN), publish_api.start_web_server())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            await publish_api.stop_web_server()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
