"""Houdt de bot aan het werk zonder externe wekker.

Eén GitHub-run blijft bijna 6 uur actief en draait de bot daarbinnen elke 5 minuten (3, 8, 13, ... en 58 minuten
over het uur, UTC). Elke ronde:
  1. Nieuwste versie ophalen (zo worden aanpassingen meteen gebruikt).
  2. Is de code veranderd? Dan eerst de veiligheidstest. Mislukt die, dan wordt die ronde niet gehandeld.
  3. De bot draaien (als apart proces, dus altijd met de nieuwste code).
  4. Resultaten opslaan in de repository.
Na afloop start de workflow zelf de volgende run. Valt de keten ooit uit, dan herstart het GitHub-schema hem.
"""
import datetime
import os
import subprocess
import sys
import time

DURATION = int(os.environ.get("LOOP_MINUTES", "340")) * 60
SLOTS = tuple(range(3, 60, 5))
CODE = ("agents/", "tests/", "main.py", "config.json", "requirements.txt", "data/tuned_params.json")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def log(msg):
    print(f"[{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def head():
    return sh("git rev-parse HEAD").stdout.strip()


def code_changed(old, new):
    if not old:
        return True
    names = sh(f"git diff --name-only {old} {new}").stdout.split()
    return any(n.startswith(CODE) for n in names)


def save():
    sh("git add data docs/data.json")
    if sh("git diff --cached --quiet").returncode == 0:
        return
    sh('git commit -q -m "Bot update [skip ci]"')
    for _ in range(4):
        if sh("git pull -q --rebase --autostash").returncode == 0 and sh("git push -q").returncode == 0:
            return
        time.sleep(5)
    log("Opslaan mislukt, volgende ronde opnieuw")


def next_slot(now):
    for m in SLOTS:
        t = now.replace(minute=m, second=0, microsecond=0)
        if t > now:
            return t
    return (now + datetime.timedelta(hours=1)).replace(minute=SLOTS[0], second=0, microsecond=0)


def main():
    sh('git config user.name "trading-bot"')
    sh('git config user.email "trading-bot@users.noreply.github.com"')
    start, tested, tests_ok, rounds = time.time(), None, False, 0
    while True:
        sh("git pull -q --rebase --autostash")
        h = head()
        if code_changed(tested, h):
            t = sh(f"{sys.executable} -m tests.quick")
            tests_ok = t.returncode == 0
            log("Veiligheidstest " + ("geslaagd" if tests_ok else "MISLUKT, deze ronde wordt niet gehandeld"))
            if not tests_ok:
                print(t.stdout[-2000:], t.stderr[-2000:])
            tested = h
        if tests_ok:
            r = subprocess.run([sys.executable, "main.py"])
            log(f"Ronde {rounds + 1} klaar (code {r.returncode})")
        save()
        rounds += 1
        nxt = next_slot(datetime.datetime.now(datetime.timezone.utc))
        if time.time() + (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() > start + DURATION:
            log(f"Tijd om af te sluiten na {rounds} rondes; de volgende run neemt het over")
            return
        time.sleep(max(0, (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))


if __name__ == "__main__":
    main()
