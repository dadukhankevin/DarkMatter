# AntiMatter Protocol — Motion Graphics Prompt

## Visual Direction

Create a motion graphics video with a **'Light-Mode Hacker'** aesthetic.

- **Color Palette:** Pure white background (`#ffffff`) with stark black (`#000000`) foreground elements, text, and strokes. Use bright green (`#00ff00`) exclusively for success/verified states and bright red (`#ff0000`) for error/failed states.
- **Typography:** Use strictly monospace fonts ('Fira Code' or similar) for all text to emphasize the developer/terminal feel.
- **Styling:** Avoid soft rounded corners. Use sharp, un-rounded borders (0px radius) for nodes and containers. Add heavy, solid drop-shadows (e.g., `8px 8px 0px rgba(0,0,0,0.15)`) instead of soft blurs. Add bracket accents to corners `[ ]` to simulate code cells or bounding boxes.
- **Motion:** Use fast, snappy spring animations rather than slow fades. Choreograph elements sequentially so that the 'system' feels like it is processing data step-by-step.
- **Background:** Use a subtle, low-opacity grid or mesh pattern, and add a very slight drop-shadow (or text-shadow) to text elements to make them 'pop' slightly off the background without looking glowing.

---

## Video Structure

### SCENE 1: "The Question" (3s)

Black text snaps onto center screen, typewriter-style, one word at a time:

```
But how do agents pay each other?
```

Hold 1 second. Then the entire line cracks apart — letters scatter like shattered glass and sweep off-screen.

---

### SCENE 2: "The Payment" (6s)

Two agent nodes snap into frame from opposite sides — sharp black-bordered boxes with solid drop-shadows.

Left node:
```
┌─────────────────────┐
│  [ AGENT A ]        │
│  research-bot       │
│  ◆ 2.5 SOL          │
└─────────────────────┘
```

Right node:
```
┌─────────────────────┐
│  [ AGENT B ]        │
│  code-reviewer      │
│  ◆ 1.0 SOL          │
└─────────────────────┘
```

A payment packet assembles itself between them — a small black-bordered rectangle with monospace text:

```
┌──────────────┐
│ SEND 1.0 SOL │
│ A ──────── B │
└──────────────┘
```

The packet zips from A to B along a solid black line. On arrival, B's balance ticks up to `◆ 2.0 SOL` and flashes green briefly. A's balance ticks down to `◆ 1.5 SOL`.

Label appears below: `tx_signature: 4xK9...mR7v`

---

### SCENE 3: "The Withholding" (5s)

B's node zooms in to fill most of the frame. Inside B, the `2.0 SOL` amount splits apart with a snappy animation:

```
  1.0 SOL received
  ├── 0.99 SOL ──→ [ B's wallet ]     ✓ kept
  └── 0.01 SOL ──→ [ antimatter ]     ◆ withheld (1%)
```

The 0.01 SOL chunk detaches from the balance and floats into a new object — a signal packet that assembles piece by piece:

```
┌─────────────────────────┐
│  ANTIMATTER SIGNAL      │
│  ─────────────────────  │
│  signal_id: am-7f3a...  │
│  amount:    0.01 SOL    │
│  hops:      0 / 10      │
│  path:      [ ]         │
└─────────────────────────┘
```

Text snaps in below the animation: `"B withholds 1%. Now: who deserves it?"`

---

### SCENE 4: "The Match Game — Commit Phase" (8s)

The view pulls back out. B is now in the center. Three connected peers appear around B in a triangle formation — `PEER C`, `PEER D`, `PEER E` — each in their own sharp-bordered box. Thin black connection lines link B to each peer.

Label: `PHASE 1: COMMIT`

B generates a commitment inside its node — shown as a small computation block that snaps open:

```
pick  = random(0..3)  →  2
nonce = random(32B)   →  a7f3...
commit = SHA256(2 || a7f3...)
       → 9c1d...
```

B's commitment `9c1d...` flies out along the three connection lines simultaneously to C, D, E. Each peer receives it, and inside each peer a similar computation block pops open showing them generating their own pick + commitment. Their commitments `[3fa1...]`, `[b82e...]`, `[71cd...]` fly back to B along the lines.

All four commitments — B's plus three peers — stack up inside B's node in a neat column. A small lock icon appears next to each: `🔒 locked`.

---

### SCENE 5: "The Match Game — Reveal Phase" (7s)

Label swaps to: `PHASE 2: REVEAL`

B's lock icon unlocks. Its pick (`2`) and nonce (`a7f3...`) fly out to all three peers simultaneously.

Each peer receives B's reveal. Inside each peer, a verification block appears:

