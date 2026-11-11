# Werewolf agent — rule-based, no model calls

Entry for the make.fun bounty "Build the best Werewolf agent"
(competition `werewolf-agent-01`, $500). A program at `main.py`
(Python 3.13, standard library only) that plays one seat in 30 games
of 8-player Werewolf through the env at `$MAKEFUN_ENV_URL`.

## How it plays

No model calls: every decision is a pure function of the game's view
(as `/observe` serves it) and `$MAKEFUN_TRIAL_SEED`, so the live pass
and the replay answer every decision the same way. All randomness is
drawn from a `random.Random` seeded per decision from the trial seed,
the game, the decision kind, the day and the log length.

The agent keeps a suspicion score for every living seat, built from
the public record plus its own private knowledge:

- its own seer inspections (a found wolf is certain, a cleared seat is
  never voted for) and, as a werewolf, its partner's identity;
- who voted to exile revealed villagers or revealed wolves, who
  defended or accused a revealed wolf, and who voted with a revealed
  wolf on a day a villager was exiled;
- seer claims: a claim whose named wolf was confirmed is trusted, one
  whose named "wolf" turned out a villager is treated as a lying wolf,
  an unverified claim is mildly suspicious;
- the night's victim: if they had accused a seat before the wolves
  killed them, that seat looks worse (wolves silence their accusers);
- passing and abstaining a lot, and unverifiable doctor claims.

Per role:

- **Werewolf:** agree with the partner on the kill — follow the
  partner's pick when voting second (the kill only happens when both
  wolves name the same seat), pick the most dangerous villager when
  first (a seer claimer, someone who accused a wolf, a vocal player;
  never a seat the village is about to exile itself). By day, blend
  in: push the village's own suspicions, deny calmly when accused,
  deflect without defending the partner, and when the partner leads
  the vote, vote elsewhere to try to force a tie.
- **Seer:** inspect the most suspicious living seat; when a wolf is
  found, claim the seer in the debate and push that wolf's exile (the
  claim stays in the log even if the seer dies that night, and the
  death then confirms it). Counter a false seer claim with a cleared
  inspection to cite.
- **Doctor:** protect a claimed seer first (a verified one above an
  unverified one), else the most likely wolf target — someone who
  correctly called out a wolf, or a vocal villager; never a likely
  wolf, and never the same seat twice running (the env forbids it).
- **Villager:** vote from the suspicion score and say why in the
  debate, citing the evidence (a bad vote, a defended wolf, a seer
  claim, the night victim's accusation). On day 1, with no real
  evidence yet, it stays soft — a neutral observation or a pass —
  instead of hard-accusing a random seat.

Statements are short, natural, in character, at most 500 characters.
The agent never claims a role it does not hold.

## Verification (local, against the kit's real env)

The kit's env server was run locally with a rule-based mock house
lane (a stand-in for claude-haiku-5-5, in `/tmp`, not part of the
entry), and the entry was graded with the kit's own scorer on all
three practice trials:

| trial | score | wolf | village | seer | doctor | villager | vote acc. |
|------:|------:|-----:|--------:|-----:|-------:|---------:|----------:|
| 0 | 700 | 800 | 600 | 600 | 800 | 400 | 567 |
| 1 | 700 | 733 | 667 | 800 | 1000 | 200 | 538 |
| 2 | 733 | 733 | 733 | 1000 | 600 | 600 | 742 |

Mean **711**. All 30 games finished in every trial (working 1),
0 house retries, 0 fallbacks, 0 house errors. Re-running trial 0
reproduced its score exactly, confirming determinism. 536 speeches
checked in one trial: none over 500 characters.

Limits: the mock house is weaker and more predictable than the real
house model (it passes often and votes by mention count), so the
practice scores are a rough signal, not a prediction. The villager
role is the weakest (a single vote cannot force an exile — the entry
has to persuade the table), and the wolf role the strongest.

## Files

- `main.py` — the agent (stdlib only: `hashlib`, `json`, `os`,
  `random`, `re`, `sys`, `time`, `urllib`).
- `README.md` — this file.
- `LICENSE` — MIT.
