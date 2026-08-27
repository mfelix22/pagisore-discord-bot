import asyncio
from aiohttp import web
from datetime import datetime

client = None
supabase = None
EO_PARTY_CHANNEL_ID = None
PAGISORE_BOT_API_SECRET = None
_web_runner = None

MAX_MESSAGE_LENGTH = 2000
TIME_GROUPS = ["pagi", "sore", "malam"]
GROUP_ICONS = {"pagi": "🌅", "sore": "🌇", "malam": "🌙"}


def init(client_, supabase_, channel_id, api_secret):
    global client, supabase, EO_PARTY_CHANNEL_ID, PAGISORE_BOT_API_SECRET
    client = client_
    supabase = supabase_
    EO_PARTY_CHANNEL_ID = channel_id
    PAGISORE_BOT_API_SECRET = api_secret


def get_member_name(members, member_id):
    for m in members:
        if m["id"] == member_id:
            return m["ign"]
    return "Unknown"


def format_event_date(date_str):
    if not date_str:
        return "No date"
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%d %B %Y")


def build_party_line(party, party_members, members):
    slots = sorted(
        [m for m in party_members if m["eo_party_id"] == party["id"]],
        key=lambda x: x["slot_number"],
    )
    names = []
    for slot in slots:
        name = get_member_name(members, slot["member_id"])
        prefix = "👑 " if slot["slot_number"] == 1 else ""
        names.append(f"{prefix}{name}")
    return f"**Party {party['party_number']}** — {', '.join(names)}"


def build_eo_message_sections(event, time_groups, parties, party_members, members):
    sections = [
        "**⚔️ EMPERIUM OVERRUN**",
        "",
        f"**{format_event_date(event.get('event_date'))}**",
        "",
    ]
    for group in TIME_GROUPS:
        group_parties = [p for p in parties if p["time_group"] == group]
        if not group_parties:
            continue
        header = f"**{GROUP_ICONS[group]} TEAM {group.upper()}**"
        apply = next(
            (
                t
                for t in time_groups
                if t["time_group"] == group and t.get("apply_to_member_id")
            ),
            None,
        )
        if apply:
            target_name = get_member_name(members, apply["apply_to_member_id"])
            header += f" — APPLY {target_name}"
        sections.append(header)
        for party in group_parties:
            sections.append(build_party_line(party, party_members, members))
    return sections


def split_messages(sections, max_length=MAX_MESSAGE_LENGTH):
    messages = []
    current = []
    for section in sections:
        if not current:
            current = [section]
            continue
        if len("\n".join(current + [section])) > max_length:
            messages.append("\n".join(current))
            current = [section]
        else:
            current.append(section)
    if current:
        messages.append("\n".join(current))
    return messages


async def send_eo_message(channel, event, time_groups, parties, party_members, members):
    sections = build_eo_message_sections(event, time_groups, parties, party_members, members)
    messages = split_messages(sections)
    for text in messages:
        await channel.send(text)


async def handle_publish_request(request):
    auth = request.headers.get("Authorization", "")
    if not PAGISORE_BOT_API_SECRET or not auth.startswith("Bearer "):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401)
    token = auth.replace("Bearer ", "").strip()
    if token != PAGISORE_BOT_API_SECRET:
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
        event_id = body.get("eventId")
        if not event_id:
            return web.json_response({"success": False, "error": "Missing eventId"}, status=400)
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    try:
        event_response = (
            supabase.table("events")
            .select("id,name,event_date")
            .eq("id", event_id)
            .eq("name", "Emperium Overrun")
            .limit(1)
            .execute()
        )
    except Exception as e:
        return web.json_response({"success": False, "error": f"Failed to load event: {str(e)}"}, status=500)

    if not event_response.data:
        return web.json_response({"success": False, "error": "EO event not found"}, status=404)

    event = event_response.data[0]

    try:
        time_groups = (
            supabase.table("eo_time_groups")
            .select("time_group,apply_to_member_id")
            .eq("event_id", event_id)
            .execute()
            .data
            or []
        )
        parties = (
            supabase.table("eo_parties")
            .select("id,time_group,party_number,raid_leader_member_id")
            .eq("event_id", event_id)
            .order("party_number")
            .execute()
            .data
            or []
        )
        party_ids = [p["id"] for p in parties]
        party_members = []
        if party_ids:
            party_members = (
                supabase.table("eo_party_members")
                .select("eo_party_id,member_id,slot_number")
                .in_("eo_party_id", party_ids)
                .order("slot_number")
                .execute()
                .data
                or []
            )
        all_members = supabase.table("members").select("id,ign").execute().data or []
    except Exception as e:
        print(f"Supabase error while loading EO data: {e}")
        return web.json_response({"success": False, "error": f"Failed to load EO data: {str(e)}"}, status=500)

    if not EO_PARTY_CHANNEL_ID:
        return web.json_response({"success": False, "error": "EO_PARTY_CHANNEL_ID not configured"}, status=500)

    await client.wait_until_ready()
    channel = client.get_channel(int(EO_PARTY_CHANNEL_ID))
    if not channel:
        return web.json_response({"success": False, "error": "Discord channel not found"}, status=500)

    try:
        await send_eo_message(channel, event, time_groups, parties, party_members, all_members)
    except Exception as e:
        print(f"Error sending EO message: {e}")
        return web.json_response({"success": False, "error": f"Discord send failed: {str(e)}"}, status=502)

    return web.json_response({"success": True, "message": "EO parties published successfully"})


async def start_web_server():
    global _web_runner

    app = web.Application()
    app.router.add_post("/api/eo/publish", handle_publish_request)
    _web_runner = web.AppRunner(app)
    await _web_runner.setup()
    site = web.TCPSite(_web_runner, "0.0.0.0", 8000)
    await site.start()
    print("PAGISORE bot HTTP server started on http://0.0.0.0:8000")

    try:
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        await _web_runner.cleanup()


async def stop_web_server():
    if _web_runner:
        await _web_runner.cleanup()
