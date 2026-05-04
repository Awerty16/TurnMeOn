import discord
from discord import app_commands
from discord.ui import Modal, TextInput
import json
import os
import asyncio
import paramiko
from wakeonlan import send_magic_packet

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

def ssh_run(command):
    config = load_config()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        config["pc_ip"],
        port=int(config["ssh_port"]),
        username=config["ssh_username"],
        password=config["ssh_password"],
        timeout=10
    )
    stdin, stdout, stderr = client.exec_command(command)
    output = stdout.read().decode().strip()
    client.close()
    return output

def is_server_running():
    try:
        output = ssh_run('tasklist /FI "IMAGENAME eq java.exe" /NH')
        return "java.exe" in output
    except Exception:
        return False

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Modals ────────────────────────────────────────────────────────────────────

class SetupModal1(Modal, title="Setup (1/2) — Network & Password"):
    mac = TextInput(label="PC MAC Address", placeholder="A1:B2:C3:D4:E5:F6")
    ip = TextInput(label="PC Local IP Address", placeholder="192.168.1.100")
    ssh_user = TextInput(label="SSH Username", placeholder="Your Windows username")
    ssh_pass = TextInput(label="SSH Password", placeholder="Your Windows password")
    password = TextInput(label="Set Bot Password", placeholder="Pick a password for managing this bot")

    async def on_submit(self, interaction: discord.Interaction):
        # Store part 1 data temporarily and show modal 2
        self._part1 = {
            "pc_mac": self.mac.value.replace("-", ":").upper(),
            "pc_ip": self.ip.value,
            "ssh_username": self.ssh_user.value,
            "ssh_password": self.ssh_pass.value,
            "ssh_port": "22",
            "password": self.password.value
        }
        modal2 = SetupModal2(self._part1)
        await interaction.response.send_modal(modal2)

class SetupModal2(Modal, title="Setup (2/2) — Server"):
    bat = TextInput(label=".bat File Path", placeholder=r"C:\minecraft-server\start.bat")
    boot_wait = TextInput(label="PC Boot Time (seconds)", placeholder="45", default="45")
    ssh_port = TextInput(label="SSH Port", placeholder="22", default="22")

    def __init__(self, part1_data):
        super().__init__()
        self.part1_data = part1_data

    async def on_submit(self, interaction: discord.Interaction):
        config = {
            **self.part1_data,
            "bat_path": self.bat.value,
            "boot_wait_seconds": int(self.boot_wait.value or 45),
            "ssh_port": self.ssh_port.value or "22"
        }
        save_config(config)
        await interaction.response.send_message(
            "✅ **Setup complete!** Bot is ready to use.\n"
            "Run `/status` to test the connection, or `/start` to wake your PC.",
            ephemeral=True
        )

class PasswordModal(Modal, title="Enter Password"):
    password = TextInput(label="Password", placeholder="Enter the bot password")

    def __init__(self, action_callback):
        super().__init__()
        self.action_callback = action_callback

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.password.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        await self.action_callback(interaction)

class UpdateFieldModal(Modal):
    def __init__(self, title, field_label, field_key, placeholder=""):
        super().__init__(title=title)
        self.field_key = field_key
        self.field_input = TextInput(label=field_label, placeholder=placeholder)
        self.add_item(self.field_input)
        self.pw_input = TextInput(label="Password", placeholder="Enter the bot password")
        self.add_item(self.pw_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        config = load_config()
        config[self.field_key] = self.field_input.value
        save_config(config)
        await interaction.response.send_message(
            f"✅ **{self.field_key}** updated successfully.",
            ephemeral=True
        )

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
            f"PC MAC:       {config.get('pc_mac', 'not set')}\n"
            f"PC IP:        {config.get('pc_ip', 'not set')}\n"
            f"SSH User:     {config.get('ssh_username', 'not set')}\n"
            f"SSH Password: {'*' * len(config.get('ssh_password', ''))}\n"
            f"SSH Port:     {config.get('ssh_port', '22')}\n"
            f".bat Path:    {config.get('bat_path', 'not set')}\n"
            f"Boot Wait:    {config.get('boot_wait_seconds', 45)}s\n"
            f"```"
        )
        await interaction.response.send_message(msg, ephemeral=True)

# ── Commands ──────────────────────────────────────────────────────────────────

@tree.command(name="setup", description="Configure the bot for the first time")
async def setup(interaction: discord.Interaction):
    await interaction.response.send_modal(SetupModal1())


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

    # Try SSH a few times in case boot is slow
    for attempt in range(1, 4):
        try:
            bat = config["bat_path"]
            # Get the directory the .bat file lives in
            bat_dir = "\\".join(bat.split("\\")[:-1])
            # Run via cmd so 'start' works correctly
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


@tree.command(name="stop", description="Stop the Minecraft server and hibernate the PC")
async def stop(interaction: discord.Interaction):
    config = load_config()
    if not config:
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Stopping server...")

    try:
        # Stop the Minecraft server gracefully via its console, then hibernate
        ssh_run('taskkill /F /IM java.exe')
        await asyncio.sleep(3)
        ssh_run('shutdown /h')
        await interaction.edit_original_response(content="✅ Server stopped and PC is hibernating.")
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


@tree.command(name="update", description="Pull the latest bot.py from GitHub and restart")
async def update(interaction: discord.Interaction):
    if not check_password:
        await interaction.response.send_message("⚠️ Bot not configured yet. Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.send_modal(UpdateBotModal())

class UpdateBotModal(Modal, title="Update Bot"):
    pw_input = TextInput(label="Password", placeholder="Enter the bot password")

    async def on_submit(self, interaction: discord.Interaction):
        if not check_password(self.pw_input.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return

        await interaction.response.send_message("⏳ Pulling latest update from GitHub...", ephemeral=True)

        try:
            import urllib.request
            url = "https://raw.githubusercontent.com/Awerty16/TurnMeOn/main/bot.py"
            urllib.request.urlretrieve(url, "bot.py")
            await interaction.edit_original_response(content="✅ Updated! Restarting bot...")
            # Restart the systemd service
            os.system("sudo systemctl restart minecraftbot")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Update failed: {e}")


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot is online as {client.user}")
    print(f"   Config file: {'found ✓' if load_config() else 'not found — run /setup'}")

# ── Run ───────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("❌ No DISCORD_TOKEN found. Create a .env file or set the environment variable.")
    exit(1)

client.run(TOKEN)
