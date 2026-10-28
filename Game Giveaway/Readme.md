# SSE Game Giveaway Bot

A Discord giveaway bot built with Python and `discord.py`. It supports multiple games, reusable message templates, scheduled or manually started giveaways, player-ID validation, duplicate-ID reports, moderator review, giveaway history, and Discord-channel-based storage.

> **Storage note:** The bot stores its records in messages inside a private `SSE giveaway` category in each authorized Discord server. Do not delete or edit bot-generated record messages manually unless you understand the effect on stored data. Keep backups of important records.

## Features

- **Server allowlist:** only Discord servers whose IDs appear in `server.txt` can use the giveaway commands. `/serverinfo` remains available so a server owner can check their server ID and authorization status.
- **Setup and permissions:** creates the storage category and channels, with manager role access configured through the bot.
- **Game registration:** configure whether a game requires a player ID, accepted currencies, and minimum/maximum ID length.
- **Templates:** save a template from a Discord message link or message ID. The bot checks template variables against its supported variable list before saving.
- **Flexible launches:** select a game, template, prize pool, number of winners, duration, destination channel, automatic/manual start mode, optional custom giveaway name, and embed/plain-message send type.
- **Player IDs:** participants can save or update their ID for a registered game. The bot checks ID length and duplicate IDs.
- **Duplicate-ID reports:** users can submit a report with proof; moderators can review reports and approve or reject them.
- **Giveaway controls:** edit, end, or cancel giveaways; browse giveaway history; view results and winner details.
- **Recovery:** the bot periodically checks stored giveaway records and can process giveaways that passed their end time while the bot was offline.
- **Logs and status:** records giveaway details in the storage channels and shows an online-status timestamp in active giveaway posts. If the bot is offline, it cannot update its own Discord messages; the timestamp will become stale.

## Requirements

- Python 3.11 or a compatible Python version supported by your hosting provider
- A Discord application and bot token
- The bot invited to your server with appropriate permissions
- Message Content and Server Members intents enabled in the Discord Developer Portal
- Python packages:
  - `discord.py==2.7.1`
  - `python-dotenv==1.2.4`

## Installation

1. Put `bot.py`, `requirements.txt`, `.env`, and `server.txt` in the same folder.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create `.env` and add your bot token:

   ```dotenv
   DISCORD_TOKEN=put_your_bot_token_here
   ```

   Keep the token private. Do not post `.env` publicly or commit it to a public repository.

4. Create `server.txt`. Put one authorized Discord server ID on each line:

   ```text
   1234578972
   1342672672
   # Lines beginning with # are ignored
   ```

   Use the actual server (guild) IDs. These example IDs are placeholders. Enable Developer Mode in Discord, then use **Copy Server ID** to obtain the correct value.

5. Run the bot:

   ```bash
   python bot.py
   ```

   On a hosting panel, set the startup file to `bot.py` and install dependencies from `requirements.txt`.

## First-time Discord setup

1. Run `/serverinfo` to copy the server ID and confirm whether the server is allowlisted.
2. Add the server ID to `server.txt` and restart the bot if needed.
3. Run `/setup` to create the private storage category and channels.
4. Run `/configure_roles` to configure manager roles and, optionally, a blacklist role.
5. Register each game with `/create_game`. Set whether an in-game ID is required, its minimum and maximum length, and the accepted currencies.
6. Post a template message in a channel the bot can read, then run `/add_template` with its message link or ID, game, and template title.
7. Run `/launch_giveaway` to configure and post a giveaway.

## Slash commands

The following commands are registered by the current `bot.py`. Discord may take a short time to display newly synced commands.

| Command | Purpose |
|---|---|
| `/serverinfo` | Show the current server ID and whether it is allowed to use the bot. Available even in non-allowlisted servers. |
| `/setup` | Create the hidden giveaway storage category and channels. |
| `/reset_storage` | Delete the giveaway storage category and its channels. **Owner-only destructive action; use carefully.** |
| `/configure_roles` | Set up to two manager roles and an optional blacklist role. |
| `/create_game` | Register a game, ID requirement, currencies, and ID length limits. |
| `/edit_game` | Edit a registered game's ID requirement, ID length limits, and currencies. |
| `/delete_game` | Delete a registered game and its saved templates and player IDs. |
| `/add_template` | Save a template from an existing Discord message. |
| `/edit_template` | Replace an existing saved template from a source message. |
| `/delete_template` | Delete a saved template. |
| `/launch_giveaway` | Launch an embed or plain-message giveaway in a selected channel, with automatic or manual start. Supports an optional custom name. |
| `/edit_giveaway` | Change the prize pool, winner count, or duration for a running/scheduled giveaway. |
| `/end_giveaway` | End an active giveaway and draw winners immediately. |
| `/delete_giveaway` | Cancel an active giveaway and disable joining. |
| `/show_giveaways` | Browse scheduled, active, completed, and cancelled giveaways. |
| `/edit_id` | Save or update your in-game ID for a selected game. |
| `/delete_player_id` | Moderator command to remove a user's saved ID for a game. |
| `/report_duplicate_id` | Submit a duplicate-ID report with proof for moderator review. |

