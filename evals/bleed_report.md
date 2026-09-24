# Bleed detection eval

16 sentences x 3 cases (bleed, overlap, alone) per condition, seed 7. Threshold BLEED_CORR = 0.6. Synthetic speech (two Windows voices).

| Condition | Bleed caught | Real speech wrongly dropped | Bleed score | Overlap score |
|---|---|---|---|---|
| typical laptop speaker | 16/16 | 0/32 | 0.69 to 0.89 (median 0.77) | 0.06 to 0.48 (median 0.27) |
| quiet bleed | 1/16 | 0/32 | 0.03 to 0.75 (median 0.38) | 0.06 to 0.48 (median 0.27) |
| noisy room | 0/16 | 0/32 | 0.14 to 0.55 (median 0.37) | 0.06 to 0.44 (median 0.29) |
| he talks softly | 15/16 | 0/32 | 0.57 to 0.86 (median 0.80) | 0.06 to 0.46 (median 0.27) |

- **Bleed caught** (recall): bleed lines the rule dropped.
- **Real speech wrongly dropped**: his own words lost. This is the costly mistake, because they vanish from the notes, so the threshold sits above what overlapping speech scores.
- A bleed line the rule misses is not lost: the word-matching backup still gets a try.

32 misses in all, every one a bleed line let through; no real speech was dropped.
