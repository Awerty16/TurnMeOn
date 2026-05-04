import discord
from discord import app_commands
from discord.ui import Modal, TextInput
import json
import os
import asyncio
import paramiko
import socket
import struct
import urllib.request
from wakeonlan import send_magic_packet
from dotenv import load_dotenv

load_dotenv()

VERSION = "1.1.0"  # Update this with each commit

CONFIG_FILE = "config.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def check_password(entered):
    config = load_config()
    if not config:
        return False
    return entered == config.get("password")

def has_mod_role(interaction: discord.Interaction):
    config = load_config()
    if not config:
        return False
    mod_role = config.get("mod_role", "").strip().lower()
    if not mod_role:
        return False
    return any(r.name.lower() == mod_role for r in interaction.user.roles)

def ssh_run(command):
    config = load_config()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        config["pc_ip"],
        port=int(config["ssh_port"]),
        username=config["ssh_username"],
        password=config["ssh_password"],
        timeout=10
    )
    stdin, stdout, stderr = ssh.exec_command(command)
    output = stdout.read().decode().strip()
    ssh.close()
    return output

def is_server_running():
    try:
        output = ssh_run('tasklist /FI "IMAGENAME eq java.exe" /NH')
        return "java.exe" in output
    except Exception:
        return False

# ── RCON ─────────────────────────────────────────────────────────────────────

class RCONClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = int(port)
        self.password = password
        self.sock = None
        self._request_id = 1

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))
        self._send(3, self.password)  # 3 = SERVERDATA_AUTH

    def _send(self, pkt_type, data):
        req_id = self._request_id
        self._request_id += 1
        payload = data.encode("utf-8") + b"\x00\x00"
        header = struct.pack("<iii", len(payload) + 8, req_id, pkt_type)
        self.sock.sendall(header + payload)
        raw_len = self.sock.recv(4)
        if not raw_len:
            return None, None
        pkt_len = struct.unpack("<i", raw_len)[0]
        raw = b""
        while len(raw) < pkt_len:
            raw += self.sock.recv(pkt_len - len(raw))
        resp_id, resp_type = struct.unpack("<ii", raw[:8])
        resp_body = raw[8:-2].decode("utf-8")
        return resp_id, resp_body

    def command(self, cmd):
        resp_id, body = self._send(2, cmd)  # 2 = SERVERDATA_EXECCOMMAND
        return body

    def disconnect(self):
        if self.sock:
            self.sock.close()

def rcon_command(cmd):
    config = load_config()
    rcon = RCONClient(
        config["pc_ip"],
        config.get("rcon_port", 25575),
        config.get("rcon_password", "")
    )
    rcon.connect()
    result = rcon.command(cmd)
    rcon.disconnect()
    return result

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Setup flow ────────────────────────────────────────────────────────────────

# Each step: (config_key, label, placeholder, hint)
SETUP_STEPS = [
    ("pc_mac",           "PC MAC Address",        "A1:B2:C3:D4:E5:F6",                   "Found via `ipconfig /all` on your Windows PC"),
    ("pc_ip",            "PC Local IP Address",   "192.168.1.100",                        "Found via `ipconfig /all` — IPv4 Address"),
    ("ssh_username",     "SSH Username",          "Your Windows username",                "The username you log into Windows with"),
    ("ssh_password",     "SSH Password",          "Your Windows password",                "The password you log into Windows with"),
    ("ssh_port",         "SSH Port",              "22",                                   "Default is 22, leave as is if unsure"),
    ("bat_path",         ".bat File Path",        r"C:\minecraft-server\start.bat",       "Full path to your Minecraft server start.bat"),
    ("rcon_password",    "RCON Password",         "From your server.properties",          "Set enable-rcon=true in server.properties first"),
    ("mod_role",         "Mod Role Name",         "e.g. Mod or Admin",                   "The Discord role allowed to use /command"),
    ("boot_wait_seconds","PC Boot Time (seconds)","45",                                   "How long your PC takes to boot from hibernation"),
    ("password",         "Bot Admin Password",    "Pick a strong password",              "Used to protect config and update commands"),
]

# In-memory store for setup sessions: user_id -> partial config dict
setup_sessions = {}

