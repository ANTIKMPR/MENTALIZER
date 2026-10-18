<img width="1457" height="346" alt="mentalizer_logo" src="https://github.com/user-attachments/assets/ea9b593b-c67c-4eec-bc52-cf76b36b9802" />

# MENTAL HEALTH, PHYSICAL HEALTH AND SOCIAL BATTERY IN TG BIO

Im too lazy to make readme so... yeah, theres some screenshots of mentalizer

<img width="1568" height="888" alt="card_sheet" src="https://github.com/user-attachments/assets/456254ac-3219-4516-baa8-b9e05e69b940" />

<img width="1520" height="1024" alt="anim_sheet" src="https://github.com/user-attachments/assets/846d643a-cf97-4881-ba5c-a80294b82fd1" />

## what it does

pops up once an hour and asks how you're doing. you move three sliders, hit apply, and your telegram bio updates automatically to something like this:

```
mental : ■■■■□
physical : ■■■■■
social battery : ■□□□□
```

sits in the system tray and stays out of your way. if you're gaming it waits until you're done (reads your steam library automatically, no setup needed).

## setup

1. `pip install pillow numpy telethon pystray python-socks[asyncio]`
2. get `api_id` and `api_hash` at [my.telegram.org](https://my.telegram.org) → API development tools
3. put them in `config.json` (created on first run)
4. `python bio_checkin.py --login` — one-time telegram login
5. `python bio_checkin.py` — runs in tray, shows menu on click

to build an exe: put `bio_checkin.py`, `mentalizer_logo.png` and `mentalizer.ico` in one folder and run `build.bat`

## config

| key | default | what it does |
|-----|---------|--------------|
| `interval_minutes` | `60` | how often the overlay appears |
| `idle_minutes` | `0` | also trigger after N minutes of no input (0 = off) |
| `scale` | `1.0` | window size (try `0.75` for compact) |
| `font` | `""` | custom font filename from `fonts/` folder |
| `position` | `"bottom-right"` | where the overlay appears |
| `game_processes` | `[]` | non-steam games to detect by process name |
| `fullscreen_busy` | `false` | treat any fullscreen app as a game |
| `menu_on_start` | `false` | open the main menu on launch |

## console commands

```
check_health   show the overlay right now
menu           open the main menu
game           check if a game is detected (and why)
steam          list found steam libraries and games
pause          pause for an hour
next           when is the next check
bio            preview what the bio looks like with saved values
status         current state
quit           exit
```

## requires

- windows (uses `GetLastInputInfo` and `SHQueryUserNotificationState`)
- python 3.10+
- telethon (updates bio from your account via mtproto)

if you're in russia you'll need a proxy. add this to `config.json`:

```json
"proxy": {"type": "mtproto", "host": "...", "port": 443, "secret": "..."}
```

or use socks5 from any vpn client that exposes a local port:

```json
"proxy": {"type": "socks5", "host": "127.0.0.1", "port": 1080}
```
