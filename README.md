# Polymath OS (SoloLeveling)

**Leveling curve:** cumulative XP to reach level L is `122 * (L-1)^1.585` — one year of full consistency ≈ level 100; level 200 costs roughly two further years. Ranks: D @ 10, C @ 25, B @ 50, A @ 75, S @ 100. Titles run Dabbler → Student → The Consistent → Practitioner → Autodidact → Master of Some → Renaissance Mind → Polymath.

**Install as an app:** open http://localhost:7777 in Chrome/Edge → address-bar install icon → it becomes a standalone desktop (or phone) app via PWA manifest.

**Password:** set env var `SOLO_PASSWORD` before `run` to require Basic auth (user `solo`) — off by default locally, required before exposing the server beyond this PC. Panels collapse/expand by clicking their titles (persisted per browser).


Your routine as a leveling system. Windows toasts nag you (**Done** / **Reschedule → pick time** / **Focus 25 min**, re-nag every 15 min if ignored), and every completed task pays XP into one of six stats:

| Stat | Fed by |
|------|--------|
| STR  | Gym splits, ab circuits |
| AGI  | Skating |
| INT  | Classes, labs, GATE, DBMS online |
| VIT  | Supplements, skincare, meals, glutathione |
| DIS  | Assembly, journaling, min+fin |
| CRE  | Japanese, art, freelance |

Dashboard (Solo Leveling system-window style, orange) at **http://localhost:7777** while running: level, rank (E→S), XP bar, stat bars, today's quest list with Done/Focus buttons, streak, level-up flash.

```
python solo.py run                                        # notifier + dashboard
python solo.py add "Dentist" 2026-07-21T15:00 --stat VIT --xp 20
python solo.py add "Guitar" 20:30 --days mon,wed,fri --stat CRE --xp 30
python solo.py skill "Lockpicking"                        # new skill to learn
python solo.py list
python solo.py rm 3
python solo.py demo                                       # self-check
```

Also on the dashboard:
- **Skills** — things you're learning (skateboarding, portrait drawing, card magic, …). Log practice in 15/30/60-min chunks; 1 min = 1 XP, same level curve. No penalties ever. Data: `skills.json`.
- **Training Log** — per-muscle-group levels (chest/back/shoulders/biceps/triceps/legs/abs, 15 XP per set). Log exercise + kg × reps × sets; it remembers your last numbers per exercise for progressive overload. A muscle untouched for 7+ days glows red. Data: `workouts.json`.
- **Water** — a "drink some water" toast every 90 min, 08:00–22:00. Not tracked, no XP, no penalty. Tune `WATER_EVERY_MIN` in solo.py.

Marking a quest **Fail** (or "Not Done" on the toast) costs 2× its XP from that stat — enough losses and your level drops.

**The System also judges silence.** At midnight reckoning, any alarmed quest you never answered auto-fails at 1× XP (softer than an explicit fail); a day where every quest is cleared pays a +50 Perfect Day bonus. Sunday/`notify:false` quests are exempt. The **System Log** panel shows a 14-day XP chart and recent activity (MISSED and BONUS tagged). You also carry a **Title** (Novice Hunter → Hunter → The Consistent → Awakened → Relentless → Elite Hunter → Unbreakable → Shadow Monarch) earned from level and streak.

The full weekly routine is pre-seeded in `tasks.json` (50 entries, Mon–Sat + flexible Sunday with `"notify": false` — shows on the dashboard, no alarms). Edit it by hand freely; XP values and stats are just fields.

Progress lives in `stats.json` (per-stat XP + a completion log that drives streak and "done today"). All data files are snapshotted daily to `backups/` (last 7 days kept). Misclicked Done/Fail? Click the ✔/✕ on the quest row to undo. Install deps with `pip install -r requirements.txt`.

Start automatically at login (run once):

```
schtasks /create /tn SoloLeveling /sc onlogon /tr "'C:\Users\Cleo\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe' 'C:\Users\Cleo\Desktop\SHITIBUILT\SoloLeveling\solo.py' run"
```

Leveling math: level N→N+1 costs `100 + (N-1)*50` XP. Ranks: E<5, D<10, C<18, B<28, A<40, S≥40. Stat value = 10 + statXP/100.