def make_setup_embed(step_index, partial_config):
    step = SETUP_STEPS[step_index]
    total = len(SETUP_STEPS)
    filled = len(partial_config)
    bar = "█" * filled + "░" * (total - filled)

    embed = discord.Embed(
        title="⚙️ Bot Setup",
        description=f"**Step {step_index + 1} of {total}**\n`{bar}`",
        color=0x57F287
    )
    embed.add_field(name=f"Enter: {step[1]}", value=f"💡 {step[3]}", inline=False)

    if partial_config:
        summary = "\n".join(
            f"✅ **{SETUP_STEPS[i][1]}**: {'`' + ('*' * 8 if SETUP_STEPS[i][0] in ('ssh_password', 'password', 'rcon_password') else str(v)) + '`'}"
            for i, (k, v) in enumerate(partial_config.items())
        )
        embed.add_field(name="Saved so far", value=summary, inline=False)

    embed.set_footer(text="Click the button below to enter your value")
    return embed

class SetupStepModal(Modal):
    def __init__(self, step_index, message):
        step = SETUP_STEPS[step_index]
        super().__init__(title=f"Step {step_index + 1}/{len(SETUP_STEPS)} — {step[1]}")
        self.step_index = step_index
        self.message = message
        self.value_input = TextInput(
            label=step[1],
            placeholder=step[2],
            default="22" if step[0] == "ssh_port" else ("45" if step[0] == "boot_wait_seconds" else discord.utils.MISSING)
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id not in setup_sessions:
            setup_sessions[user_id] = {}

        key = SETUP_STEPS[self.step_index][0]
        value = self.value_input.value.strip()

        # Normalise specific fields
        if key == "pc_mac":
            value = value.replace("-", ":").upper()
        if key == "boot_wait_seconds":
            try:
                value = int(value)
            except ValueError:
                value = 45

        setup_sessions[user_id][key] = value
        next_step = self.step_index + 1

        if next_step >= len(SETUP_STEPS):
            # All done — save config
            config = {**setup_sessions[user_id], "rcon_port": 25575}
            save_config(config)
            del setup_sessions[user_id]
            embed = discord.Embed(
                title="✅ Setup Complete!",
                description="All settings saved. Your bot is ready to use!\n\nRun `/start` to wake your PC or `/status` to check the connection.",
                color=0x57F287
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Show next step
            embed = make_setup_embed(next_step, setup_sessions[user_id])
            view = SetupStepView(next_step, self.message)
            await interaction.response.edit_message(embed=embed, view=view)


class SetupStepView(discord.ui.View):
    def __init__(self, step_index, message=None):
        super().__init__(timeout=300)
        self.step_index = step_index
        self.message = message
        step_name = SETUP_STEPS[step_index][1]
        btn = discord.ui.Button(label=f"Enter {step_name}", style=discord.ButtonStyle.primary, emoji="✏️")
        btn.callback = self.button_callback
        self.add_item(btn)

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupStepModal(self.step_index, self.message))

    async def on_timeout(self):
        if self.message:
            try:
                embed = discord.Embed(title="⏰ Setup Timed Out", description="Run `/setup` to start again.", color=0xFF0000)
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass


class UpdateFieldModal(Modal):
    def __init__(self, title, field_label, field_key, placeholder=""):
        super().__init__(title=title)
        self.field_key = field_key
        self.field_input = TextInput(label=field_label, placeholder=placeholder)
        self.add_item(self.field_input)
        self.pw_input = TextInput(label="Bot Password", placeholder="Enter the bot password")
        self.add_item(self.pw_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        config = load_config()
        config[self.field_key] = self.field_input.value
        save_config(config)
        await interaction.response.send_message("✅ Updated successfully.", ephemeral=True)


class UpdateSSHModal(Modal, title="Update SSH Credentials"):
    ssh_user = TextInput(label="SSH Username", placeholder="Your Windows username")
    ssh_pass = TextInput(label="SSH Password", placeholder="Your Windows password")
    ssh_port = TextInput(label="SSH Port", placeholder="22", default="22")
    pw_input = TextInput(label="Bot Password", placeholder="Enter the bot password")

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        config = load_config()
        config["ssh_username"] = self.ssh_user.value
        config["ssh_password"] = self.ssh_pass.value
        config["ssh_port"] = self.ssh_port.value or "22"
        save_config(config)
        await interaction.response.send_message("✅ SSH credentials updated.", ephemeral=True)


class ConfigPasswordModal(Modal, title="View Config"):
    pw_input = TextInput(label="Password", placeholder="Enter the bot password")

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        config = load_config()
        msg = (
            "🔧 **Current Config**\n"
            f"```\n"
            f"PC MAC:        {config.get('pc_mac', 'not set')}\n"
            f"PC IP:         {config.get('pc_ip', 'not set')}\n"
            f"SSH User:      {config.get('ssh_username', 'not set')}\n"
            f"SSH Password:  {'*' * len(config.get('ssh_password', ''))}\n"
            f"SSH Port:      {config.get('ssh_port', '22')}\n"
            f".bat Path:     {config.get('bat_path', 'not set')}\n"
            f"Boot Wait:     {config.get('boot_wait_seconds', 45)}s\n"
            f"RCON Port:     {config.get('rcon_port', 25575)}\n"
            f"RCON Password: {'*' * len(config.get('rcon_password', ''))}\n"
            f"Mod Role:      {config.get('mod_role', 'not set')}\n"
            f"```"
        )
        await interaction.response.send_message(msg, ephemeral=True)


class UpdateBotModal(Modal, title="Update Bot"):
    pw_input = TextInput(label="Password", placeholder="Enter the bot password")

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        await interaction.response.send_message("⏳ Pulling latest update from GitHub...", ephemeral=True)
        try:
            url = "https://raw.githubusercontent.com/Awerty16/TurnMeOn/main/bot.py"
            urllib.request.urlretrieve(url, "bot.py")
            await interaction.edit_original_response(content="✅ Updated! Restarting bot...")
            os.system("sudo systemctl restart minecraftbot")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Update failed: {e}")


class RunCommandModal(Modal, title="Run Server Command"):
    command = TextInput(label="Command", placeholder="e.g. say Hello! or op Steve")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ Running command...", ephemeral=True)
        try:
            result = rcon_command(self.command.value)
            response = result if result else "✅ Command sent (no output returned)"
            await interaction.edit_original_response(content=f"```\n{response}\n```")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ RCON error: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────

@tree.command(name="setup", description="Configure the bot for the first time")
async def setup(interaction: discord.Interaction):
    setup_sessions[interaction.user.id] = {}
    embed = make_setup_embed(0, {})
    view = SetupStepView(0)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


@tree.command(name="start", description="Wake the PC and start the Minecraft server")
async def start(interaction: discord.Interaction):
    config = load_config()
    if not config:
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Sending Wake-on-LAN packet...")

    try:
        send_magic_packet(config["pc_mac"])
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Failed to send WoL packet: {e}")
        return

    await interaction.edit_original_response(content=f"📡 WoL packet sent! Waiting {config['boot_wait_seconds']}s for PC to boot...")
    await asyncio.sleep(config["boot_wait_seconds"])

    for attempt in range(1, 4):
        try:
            bat = config["bat_path"]
            bat_dir = "\\".join(bat.split("\\")[:-1])
            ssh_run(f'cmd /c "cd /d "{bat_dir}" && start /B "" "{bat}""')
            await interaction.edit_original_response(content="✅ **Server is starting!** Give it a minute to load.")
            return
        except Exception as e:
            if attempt < 3:
                await interaction.edit_original_response(
                    content=f"⏳ PC not ready yet, retrying... (attempt {attempt}/3)"
                )
                await asyncio.sleep(15)
            else:
                await interaction.edit_original_response(
                    content=f"❌ Couldn't connect after 3 attempts. PC may still be booting — try `/start` again.\n`{e}`"
                )


@tree.command(name="stop", description="Gracefully stop the Minecraft server and hibernate the PC")
async def stop(interaction: discord.Interaction):
    config = load_config()
    if not config:
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Stopping server gracefully...")

    try:
        rcon_command("stop")
        await interaction.edit_original_response(content="⏳ Stop command sent, waiting for server to shut down...")
        await asyncio.sleep(10)
        ssh_run("shutdown /h")
        await interaction.edit_original_response(content="✅ Server stopped and world saved. PC is hibernating.")
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error: {e}")


@tree.command(name="status", description="Check if the Minecraft server is running")
async def status(interaction: discord.Interaction):
    config = load_config()
    if not config:
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Checking server status...")

    try:
        running = is_server_running()
        if running:
            await interaction.edit_original_response(content="🟢 **Server is running!**")
        else:
            await interaction.edit_original_response(content="🔴 **Server is not running.**")
    except Exception as e:
        await interaction.edit_original_response(
            content=f"🔴 **Could not reach PC.** It may be hibernating.\n`{e}`"
        )


@tree.command(name="command", description="Run a command on the Minecraft server (mods only)")
async def command(interaction: discord.Interaction):
    if not load_config():
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return
    if not has_mod_role(interaction):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.send_modal(RunCommandModal())


@tree.command(name="config", description="View current bot configuration")
async def config_cmd(interaction: discord.Interaction):
    if not load_config():
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.send_modal(ConfigPasswordModal())


@tree.command(name="setmac", description="Update the PC MAC address")
async def setmac(interaction: discord.Interaction):
    await interaction.response.send_modal(
        UpdateFieldModal("Update MAC Address", "PC MAC Address", "pc_mac", "A1:B2:C3:D4:E5:F6")
    )

@tree.command(name="setip", description="Update the PC local IP address")
async def setip(interaction: discord.Interaction):
    await interaction.response.send_modal(
        UpdateFieldModal("Update IP Address", "PC Local IP Address", "pc_ip", "192.168.1.100")
    )

@tree.command(name="setbat", description="Update the .bat file path")
async def setbat(interaction: discord.Interaction):
    await interaction.response.send_modal(
        UpdateFieldModal("Update .bat Path", ".bat File Path", "bat_path", r"C:\minecraft-server\start.bat")
    )

@tree.command(name="setboot", description="Update the PC boot wait time")
async def setboot(interaction: discord.Interaction):
    await interaction.response.send_modal(
        UpdateFieldModal("Update Boot Wait Time", "Boot Wait (seconds)", "boot_wait_seconds", "45")
    )

@tree.command(name="setssh", description="Update SSH credentials")
async def setssh(interaction: discord.Interaction):
    await interaction.response.send_modal(UpdateSSHModal())

@tree.command(name="setmod", description="Update the mod role name")
async def setmod(interaction: discord.Interaction):
    await interaction.response.send_modal(
        UpdateFieldModal("Update Mod Role", "Mod Role Name", "mod_role")
    )

@tree.command(name="reset", description="Reset all bot configuration")
async def reset(interaction: discord.Interaction):
    if not load_config():
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.send_modal(ResetModal())


class ResetModal(Modal, title="Reset Bot Configuration"):
    pw_input = TextInput(label="Bot Password", placeholder="Enter the bot password to confirm reset")

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        os.remove(CONFIG_FILE)
        await interaction.response.send_message(
            "🗑️ **Config has been reset.** Run `/setup` to configure the bot again.",
            ephemeral=True
        )

@tree.command(name="update", description="Pull the latest bot.py from GitHub and restart")
async def update(interaction: discord.Interaction):
    if not load_config():
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.send_modal(UpdateBotModal())


@tree.command(name="botstatus", description="Show bot version and uptime")
async def botstatus(interaction: discord.Interaction):
    now = discord.utils.utcnow()
    delta = now - bot_start_time
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime = f"{hours}h {minutes}m {seconds}s"

    await interaction.response.send_message(
        f"🤖 **Bot Status**\n"
        f"```\n"
        f"Version:  {VERSION}\n"
        f"Status:   Online ✅\n"
        f"Uptime:   {uptime}\n"
        f"```",
        ephemeral=False
    )


# ── Events ────────────────────────────────────────────────────────────────────

bot_start_time = discord.utils.utcnow()

@client.event
async def on_ready():
    global bot_start_time
    bot_start_time = discord.utils.utcnow()
    await tree.sync()
    print(f"✅ Bot is online as {client.user}")
    print(f"   Version: {VERSION}")
    print(f"   Config file: {'found ✓' if load_config() else 'not found — run /setup'}")

# ── Run ───────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("❌ No DISCORD_TOKEN found. Create a .env file or set the environment variable.")
    exit(1)

client.run(TOKEN)