Access is subject to the bot's server allowlist and the permissions enforced by each command. Administrator defaults shown by Discord are not a replacement for the bot's own authorization checks.

## Supported template variables

Use these exact names in a template:

| Variable | Replaced with |
|---|---|
| `{giveaway_name}` | Custom giveaway name, if supplied. |
| `{game_name}` | Registered game name. |
| `{total_prize}` | Total prize pool entered when launching. |
| `{prize_per_person}` | Prize pool divided by the configured number of winners. |
| `{currency}` | Selected game's currency used for the giveaway. |
| `{winners}` | Number of winners. |
| `{end_time}` | Giveaway end time. |
| `{joined}` | Current participant count. |

Example template:

```text
🎉 {giveaway_name}
🎮 Game: {game_name}

🏆 Total Prize: {total_prize} {currency}
🎁 Prize Per Winner: {prize_per_person} {currency}
👥 Winners: {winners}
⏰ Ends: {end_time}

📋 How to Participate
- Click the Join Giveaway button.
- Submit your in-game ID if required.
- Only one entry per player is allowed.

👥 Players Joined: {joined}
🍀 Good luck, everyone!
```

`{giveaway_name}` is optional as a launch option, but the variable itself can be used in templates. Use only the variable names supported by the bot; unknown variables are rejected when adding a template.

## Storage channels

`/setup` creates the `SSE giveaway` category and these channels:

- `#giveaway-games` — registered games and their settings
- `#giveaway-templates` — saved templates and template guidance
- `#player-ids` — saved user-to-game ID records
- `#giveaway-configs` — server configuration and giveaway state records
- `#giveaway-logs` — readable giveaway log entries
- `#giveaway-reports` — duplicate-ID reports and moderator review controls

The bot needs permission to view and send messages in these channels, read message history, manage messages where required, create/use threads where supported by the configured giveaway workflow, and send messages in the chosen giveaway channel. Role hierarchy and channel overrides can affect whether actions succeed.

## Hosting and troubleshooting

- **Missing token:** ensure `.env` is beside `bot.py` and contains `DISCORD_TOKEN=...`.
- **Server not allowed:** copy the server ID from `/serverinfo`, add it to `server.txt`, and restart the process if the allowlist is only loaded at startup or changes are not being picked up.
- **Slash commands missing:** check that the bot was invited with the `applications.commands` scope, that it can connect to Discord, and that the startup log says slash commands synced.
- **Storage errors:** check channel permissions and do not rename/delete the bot's storage channels while the bot is running.
- **Timeouts/recovery warnings:** transient Discord API or hosting network timeouts can occur. Check hosting logs and resource usage. Recovery helps process overdue giveaways after a restart, but it cannot keep the bot online if the hosting process is stopped.
- **Online indicator:** a bot cannot edit a Discord post while offline. A stale last-check timestamp is therefore expected during downtime.
- **Reset warning:** `/reset_storage` deletes the storage category and its channels. This can erase the bot's stored games, templates, IDs, configuration, and giveaway records. Use it only when you intend to reset the system.

## Security

- Never share your bot token or commit `.env` to a repository.
- Keep `server.txt` under the bot owner's control.
- Limit access to the storage category because it contains user IDs and giveaway records.
- Back up important storage messages before destructive resets or manual cleanup.
- Review permissions before granting manager roles.

## License

**No explicit software license is declared in the current source file, and no `LICENSE` file was provided with it.** Therefore, the project should not be described as MIT, Apache-2.0, GPL, or another open-source license unless the copyright holder deliberately chooses and adds one.

Until a license is added, default copyright rules generally apply: other people do not automatically receive permission to redistribute, modify, or reuse the code just because they can access it. If you own the code and want others to reuse it, choose a license (for example, MIT for a permissive open-source license) and add the corresponding full `LICENSE` file. Do not label it MIT without making that choice.

## Credits

- Python
- [`discord.py`](https://discordpy.readthedocs.io/)
- [`python-dotenv`](https://github.com/theskumar/python-dotenv)

This README describes the current bot source and may need updating as features and commands change.