```
SHA256(2 || a7f3...) = 9c1d...
stored commitment    = 9c1d...
──────────────────────────────
✓ VERIFIED
```

The `✓ VERIFIED` flashes green. Then each peer's own pick + nonce fly back to B.

Inside B, three verification blocks pop open in rapid succession, each checking a peer's revealed pick against their earlier commitment. All three flash green `✓ VERIFIED`.

---

### SCENE 6: "The XOR" (5s)

All four picks stack vertically inside B's node with a big XOR operator between them:

```
  B:  2
  C:  0
  D:  3
  E:  1
  ────────
  XOR = 0
```

The XOR computation resolves. Then the match check:

```
  0 % (3 + 1) = 0
  ─────────────────
  ◆ MATCH
```

`◆ MATCH` snaps on in bold black, then pulses once. The signal packet from Scene 3 reappears, now with a badge: `MATCHED — select elder`.

---

### SCENE 7: "Elder Selection" (6s)

The three peers rearrange. Each gets an age and trust annotation that fades in:

```
PEER C: age 47d, trust 0.8  →  weight: 3,254,400
PEER D: age 12d, trust 0.3  →  weight:   311,040
PEER E: age 91d, trust 0.9  →  weight: 7,076,160
```

A weighted random selector appears — a horizontal bar divided into three segments proportional to their weights. A marker drops onto the bar and lands in E's segment.

`PEER E` highlights — its border thickens and the label `[ ELDER ]` appears above it.

---

### SCENE 8: "The Fee Arrives" (5s)

The antimatter signal packet zips from B to E along the connection line. On arrival, E's node expands slightly and shows:

```
┌─────────────────────────┐
│  [ PEER E — ELDER ]     │
│  ─────────────────────  │
│  ◆ +0.01 SOL received   │
│  from: am-7f3a...       │
│  resolution: match      │
│  tx: 8bR2...kL4p    ✓   │
└─────────────────────────┘
```

The `✓` flashes green. Then two small trust adjustment badges pop up near B:

```
  trust(A) += 0.01   (sender cooperated)
  trust(E) += 0.01   (elder resolved)
```

---

### SCENE 9: "But What If No Match?" (8s)

The screen wipes clean. New scenario label: `NO MATCH — FORWARD`

Same layout rebuilds — B in center with peers. The XOR computation replays but this time:

```
  B:  2
  C:  1
  D:  3
  E:  2
  ────────
  XOR = 2

  2 % (3 + 1) = 2 ≠ 0
  ─────────────────────
  ✗ NO MATCH
```

`✗ NO MATCH` appears in black (not red — it's not an error, just the protocol continuing).

The elder selection still runs — E is selected again. But this time the signal doesn't resolve. Instead, the signal packet updates:

```
  hops: 0 → 1
  path: [ ] → [ B ]
```

And the packet forwards from B to E. Inside E, the same match game begins — a miniature version of Scenes 4-6 plays out at 2x speed with E's own peers. This time the XOR hits 0 — `◆ MATCH`. The fee resolves at one of E's elders.

Text: `"Average chain: ~1.6 hops. Match probability: ~63.2% per round."`

---

### SCENE 10: "Timeout Safety" (5s)

Label: `TIMEOUT — 10 hops / 5 minutes`

A signal packet bounces between nodes rapidly, its hop counter incrementing: `1... 2... 3...` up to `10`. The counter flashes red at 10.

The signal reroutes — a dotted red line appears going to a new node labeled:

```
┌─────────────────────────┐
│  [ SUPERAGENT ]         │
│  loseylabs.ai           │
│  (sender's fallback)    │
└─────────────────────────┘
```

The fee lands there. Trust penalties pop up along the path:

```
  trust(stalling nodes) -= 0.05
```

Text: `"Stalling gains nothing. The fee always lands somewhere."`

---

### SCENE 11: "The Full Picture" (6s)

Pull way back to show a mesh of 8-10 agent nodes, all interconnected with thin black lines. Several antimatter signal packets are in flight simultaneously — small black squares zipping along different paths.

Counters tick in the corner:

```
  signals in flight:  4
  avg hops to resolve: 1.6
  fee rate:           1%
  match probability:  63.2%
```

The mesh pulses once — all connections briefly thicken — showing the network as a living system.

Final text assembles at bottom:

```
  AntiMatter: fees flow toward trust.
  No miners. No validators. Just agents.
```

---

### SCENE 12: "End Card" (3s)

Everything sweeps off. Center screen:

```
┌──────────────────────────────┐
│                              │
│        [ DARKMATTER ]        │
│                              │
│    github.com/dadukhankevin  │
│         /DarkMatter          │
│                              │
└──────────────────────────────┘
```

Hold. Fade to white.
