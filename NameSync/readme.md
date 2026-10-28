# SSE NameSync

**SSE NameSync** is a Discord bot built with Python and `discord.py` that automatically updates server members' nicknames according to role-based nickname formats. It stores its configuration in a private Discord text channel and includes tools for previewing changes and diagnosing skipped or failed nickname updates.

## Features

- Configure nickname formats for up to 10 roles in one command.
- Automatically update members' nicknames according to their configured roles.
- When a member has multiple configured roles, use the format for their highest-positioned configured role.
- Store role formats and the experimental logging setting in a private `#nickname-formats` channel; no separate database is required.
- Preview changes without editing nicknames using dry-run diagnostics.
- Test a single member's proposed nickname without changing it.
- Enable optional private diagnostic logs with reason totals and examples of successes, failures, and skips.
- Check setup and bot permissions, list saved formats, and remove formats.
- Keep legacy `/add_roles` and `/remove` command aliases.

## Requirements

- Python 3.10 or newer recommended.
- A Discord application and bot account.
- `discord.py` 2.x and `python-dotenv`.
- Permission to invite and configure a bot in your Discord server.

## Installation

### 1. Create and invite the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. Open **Bot** and create the bot if needed. Copy its token and keep it secret.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent**.
4. In **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes.
5. Select the permissions described in [Required permissions](#required-permissions), then use the generated URL to invite the bot.

### 2. Install dependencies

Create `requirements.txt` beside `bot.py`:

```txt
discord.py>=2.7.1,<3.0.0
python-dotenv>=1.0.0,<2.0.0
```

Install the packages:

```bash
python -m pip install -r requirements.txt
```

### 3. Configure the token

Create a `.env` file in the same folder as `bot.py`:

```env
DISCORD_TOKEN=put_your_bot_token_here
```

Replace the example value with your actual bot token. The code loads `.env` from the directory containing `bot.py` and stops with an error if `DISCORD_TOKEN` is missing.

**Never publish your token** in source code, screenshots, or a public repository. Add `.env` to `.gitignore`.

### 4. Run the bot

```bash
python bot.py
```

On successful startup, the console prints the logged-in bot account and the installed `discord.py` version. Slash commands are synced at startup.

## First-time setup

1. Invite SSE NameSync to your server.
2. Confirm **Server Members Intent** is enabled in both the Developer Portal and the code.
3. Run `/setup`. The bot creates the private `#nickname-formats` channel if it does not already exist and reports setup checks.
4. Move the bot's highest role **above the roles whose members it needs to rename**.
5. Run `/add_format` to configure a format for one or more roles.
6. Run `/diagnose_transform` to preview likely changes.
7. If the preview looks right, run `/transform`.

## Nickname format syntax

Every format must contain the `{user}` placeholder. The bot replaces it with the member's global display name when available, otherwise their Discord username.

| Format | Example result |
|---|---|
| `[Mod] {user}` | `[Mod] ExampleUser` |
| `SSE • {user}` | `SSE • ExampleUser` |
| `★ {user}` | `★ ExampleUser` |

Discord server nicknames are limited to **32 characters**. The format is validated using `ExampleUser` as a sample name. If a generated nickname for a particular member exceeds the limit, that member is skipped and can be identified in diagnostics.

If a member has multiple roles with saved formats, the format belonging to their **highest-positioned configured role** is selected.

## Slash commands

The commands below are server-only and use Discord's default command permission settings. Server administrators can further adjust them in **Server Settings → Integrations**. These defaults are not a custom in-code allowlist for specific roles.

| Command | Purpose |
|---|---|
| `/setup` | Create/check the private storage channel and show setup checks. |
| `/add_format` | Save a format for one or more roles; supports up to 10 role parameters. |
| `/add_roles` | Legacy alias that saves a format for one role. |
| `/view_formats` | List all saved role formats. |
| `/remove_format` | Remove one role's format, or all formats with `remove_all:true`. |
| `/remove` | Legacy format-removal alias. |
| `/transform` | Scan server members and apply saved formats. Posts a summary in the current channel. |
| `/experimental` | Enable or disable detailed transform logs using the `enabled` boolean. |
| `/diagnose_transform` | Dry-run a transform and report likely updates, skips, and reasons without changing nicknames. |
| `/test_member` | Inspect one member's proposed nickname without changing it. |
| `/bot_status` | Check the bot member, nickname permission, top role, storage channel, and saved-format count. |

### Example workflow

1. `/add_format name_format:"[Mod] {user}" role_1:@Moderators`
2. `/view_formats`
3. `/diagnose_transform`
4. `/transform`

To see detailed diagnostics after a transform, run `/experimental enabled:true`. To turn them off, run `/experimental enabled:false`.

## Understanding `/transform`

The public result message includes:

- **Updated** — nicknames successfully changed.
- **Failed** — attempted changes that failed due to Discord/API errors or another exception.
- **Skipped** — members intentionally not changed, for example bots, members without a configured role, members whose nickname already matches, the server owner, members at or above the bot's top role, or members whose generated nickname is too long.
- **Members scanned** and **Formats loaded**.

When experimental logs are enabled, the person who ran the command also receives a private diagnostic embed with reason totals and up to 12 sample entries per category. Reason totals cover all scanned members; examples are capped to keep messages manageable.

`/diagnose_transform` and `/test_member` are dry-run tools. Their permission checks are estimates; Discord's response during a real nickname edit is authoritative.

## Required permissions

SSE NameSync generally needs:

- **Manage Nicknames** — to edit members' server nicknames.
- **Manage Channels** — to create the storage channel during setup.
- **View Channels**, **Send Messages**, and **Read Message History** for the storage channel.
- **Send Messages** and **Embed Links** in the channel where `/transform` runs, so the public summary can be posted.
- Server member access through the enabled **Server Members Intent**.

The bot cannot change the server owner's nickname. It also cannot change members whose highest role is equal to or higher than the bot's highest role. Position the bot's role appropriately while following your server's security practices.

In the code, `/setup` uses Discord's `Manage Server` default permission. Format-management and transform commands use the `Manage Roles` default permission. These are Discord command defaults, not a custom role allowlist enforced by the bot itself.

## Data storage

The bot uses a private text channel named `#nickname-formats` as its storage:

- Each role format is stored as a message in the form `ROLE_ID | nickname format`.
- The experimental setting is stored in a message beginning with `BOT_SETTING | experimental |`.
- Replacing a role format deletes the previous stored message for that role.
- `/remove_format` deletes the selected format or all saved format messages.

The channel is created with `@everyone` denied access and the bot granted access. Review its permission overwrites after setup and grant access only to trusted administrators who need it. Anyone who can read or modify the stored messages may be able to affect the bot's configuration.

Do not delete the storage channel unless you intend to recreate it and configure the formats again. Keep a secure backup of your role-format configuration if it is important to your server.

## Hosting

You can run SSE NameSync on a computer, a VPS, or a Python-compatible bot-hosting service.

For a hosting panel:

1. Upload `bot.py` and `requirements.txt`.
2. Add `DISCORD_TOKEN` as an environment variable in the host's settings, or provide `.env` securely if supported.
3. Install dependencies with `pip install -r requirements.txt` if the host does not do so automatically.
4. Set the startup command to `python bot.py` (or the host's equivalent).
5. Start the service and inspect the console for the login and slash-command sync messages.

If the token is exposed, reset it immediately in the Discord Developer Portal.

## Troubleshooting

### `DISCORD_TOKEN is missing`

- Ensure `.env` is beside `bot.py`, or set `DISCORD_TOKEN` in the hosting panel.
- Check that the variable name is spelled exactly as shown.
- Use the bot token, not the application ID or public key.

### Slash commands do not appear

- Confirm the bot was invited with the `applications.commands` scope.
- Check the console for `Slash commands synced and ready!`.
- Wait briefly, refresh Discord, and confirm you are using the correct server and application.

### Nicknames fail to update

- Enable Server Members Intent in the Developer Portal.
- Grant the bot **Manage Nicknames**.
- Move the bot's highest role above target members' highest roles.
- Remember that the server owner cannot be renamed by the bot.
- Use `/bot_status`, `/diagnose_transform`, and `/test_member` to investigate.

### The bot cannot create `#nickname-formats`

- Grant **Manage Channels** and ensure the bot has access to the server.
- Confirm it can resolve server roles and its own server member.
- Run `/setup` again and read the returned error.
- Check channel limits and server permission overrides.

### The public transform summary is not posted

- Grant **Send Messages** and **Embed Links** in the channel where `/transform` is run.
- If posting is denied, the bot attempts to show the result privately instead.

### Members are skipped

This can be expected. A member may be a bot, have no configured role, already have the target nickname, be the server owner, be at or above the bot in the role hierarchy, or have a generated nickname longer than 32 characters. Run `/experimental enabled:true` before `/transform` to see reason totals and examples.

## Security notes

- Treat the bot token like a password.
- Keep `.env` out of version control.
- Keep the storage channel private and restrict who can edit its messages.
- Grant only the permissions the bot needs.
- Review role hierarchy and command permissions before transforming a large server.
- Run `/diagnose_transform` before applying new formats.

## License

The supplied `bot.py` does not specify a license. Unless you add a license file or explicit license notice, others should not assume they have permission to copy, modify, redistribute, or publish the code. Choose a license that reflects your intentions and include it in the repository as `LICENSE`.

## Contributing

Test modifications in a private test server before using them in a production server. Never commit secrets, and update this README when adding commands or configuration options.

## Disclaimer

This project is provided as-is. Discord API behavior, permissions, and hosting environments may change. Test nickname changes before using the bot on a large community.
